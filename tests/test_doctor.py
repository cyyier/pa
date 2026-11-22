"""pac doctor / pac bundle / friendly start-up failures for binaries across Linux, macOS, and Windows."""
import json
import os
import shutil
import struct
import subprocess
import sys
import sysconfig
import textwrap

import pytest

from pa_lang import doctor
from pa_lang.build import compile_pa, python_link_info
from pa_lang.cli import main as pac
from pa_lang.lsp_server import uri_to_path

MAIN = "import w\n\ndef main() -> int:\n    print(w.f(2.0))\n    return 0\n"
PAW = "def f(x: f64) -> f64:\n    return x * 10.0\n"
SONAME = f"libpython{sysconfig.get_config_var('LDVERSION') or sysconfig.get_config_var('VERSION')}.so.1.0"
LIBDIR = sysconfig.get_config_var("LIBDIR")


def clean_env(**extra):
    drop = ("LD_LIBRARY_PATH", "DYLD_LIBRARY_PATH", "PYTHONHOME", "PA_PYTHON_HOME", "VIRTUAL_ENV")
    env = {k: v for k, v in os.environ.items() if k not in drop}
    env.update(extra)
    return env


def build(tmp_path, name="proj", paw=PAW, **kw):
    src = tmp_path / name
    src.mkdir()
    (src / "w.paw").write_text(textwrap.dedent(paw), encoding="utf-8")
    (src / "main.pa").write_text(MAIN, encoding="utf-8")
    return src, compile_pa(str(src / "main.pa"), out=str(src / "prog"), **kw)


def system_has_libpython():
    if sys.platform != "linux":
        return True
    dirs = doctor._ldconfig_dirs() + doctor.DEFAULT_ELF_DIRS
    return any(os.path.exists(os.path.join(d, SONAME)) for d in dirs)


needs_clean_linux = pytest.mark.skipif(
    sys.platform != "linux" or system_has_libpython(),
    reason="Linux-only loader-failure test or libpython is installed system-wide",
)


@pytest.mark.skipif(sys.platform != "linux", reason="ELF inspection test")
def test_elf_reader_lists_libpython_and_origin_rpaths(tmp_path):
    _, exe = build(tmp_path, relocatable=True)
    dyn = doctor.elf_dynamic(exe)
    assert SONAME in dyn["needed"]
    rp = dyn["runpath"] + dyn["rpath"]
    assert "$ORIGIN" in rp and "$ORIGIN/../lib" in rp
    assert LIBDIR not in rp                                   # relocatable: no build-machine directory baked in
    _, normal = build(tmp_path, name="normal")
    assert LIBDIR in doctor.elf_dynamic(normal)["runpath"] + doctor.elf_dynamic(normal)["rpath"]


def test_native_binary_inspection_and_rpath_cross_platform(tmp_path):
    _, exe = build(tmp_path, relocatable=True)
    dyn = doctor.inspect_binary(exe)
    assert dyn is not None
    expected_fmt = {"linux": "elf", "darwin": "macho", "win32": "pe"}[sys.platform]
    assert dyn["format"] == expected_fmt
    py_libs = [lib for lib in dyn["needed"] if doctor.is_python_lib(lib)]
    assert py_libs, dyn["needed"]
    if sys.platform == "darwin":
        assert "@loader_path" in dyn["rpath"]
        assert all(lib.startswith("@rpath/") for lib in py_libs)


