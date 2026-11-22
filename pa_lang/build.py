"""Compile .pa -> native executable / shared library (llvmlite + gcc + runtime_bridge.c)."""
from __future__ import annotations

import ast
import os
import subprocess
import sys
import sysconfig
import tempfile
from typing import Optional

from llvmlite import binding as llvm

from .codegen import CodegenError, generate
from .infer import analyze
from .validator import check_source, default_search_dirs
from .diagnostics import DiagnosticSink

BRIDGE_C = os.path.join(os.path.dirname(__file__), "runtime_bridge.c")
CORE_C = os.path.join(os.path.dirname(__file__), "runtime_core.c")


class BuildError(Exception):
    pass


def _machine():
    llvm.initialize_native_target()
    llvm.initialize_native_asmprinter()
    return llvm.Target.from_default_triple().create_target_machine(opt=2, reloc="pic", codemodel="default")


def ir_to_object(ir_text: str) -> bytes:
    tm = _machine()
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


def python_flags() -> tuple:
    inc = sysconfig.get_config_var("INCLUDEPY")
    libdir = sysconfig.get_config_var("LIBDIR")
    ver = sysconfig.get_config_var("LDVERSION") or sysconfig.get_config_var("VERSION")
    return inc, libdir, f"python{ver}"


def compile_pa(path: str, out: Optional[str] = None, shared: bool = False, emit_ir: bool = False,
               mode: str = "dev", python_home: Optional[str] = None, python_path: Optional[list] = None,
               relocatable: bool = False) -> str:
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
    dirs = default_search_dirs(path)
    diags = check_source(source, path, search_dirs=dirs)
    errors = [d for d in diags if d.severity == "error"]
    if errors:
        raise BuildError("\n".join(d.brief() for d in errors))
    tree = ast.parse(source, path)
    info = analyze(tree, DiagnosticSink(source, path), source, path, dirs)
    paw_errors = []
    for pm in info.paw.values():
        with open(pm.path, encoding="utf-8") as f:
            paw_errors += [d for d in check_source(f.read(), pm.path, mode=mode) if d.severity == "error"]
    if paw_errors:
        raise BuildError("\n".join(d.brief() for d in paw_errors))
    try:
        ir_text, paws = generate(tree, info, os.path.basename(path), shared, mode)
    except CodegenError as e:
        raise BuildError(f"{path}:{e.line}: error PA_E100: {e}")
    if emit_ir:
        return ir_text
    if sys.platform != "linux" and not os.environ.get("PA_ALLOW_UNSUPPORTED_PLATFORM"):
        raise BuildError(f"pac build supports Linux only for now (platform: {sys.platform}); "
                         "`pac check`, `pac lsp` and `pac build --emit-ir` work everywhere. "
                         "Set PA_ALLOW_UNSUPPORTED_PLATFORM=1 to try anyway.")
    out = out or os.path.splitext(path)[0] + (".so" if shared else "")
    with tempfile.TemporaryDirectory() as tmp:
        obj = os.path.join(tmp, "pa_prog.o")
        with open(obj, "wb") as f:
            f.write(ir_to_object(ir_text))
        objs, libs = [obj, build_core(tmp, mode)], ["-lm"]
        if shared:
            objs.append(build_wrappers(tmp, info))
        if paws:
            objs.append(build_bridge(tmp, mode, python_home, python_path, relocatable))
            objs.append(build_embed(tmp, info))
            inc, libdir, lib = python_flags()
            rpaths = ["-Wl,-rpath,$ORIGIN", "-Wl,-rpath,$ORIGIN/../lib"] + ([] if relocatable else [f"-Wl,-rpath,{libdir}"])
            libs += [f"-L{libdir}", f"-l{lib}", *rpaths, "-lpthread", "-ldl", "-lutil"]
        cmd = ["gcc", "-O2"] + (["-shared", "-fPIC"] if shared else []) + objs + ["-o", out] + libs
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode:
            raise BuildError("link failed:\n" + r.stderr)
    return out


def build_core(tmp: str, mode: str) -> str:
    obj = os.path.join(tmp, "runtime_core.o")
    r = subprocess.run(["gcc", "-O2", "-fPIC", "-c", CORE_C, "-o", obj, f'-DPA_MODE="{mode}"'], capture_output=True, text=True)
    if r.returncode:
        raise BuildError("core runtime compile failed:\n" + r.stderr)
    return obj


