""".paw (Pa Worker / Bridge) modules: standard Python with a locked static In -> Out signature.

The front-end reads a .paw file only as a "header": top-level public function signatures.
"""
from __future__ import annotations

import ast
import os
from dataclasses import dataclass, field
from typing import Optional

from .diagnostics import DiagnosticSink
from .types import (BOOL, F32, F64, I32, I64, STR_VIEW, U8, U32, U64, UNKNOWN, VOID, FixedList, Span, Type, Vec,
                    parse_annotation)

NUMERIC = (I32, I64, U8, U32, U64, F32, F64, BOOL)
ELEM_CODES = {"i32": 0, "i64": 1, "u8": 2, "f32": 3, "f64": 4, "bool": 5, "u32": 6, "u64": 7}


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


def parse_paw(source: str, filename: str, sink: Optional[DiagnosticSink] = None, lint: bool = False) -> PawModule:
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
        if lint:
            lint_function(fn, mod.funcs[fn.name], sink)
    return mod


# ---------------------------------------------------------------------------
# dev-mode deep static In -> Out lint of a .paw function body
# ---------------------------------------------------------------------------
def _own_nodes(fn):
    """Walk a function body without descending into nested functions/lambdas/classes."""
    stack = list(fn.body)
    while stack:
        n = stack.pop()
        yield n
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)):
            continue
        stack.extend(ast.iter_child_nodes(n))


def always_returns(stmts) -> bool:
    for s in stmts:
        if isinstance(s, (ast.Return, ast.Raise)):
            return True
        if isinstance(s, ast.If) and s.orelse and always_returns(s.body) and always_returns(s.orelse):
            return True
        if isinstance(s, ast.While) and isinstance(s.test, ast.Constant) and s.test.value is True:
            return True
        if isinstance(s, ast.With) and always_returns(s.body):
            return True
        if isinstance(s, ast.Try) and always_returns(s.body) and all(always_returns(h.body) for h in s.handlers):
            return True
    return False


def _ret_kind(t: Type) -> str:
    if t == VOID: return "void"
    if t in (F32, F64): return "float"
    if t == BOOL: return "bool"
    if isinstance(t, Vec): return "vec"
    return "int"


def _expr_kind(v) -> Optional[str]:
    """Statically evident kind of a returned expression, or None when unknown."""
    if isinstance(v, ast.Constant):
        c = v.value
        return "none" if c is None else "bool" if isinstance(c, bool) else "int" if isinstance(c, int) \
            else "float" if isinstance(c, float) else "str"
    if isinstance(v, (ast.Dict, ast.Set)): return "dict/set"
    if isinstance(v, ast.JoinedStr): return "str"
    if isinstance(v, (ast.List, ast.ListComp, ast.Tuple)): return "vec"
    if isinstance(v, ast.Compare): return "bool"
    if isinstance(v, ast.Call) and isinstance(v.func, ast.Name):
        return {"float": "float", "int": "int", "len": "int", "bool": "bool", "str": "str"}.get(v.func.id)
    return None


def lint_function(fn: ast.FunctionDef, pf: PawFunc, sink: DiagnosticSink) -> None:
    kind = _ret_kind(pf.ret)
    buffers = {n for n, t in pf.params if isinstance(t, (FixedList, Span, Vec))}
    for node in _own_nodes(fn):
        targets = node.targets if isinstance(node, ast.Assign) else [node.target] if isinstance(node, ast.AugAssign) else []
        for t in targets:
            if isinstance(t, ast.Subscript) and isinstance(t.value, ast.Name) and t.value.id in buffers:
                sink.report("PA_E084", node, f"'{fn.name}' writes into read-only input buffer '{t.value.id}'",
                            inferred_context={"param": t.value.id, "type": dict(pf.params)[t.value.id].fmt()},
                            causal_chain=["buffer params are zero-copy read-only memoryviews",
                                          "item assignment raises TypeError at run time"],
                            patches=[{"line": node.lineno, "replacement": f"{t.value.id} = np.array({t.value.id})  # private copy first"}])
        if isinstance(node, ast.Return):
            lint_return(node, fn, kind, pf, sink)
    if kind != "void" and not always_returns(fn.body):
        sink.report("PA_E083", fn, f"'{fn.name}' can fall off the end and return None (declared {pf.ret.fmt()})",
                    inferred_context={"function": fn.name, "declared": pf.ret.fmt()},
                    causal_chain=["not every path ends in return/raise", "Python then returns None",
                                  "the bridge rejects None on entry to .pa"])


def lint_return(node: ast.Return, fn, kind: str, pf: PawFunc, sink: DiagnosticSink) -> None:
    got = "none" if node.value is None else _expr_kind(node.value)
    if got is None:
        return
    if kind == "void":
        bad = got != "none"
    elif kind == "vec":
        bad = got not in ("vec",)
    else:
        bad = got != kind
    if bad:
        sink.report("PA_E083", node, f"'{fn.name}' returns {got} where {pf.ret.fmt()} is declared",
                    inferred_context={"function": fn.name, "declared": pf.ret.fmt(), "returned": got},
                    causal_chain=[f"signature fixes the output to {pf.ret.fmt()}", f"this return yields {got}",
                                  "the bridge rejects it as type drift"],
                    patches=[{"line": node.lineno, "replacement": {
                        "float": "return float(...)", "int": "return int(...)", "bool": "return bool(...)",
                        "vec": "return a list/array of numbers", "void": "return None"}[kind]}])


def load_paw(path: str) -> PawModule:
    with open(path, encoding="utf-8") as f:
        return parse_paw(f.read(), path)
