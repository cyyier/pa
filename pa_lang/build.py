"""Compile .pa -> native executable / shared library across Linux, macOS, and Windows."""
from __future__ import annotations

import ast
import glob
import os
import shutil
import subprocess
import sys
import sysconfig
import tempfile
from typing import Optional

from llvmlite import binding as llvm

from .codegen import CodegenError, generate
from .diagnostics import DiagnosticSink
from .infer import analyze
from .validator import check_source, default_search_dirs

BRIDGE_C = os.path.join(os.path.dirname(__file__), "runtime_bridge.c")
CORE_C = os.path.join(os.path.dirname(__file__), "runtime_core.c")
SUPPORTED_PLATFORMS = ("linux", "darwin", "win32")


class BuildError(Exception):
    pass


def find_c_compiler() -> tuple[str, str]:
    """Find an available C compiler driver and return (kind, path).

    kind is one of 'msvc', 'clang', 'gcc', 'mingw'. Honours PA_CC if set.
    """
    override = os.environ.get("PA_CC")
    if override:
        resolved = shutil.which(override) or (override if os.path.isfile(override) else "")
        if not resolved:
            raise BuildError(f"PA_CC compiler '{override}' not found")
        base = os.path.basename(resolved).lower()
        if sys.platform == "win32" and base in ("cl", "cl.exe"):
            return "msvc", resolved
        if "clang" in base:
            return "clang", resolved
        return ("mingw" if sys.platform == "win32" else "gcc"), resolved

    if sys.platform == "win32":
        candidates = (("msvc", "cl"), ("clang", "clang"), ("mingw", "gcc"))
    elif sys.platform == "darwin":
        candidates = (("clang", "clang"), ("clang", "cc"), ("gcc", "gcc"))
    else:
        candidates = (("gcc", "gcc"), ("clang", "clang"), ("gcc", "cc"))

    for kind, cmd in candidates:
        hit = shutil.which(cmd)
        if hit:
            return kind, hit

    hints = {
        "win32": "open a Visual Studio Developer Prompt (`cl`) or install LLVM (`clang`) / MinGW (`gcc`)",
        "darwin": "install Xcode Command Line Tools (`xcode-select --install`)",
    }.get(sys.platform, "install a C compiler (e.g. `apt install build-essential`)")
    raise BuildError(f"no C compiler found ({hints})")


def _machine(compiler_kind: str = ""):
    llvm.initialize_native_target()
    llvm.initialize_native_asmprinter()
    if sys.platform == "win32" and compiler_kind == "mingw":
        target = llvm.Target.from_triple("x86_64-w64-windows-gnu")
    else:
        target = llvm.Target.from_default_triple()
    reloc = "default" if sys.platform == "win32" else "pic"
    return target.create_target_machine(opt=2, reloc=reloc, codemodel="default")


def ir_to_object(ir_text: str, compiler_kind: str = "") -> bytes:
    tm = _machine(compiler_kind)
    mod = llvm.parse_assembly(ir_text)
    mod.triple = tm.triple
    mod.data_layout = str(tm.target_data)
    mod.verify()
    try:
        pb = llvm.create_pass_builder(tm, llvm.create_pipeline_tuning_options(speed_level=2))
        pb.getModulePassManager().run(mod, pb)
    except Exception:  # optimizer API differences must never block a build
        pass
    return tm.emit_object(mod)