def test_macho_and_pe_parsers_pure_python(tmp_path):
    # Synthetic 64-bit LE Mach-O with LC_LOAD_DYLIB (@rpath/libpython3.12.dylib) and LC_RPATH (@loader_path)
    def pad8(b: bytes) -> bytes:
        rem = len(b) % 8
        return b + (b"\x00" * (8 - rem) if rem else b"")

    dylib_name = pad8(b"@rpath/libpython3.12.dylib\x00")
    lc_dylib = struct.pack("<IIIIII", 0x0C, 24 + len(dylib_name), 24, 0, 0, 0) + dylib_name
    rpath_str = pad8(b"@loader_path\x00")
    lc_rpath = struct.pack("<III", 0x8000001C, 12 + len(rpath_str), 12) + rpath_str
    cmds = lc_dylib + lc_rpath
    macho = struct.pack("<IiiIIIII", 0xFEEDFACF, 0x0100000C, 0, 2, 2, len(cmds), 0, 0) + cmds
    mpath = tmp_path / "app_macho"
    mpath.write_bytes(macho)
    mdyn = doctor.inspect_binary(str(mpath))
    assert mdyn == {"format": "macho", "needed": ["@rpath/libpython3.12.dylib"], "rpath": ["@loader_path"], "runpath": []}
    assert doctor.resolve_lib("@rpath/libpython3.12.dylib", str(tmp_path), mdyn, env={}) is None
    (tmp_path / "libpython3.12.dylib").write_bytes(b"")
    assert doctor.resolve_lib("@rpath/libpython3.12.dylib", str(tmp_path), mdyn, env={}) == str(tmp_path / "libpython3.12.dylib")

    # Synthetic PE32+ with Import Table referencing python312.dll and api-ms-win-*
    pe_buf = bytearray(0x300)
    pe_buf[0:2] = b"MZ"
    struct.pack_into("<I", pe_buf, 0x3C, 0x80)
    pe_buf[0x80:0x84] = b"PE\x00\x00"
    opt_size = 240
    struct.pack_into("<HHIIIHH", pe_buf, 0x84, 0x8664, 1, 0, 0, 0, opt_size, 0)
    opt_off = 0x84 + 20
    struct.pack_into("<H", pe_buf, opt_off, 0x20B)
    struct.pack_into("<I", pe_buf, opt_off + 108, 16)
    struct.pack_into("<II", pe_buf, opt_off + 112 + 8, 0x2000, 60)
    sec_off = opt_off + opt_size
    struct.pack_into("<IIII", pe_buf, sec_off + 8, 0x1000, 0x2000, 0x100, 0x200)
    # Import descriptors at offset 0x200 (RVA 0x2000); names at 0x2040 / 0x2050 (offsets 0x240 / 0x250)
    struct.pack_into("<IIIII", pe_buf, 0x200, 1, 0, 0, 0x2040, 1)
    struct.pack_into("<IIIII", pe_buf, 0x214, 1, 0, 0, 0x2050, 1)
    pe_buf[0x240:0x240 + 14] = b"python312.dll\x00"
    pe_buf[0x250:0x250 + 28] = b"api-ms-win-crt-heap-l1-1.dll"
    pe_path = tmp_path / "app.exe"
    pe_path.write_bytes(bytes(pe_buf))
    pdyn = doctor.inspect_binary(str(pe_path))
    assert pdyn == {"format": "pe", "needed": ["python312.dll", "api-ms-win-crt-heap-l1-1.dll"], "rpath": [], "runpath": []}
    assert doctor.is_python_lib("python312.dll") and doctor.is_python_lib("/Library/Frameworks/Python.framework/Versions/3.12/Python")
    assert doctor.resolve_lib("api-ms-win-crt-heap-l1-1.dll", str(tmp_path), pdyn, env={}) is not None
    (tmp_path / "python312.dll").write_bytes(b"")
    assert doctor.resolve_lib("PYTHON312.DLL", str(tmp_path), pdyn, env={}) is not None

    # Windows file URI conversion
    assert uri_to_path("file:///C:/Users/alice/main.pa").endswith("Users\\alice\\main.pa" if sys.platform == "win32" else "C:/Users/alice/main.pa")


@needs_clean_linux
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
    _, normal = build(tmp_path, name="normal")
    assert doctor.check_binary(normal, env={}).ok


def test_pac_doctor_environment_check_runs_everywhere(tmp_path, capsys):
    assert pac(["doctor"]) == 0
    env_out = capsys.readouterr().out
    for item in ("llvmlite", "Python.h", "shared "):
        assert item in env_out
    assert pac(["doctor", str(tmp_path / "nope")]) == 1


@needs_clean_linux
def test_pac_doctor_missing_binary_cli_exit_code(tmp_path, capsys):
    _, exe = build(tmp_path, relocatable=True)
    old = os.environ.pop("LD_LIBRARY_PATH", None)
    try:
        assert pac(["doctor", exe]) == 1
        out = capsys.readouterr().out
        assert "MISSING: " + SONAME in out and "pac bundle" in out
    finally:
        if old is not None:
            os.environ["LD_LIBRARY_PATH"] = old