def c_type(t) -> str:
    from .types import F32, F64, I32, I64, U8, BOOL, VOID, ViewT, Span, resolve
    t = resolve(t)
    table = {I32: "int32_t", I64: "int64_t", U8: "uint8_t", F32: "float", F64: "double", BOOL: "_Bool", VOID: "void"}
    if t in table:
        return table[t]
    if isinstance(t, (Span, ViewT)):
        return "pa_span"
    return "void *"


def wrapper_source(info) -> str:
    """C wrappers for @export functions: guard (setjmp) so a fault returns to the host instead of exit(70)."""
    out = ["#include <setjmp.h>", "#include <stdint.h>",
           "typedef struct { void *p; int64_t n; } pa_span;",
           "extern void *pa_guard_enter(void *); extern void pa_guard_leave(void *);"]
    for name, fi in info.funcs.items():
        if not fi.exported:
            continue
        ret = c_type(fi.ret)
        params = [(c_type(t), f"a{i}") for i, t in enumerate(fi.params.values())]
        decl = ", ".join(f"{t} {n}" for t, n in params) or "void"
        call = ", ".join(n for _, n in params)
        out.append(f"{ret} pa_impl_{name}({decl});")
        body = [f"{ret} {name}({decl}) {{", "    jmp_buf jb;", "    void *prev = pa_guard_enter(&jb);",
                "    if (setjmp(jb)) { pa_guard_leave(prev); return" + ("" if ret == "void" else " 0") + "; }"]
        if ret == "void":
            body += [f"    pa_impl_{name}({call});", "    pa_guard_leave(prev);"]
        else:
            body += [f"    {ret} r = pa_impl_{name}({call});", "    pa_guard_leave(prev);", "    return r;"]
        out += body + ["}"]
    return "\n".join(out) + "\n"


def build_wrappers(tmp: str, info) -> str:
    src, obj = os.path.join(tmp, "pa_wrappers.c"), os.path.join(tmp, "pa_wrappers.o")
    with open(src, "w") as f:
        f.write(wrapper_source(info))
    r = subprocess.run(["gcc", "-O2", "-fPIC", "-c", src, "-o", obj], capture_output=True, text=True)
    if r.returncode:
        raise BuildError("wrapper compile failed:\n" + r.stderr)
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


def embed_source(info) -> str:
    """C table of the imported .paw sources: they travel inside the binary, so no source path is baked in."""
    rows = []
    for alias, pm in info.paw.items():
        with open(pm.path, "rb") as f:
            rows.append(f'    {{ "{pm.name}",\n{c_string_literal(f.read())} }},')
    return ("typedef struct { const char *name; const char *src; } PaEmbed;\n"
            "const PaEmbed pa_embedded_paw[] = {\n" + "\n".join(rows) + "\n    { 0, 0 }\n};\n")


def build_embed(tmp: str, info) -> str:
    src, obj = os.path.join(tmp, "pa_paw_embed.c"), os.path.join(tmp, "pa_paw_embed.o")
    with open(src, "w") as f:
        f.write(embed_source(info))
    r = subprocess.run(["gcc", "-O2", "-fPIC", "-c", src, "-o", obj], capture_output=True, text=True)
    if r.returncode:
        raise BuildError("paw embed compile failed:\n" + r.stderr)
    return obj


def default_python_paths() -> list:
    paths = sysconfig.get_paths()
    return [p for p in dict.fromkeys([paths.get("purelib"), paths.get("platlib")]) if p]


def build_bridge(tmp: str, mode: str = "dev", python_home: Optional[str] = None,
                 python_path: Optional[list] = None, relocatable: bool = False) -> str:
    inc, _, _ = python_flags()
    home = "" if relocatable else (python_home or sys.base_prefix)
    pypath = "" if relocatable else ":".join(python_path if python_path is not None else default_python_paths())
    obj = os.path.join(tmp, "runtime_bridge.o")
    cmd = ["gcc", "-O2", "-fPIC", "-c", BRIDGE_C, "-o", obj, f"-I{inc}",
           f'-DPA_PYHOME="{home}"', f'-DPA_PYPATH="{pypath}"',
           f'-DPA_MODE="{mode}"', f"-DPA_DEEP_CHECKS={1 if mode == 'dev' else 0}",
           f"-DPA_SNAP_LIMIT={1024 if mode == 'dev' else 16}"]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode:
        raise BuildError("bridge compile failed:\n" + r.stderr)
    return obj
