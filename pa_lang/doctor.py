"""pac doctor / pac bundle: detect and fix a missing Python library (or stdlib) across Linux ELF, macOS Mach-O, and Windows PE.

A binary that links libpython / Python DLL fails *inside the dynamic loader* if the library cannot be found, long
before any of our code runs. So detection happens here (pure-Python ELF / Mach-O / PE inspection matching the OS loader)
and the fix is assisted: `pac bundle` copies the Python runtime library (and optionally a trimmed stdlib) next to the
binary, where the loader finds it automatically (`$ORIGIN` on Linux, `@loader_path` on macOS, exe directory on Windows).
"""
from __future__ import annotations

import glob
import os
import re
import shutil
import struct
import subprocess
import sys
import sysconfig
from dataclasses import dataclass, field
from typing import Optional

DEFAULT_ELF_DIRS = [
    "/lib", "/usr/lib", "/lib64", "/usr/lib64", "/usr/local/lib",
    "/lib/x86_64-linux-gnu", "/usr/lib/x86_64-linux-gnu",
    "/lib/aarch64-linux-gnu", "/usr/lib/aarch64-linux-gnu",
]
STDLIB_IGNORE = shutil.ignore_patterns(
    "__pycache__", "test", "tests", "idlelib", "tkinter", "turtledemo",
    "site-packages", "config-*", "ensurepip", "lib2to3", "*.pyc",
)
_PY_DLL_RE = re.compile(r"python3?\d+\.dll$", re.I)


# ---------------------------------------------------------------------------
# Pure-Python executable inspection (64-bit LE ELF, 64-bit LE Mach-O, PE32/PE32+)
# ---------------------------------------------------------------------------
def _cstr(data: bytes, start: int, end: Optional[int] = None) -> str:
    lim = len(data) if end is None else min(end, len(data))
    if start < 0 or start >= lim:
        return ""
    nul = data.find(b"\x00", start, lim)
    chunk = data[start:lim if nul < 0 else nul]
    return chunk.decode("utf-8", errors="replace")


def elf_dynamic(path: str) -> Optional[dict]:
    """DT_NEEDED / DT_RPATH / DT_RUNPATH of an ELF file, or None if it is not a 64-bit LE ELF."""
    with open(path, "rb") as f:
        data = f.read()
    if len(data) < 64 or data[:4] != b"\x7fELF" or data[4] != 2 or data[5] != 1:
        return None
    shoff, = struct.unpack_from("<Q", data, 0x28)
    shentsize, shnum = struct.unpack_from("<HH", data, 0x3A)
    secs = [struct.unpack_from("<IIQQQQIIQQ", data, shoff + i * shentsize) for i in range(shnum)]
    out = {"format": "elf", "needed": [], "rpath": [], "runpath": []}
    for s in secs:
        if s[1] != 6:                                   # SHT_DYNAMIC
            continue
        strtab = secs[s[6]]
        for off in range(s[4], s[4] + s[5], 16):
            tag, val = struct.unpack_from("<qQ", data, off)
            if tag == 0:
                break
            if tag in (1, 15, 29):
                name = _cstr(data, strtab[4] + val)
                out[{1: "needed", 15: "rpath", 29: "runpath"}[tag]].extend(name.split(":") if tag != 1 else [name])
    return out


def macho_dynamic(path: str) -> Optional[dict]:
    """LC_LOAD*_DYLIB and LC_RPATH entries of a 64-bit little-endian Mach-O binary, or None."""
    with open(path, "rb") as f:
        data = f.read()
    if len(data) < 32:
        return None
    magic, = struct.unpack_from("<I", data, 0)
    if magic != 0xFEEDFACF:                             # MH_MAGIC_64
        return None
    _, _, _, _, ncmds, sizeofcmds, _, _ = struct.unpack_from("<IiiIIIII", data, 0)
    out = {"format": "macho", "needed": [], "rpath": [], "runpath": []}
    off = 32
    end = min(len(data), 32 + sizeofcmds)
    for _ in range(ncmds):
        if off + 8 > end:
            break
        cmd, cmdsize = struct.unpack_from("<II", data, off)
        if cmdsize < 8 or off + cmdsize > len(data):
            break
        if cmd in (0x0C, 0x80000018, 0x8000001F, 0x80000020) and cmdsize >= 24:
            str_off, = struct.unpack_from("<I", data, off + 8)
            name = _cstr(data, off + str_off, off + cmdsize)
            if name:
                out["needed"].append(name)
        elif cmd == 0x8000001C and cmdsize >= 12:       # LC_RPATH
            str_off, = struct.unpack_from("<I", data, off + 8)
            rp = _cstr(data, off + str_off, off + cmdsize)
            if rp:
                out["rpath"].extend(rp.split(":"))
        off += cmdsize
    return out


