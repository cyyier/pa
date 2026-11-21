"""pac doctor / pac bundle / friendly start-up failures for binaries that cannot find their Python."""
import json
import os
import shutil
import subprocess
import sys
import sysconfig
import textwrap

import pytest

from pa_lang import doctor
from pa_lang.build import compile_pa
from pa_lang.cli import main as pac

MAIN = "import w\n\ndef main() -> int:\n    print(w.f(2.0))\n    return 0\n"
PAW = "def f(x: f64) -> f64:\n    return x * 10.0\n"
SONAME = f"libpython{sysconfig.get_config_var('LDVERSION') or sysconfig.get_config_var('VERSION')}.so.1.0"
LIBDIR = sysconfig.get_config_var("LIBDIR")


def clean_env(**extra):
    env = {k: v for k, v in os.environ.items() if k not in ("LD_LIBRARY_PATH", "PYTHONHOME", "PA_PYTHON_HOME", "VIRTUAL_ENV")}
    env.update(extra)
    return env


def build(tmp_path, name="proj", paw=PAW, **kw):
    src = tmp_path / name
    src.mkdir()
    (src / "w.paw").write_text(textwrap.dedent(paw))
    (src / "main.pa").write_text(MAIN)
    return src, compile_pa(str(src / "main.pa"), out=str(src / "prog"), **kw)


def system_has_libpython():
    dirs = doctor._ldconfig_dirs() + doctor.DEFAULT_DIRS
    return any(os.path.exists(os.path.join(d, SONAME)) for d in dirs)


needs_clean_machine = pytest.mark.skipif(system_has_libpython(), reason="libpython is installed system-wide here")


def test_elf_reader_lists_libpython_and_origin_rpaths(tmp_path):
    _, exe = build(tmp_path, relocatable=True)
    dyn = doctor.elf_dynamic(exe)
    assert SONAME in dyn["needed"]
    rp = dyn["runpath"] + dyn["rpath"]
    assert "$ORIGIN" in rp and "$ORIGIN/../lib" in rp
    assert LIBDIR not in rp                                   # relocatable: no build-machine directory baked in
    _, normal = build(tmp_path, name="normal")
    assert LIBDIR in doctor.elf_dynamic(normal)["runpath"] + doctor.elf_dynamic(normal)["rpath"]


@needs_clean_machine
def test_doctor_detects_missing_libpython_and_the_loader_really_fails(tmp_path):
    _, exe = build(tmp_path, relocatable=True)
    rep = doctor.check_binary(exe, env={})
    assert not rep.ok and rep.missing == [SONAME]
    text = rep.text()
    assert "MISSING" in text and "LD_LIBRARY_PATH" in text and "pac bundle" in text and "uv python install" in text
    r = subprocess.run([exe], capture_output=True, text=True, env=clean_env())
    assert r.returncode == 127 and SONAME in r.stderr         # the cryptic ld.so error doctor explains
    ok = doctor.check_binary(exe, env={"LD_LIBRARY_PATH": LIBDIR})
    assert ok.ok and ok.found[SONAME].startswith(LIBDIR)
    # a normal build carries the absolute rpath and is fine without any environment
    _, normal = build(tmp_path, name="normal")
    assert doctor.check_binary(normal, env={}).ok


@needs_clean_machine
def test_pac_doctor_cli_exit_codes_and_output(tmp_path, capsys):
    _, exe = build(tmp_path, relocatable=True)
    old = os.environ.pop("LD_LIBRARY_PATH", None)
    try:
        assert pac(["doctor", exe]) == 1
        out = capsys.readouterr().out
        assert "MISSING: " + SONAME in out and "pac bundle" in out
    finally:
        if old is not None:
            os.environ["LD_LIBRARY_PATH"] = old
    assert pac(["doctor"]) == 0                                # this build machine is healthy
    env_out = capsys.readouterr().out
    for item in ("gcc", "llvmlite", "Python.h", "shared libpython"):
        assert item in env_out
    assert pac(["doctor", str(tmp_path / "nope")]) == 1