@needs_clean_linux
def test_pac_bundle_makes_the_binary_start_without_environment(tmp_path, capsys):
    _, exe = build(tmp_path, relocatable=True)
    deploy = tmp_path / "deploy"
    deploy.mkdir()
    moved = str(shutil.copy(exe, deploy / os.path.basename(exe)))
    assert subprocess.run([moved], capture_output=True, env=clean_env()).returncode == 127
    assert pac(["bundle", moved]) == 0
    assert (deploy / SONAME).is_file()
    assert "libpython alone is not enough" in capsys.readouterr().out
    assert doctor.check_binary(moved, env={}).ok
    r = subprocess.run([moved], capture_output=True, text=True, env=clean_env(), cwd=deploy)
    assert (r.returncode == 0 and r.stdout.strip() == "20.0") or (
        r.returncode == 70 and "cannot start the embedded Python runtime" in r.stderr and "--with-stdlib" in r.stderr
    ), (r.returncode, r.stdout, r.stderr)
    r = subprocess.run([moved], capture_output=True, text=True, cwd=deploy, env=clean_env(PA_PYTHON_HOME=sys.base_prefix))
    assert r.returncode == 0 and r.stdout.strip() == "20.0", r.stderr
    assert pac(["bundle", moved]) == 0                          # idempotent
    assert "home   :" in doctor.check_binary(moved, env={}).text()


def test_bundled_stdlib_is_picked_up_as_python_home(tmp_path):
    paw = '''
        import os
        import sys

        def f(x: f64) -> f64:
            p = os.path.normcase(os.path.realpath(sys.prefix))
            e = os.path.normcase(os.path.realpath(os.environ["EXPECT_PREFIX"]))
            return 1.0 if p.startswith(e) else 0.0
    '''
    _, exe = build(tmp_path, paw=paw, relocatable=True)
    deploy = tmp_path / "deploy"
    deploy.mkdir()
    moved = str(shutil.copy(exe, deploy / os.path.basename(exe)))
    assert pac(["bundle", moved, "--with-stdlib"]) == 0
    if sys.platform == "win32":
        assert (deploy / "python-home" / "Lib" / "os.py").is_file()
        assert (deploy / f"python{sys.version_info.major}{sys.version_info.minor}.dll").is_file()
    else:
        assert (deploy / "python-home" / "lib" / f"python{doctor.python_version()}" / "os.py").is_file()
        assert not (deploy / "python-home" / "lib" / f"python{doctor.python_version()}" / "test").exists()
    env = clean_env(EXPECT_PREFIX=str(deploy / "python-home"))
    r = subprocess.run([moved], capture_output=True, text=True, env=env, cwd=deploy)
    assert r.returncode == 0 and r.stdout.strip() == "1.0", (r.stdout, r.stderr)


def test_unusable_python_home_gives_a_friendly_startup_fault(tmp_path):
    _, exe = build(tmp_path)
    env = clean_env(PA_PYTHON_HOME="/no/such/python")
    if sys.platform == "win32":
        env["PATH"] = os.pathsep.join([python_link_info()["dll_dir"], env.get("PATH", "")])
    r = subprocess.run([exe], capture_output=True, text=True, cwd=tmp_path, env=env)
    assert r.returncode == 70
    assert "cannot start the embedded Python runtime" in r.stderr and "pac bundle" in r.stderr and "pac doctor" in r.stderr
    fault = json.loads((tmp_path / "pa_boundary_fault.json").read_text(encoding="utf-8"))
    assert fault["direction"] == "STARTUP" and fault["worker"] == "python-runtime"
    assert "/no/such/python" in fault["violation"]


def test_relocatable_build_prints_next_steps(tmp_path, capsys):
    src = tmp_path / "p"
    src.mkdir()
    (src / "w.paw").write_text(PAW, encoding="utf-8")
    (src / "main.pa").write_text(MAIN, encoding="utf-8")
    assert pac(["build", str(src / "main.pa"), "-o", str(src / "prog"), "--relocatable"]) == 0
    err = capsys.readouterr().err
    assert "pac doctor" in err and "pac bundle" in err
    assert pac(["build", str(src / "main.pa"), "-o", str(src / "prog2")]) == 0
    assert "pac bundle" not in capsys.readouterr().err         # normal builds stay quiet


def test_pure_native_binaries_have_nothing_to_bundle(tmp_path, capsys):
    (tmp_path / "main.pa").write_text("def main() -> int:\n    return 0\n", encoding="utf-8")
    exe = compile_pa(str(tmp_path / "main.pa"), out=str(tmp_path / "prog"))
    dyn = doctor.inspect_binary(exe)
    assert dyn is not None and not any(doctor.is_python_lib(lib) for lib in dyn["needed"])
    assert pac(["bundle", exe]) == 0
    assert "nothing to bundle" in capsys.readouterr().out