def pe_dynamic(path: str) -> Optional[dict]:
    """Imported DLL names from a Windows PE32 / PE32+ executable or DLL, or None."""
    with open(path, "rb") as f:
        data = f.read()
    if len(data) < 0x40 or data[:2] != b"MZ":
        return None
    pe_off, = struct.unpack_from("<I", data, 0x3C)
    if pe_off + 24 > len(data) or data[pe_off:pe_off + 4] != b"PE\x00\x00":
        return None
    _, nsec, _, _, _, opt_size, _ = struct.unpack_from("<HHIIIHH", data, pe_off + 4)
    opt_off = pe_off + 24
    if opt_off + opt_size > len(data) or opt_size < 24:
        return None
    opt_magic, = struct.unpack_from("<H", data, opt_off)
    if opt_magic == 0x20B:                              # PE32+ (64-bit)
        num_rva_off, dd_off = opt_off + 108, opt_off + 112
    elif opt_magic == 0x10B:                            # PE32 (32-bit)
        num_rva_off, dd_off = opt_off + 92, opt_off + 96
    else:
        return None
    out = {"format": "pe", "needed": [], "rpath": [], "runpath": []}
    if num_rva_off + 4 > opt_off + opt_size:
        return out
    num_rva, = struct.unpack_from("<I", data, num_rva_off)
    if num_rva < 2 or dd_off + 16 > opt_off + opt_size:
        return out
    imp_rva, _ = struct.unpack_from("<II", data, dd_off + 8)
    if imp_rva == 0:
        return out

    sec_off = opt_off + opt_size
    sections = []
    for i in range(nsec):
        s_off = sec_off + i * 40
        if s_off + 40 > len(data):
            break
        vsize, vaddr, rsize, rptr = struct.unpack_from("<IIII", data, s_off + 8)
        sections.append((vaddr, vsize, rsize, rptr))

    def rva_to_off(rva: int) -> Optional[int]:
        for vaddr, vsize, rsize, rptr in sections:
            span = max(vsize, rsize)
            if vaddr <= rva < vaddr + span:
                return rptr + (rva - vaddr)
        return None

    desc_off = rva_to_off(imp_rva)
    while desc_off is not None and desc_off + 20 <= len(data):
        ilt_rva, _, _, name_rva, iat_rva = struct.unpack_from("<IIIII", data, desc_off)
        if ilt_rva == 0 and name_rva == 0 and iat_rva == 0:
            break
        if name_rva:
            noff = rva_to_off(name_rva)
            if noff is not None:
                dll = _cstr(data, noff)
                if dll:
                    out["needed"].append(dll)
        desc_off += 20
    return out


def inspect_binary(path: str) -> Optional[dict]:
    return elf_dynamic(path) or macho_dynamic(path) or pe_dynamic(path)


def is_python_lib(name: str) -> bool:
    """True when `name` refers to CPython's shared library / DLL / Framework binary."""
    b = name.replace("\\", "/").rsplit("/", 1)[-1]
    if b.startswith("libpython"):
        return True
    if name.replace("\\", "/").endswith("/Python") or b == "Python":
        return True
    return bool(_PY_DLL_RE.fullmatch(b))