@needs_clean_machine
def test_pac_bundle_makes_the_binary_start_without_environment(tmp_path, capsys):
    src, exe = build(tmp_path, relocatable=True)
    deploy = tmp_path / "deploy"
    deploy.mkdir()
    moved = str(shutil.copy(exe, deploy / "prog"))
    assert subprocess.run([moved], capture_output=True, env=clean_env()).returncode == 127
    assert pac(["bundle", moved]) == 0
    assert (deploy / SONAME).is_file()
    assert "libpython alone is not enough" in capsys.readouterr().out
    assert doctor.check_binary(moved, env={}).ok                                      # the loader is satisfied now
    # ... but a --relocatable binary still needs a Python home: friendly fault instead of a crash
    r = subprocess.run([moved], capture_output=True, text=True, env=clean_env(), cwd=deploy)
    assert r.returncode == 70 and "cannot start the embedded Python runtime" in r.stderr and "--with-stdlib" in r.stderr
    r = subprocess.run([moved], capture_output=True, text=True, cwd=deploy, env=clean_env(PA_PYTHON_HOME=sys.base_prefix))
    assert r.returncode == 0 and r.stdout.strip() == "20.0", r.stderr
    assert pac(["bundle", moved]) == 0                          # idempotent
    assert "home   :" in doctor.check_binary(moved, env={}).text()


@needs_clean_machine
def test_bundled_stdlib_is_picked_up_as_python_home(tmp_path):
    paw = '''
        import os
        import sys

        def f(x: f64) -> f64:
            return 1.0 if sys.prefix.startswith(os.environ["EXPECT_PREFIX"]) else 0.0
    '''
    src, exe = build(tmp_path, paw=paw, relocatable=True)
    deploy = tmp_path / "deploy"
    deploy.mkdir()
    moved = str(shutil.copy(exe, deploy / "prog"))
    assert pac(["bundle", moved, "--with-stdlib"]) == 0
    assert (deploy / "python-home" / "lib" / f"python{doctor.python_version()}" / "os.py").is_file()
    assert not (deploy / "python-home" / "lib" / f"python{doctor.python_version()}" / "test").exists()   # trimmed
    env = clean_env(EXPECT_PREFIX=str(deploy / "python-home"))
    r = subprocess.run([moved], capture_output=True, text=True, env=env, cwd=deploy)
    assert r.returncode == 0 and r.stdout.strip() == "1.0", r.stderr


def test_unusable_python_home_gives_a_friendly_startup_fault(tmp_path):
    _, exe = build(tmp_path)
    r = subprocess.run([exe], capture_output=True, text=True, cwd=tmp_path,
                       env=clean_env(PA_PYTHON_HOME="/no/such/python"))
    assert r.returncode == 70
    assert "cannot start the embedded Python runtime" in r.stderr and "pac bundle" in r.stderr and "pac doctor" in r.stderr
    fault = json.loads((tmp_path / "pa_boundary_fault.json").read_text())
    assert fault["direction"] == "STARTUP" and fault["worker"] == "python-runtime"
    assert "/no/such/python" in fault["violation"]


def test_relocatable_build_prints_next_steps(tmp_path, capsys):
    src = tmp_path / "p"
    src.mkdir()
    (src / "w.paw").write_text(PAW)
    (src / "main.pa").write_text(MAIN)
    assert pac(["build", str(src / "main.pa"), "-o", str(src / "prog"), "--relocatable"]) == 0
    err = capsys.readouterr().err
    assert SONAME in err and "pac doctor" in err and "pac bundle" in err
    assert pac(["build", str(src / "main.pa"), "-o", str(src / "prog2")]) == 0
    assert "pac bundle" not in capsys.readouterr().err         # normal builds stay quiet


def test_pure_native_binaries_have_nothing_to_bundle(tmp_path, capsys):
    (tmp_path / "main.pa").write_text("def main() -> int:\n    return 0\n")
    exe = compile_pa(str(tmp_path / "main.pa"), out=str(tmp_path / "prog"))
    assert doctor.check_binary(exe, env={}).ok and not doctor.elf_dynamic(exe)["needed"][0].startswith("libpython")
    assert pac(["bundle", exe]) == 0
    assert "nothing to bundle" in capsys.readouterr().out