def python_link_info() -> dict:
    """Locate Python headers, link library, and shared runtime library directory across OSes."""
    include = (
        sysconfig.get_paths().get("include")
        or sysconfig.get_config_var("INCLUDEPY")
        or os.path.join(sys.base_prefix, "include")
    )
    ver = sysconfig.get_config_var("LDVERSION") or sysconfig.get_config_var("VERSION") or f"{sys.version_info.major}.{sys.version_info.minor}"
    ver_nodot = f"{sys.version_info.major}{sys.version_info.minor}"

    if sys.platform == "win32":
        lib_name = f"python{ver_nodot}"
        lib_dirs = [d for d in (os.path.join(sys.base_prefix, "libs"), os.path.join(sys.prefix, "libs"),
                                sysconfig.get_config_var("LIBDIR") or "") if d]
        lib_file = ""
        for d in lib_dirs:
            cand = os.path.join(d, f"{lib_name}.lib")
            if os.path.isfile(cand):
                lib_file = cand
                break
        libdir = os.path.dirname(lib_file) if lib_file else (lib_dirs[0] if lib_dirs else "")
        dll_dir = ""
        for d in (sys.base_prefix, os.path.dirname(sys.executable), sys.prefix):
            if d and os.path.isfile(os.path.join(d, f"{lib_name}.dll")):
                dll_dir = d
                break
        return {"include": include, "libdir": libdir, "lib_file": lib_file, "lib_name": lib_name,
                "dll_dir": dll_dir or sys.base_prefix}

    if sys.platform == "darwin":
        lib_name = f"python{ver}"
        ldlib = sysconfig.get_config_var("LDLIBRARY") or ""
        fw_prefix = sysconfig.get_config_var("PYTHONFRAMEWORKPREFIX") or ""
        search_dirs = [d for d in (
            sysconfig.get_config_var("LIBDIR"),
            sysconfig.get_config_var("LIBPL"),
            os.path.join(sys.base_prefix, "lib"),
            os.path.join(sys.prefix, "lib"),
            sys.base_prefix,
        ) if d]
        cands = []
        for d in search_dirs:
            cands.append(os.path.join(d, f"libpython{ver}.dylib"))
            if ldlib:
                cands.append(os.path.join(d, ldlib))
        if fw_prefix and ldlib:
            cands.append(os.path.join(fw_prefix, ldlib))
        cands.append(os.path.join(sys.base_prefix, "Python"))
        lib_file = next((os.path.realpath(c) for c in cands if os.path.isfile(c)), "")
        libdir = os.path.dirname(lib_file) if lib_file else (search_dirs[0] if search_dirs else "")
        return {"include": include, "libdir": libdir, "lib_file": lib_file, "lib_name": lib_name, "dll_dir": libdir}

    libdir = sysconfig.get_config_var("LIBDIR") or os.path.join(sys.base_prefix, "lib")
    so_hits = sorted(glob.glob(os.path.join(libdir, f"libpython{ver}.so*")))
    return {"include": include, "libdir": libdir, "lib_file": so_hits[0] if so_hits else "",
            "lib_name": f"python{ver}", "dll_dir": libdir}


def python_flags() -> tuple:
    info = python_link_info()
    return info["include"], info["libdir"], info["lib_name"]


def _compile_c(kind: str, cc: str, src: str, obj: str, includes=(), defines=()) -> None:
    if kind == "msvc":
        cmd = [cc, "/nologo", "/O2", "/MD", "/c", src, f"/Fo:{obj}"]
        cmd += [f"/I{i}" for i in includes]
        cmd += [f"/D{d}" for d in defines]
    else:
        pic = [] if sys.platform == "win32" else ["-fPIC"]
        cmd = [cc, "-O2", *pic, "-c", src, "-o", obj]
        cmd += [f"-I{i}" for i in includes]
        cmd += [f"-D{d}" for d in defines]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode:
        raise BuildError(f"C compile failed for {os.path.basename(src)}:\n{r.stderr or r.stdout}")


