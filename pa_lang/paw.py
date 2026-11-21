""".paw (Pa Worker / Bridge) modules: standard Python with a locked static In -> Out signature.

The front-end reads a .paw file only as a "header": top-level public function signatures.
"""
from __future__ import annotations

import ast
import os
from dataclasses import dataclass, field
from typing import Optional

from .diagnostics import DiagnosticSink
from .types import (BOOL, F32, F64, I32, I64, STR_VIEW, U8, UNKNOWN, VOID, FixedList, Span, Type, Vec,
                    parse_annotation)

NUMERIC = (I32, I64, U8, F32, F64, BOOL)
ELEM_CODES = {"i32": 0, "i64": 1, "u8": 2, "f32": 3, "f64": 4, "bool": 5}


@dataclass
class PawFunc:
    name: str
    params: list          # [(name, Type)]
    ret: Type
    line: int
    allow_nan: bool = False


@dataclass
class PawModule:
    name: str
    path: str
    funcs: dict = field(default_factory=dict)


def find_paw(name: str, search_dirs: list) -> Optional[str]:
    for d in search_dirs:
        p = os.path.join(d, name + ".paw")
        if os.path.isfile(p):
            return p
    return None


def boundary_param_ok(t: Type) -> bool:
    if t in NUMERIC or t == STR_VIEW:
        return True
    return isinstance(t, (FixedList, Span, Vec)) and t.elem in NUMERIC


def boundary_ret_ok(t: Type) -> bool:
    return t in NUMERIC or t == VOID or (isinstance(t, Vec) and t.elem in NUMERIC)


def parse_paw(source: str, filename: str, sink: Optional[DiagnosticSink] = None) -> PawModule:
    """Extract (and validate) public function signatures from a .paw source."""
    sink = sink or DiagnosticSink(source, filename)
    mod = PawModule(os.path.splitext(os.path.basename(filename))[0], filename)
    try:
        tree = ast.parse(source, filename)
    except SyntaxError as e:
        sink.report("PA_E000", None, f"syntax error: {e.msg}", line=e.lineno or 1)
        return mod
    for fn in tree.body:
        if not isinstance(fn, ast.FunctionDef) or fn.name.startswith("_"):
            continue
        params = fn.args.posonlyargs + fn.args.args
        if fn.args.vararg or fn.args.kwarg or fn.args.kwonlyargs:
            sink.report("PA_E082", fn, f"'{fn.name}' must not use *args/**kwargs/keyword-only params")
            continue
        if any(p.annotation is None for p in params) or fn.returns is None:
            sink.report("PA_E080", fn, f"public function '{fn.name}' lacks a static In -> Out signature",
                        inferred_context={"function": fn.name},
                        patches=[{"line": fn.lineno, "replacement": f"def {fn.name}(a: f64) -> f64:"}])
            continue
        if isinstance(fn.returns, ast.Name) and fn.returns.id in ("str", "str_view", "bytes", "bytes_view"):
            sink.report("PA_E082", fn, f"'{fn.name}' must not return a string; return the characters as Vec[u8]",
                        inferred_context={"function": fn.name, "ret": fn.returns.id},
                        causal_chain=["a returned string would need an owner on the .pa side",
                                      "strings cross the boundary inbound only (str_view)"],
                        patches=[{"line": fn.lineno, "replacement": f"def {fn.name}(...) -> Vec[u8]:"},
                                 {"line": fn.lineno, "action": "encode",
                                  "replacement": "return np.frombuffer(s.encode('utf-8'), dtype=np.uint8)"}])
            continue
        ptypes = [(p.arg, parse_annotation(p.annotation, {})) for p in params]
        ret = parse_annotation(fn.returns, {})
        bad = [n for n, t in ptypes if not boundary_param_ok(t)]
        if bad or not boundary_ret_ok(ret):
            sink.report("PA_E082", fn, f"'{fn.name}' uses a type that cannot cross the Pa boundary",
                        inferred_context={"bad_params": bad, "ret": ret.fmt(),
                                          "allowed": "i32 i64 u8 f32 f64 bool str_view span[T] list[T,N] Vec[T] (T numeric); return: scalar | Vec[T] | void"})
            continue
        deco = {d.id for d in fn.decorator_list if isinstance(d, ast.Name)}
        mod.funcs[fn.name] = PawFunc(fn.name, ptypes, ret, fn.lineno, allow_nan="allow_nan" in deco)
    return mod


def load_paw(path: str) -> PawModule:
    with open(path, encoding="utf-8") as f:
        return parse_paw(f.read(), path)
