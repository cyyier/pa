"""pac doctor / pac bundle: detect and fix a missing libpython (or Python home) for built binaries.

A binary that links libpython fails *inside the dynamic loader* if the library cannot be found, long before any
of our code runs. So detection happens here (ELF inspection, same search order as ld.so) and the fix is assisted:
`pac bundle` copies libpython (optionally a trimmed stdlib) next to the binary, where the `$ORIGIN` rpath finds it.
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

DEFAULT_DIRS = ["/lib", "/usr/lib", "/lib64", "/usr/lib64", "/usr/local/lib", "/lib/x86_64-linux-gnu",
                "/usr/lib/x86_64-linux-gnu", "/lib/aarch64-linux-gnu", "/usr/lib/aarch64-linux-gnu"]
STDLIB_IGNORE = shutil.ignore_patterns("__pycache__", "test", "tests", "idlelib", "tkinter", "turtledemo",
                                       "site-packages", "config-*", "ensurepip", "lib2to3", "*.pyc")


# ---------------------------------------------------------------------------
# ELF inspection (64-bit little endian is enough for our targets)
# ---------------------------------------------------------------------------
def elf_dynamic(path: str) -> Optional[dict]:
    """DT_NEEDED / DT_RPATH / DT_RUNPATH of an ELF file, or None if it is not a 64-bit LE ELF."""
    with open(path, "rb") as f:
        data = f.read()
    if data[:4] != b"\x7fELF" or data[4] != 2 or data[5] != 1:
        return None
    shoff, = struct.unpack_from("<Q", data, 0x28)
    shentsize, shnum = struct.unpack_from("<HH", data, 0x3A)
    secs = [struct.unpack_from("<IIQQQQIIQQ", data, shoff + i * shentsize) for i in range(shnum)]
    out = {"needed": [], "rpath": [], "runpath": []}
    for s in secs:
        if s[1] != 6:                                   # SHT_DYNAMIC
            continue
        strtab = secs[s[6]]
        for off in range(s[4], s[4] + s[5], 16):
            tag, val = struct.unpack_from("<qQ", data, off)
            if tag == 0:
                break
            if tag in (1, 15, 29):
                start = strtab[4] + val
                name = data[start:data.index(b"\0", start)].decode()
                out[{1: "needed", 15: "rpath", 29: "runpath"}[tag]].extend(name.split(":") if tag != 1 else [name])
    return out


def _ldconfig_dirs() -> list:
    try:
        r = subprocess.run(["ldconfig", "-p"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return []
    return list(dict.fromkeys(os.path.dirname(m.group(1)) for m in re.finditer(r"=> (\S+)", r.stdout)))


def resolve_lib(name: str, origin: str, dyn: dict, env=None) -> Optional[str]:
    """Where would ld.so find `name` for a binary located in `origin`? (RPATH, LD_LIBRARY_PATH, RUNPATH, cache, defaults)"""
    env = os.environ if env is None else env
    expand = lambda d: d.replace("${ORIGIN}", origin).replace("$ORIGIN", origin)
    dirs = [expand(d) for d in (dyn["rpath"] if not dyn["runpath"] else [])]
    dirs += [d for d in env.get("LD_LIBRARY_PATH", "").split(":") if d]
    dirs += [expand(d) for d in dyn["runpath"]] + _ldconfig_dirs() + DEFAULT_DIRS
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
    return [f"uv python install {v}   (then: export LD_LIBRARY_PATH=$(dirname $(uv python find {v}))/../lib)",
            f"apt install libpython{v}   |   dnf install python{v}-libs   |   conda install python={v}",
            "or build on the target machine (`pac build` there); the library is then found via the baked rpath"]


def check_binary(path: str, env=None) -> Report:
    """Will `path` start? Every DT_NEEDED library must resolve; libpython gets tailored advice."""
    rep = Report()
    path = os.path.abspath(path)
    if not os.path.isfile(path):
        return Report(ok=False, lines=[f"error: {path} does not exist"])
    dyn = elf_dynamic(path)
    if dyn is None:
        return Report(ok=False, lines=[f"error: {path} is not a 64-bit little-endian ELF binary (Linux only)"])
    origin = os.path.dirname(path)
    rep.lines.append(f"binary : {path}")
    rep.lines.append(f"needs  : {', '.join(dyn['needed']) or '(nothing)'}")
    rep.lines.append(f"search : RUNPATH={':'.join(dyn['runpath']) or '-'}  RPATH={':'.join(dyn['rpath']) or '-'}  "
                     f"LD_LIBRARY_PATH={(env or os.environ).get('LD_LIBRARY_PATH', '') or '-'}")
    for lib in dyn["needed"]:
        hit = resolve_lib(lib, origin, dyn, env)
        if hit:
            rep.found[lib] = hit
        else:
            rep.missing.append(lib)
    for lib, hit in rep.found.items():
        if lib.startswith("libpython"):
            rep.lines.append(f"found  : {lib} -> {hit}")
    for lib in rep.missing:
        rep.lines.append(f"MISSING: {lib}")
    if any(lib.startswith("libpython") for lib in dyn["needed"]):
        rep.lines.append(f"home   : {python_home_status(origin, env)}")
    py = [m for m in rep.missing if m.startswith("libpython")]
    if py:
        local = _local_libpython(py[0])
        if local:
            rep.fixes.append(f"export LD_LIBRARY_PATH={os.path.dirname(local)}:$LD_LIBRARY_PATH   (found on this machine: {local})")
        rep.fixes.append(f"pac bundle {path}   (copies {py[0]} next to the binary; add --with-stdlib for a self-contained Python home)")
        rep.fixes += install_hints(py[0])
    elif rep.missing:
        rep.fixes.append("install the missing system libraries listed above (e.g. via your package manager)")
    rep.ok = not rep.missing
    rep.lines.append("status : OK, the binary can start" if rep.ok else "status : the binary will NOT start (loader error)")
    return rep


def python_home_status(origin: str, env=None) -> str:
    """Which Python home the binary will use (same priority as the runtime, minus the build-time default)."""
    env = os.environ if env is None else env
    v = python_version()
    has = lambda home: os.path.isfile(os.path.join(home, "lib", f"python{v}", "os.py"))
    if env.get("PA_PYTHON_HOME"):
        h = env["PA_PYTHON_HOME"]
        return f"PA_PYTHON_HOME={h}" + ("" if has(h) else "  (WARNING: no lib/python%s/os.py there)" % v)
    if env.get("PYTHONHOME"):
        return f"PYTHONHOME={env['PYTHONHOME']}"
    bundled = os.path.join(origin, "python-home")
    if has(bundled):
        return f"{bundled} (bundled)"
    return ("not set here -> the build-time default is used if it still exists; otherwise set PA_PYTHON_HOME, or ship a "
            "stdlib with `pac bundle <binary> --with-stdlib` (a binary built with --relocatable has no default)")


def _local_libpython(soname: str) -> Optional[str]:
    """libpython of the interpreter running pac, when its soname matches."""
    libdir = sysconfig.get_config_var("LIBDIR") or ""
    for cand in (os.path.join(libdir, soname), os.path.join(libdir, sysconfig.get_config_var("LDLIBRARY") or "-")):
        if os.path.exists(cand):
            return os.path.realpath(cand) if os.path.basename(os.path.realpath(cand)) == soname else cand
    return None


def check_environment() -> Report:
    """Build-machine health: gcc, llvmlite, Python headers and a shared libpython."""
    rep = Report()
    v = python_version()

    def item(ok: bool, text: str, fix: str = "") -> None:
        rep.lines.append(("ok     : " if ok else "MISSING: ") + text)
        if not ok:
            rep.ok = False
            if fix:
                rep.fixes.append(fix)

    gcc = shutil.which("gcc")
    item(bool(gcc), f"gcc ({gcc})" if gcc else "gcc", "install a C compiler (apt install build-essential | xcode-select --install)")
    try:
        import llvmlite
        item(True, f"llvmlite {llvmlite.__version__}")
    except ImportError:
        item(False, "llvmlite", "pip install llvmlite")
    inc = sysconfig.get_config_var("INCLUDEPY") or ""
    item(os.path.isfile(os.path.join(inc, "Python.h")), f"Python.h ({inc})",
         f"install Python headers: apt install python{v}-dev | uv python install {v} | conda install python={v}")
    libdir = sysconfig.get_config_var("LIBDIR") or ""
    ver = sysconfig.get_config_var("LDVERSION") or sysconfig.get_config_var("VERSION")
    shared = glob.glob(os.path.join(libdir, f"libpython{ver}.so*"))
    item(bool(shared), f"shared libpython{ver} ({shared[0] if shared else libdir})",
         f"this Python has no shared libpython: apt install libpython{v}-dev | use `uv python install {v}` | conda install python={v}")
    item(True, f"python {sys.version.split()[0]} at {sys.executable}")
    return rep


def relocatable_notice(binary: str) -> str:
    """Printed after `pac build --relocatable`: what the target machine must provide."""
    dyn = elf_dynamic(binary) or {"needed": []}
    libs = [n for n in dyn["needed"] if n.startswith("libpython")]
    if not libs:
        return ""
    return (f"note: --relocatable binary needs {libs[0]} at run time (LD_LIBRARY_PATH, next to the binary, or a system install)\n"
            f"      check : pac doctor {binary}\n      fix   : pac bundle {binary} [--with-stdlib]")


def bundle(binary: str, dest: Optional[str] = None, with_stdlib: bool = False) -> Report:
    """Copy libpython (and optionally a trimmed stdlib) next to the binary so it runs without environment variables."""
    binary = os.path.abspath(binary)
    dest = os.path.abspath(dest or os.path.dirname(binary))
    dyn = elf_dynamic(binary)
    rep = Report()
    if dyn is None:
        return Report(ok=False, lines=[f"error: {binary} is not a 64-bit little-endian ELF binary"])
    libs = [n for n in dyn["needed"] if n.startswith("libpython")]
    if not libs:
        return Report(ok=True, lines=["nothing to bundle: the binary does not link libpython"])
    src = resolve_lib(libs[0], os.path.dirname(binary), dyn) or _local_libpython(libs[0])
    if not src:
        return Report(ok=False, lines=[f"error: cannot find {libs[0]} on this machine to bundle"], fixes=install_hints(libs[0]))
    os.makedirs(dest, exist_ok=True)
    target = os.path.join(dest, libs[0])
    if os.path.abspath(src) != os.path.abspath(target):
        shutil.copy2(src, target)
    rep.lines.append(f"bundled: {libs[0]} <- {src}")
    if not with_stdlib:
        rep.lines.append("note   : libpython alone is not enough for a --relocatable binary: it also needs a Python home "
                         "(PA_PYTHON_HOME) or `--with-stdlib`")
    if dest != os.path.dirname(binary):
        rep.lines.append(f"note   : binary and library are in different directories; keep {libs[0]} in the binary's directory "
                         "or add it to LD_LIBRARY_PATH")
    if with_stdlib:
        stdlib = sysconfig.get_paths()["stdlib"]
        home = os.path.join(dest, "python-home")
        lib_dest = os.path.join(home, "lib", os.path.basename(stdlib))
        if not os.path.isdir(lib_dest):
            shutil.copytree(stdlib, lib_dest, ignore=STDLIB_IGNORE, symlinks=True)
        rep.lines.append(f"bundled: Python stdlib -> {home}  (used automatically: <binary dir>/python-home)")
        rep.lines.append("note   : third-party packages (numpy, ...) still come from PA_PYPATH / VIRTUAL_ENV")
    rep.lines.append(f"verify : pac doctor {binary}")
    return rep