def _link_binary(kind: str, cc: str, objs: list[str], out: str, shared: bool,
                 paws: set, relocatable: bool, pyinfo: dict) -> None:
    libdir = pyinfo.get("libdir", "")
    lib_file = pyinfo.get("lib_file", "")
    lib_name = pyinfo.get("lib_name", "")
    if sys.platform == "win32":
        if kind == "msvc":
            cmd = [cc, "/nologo", "/MD", *(["/LD"] if shared else []), *objs, f"/Fe:{out}", "/link"]
            if paws:
                if libdir:
                    cmd.append(f"/LIBPATH:{libdir}")
                cmd.append(lib_file or f"{lib_name}.lib")
        elif kind == "clang":
            cmd = [cc, "-O2", *(["-shared"] if shared else []), *objs, "-o", out]
            if paws:
                if lib_file:
                    cmd.append(lib_file)
                else:
                    if libdir:
                        cmd.append(f"-L{libdir}")
                    cmd.append(f"-l{lib_name}")
        else:  # mingw gcc
            cmd = [cc, "-O2", *(["-shared"] if shared else []), *objs, "-o", out, "-lm"]
            if paws:
                if libdir:
                    cmd.append(f"-L{libdir}")
                cmd.append(f"-l{lib_name}")
    elif sys.platform == "darwin":
        libs = ["-lm"]
        if paws:
            rpaths = ["-Wl,-rpath,@loader_path", "-Wl,-rpath,@loader_path/../lib"]
            if not relocatable and libdir:
                rpaths.append(f"-Wl,-rpath,{libdir}")
            if lib_file and (os.path.basename(lib_file) == "Python" or not lib_file.endswith(".dylib")):
                libs += [lib_file, *rpaths]
            else:
                if libdir:
                    libs.append(f"-L{libdir}")
                libs += [f"-l{lib_name}", *rpaths]
        cmd = [cc, "-O2", *(["-dynamiclib"] if shared else []), *objs, "-o", out, *libs]
    else:
        libs = ["-lm"]
        if paws:
            rpaths = ["-Wl,-rpath,$ORIGIN", "-Wl,-rpath,$ORIGIN/../lib"]
            if not relocatable and libdir:
                rpaths.append(f"-Wl,-rpath,{libdir}")
            libs += [f"-L{libdir}", f"-l{lib_name}", *rpaths, "-lpthread", "-ldl", "-lutil"]
        cmd = [cc, "-O2", *(["-shared", "-fPIC"] if shared else []), *objs, "-o", out, *libs]

    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode:
        raise BuildError("link failed:\n" + (r.stderr or r.stdout))

    if sys.platform == "darwin" and paws and relocatable:
        _macos_make_rpath_relative(out)


def _macos_make_rpath_relative(binary: str) -> None:
    """Rewrite absolute Python dylib install names to @rpath/<name> and ad-hoc re-sign."""
    from .doctor import is_python_lib, macho_dynamic
    dyn = macho_dynamic(binary)
    if not dyn:
        return
    changed = False
    for dep in dyn["needed"]:
        if is_python_lib(dep) and dep.startswith("/"):
            new_ref = f"@rpath/{os.path.basename(dep)}"
            r = subprocess.run(["install_name_tool", "-change", dep, new_ref, binary], capture_output=True)
            if r.returncode == 0:
                changed = True
    if changed:
        subprocess.run(["codesign", "--force", "--sign", "-", binary], capture_output=True)


def compile_pa(path: str, out: Optional[str] = None, shared: bool = False, emit_ir: bool = False,
               mode: str = "dev", python_home: Optional[str] = None, python_path: Optional[list] = None,
               relocatable: bool = False, include_dirs: Optional[list] = None) -> str:
    """Compile a .pa file. Returns the output path (or the IR text when emit_ir).

    dev : deep static lint of every imported .paw + full runtime boundary customs (element scans, big snapshots)
    prod: signature check only + light runtime customs (None/type/range/length; no per-element NaN scans)

    Paths: imported .paw sources are *embedded* in the binary (no source-directory path is baked in). The CPython
    location is resolved at run time: PA_PYTHON_HOME / PA_PYPATH / VIRTUAL_ENV first, then the build-time defaults
    (python_home / python_path, default: this interpreter's prefix and site-packages) if they still exist.
    relocatable=True bakes no Python paths at all (the host must provide the environment variables).
    """
    if mode not in ("dev", "prod"):
        raise BuildError(f"unknown mode '{mode}' (use dev or prod)")
    with open(path, encoding="utf-8") as f:
        source = f.read()
    dirs = default_search_dirs(path) + list(include_dirs or [])
    diags = check_source(source, path, search_dirs=dirs)
    errors = [d for d in diags if d.severity == "error"]
    if errors:
        raise BuildError("\n".join(d.brief() for d in errors))
    tree = ast.parse(source, path)
    from .modules import link_modules
    linked_tree, _, paw_paths = link_modules(tree, path, dirs)
    info = analyze(linked_tree, DiagnosticSink(source, path), source, path, dirs, paw_paths=paw_paths)
    paw_errors = []
    for pm in info.paw.values():
        with open(pm.path, encoding="utf-8") as f:
            paw_errors += [d for d in check_source(f.read(), pm.path, mode=mode) if d.severity == "error"]
    if paw_errors:
        raise BuildError("\n".join(d.brief() for d in paw_errors))
    try:
        ir_text, paws = generate(linked_tree, info, os.path.basename(path), shared, mode)
    except CodegenError as e:
        raise BuildError(f"{path}:{e.line}: error PA_E100: {e}")
    if emit_ir:
        return ir_text
    if sys.platform not in SUPPORTED_PLATFORMS and not os.environ.get("PA_ALLOW_UNSUPPORTED_PLATFORM"):
        raise BuildError(f"pac build supports Linux, macOS, and Windows (platform: {sys.platform}); "
                         "`pac check`, `pac lsp` and `pac build --emit-ir` work everywhere. "
                         "Set PA_ALLOW_UNSUPPORTED_PLATFORM=1 to try anyway.")
    kind, cc = find_c_compiler()
    shared_ext = ".dll" if sys.platform == "win32" else ".dylib" if sys.platform == "darwin" else ".so"
    exe_ext = ".exe" if sys.platform == "win32" else ""
    out = out or os.path.splitext(path)[0] + (shared_ext if shared else exe_ext)
    if sys.platform == "win32" and not shared and not out.lower().endswith(".exe"):
        out += ".exe"

    obj_ext = ".obj" if sys.platform == "win32" else ".o"
    with tempfile.TemporaryDirectory() as tmp:
        obj = os.path.join(tmp, f"pa_prog{obj_ext}")
        with open(obj, "wb") as f:
            f.write(ir_to_object(ir_text, compiler_kind=kind))
        objs = [obj, build_core(tmp, mode, kind, cc)]
        if shared:
            objs.append(build_wrappers(tmp, info, kind, cc))
        pyinfo = {}
        if paws:
            pyinfo = python_link_info()
            objs.append(build_bridge(tmp, mode, pyinfo["include"], kind, cc))
            objs.append(build_embed(tmp, info, python_home, python_path, relocatable, kind, cc))
        _link_binary(kind, cc, objs, out, shared, paws, relocatable, pyinfo)
    return out


