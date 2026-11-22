"""No hard-coded source paths: .paw sources are embedded, Python location is resolved at run time."""
import os
import shutil
import subprocess
import sys
import sysconfig
import textwrap
import tomllib

import pytest

from pa_lang.build import compile_pa, c_string_literal, default_python_paths, python_link_info

MAIN = "import w\n\ndef main() -> int:\n    print(w.f(2.0))\n    return 0\n"
PAW = "def f(x: f64) -> f64:\n    return x * 10.0\n"


def _runtime_lib_env(env: dict) -> dict:
    pyinfo = python_link_info()
    if sys.platform == "win32":
        env["PATH"] = os.pathsep.join([pyinfo["dll_dir"], env.get("PATH", "")])
    elif sys.platform == "darwin":
        env["DYLD_LIBRARY_PATH"] = os.pathsep.join(
            [p for p in (pyinfo["libdir"], env.get("DYLD_LIBRARY_PATH", "")) if p]
        )
    else:
        env["LD_LIBRARY_PATH"] = pyinfo["libdir"]
    return env


def build(tmp_path, name="proj", paw=PAW, main=MAIN, **kw):
    src = tmp_path / name
    src.mkdir()
    (src / "w.paw").write_text(textwrap.dedent(paw), encoding="utf-8")
    (src / "main.pa").write_text(main, encoding="utf-8")
    exe = compile_pa(str(src / "main.pa"), out=str(src / "prog"), **kw)
    return src, exe


def run(exe, cwd, env=None):
    return subprocess.run([exe], capture_output=True, text=True, encoding="utf-8", cwd=cwd, env=env or os.environ)


def test_binary_survives_moving_and_deleting_the_source_tree(tmp_path):
    src, exe = build(tmp_path)
    dest = tmp_path / "deploy"
    dest.mkdir()
    moved = shutil.copy(exe, dest / os.path.basename(exe))
    shutil.rmtree(src)                                    # the .paw and .pa are gone
    r = run(moved, dest)
    assert r.returncode == 0 and r.stdout.strip() == "20.0", r.stderr


def test_no_source_directory_is_baked_into_the_binary(tmp_path):
    src, exe = build(tmp_path)
    blob = open(exe, "rb").read()
    assert str(src).encode() not in blob
    assert str(tmp_path).encode() not in blob


def test_pa_path_overrides_embedded_source_for_live_edits(tmp_path):
    src, exe = build(tmp_path)
    live = tmp_path / "live"
    live.mkdir()
    (live / "w.paw").write_text("def f(x: f64) -> f64:\n    return x * 100.0\n")
    assert run(exe, tmp_path).stdout.strip() == "20.0"                              # embedded copy
    r = run(exe, tmp_path, dict(os.environ, PA_PATH=str(live)))
    assert r.stdout.strip() == "200.0"                                               # live override


def test_embedding_survives_quotes_backslashes_unicode_and_trigraphs(tmp_path):
    paw = '''
        S = "a\\"b\\\\c\\n??/ \\u00e9 \\U0001F600 '' ??= ??( %s %d"

        def f(x: f64) -> f64:
            return float(len(S.encode("utf-8"))) + x
    '''
    expect = float(len('a"b\\c\n??/ \u00e9 \U0001F600 \'\' ??= ??( %s %d'.encode("utf-8"))) + 2.0
    _, exe = build(tmp_path, paw=paw)
    r = run(exe, tmp_path)
    assert r.returncode == 0 and float(r.stdout) == expect, r.stderr
    assert "\\134" in c_string_literal(b"\\")                                       # backslash -> octal escape
    assert "?" not in c_string_literal(b"??/") and "\\077" in c_string_literal(b"?")   # no trigraphs possible


def test_missing_baked_python_paths_are_ignored(tmp_path):
    _, exe = build(tmp_path, python_path=["/definitely/not/here", "/neither/here"], python_home="/no/such/python")
    r = run(exe, tmp_path)
    assert r.returncode == 0 and r.stdout.strip() == "20.0", r.stderr


def test_relocatable_build_resolves_python_from_environment(tmp_path):
    third = '''
        import numpy as np

        def f(x: f64) -> f64:
            return float(np.sum(np.array([x, x])))
    '''
    _, exe = build(tmp_path, paw=third, relocatable=True)
    env = _runtime_lib_env(dict(
        os.environ,
        PA_PYTHON_HOME=sys.base_prefix,
        PA_PYPATH=os.pathsep.join(default_python_paths()),
    ))
    env.pop("VIRTUAL_ENV", None)
    r = run(exe, tmp_path, env)
    assert r.returncode == 0 and r.stdout.strip() == "4.0", r.stderr


def test_virtual_env_site_packages_are_found_at_run_time(tmp_path):
    third = "import numpy as np\n\ndef f(x: f64) -> f64:\n    return float(np.float64(x) * 3)\n"
    _, exe = build(tmp_path, paw=third, relocatable=True)
    env = _runtime_lib_env(dict(os.environ, PA_PYTHON_HOME=sys.base_prefix, VIRTUAL_ENV=sys.prefix))
    env.pop("PA_PYPATH", None)
    r = run(exe, tmp_path, env)
    assert r.returncode == 0 and r.stdout.strip() == "6.0", r.stderr


def test_cli_exposes_path_flags():
    from pa_lang.cli import main as pac
    out = subprocess.run([sys.executable, "-m", "pa_lang.cli", "build", "--help"], capture_output=True, text=True).stdout
    for flag in ("--python-home", "--python-path", "--relocatable"):
        assert flag in out


def test_packaging_ships_the_c_runtime_sources():
    root = os.path.dirname(os.path.dirname(__file__))
    cfg = tomllib.load(open(os.path.join(root, "pyproject.toml"), "rb"))
    assert "*.c" in cfg["tool"]["setuptools"]["package-data"]["pa_lang"]
    for f in ("runtime_bridge.c", "runtime_core.c"):
        assert os.path.isfile(os.path.join(root, "pa_lang", f))
