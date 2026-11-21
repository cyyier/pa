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


def compile_pa(path: str, out: Optional[str] = None, shared: bool = False, emit_ir: bool = False) -> str:
    """Compile a .pa file. Returns the output path (or the IR text when emit_ir)."""
    with open(path, encoding="utf-8") as f:
        source = f.read()
    dirs = default_search_dirs(path)
    diags = check_source(source, path, search_dirs=dirs)
    errors = [d for d in diags if d.severity == "error"]
    if errors:
        raise BuildError("\n".join(d.brief() for d in errors))
    tree = ast.parse(source, path)
    info = analyze(tree, DiagnosticSink(source, path), source, path, dirs)
    try:
        ir_text, paws = generate(tree, info, os.path.basename(path), shared)
    except CodegenError as e:
        raise BuildError(f"{path}:{e.line}: error PA_E100: {e}")
    if emit_ir:
        return ir_text
    out = out or os.path.splitext(path)[0] + (".so" if shared else "")
    with tempfile.TemporaryDirectory() as tmp:
        obj = os.path.join(tmp, "pa_prog.o")
        with open(obj, "wb") as f:
            f.write(ir_to_object(ir_text))
        objs, libs = [obj], ["-lm"]
        if paws:
            objs.append(build_bridge(tmp, [os.path.dirname(os.path.abspath(p.path)) for p in info.paw.values()] + [os.path.abspath(d) for d in dirs]))
            inc, libdir, lib = python_flags()
            libs += [f"-L{libdir}", f"-l{lib}", f"-Wl,-rpath,{libdir}", "-lpthread", "-ldl", "-lutil"]
        cmd = ["gcc", "-O2"] + (["-shared", "-fPIC"] if shared else []) + objs + ["-o", out] + libs
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode:
            raise BuildError("link failed:\n" + r.stderr)
    return out


def build_bridge(tmp: str, pawdirs: list) -> str:
    inc, _, _ = python_flags()
    pypath = ":".join(p for p in sys.path if p and os.path.isdir(p))
    obj = os.path.join(tmp, "runtime_bridge.o")
    cmd = ["gcc", "-O2", "-fPIC", "-c", BRIDGE_C, "-o", obj, f"-I{inc}",
           f'-DPA_PYHOME="{sys.base_prefix}"', f'-DPA_PYPATH="{pypath}"',
           f'-DPA_PAWDIRS="{":".join(dict.fromkeys(pawdirs))}"']
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode:
        raise BuildError("bridge compile failed:\n" + r.stderr)
    return obj