def build_core(tmp: str, mode: str, kind: str = "", cc: str = "") -> str:
    if not cc:
        kind, cc = find_c_compiler()
    obj = os.path.join(tmp, "runtime_core" + (".obj" if sys.platform == "win32" else ".o"))
    _compile_c(kind, cc, CORE_C, obj, defines=[f"PA_MODE_PROD={1 if mode == 'prod' else 0}"])
    return obj


def c_type(t) -> str:
    from .types import F32, F64, I32, I64, U8, U32, U64, BOOL, VOID, ViewT, Span, resolve
    t = resolve(t)
    table = {I32: "int32_t", I64: "int64_t", U8: "uint8_t", U32: "uint32_t", U64: "uint64_t",
             F32: "float", F64: "double", BOOL: "_Bool", VOID: "void"}
    if t in table:
        return table[t]
    if isinstance(t, (Span, ViewT)):
        return "pa_span"
    return "void *"


def wrapper_source(info) -> str:
    """C wrappers for @export functions: guard (setjmp) so a fault returns to the host instead of exit(70)."""
    out = [
        "#include <setjmp.h>",
        "#include <stdint.h>",
        "#ifdef _WIN32",
        "#define PA_EXPORT __declspec(dllexport)",
        "#else",
        '#define PA_EXPORT __attribute__((visibility("default")))',
        "#endif",
        "typedef struct { void *p; int64_t n; } pa_span;",
        "extern void *pa_guard_enter(void *); extern void pa_guard_leave(void *);",
    ]
    for name, fi in info.funcs.items():
        if not fi.exported:
            continue
        ret = c_type(fi.ret)
        params = [(c_type(t), f"a{i}") for i, t in enumerate(fi.params.values())]
        decl = ", ".join(f"{t} {n}" for t, n in params) or "void"
        abi_ret = "void" if ret == "pa_span" else ret
        abi_params = (["pa_span *"] if ret == "pa_span" else []) + [
            "const pa_span *" if t == "pa_span" else t for t, _ in params
        ]
        abi_decl = ", ".join(abi_params) or "void"
        call_args = (["&_out"] if ret == "pa_span" else []) + [
            f"&{n}" if t == "pa_span" else n for t, n in params
        ]
        call = ", ".join(call_args)
        out.append(f"{abi_ret} pa_abi_{name}({abi_decl});")
        body = [f"PA_EXPORT {ret} {name}({decl}) {{", "    jmp_buf jb;", "    void *prev = pa_guard_enter(&jb);"]
        if ret == "void":
            body += [
                "    if (setjmp(jb)) { pa_guard_leave(prev); return; }",
                f"    pa_abi_{name}({call});",
                "    pa_guard_leave(prev);",
            ]
        elif ret == "pa_span":
            body += [
                "    if (setjmp(jb)) { pa_guard_leave(prev); pa_span z = {0, 0}; return z; }",
                "    pa_span _out;",
                f"    pa_abi_{name}({call});",
                "    pa_guard_leave(prev);",
                "    return _out;",
            ]
        else:
            body += [
                "    if (setjmp(jb)) { pa_guard_leave(prev); return 0; }",
                f"    {ret} r = pa_abi_{name}({call});",
                "    pa_guard_leave(prev);",
                "    return r;",
            ]
        out += body + ["}"]
    return "\n".join(out) + "\n"