def _ldconfig_dirs() -> list:
    try:
        r = subprocess.run(["ldconfig", "-p"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return []
    return list(dict.fromkeys(os.path.dirname(m.group(1)) for m in re.finditer(r"=> (\S+)", r.stdout)))


def _find_in_dir_ci(d: str, filename: str) -> Optional[str]:
    p = os.path.join(d, filename)
    if os.path.exists(p):
        return p
    if os.path.isdir(d):
        target = filename.lower()
        try:
            for entry in os.listdir(d):
                if entry.lower() == target:
                    return os.path.join(d, entry)
        except OSError:
            pass
    return None


def resolve_lib(name: str, origin: str, dyn: dict, env=None) -> Optional[str]:
    """Resolve `name` according to the target binary format's dynamic loader rules."""
    env = os.environ if env is None else env
    fmt = dyn.get("format", "elf")

    if fmt == "macho":
        if name.startswith(("/usr/lib/", "/System/Library/")):
            return name                                 # lives in dyld shared cache on modern macOS
        if name.startswith(("@loader_path/", "@executable_path/")):
            rel = name.split("/", 1)[1]
            cand = os.path.normpath(os.path.join(origin, rel))
            return cand if os.path.exists(cand) else None
        if name.startswith("@rpath/"):
            rel = name[len("@rpath/"):]
            expand = lambda d: d.replace("@loader_path", origin).replace("@executable_path", origin)
            dirs = [expand(d) for d in dyn.get("rpath", [])]
            dirs += [d for d in env.get("DYLD_LIBRARY_PATH", "").split(":") if d]
            fallback = env.get("DYLD_FALLBACK_LIBRARY_PATH", "/usr/local/lib:/usr/lib")
            dirs += [d for d in fallback.split(":") if d]
            for d in dirs:
                cand = os.path.normpath(os.path.join(d, rel))
                if os.path.exists(cand):
                    return cand
            return None
        return name if os.path.exists(name) else None

    if fmt == "pe":
        low = name.lower()
        if low.startswith(("api-ms-win-", "ext-ms-win-")):
            return f"<windows-api-set>/{name}"
        sys_root = env.get("SystemRoot") or env.get("WINDIR") or r"C:\Windows"
        dirs = [origin, os.path.join(sys_root, "System32"), sys_root]
        dirs += [d for d in env.get("PATH", "").split(";") if d]
        for d in dirs:
            hit = _find_in_dir_ci(d, name)
            if hit:
                return hit
        return None

    # Default: Linux ELF ld.so search order
    expand = lambda d: d.replace("${ORIGIN}", origin).replace("$ORIGIN", origin)
    dirs = [expand(d) for d in (dyn["rpath"] if not dyn["runpath"] else [])]
    dirs += [d for d in env.get("LD_LIBRARY_PATH", "").split(":") if d]
    dirs += [expand(d) for d in dyn["runpath"]] + _ldconfig_dirs() + DEFAULT_ELF_DIRS
    for d in dirs:
        p = os.path.join(d, name)
        if os.path.exists(p):
            return p
    return None


# ---------------------------------------------------------------------------
@dataclass
class Report:
    ok: bool = True
    lines: list = field(default_factory=list)
    fixes: list = field(default_factory=list)
    found: dict = field(default_factory=dict)
    missing: list = field(default_factory=list)

    def text(self) -> str:
        out = list(self.lines)
        if self.fixes:
            out += ["", "How to fix:"] + [f"  {i}. {f}" for i, f in enumerate(self.fixes, 1)]
        return "\n".join(out)


def python_version() -> str:
    return f"{sys.version_info.major}.{sys.version_info.minor}"


def install_hints(soname: str = "") -> list:
    v = python_version()
    if sys.platform == "darwin":
        return [
            f"uv python install {v}   (then: export DYLD_LIBRARY_PATH=$(dirname $(uv python find {v}))/../lib)",
            f"brew install python@{v}   |   conda install python={v}",
            "or build on the target machine (`pac build` there); the library is then found via @rpath",
        ]
    if sys.platform == "win32":
        return [
            f"install Python {v} from python.org / winget / `uv python install {v}` and add its directory to PATH",
            "or run `pac bundle <binary> --with-stdlib` to copy python3XX.dll + Lib next to the executable",
        ]
    return [
        f"uv python install {v}   (then: export LD_LIBRARY_PATH=$(dirname $(uv python find {v}))/../lib)",
        f"apt install libpython{v}   |   dnf install python{v}-libs   |   conda install python={v}",
        "or build on the target machine (`pac build` there); the library is then found via the baked rpath",
    ]


def _has_stdlib_dir(home: str) -> bool:
    v = python_version()
    return (
        os.path.isfile(os.path.join(home, "lib", f"python{v}", "os.py"))
        or os.path.isfile(os.path.join(home, "Lib", "os.py"))
    )


def python_home_status(origin: str, env=None) -> str:
    """Which Python home the binary will use (same priority as the runtime, minus the build-time default)."""
    env = os.environ if env is None else env
    v = python_version()
    if env.get("PA_PYTHON_HOME"):
        h = env["PA_PYTHON_HOME"]
        return f"PA_PYTHON_HOME={h}" + ("" if _has_stdlib_dir(h) else "  (WARNING: no Python stdlib os.py there)")
    if env.get("PYTHONHOME"):
        return f"PYTHONHOME={env['PYTHONHOME']}"
    bundled = os.path.join(origin, "python-home")
    if _has_stdlib_dir(bundled):
        return f"{bundled} (bundled)"
    return ("not set here -> the build-time default is used if it still exists; otherwise set PA_PYTHON_HOME, or ship a "
            "stdlib with `pac bundle <binary> --with-stdlib` (a binary built with --relocatable has no default)")


def _local_libpython(needed_name: str) -> Optional[str]:
    """Shared Python library of the interpreter running pac, when its filename matches."""
    base = needed_name.replace("\\", "/").rsplit("/", 1)[-1]
    if sys.platform == "win32":
        for d in (sys.base_prefix, os.path.dirname(sys.executable), sys.prefix):
            hit = _find_in_dir_ci(d, base) if d else None
            if hit:
                return hit
        return None
    if sys.platform == "darwin":
        from .build import python_link_info
        lf = python_link_info().get("lib_file", "")
        if lf and os.path.isfile(lf) and (os.path.basename(lf) == base or is_python_lib(lf)):
            return lf
        return None
    libdir = sysconfig.get_config_var("LIBDIR") or ""
    for cand in (os.path.join(libdir, base), os.path.join(libdir, sysconfig.get_config_var("LDLIBRARY") or "-")):
        if os.path.exists(cand):
            return os.path.realpath(cand) if os.path.basename(os.path.realpath(cand)) == base else cand
    return None


def check_binary(path: str, env=None) -> Report:
    """Will `path` start? Every needed shared library must resolve; Python libs get tailored advice."""
    rep = Report()
    path = os.path.abspath(path)
    if not os.path.isfile(path):
        return Report(ok=False, lines=[f"error: {path} does not exist"])
    dyn = inspect_binary(path)
    if dyn is None:
        return Report(ok=False, lines=[f"error: {path} is not a supported ELF, Mach-O, or PE binary"])
    fmt = dyn["format"]
    origin = os.path.dirname(path)
    env_map = env if env is not None else os.environ
    rep.lines.append(f"binary : {path} ({fmt})")
    rep.lines.append(f"needs  : {', '.join(dyn['needed']) or '(nothing)'}")
    if fmt == "elf":
        rep.lines.append(f"search : RUNPATH={':'.join(dyn['runpath']) or '-'}  RPATH={':'.join(dyn['rpath']) or '-'}  "
                         f"LD_LIBRARY_PATH={env_map.get('LD_LIBRARY_PATH', '') or '-'}")
    elif fmt == "macho":
        rep.lines.append(f"search : RPATH={':'.join(dyn['rpath']) or '-'}  "
                         f"DYLD_LIBRARY_PATH={env_map.get('DYLD_LIBRARY_PATH', '') or '-'}")
    else:
        rep.lines.append(f"search : exe_dir={origin}  PATH={env_map.get('PATH', '') or '-'}")

    for lib in dyn["needed"]:
        hit = resolve_lib(lib, origin, dyn, env)
        if hit:
            rep.found[lib] = hit
        else:
            rep.missing.append(lib)
    for lib, hit in rep.found.items():
        if is_python_lib(lib):
            rep.lines.append(f"found  : {lib} -> {hit}")
    for lib in rep.missing:
        rep.lines.append(f"MISSING: {lib}")
    if any(is_python_lib(lib) for lib in dyn["needed"]):
        rep.lines.append(f"home   : {python_home_status(origin, env)}")

    py = [m for m in rep.missing if is_python_lib(m)]
    if py:
        local = _local_libpython(py[0])
        if local:
            if fmt == "macho":
                rep.fixes.append(f"export DYLD_LIBRARY_PATH={os.path.dirname(local)}:$DYLD_LIBRARY_PATH   (found: {local})")
            elif fmt == "pe":
                rep.fixes.append(f"add {os.path.dirname(local)} to PATH   (found on this machine: {local})")
            else:
                rep.fixes.append(f"export LD_LIBRARY_PATH={os.path.dirname(local)}:$LD_LIBRARY_PATH   (found on this machine: {local})")
        rep.fixes.append(f"pac bundle {path}   (copies {py[0]} next to the binary; add --with-stdlib for a self-contained Python home)")
        rep.fixes += install_hints(py[0])
    elif rep.missing:
        rep.fixes.append("install the missing system libraries listed above (e.g. via your package manager)")
    rep.ok = not rep.missing
    rep.lines.append("status : OK, the binary can start" if rep.ok else "status : the binary will NOT start (loader error)")
    return rep


def check_environment() -> Report:
    """Build-machine health: C compiler, llvmlite, Python headers, and shared Python library."""
    from .build import BuildError, find_c_compiler, python_link_info

    rep = Report()
    v = python_version()

    def item(ok: bool, text: str, fix: str = "") -> None:
        rep.lines.append(("ok     : " if ok else "MISSING: ") + text)
        if not ok:
            rep.ok = False
            if fix:
                rep.fixes.append(fix)

    try:
        kind, cc = find_c_compiler()
        item(True, f"{kind} ({cc})")
    except BuildError as e:
        item(False, "C compiler", str(e))

    try:
        import llvmlite
        item(True, f"llvmlite {llvmlite.__version__}")
    except ImportError:
        item(False, "llvmlite", "pip install llvmlite")

    pyinfo = python_link_info()
    inc = pyinfo["include"] or ""
    inc_fix = {
        "win32": f"reinstall Python {v} with development headers/libs (python.org or `uv python install {v}`)",
        "darwin": f"install Python {v} headers (`uv python install {v}` or `brew install python@{v}`)",
    }.get(sys.platform, f"install Python headers: apt install python{v}-dev | uv python install {v} | conda install python={v}")
    item(os.path.isfile(os.path.join(inc, "Python.h")), f"Python.h ({inc})", inc_fix)

    ver = sysconfig.get_config_var("LDVERSION") or sysconfig.get_config_var("VERSION") or v
    if sys.platform == "win32":
        has_lib = bool(pyinfo["lib_file"] and os.path.isfile(pyinfo["lib_file"]))
        has_dll = os.path.isfile(os.path.join(pyinfo["dll_dir"], f"{pyinfo['lib_name']}.dll"))
        item(has_lib and has_dll, f"shared {pyinfo['lib_name']} ({pyinfo['lib_file'] or pyinfo['libdir']})",
             f"install Python {v} with python{v.replace('.', '')}.lib and .dll")
    elif sys.platform == "darwin":
        lf = pyinfo["lib_file"]
        item(bool(lf and os.path.isfile(lf)), f"shared libpython{ver} ({lf or pyinfo['libdir']})",
             f"this Python has no shared libpython: `uv python install {v}` | `brew install python@{v}`")
    else:
        libdir = pyinfo["libdir"] or ""
        shared = glob.glob(os.path.join(libdir, f"libpython{ver}.so*"))
        item(bool(shared), f"shared libpython{ver} ({shared[0] if shared else libdir})",
             f"this Python has no shared libpython: apt install libpython{v}-dev | use `uv python install {v}` | conda install python={v}")

    item(True, f"python {sys.version.split()[0]} at {sys.executable}")
    return rep


def relocatable_notice(binary: str) -> str:
    """Printed after `pac build --relocatable`: what the target machine must provide."""
    dyn = inspect_binary(binary) or {"needed": [], "format": ""}
    libs = [n for n in dyn["needed"] if is_python_lib(n)]
    if not libs:
        return ""
    env_hint = "PATH" if dyn.get("format") == "pe" else "DYLD_LIBRARY_PATH" if dyn.get("format") == "macho" else "LD_LIBRARY_PATH"
    return (f"note: --relocatable binary needs {libs[0]} at run time ({env_hint}, next to the binary, or a system install)\n"
            f"      check : pac doctor {binary}\n      fix   : pac bundle {binary} [--with-stdlib]")


def bundle(binary: str, dest: Optional[str] = None, with_stdlib: bool = False) -> Report:
    """Copy Python's shared lib (and optionally a trimmed stdlib) next to the binary so it runs without env vars."""
    binary = os.path.abspath(binary)
    dest = os.path.abspath(dest or os.path.dirname(binary))
    dyn = inspect_binary(binary)
    rep = Report()
    if dyn is None:
        return Report(ok=False, lines=[f"error: {binary} is not a supported ELF, Mach-O, or PE binary"])
    fmt = dyn["format"]
    libs = [n for n in dyn["needed"] if is_python_lib(n)]
    if not libs:
        return Report(ok=True, lines=["nothing to bundle: the binary does not link Python"])

    os.makedirs(dest, exist_ok=True)
    for lib_ref in libs:
        src = resolve_lib(lib_ref, os.path.dirname(binary), dyn) or _local_libpython(lib_ref)
        if not src or not os.path.exists(src):
            return Report(ok=False, lines=[f"error: cannot find {lib_ref} on this machine to bundle"], fixes=install_hints(lib_ref))
        base = lib_ref.replace("\\", "/").rsplit("/", 1)[-1]
        target = os.path.join(dest, base)
        if os.path.abspath(src) != os.path.abspath(target):
            shutil.copy2(src, target)
        rep.lines.append(f"bundled: {base} <- {src}")

        if fmt == "macho" and sys.platform == "darwin" and dest == os.path.dirname(binary) and lib_ref.startswith("/"):
            new_ref = f"@rpath/{base}"
            r = subprocess.run(["install_name_tool", "-change", lib_ref, new_ref, binary], capture_output=True)
            if r.returncode == 0:
                subprocess.run(["codesign", "--force", "--sign", "-", binary], capture_output=True)

    # On Windows, python3XX.dll often delegates stable ABI entries to python3.dll in the same dir.
    if fmt == "pe":
        src0 = resolve_lib(libs[0], os.path.dirname(binary), dyn) or _local_libpython(libs[0])
        if src0:
            py3 = _find_in_dir_ci(os.path.dirname(src0), "python3.dll")
            if py3 and os.path.abspath(py3) != os.path.abspath(os.path.join(dest, "python3.dll")):
                shutil.copy2(py3, os.path.join(dest, "python3.dll"))

    if not with_stdlib:
        rep.lines.append("note   : libpython alone is not enough for a --relocatable binary: it also needs a Python home "
                         "(PA_PYTHON_HOME) or `--with-stdlib`")
    if dest != os.path.dirname(binary):
        env_var = "PATH" if fmt == "pe" else "DYLD_LIBRARY_PATH" if fmt == "macho" else "LD_LIBRARY_PATH"
        rep.lines.append(f"note   : binary and library are in different directories; keep them together or add to {env_var}")
    if with_stdlib:
        stdlib = sysconfig.get_paths()["stdlib"]
        home = os.path.join(dest, "python-home")
        if fmt == "pe":
            lib_dest = os.path.join(home, "Lib")
            if not os.path.isdir(lib_dest):
                shutil.copytree(stdlib, lib_dest, ignore=STDLIB_IGNORE, symlinks=False)
            dlls_src = os.path.join(sys.base_prefix, "DLLs")
            dlls_dest = os.path.join(home, "DLLs")
            if os.path.isdir(dlls_src) and not os.path.isdir(dlls_dest):
                shutil.copytree(dlls_src, dlls_dest, ignore=STDLIB_IGNORE)
        else:
            lib_dest = os.path.join(home, "lib", os.path.basename(stdlib))
            if not os.path.isdir(lib_dest):
                shutil.copytree(stdlib, lib_dest, ignore=STDLIB_IGNORE, symlinks=True)
        rep.lines.append(f"bundled: Python stdlib -> {home}  (used automatically: <binary dir>/python-home)")
        rep.lines.append("note   : third-party packages (numpy, ...) still come from PA_PYPATH / VIRTUAL_ENV")
    rep.lines.append(f"verify : pac doctor {binary}")
    return rep