def build_wrappers(tmp: str, info, kind: str = "", cc: str = "") -> str:
    if not cc:
        kind, cc = find_c_compiler()
    ext = ".obj" if sys.platform == "win32" else ".o"
    src, obj = os.path.join(tmp, "pa_wrappers.c"), os.path.join(tmp, f"pa_wrappers{ext}")
    with open(src, "w", encoding="utf-8") as f:
        f.write(wrapper_source(info))
    _compile_c(kind, cc, src, obj)
    return obj


def c_string_literal(data: bytes) -> str:
    """Escape bytes as adjacent C string literals (octal escapes; no trigraph / hex-continuation pitfalls)."""
    chunks, cur = [], []
    for b in data:
        cur.append(chr(b) if 32 <= b < 127 and b not in (34, 92, 63) else f"\\{b:03o}")
        if len(cur) >= 60:
            chunks.append("".join(cur))
            cur = []
    chunks.append("".join(cur))
    return "\n".join(f'"{c}"' for c in chunks)


def embed_source(info, pyhome: str = "", pypath: str = "") -> str:
    """C table of imported .paw sources and baked CPython fallback paths (no command-line quoting issues)."""
    rows, seen = [], set()
    for alias, pm in info.paw.items():
        if pm.name in seen:
            continue
        seen.add(pm.name)
        with open(pm.path, "rb") as f:
            rows.append(f'    {{ "{pm.name}",\n{c_string_literal(f.read())} }},')
    return (
        f"const char pa_baked_pyhome[] =\n{c_string_literal(pyhome.encode('utf-8'))};\n"
        f"const char pa_baked_pypath[] =\n{c_string_literal(pypath.encode('utf-8'))};\n"
        "typedef struct { const char *name; const char *src; } PaEmbed;\n"
        "const PaEmbed pa_embedded_paw[] = {\n" + "\n".join(rows) + "\n    { 0, 0 }\n};\n"
    )


def default_python_paths() -> list:
    paths = sysconfig.get_paths()
    return [p for p in dict.fromkeys([paths.get("purelib"), paths.get("platlib")]) if p]


def build_embed(tmp: str, info, python_home: Optional[str] = None,
                python_path: Optional[list] = None, relocatable: bool = False,
                kind: str = "", cc: str = "") -> str:
    if not cc:
        kind, cc = find_c_compiler()
    home = "" if relocatable else (python_home or sys.base_prefix)
    pypath = "" if relocatable else os.pathsep.join(python_path if python_path is not None else default_python_paths())
    ext = ".obj" if sys.platform == "win32" else ".o"
    src, obj = os.path.join(tmp, "pa_paw_embed.c"), os.path.join(tmp, f"pa_paw_embed{ext}")
    with open(src, "w", encoding="utf-8") as f:
        f.write(embed_source(info, home, pypath))
    _compile_c(kind, cc, src, obj)
    return obj


def build_bridge(tmp: str, mode: str = "dev", include: Optional[str] = None,
                 kind: str = "", cc: str = "") -> str:
    if not cc:
        kind, cc = find_c_compiler()
    inc = include or python_link_info()["include"]
    ext = ".obj" if sys.platform == "win32" else ".o"
    obj = os.path.join(tmp, f"runtime_bridge{ext}")
    _compile_c(
        kind, cc, BRIDGE_C, obj,
        includes=[inc],
        defines=[
            f"PA_MODE_PROD={1 if mode == 'prod' else 0}",
            f"PA_DEEP_CHECKS={1 if mode == 'dev' else 0}",
            f"PA_SNAP_LIMIT={1024 if mode == 'dev' else 16}",
        ],
    )
    return obj
