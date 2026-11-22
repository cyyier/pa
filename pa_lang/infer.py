"""Zero-annotation bidirectional type inference + stack-bound escape analysis."""
from __future__ import annotations

import ast
from dataclasses import dataclass, field
from typing import Optional

from .diagnostics import DiagnosticSink, struct_surgery
from .paw import PawModule, find_paw, load_paw
from .types import (
    BOOL, COPY_WARN_LIMIT, F32, F64, I32, I64, STACK_LIMIT, STR_VIEW, UNKNOWN, VOID,
    BUILTINS_OK, INTRINSICS, MATH_UNARY, MATH_BINARY, DynList, FixedList, Ptr, Span, StackVec, StructT, Type, Vec,
    is_aggregate, is_numeric, parse_annotation, promote, resolve, SCALARS, StrOwned, is_str, U8, U32, U64, display, simple_display, Scalar,
)

ROOT = "<root>"


@dataclass
class LocalInfo:
    name: str
    type: Type
    line: int
    storage: str = "scalar"      # scalar | stack | heap | param | borrowed
    escapes: bool = False
    note: str = ""


@dataclass
class FuncInfo:
    name: str
    node: ast.FunctionDef
    exported: bool = False
    params: dict = field(default_factory=dict)       # name -> Type
    ret: Type = UNKNOWN
    locals: dict = field(default_factory=dict)       # name -> LocalInfo
    raii_free_lines: list = field(default_factory=list)
    copy_lines: dict = field(default_factory=dict)   # line -> bytes
    hints: list = field(default_factory=list)        # (line, name, text) -- detailed flavour
    hints_simple: list = field(default_factory=list) # (line, name, text) -- default, lowest mental load


@dataclass
class ModuleInfo:
    structs: dict = field(default_factory=dict)
    consts: dict = field(default_factory=dict)
    funcs: dict = field(default_factory=dict)
    callsites: dict = field(default_factory=dict)    # func -> {param_name: Type}
    paw: dict = field(default_factory=dict)          # import alias -> PawModule
    expr_types: dict = field(default_factory=dict)   # id(ast node) -> Type (final pass)
    probing: set = field(default_factory=set)
    fconsts: dict = field(default_factory=dict)      # module-level float/bool/str constants: name -> (value, Type)


def _decorators(node) -> set:
    out = set()
    for d in getattr(node, "decorator_list", []):
        t = d.func if isinstance(d, ast.Call) else d
        if isinstance(t, ast.Name):
            out.add(t.id)
    return out


def const_value(node, consts: dict):
    """Evaluate a compile-time integer expression, else None."""
    if isinstance(node, ast.Constant) and isinstance(node.value, int) and not isinstance(node.value, bool):
        return node.value
    if isinstance(node, ast.Name):
        return consts.get(node.id)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
        v = const_value(node.operand, consts)
        return None if v is None else (-v if isinstance(node.op, ast.USub) else v)
    if isinstance(node, ast.BinOp):
        a, b = const_value(node.left, consts), const_value(node.right, consts)
        if a is None or b is None:
            return None
        ops = {ast.Add: lambda x, y: x + y, ast.Sub: lambda x, y: x - y,
               ast.Mult: lambda x, y: x * y, ast.FloorDiv: lambda x, y: x // y if y else None}
        f = ops.get(type(node.op))
        return f(a, b) if f else None
    return None


def eval_const(node, mod) -> object:
    """Compile-time value (int/float/bool/str) of a module-level constant expression, or None."""
    if isinstance(node, ast.Constant) and isinstance(node.value, (bool, int, float, str)):
        return node.value
    if isinstance(node, ast.Name):
        if node.id in mod.consts:
            return mod.consts[node.id]
        return mod.fconsts[node.id][0] if node.id in mod.fconsts else None
    num = lambda v: isinstance(v, (int, float)) and not isinstance(v, bool)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
        v = eval_const(node.operand, mod)
        return (-v if isinstance(node.op, ast.USub) else v) if num(v) else None
    if isinstance(node, ast.BinOp):
        a, b = eval_const(node.left, mod), eval_const(node.right, mod)
        if not (num(a) and num(b)):
            return None
        try:
            if isinstance(node.op, ast.Add): return a + b
            if isinstance(node.op, ast.Sub): return a - b
            if isinstance(node.op, ast.Mult): return a * b
            if isinstance(node.op, ast.Div): return a / b
            if isinstance(node.op, ast.FloorDiv): return a // b
            if isinstance(node.op, ast.Mod): return a % b
            return None
        except ZeroDivisionError:
            return None
    return None


def literal_int_type(v: int) -> Type:
    """Default integer is i64 (64-bit registers: same speed, no silent i32 overflow)."""
    return I64


def widen(old: Type, new: Type) -> Type:
    if old == new or isinstance(old, DynList) or new == UNKNOWN:
        return old
    if is_str(old) and is_str(new):
        return old if isinstance(old, StrOwned) else new
    if old == UNKNOWN:
        return new
    if isinstance(old, (Span, Vec, Ptr)) and type(old) is type(new) and old.elem == UNKNOWN:
        return new
    if is_numeric(old) and is_numeric(new):
        return promote(old, new)
    return old


def paw_arg_compatible(pt: Type, at: Type) -> bool:
    if at == UNKNOWN:
        return True
    if isinstance(pt, FixedList):
        return isinstance(at, FixedList) and at.elem == pt.elem and at.n == pt.n
    if isinstance(pt, Vec):
        return isinstance(at, (FixedList, Vec, StackVec, Span)) and at.elem == pt.elem
    if isinstance(pt, Span):
        return isinstance(at, (FixedList, Vec, StackVec, Span)) and at.elem == pt.elem
    if pt == STR_VIEW:
        return is_str(at)
    if pt == BOOL:
        return at == BOOL
    if isinstance(pt, Scalar) and pt.kind == "int":            # int params never accept floats (silent truncation)
        return isinstance(at, Scalar) and at.kind in ("int", "bool")
    return is_numeric(at) or at == BOOL


def camel_hint(_name: str) -> str:
    return "Record"


class FuncInferer:
    def __init__(self, mod: ModuleInfo, fn: ast.FunctionDef, info: FuncInfo, sink: DiagnosticSink):
        self.mod, self.fn, self.info, self.sink = mod, fn, info, sink
        self.env: dict = dict(info.params)
        self.dyn: dict = {}               # name -> DynList
        self.returns: list = []           # (node, Type)
        self.span_locals: dict = {}       # var -> base name (local)
        self.allocs: dict = {}            # var -> line
        self.mut_forwarded: dict = {}     # list var -> callee it was only mut()-forwarded to

    # ---- driver ------------------------------------------------------
    def run(self) -> None:
        for n, t in self.info.params.items():
            self.info.locals[n] = LocalInfo(n, t, self.fn.lineno, "param" if is_aggregate(t) else "scalar")
        self.block(self.fn.body)
        self.finalize_lists()
        self.check_allocs()
        self.check_span_escape()
        rts = [resolve(t) for _, t in self.returns if t != VOID]
        self.info.ret = rts[0] if rts else VOID
        for local in self.info.locals.values():
            local.type = resolve(local.type)
            self.env[local.name] = local.type

    # ---- statements --------------------------------------------------
    def block(self, stmts) -> None:
        for s in stmts:
            self.stmt(s)

    def stmt(self, s) -> None:
        if isinstance(s, ast.Assign):
            self.assign(s)
        elif isinstance(s, ast.AugAssign):
            self.aug_assign(s)
        elif isinstance(s, ast.Expr):
            self.expr(s.value)
        elif isinstance(s, ast.Return):
            t = self.expr(s.value) if s.value else VOID
            self.returns.append((s, t))
        elif isinstance(s, ast.If):
            self.expr(s.test)
            self.block(s.body)
            self.block(s.orelse)
        elif isinstance(s, ast.While):
            self.expr(s.test)
            self.block(s.body)
        elif isinstance(s, ast.For):
            self.for_loop(s)

    def for_loop(self, s: ast.For) -> None:
        it = s.iter
        if isinstance(it, ast.Call) and isinstance(it.func, ast.Name) and it.func.id == "range":
            ts = [self.expr(a) for a in it.args]
            vt = I64
        else:
            lt = resolve(self.expr(it))
            vt = U8 if lt == STR_VIEW else getattr(lt, "elem", UNKNOWN)
        if isinstance(s.target, ast.Name):
            self.bind(s.target.id, vt, s.lineno)
        self.block(s.body)

    @staticmethod
    def assign_compatible(old: Type, new: Type) -> bool:
        old, new = resolve(old), resolve(new)
        if UNKNOWN in (old, new) or old == new:
            return True
        if is_numeric(old) and is_numeric(new) or (old == BOOL and is_numeric(new)) or (new == BOOL and is_numeric(old)):
            return True
        if is_str(old) and is_str(new):
            return True
        if isinstance(old, (Vec, StackVec)) and isinstance(new, (Vec, StackVec)):
            return old.elem == new.elem
        return False

    def bind(self, name: str, t: Type, line: int, storage: Optional[str] = None) -> None:
        old = self.env.get(name)
        if old is not None and not isinstance(t, DynList) and not isinstance(old, DynList) \
                and not self.assign_compatible(old, t):
            self.sink.report("PA_E086", None, f"'{name}' was {display(resolve(old))} and is now assigned {display(resolve(t))}",
                             line=line, inferred_context={"variable": name, "was": display(resolve(old)), "now": display(resolve(t))},
                             causal_chain=[f"'{name}' first has type {display(resolve(old))}",
                                           f"line {line} rebinds it to {display(resolve(t))}",
                                           "a variable has exactly one storage layout"],
                             patches=[{"line": line, "action": "rename", "replacement": f"{name}2 = ..."}])
        if name in self.env:
            t = widen(self.env[name], t) if not isinstance(t, DynList) else t
        self.env[name] = t
        if name not in self.info.locals or isinstance(t, DynList):
            st = storage or ("scalar" if not is_aggregate(t) and not isinstance(t, DynList) else "stack")
            self.info.locals[name] = LocalInfo(name, t, line, st)
        else:
            self.info.locals[name].type = t
            if isinstance(t, StrOwned):
                self.info.locals[name].storage = "heap"

    def assign(self, s: ast.Assign) -> None:
        target = s.targets[0]
        val = s.value
        if isinstance(target, ast.Name):
            self.assign_name(target.id, val, s)
        else:
            self.expr(val)
            self.expr(target)

    def assign_name(self, name: str, val, s) -> None:
        if isinstance(val, ast.List) and not val.elts:
            dl = DynList(line=s.lineno)
            self.dyn[name] = dl
            self.bind(name, dl, s.lineno, "stack")
            return
        if isinstance(val, ast.Call) and isinstance(val.func, ast.Name):
            fname = val.func.id
            if fname == "alloc":
                self.allocs[name] = s.lineno
            elif fname == "span" and val.args and isinstance(val.args[0], ast.Name):
                self.span_locals[name] = val.args[0].id
        t = self.expr(val)
        if isinstance(val, ast.Name):
            t = self.value_copy(name, val, t, s)
        if isinstance(t, Ptr) and isinstance(val, ast.Name):
            self.sink.report("PA_E070", s, f"'{name} = {val.id}' aliases a ptr (double-free risk)",
                             inferred_context={"type": resolve(t).fmt()},
                             causal_chain=[f"{val.id} is {t.fmt()}", f"bare assignment duplicates ownership of {name}"],
                             patches=[{"line": s.lineno, "replacement": f"copy({name}, {val.id}, count)"}])
        self.bind(name, t, s.lineno, "heap" if isinstance(resolve(t), Vec) and not isinstance(t, DynList) else None)
        if isinstance(val, ast.Call) and isinstance(val.func, ast.Name) and val.func.id in self.mod.structs:
            self.info.locals[name].storage = "stack"

    def value_copy(self, name: str, val: ast.Name, t: Type, s) -> Type:
        rt = resolve(t)
        if isinstance(t, DynList):
            return Vec(t.elem or UNKNOWN)
        if not is_aggregate(rt):
            return t
        size = rt.size if not isinstance(rt, Vec) else 0
        self.info.copy_lines[s.lineno] = size
        if size > COPY_WARN_LIMIT:
            self.sink.report("PA_W001", s, f"value copy '{name} = {val.id}' memcpy's {size} bytes on the stack",
                             inferred_context={"type": rt.fmt(), "bytes": size},
                             causal_chain=[f"{val.id}: {rt.fmt()} = {size}B", "assignment has value semantics (llvm.memcpy)"])
        return t

    def aug_assign(self, s: ast.AugAssign) -> None:
        rt = self.expr(s.value)
        if isinstance(s.target, ast.Name) and isinstance(s.op, ast.Add) and is_str(self.env.get(s.target.id, UNKNOWN)) \
                and is_str(resolve(rt)):
            self.env[s.target.id] = StrOwned()
            self.info.locals[s.target.id].type = StrOwned()
            self.info.locals[s.target.id].storage = "heap"
            return
        if isinstance(s.target, ast.Name) and s.target.id in self.env:
            self.env[s.target.id] = widen(self.env[s.target.id], rt)
            self.info.locals[s.target.id].type = self.env[s.target.id]
        else:
            self.expr(s.target)

    # ---- expressions -------------------------------------------------
    def expr(self, e) -> Type:
        if e is None:
            return VOID
        m = getattr(self, "e_" + type(e).__name__, None)
        t = m(e) if m else UNKNOWN
        self.mod.expr_types[id(e)] = t
        return t

    def e_Constant(self, e) -> Type:
        v = e.value
        if isinstance(v, bool):
            return BOOL
        if isinstance(v, int):
            return literal_int_type(v)
        if isinstance(v, float):
            return F64
        if isinstance(v, str):
            return STR_VIEW
        return UNKNOWN

    def e_Name(self, e) -> Type:
        if e.id in self.env:
            return self.env[e.id]
        if e.id in self.mod.consts:
            return literal_int_type(self.mod.consts[e.id])
        if e.id in self.mod.fconsts:
            return self.mod.fconsts[e.id][1]
        return UNKNOWN

    def e_UnaryOp(self, e) -> Type:
        t = self.expr(e.operand)
        return BOOL if isinstance(e.op, ast.Not) else (I32 if t == BOOL else t)

    def e_BoolOp(self, e) -> Type:
        for v in e.values:
            self.expr(v)
        return BOOL

    def e_Compare(self, e) -> Type:
        self.expr(e.left)
        for c in e.comparators:
            self.expr(c)
        return BOOL

    def e_IfExp(self, e) -> Type:
        self.expr(e.test)
        a, b = self.expr(e.body), self.expr(e.orelse)
        return widen(a, b)

    def e_BinOp(self, e) -> Type:
        if isinstance(e.op, ast.Mult) and isinstance(e.left, ast.List):
            return self.repeat_list(e)
        a, b = self.expr(e.left), self.expr(e.right)
        if isinstance(e.op, ast.Add) and is_str(resolve(a)) and is_str(resolve(b)):
            return StrOwned()
        if isinstance(e.op, ast.Add) and (isinstance(resolve(a), (FixedList, StackVec, Vec))
                                          or isinstance(resolve(b), (FixedList, StackVec, Vec))
                                          or isinstance(e.left, ast.List) or isinstance(e.right, ast.List)):
            self.sink.report("PA_E071", e, "list concatenation 'a + b' creates an implicit heap list",
                             inferred_context={"lhs": resolve(a).fmt(), "rhs": resolve(b).fmt()},
                             causal_chain=["operands are lists", "'+' on lists allocates a new heap list"])
            return UNKNOWN
        if isinstance(e.op, ast.Div):
            return F64 if not (F32 in (a, b) and F64 not in (a, b)) else F32
        return promote(a, b)

    def repeat_list(self, e: ast.BinOp) -> Type:
        elem = self.expr(e.left)
        self.expr(e.right)
        el = elem.elem if isinstance(elem, FixedList) else UNKNOWN
        if el == UNKNOWN:
            return UNKNOWN
        n = const_value(e.right, self.mod.consts)
        if n is None or n <= 0:
            return Vec(el)
        return self.fixed_or_vec(FixedList(el, n * len(e.left.elts)))

    def fixed_or_vec(self, t: FixedList) -> Type:
        return Vec(t.elem) if t.size > STACK_LIMIT else t

    def e_List(self, e: ast.List) -> Type:
        if not e.elts:
            return DynList(line=e.lineno)
        if any(isinstance(x, ast.List) for x in e.elts):
            return self.nested_list(e)
        ts = [self.expr(x) for x in e.elts]
        elem = self.unify(ts)
        if elem is None:
            return self.hetero(e, [list(e.elts)] if True else [], ts)
        return self.fixed_or_vec(FixedList(elem, len(ts)))

    def nested_list(self, e: ast.List) -> Type:
        rows = [x for x in e.elts if isinstance(x, ast.List)]
        if len(rows) != len(e.elts):
            return self.hetero(e, None, [self.expr(x) for x in e.elts])
        row_ts = [self.expr(r) for r in rows]
        if any(t == UNKNOWN for t in row_ts):
            if any(self.row_is_hetero(r) for r in rows):
                return self.hetero(e, [list(r.elts) for r in rows], row_ts)
            return UNKNOWN
        first = row_ts[0]
        if any(t != first for t in row_ts):
            return self.hetero(e, [list(r.elts) for r in rows], row_ts)
        return self.fixed_or_vec(FixedList(first, len(rows))) if isinstance(first, FixedList) or True else UNKNOWN

    def row_is_hetero(self, r: ast.List) -> bool:
        return self.unify([self.expr(x) for x in r.elts]) is None

    def unify(self, ts: list) -> Optional[Type]:
        first = ts[0]
        out = first
        for t in ts[1:]:
            if t == out:
                continue
            if is_numeric(t) and is_numeric(out) and t.kind == out.kind == "int":
                out = promote(out, t)
                continue
            return None
        return out

    def hetero(self, e: ast.List, rows, ts: list) -> Type:
        if rows is None:
            rows = []
        patches = []
        surgery = struct_surgery(rows) if rows else None
        ctx = {"element_types": [resolve(t).fmt() for t in ts]}
        if surgery:
            ctx["target_layout"] = surgery["target_layout"]
            patches = [
                {"action": "insert_before_function", "line": self.fn.lineno,
                 "replacement": surgery["struct_definition"]},
                {"action": "replace_expression", "line": e.lineno,
                 "original": ast.unparse(e), "replacement": surgery["literal"]},
            ]
        self.sink.report("PA_E030", e, "heterogeneous list cannot be laid out contiguously",
                         inferred_context=ctx,
                         causal_chain=[f"literal elements/rows have types {ctx['element_types']}",
                                       "no single T satisfies [N x T] / Vec<T>",
                                       "PyObject* pointer-chasing would be required"],
                         patches=patches)
        return UNKNOWN

    def e_Subscript(self, e) -> Type:
        bt = resolve(self.expr(e.value))
        if isinstance(e.slice, ast.Slice):
            is_s = is_str(bt)
            self.sink.report("PA_E071", e, "string slicing 'S[a:b]' is not supported (it would allocate a new string)" if is_s
                             else "slice 'arr[a:b]' creates an implicit heap copy",
                             inferred_context={"base": bt.fmt()},
                             causal_chain=["Python slicing allocates a new list"],
                             patches=[{"line": e.lineno, "replacement":
                                       "index s[i] (u8) / for ch in s, or copy bytes with an explicit loop" if is_s
                                       else "span(arr, start, len)"}])
            return UNKNOWN
        if isinstance(e.slice, ast.Tuple):          # m[i, j] == m[i][j]
            t = bt
            for ix in e.slice.elts:
                self.expr(ix)
                t = resolve(getattr(t, "elem", UNKNOWN))
            return t
        self.expr(e.slice)
        return U8 if bt == STR_VIEW else getattr(bt, "elem", UNKNOWN)

    def e_Attribute(self, e) -> Type:
        bt = resolve(self.expr(e.value))
        if isinstance(bt, StructT):
            return bt.field_type(e.attr) or UNKNOWN
        return UNKNOWN

    def e_Call(self, e: ast.Call) -> Type:
        if isinstance(e.func, ast.Attribute):
            if isinstance(e.func.value, ast.Name) and e.func.value.id in self.mod.paw:
                return self.paw_call(e)
            return self.method_call(e)
        if isinstance(e.func, ast.Name) and e.func.id in self.mod.funcs:
            self.infer_mut_elems(e)
        arg_ts = [self.expr(a) for a in e.args]
        kw_ts = {k.arg: self.expr(k.value) for k in e.keywords}
        if not isinstance(e.func, ast.Name):
            return UNKNOWN
        n = e.func.id
        if n in self.mod.structs:
            return self.mod.structs[n]
        if n in self.mod.funcs:
            return self.local_call(n, arg_ts, kw_ts)
        return self.builtin_call(n, e, arg_ts)

    def infer_mut_elems(self, e: ast.Call) -> None:
        """`x = []` passed only as mut(x) to a callee that appends: take the element type from the callee."""
        callee = self.mod.funcs[e.func.id]
        params = [a.arg for a in callee.node.args.args]
        for p, a in list(zip(params, e.args)) + [(k.arg, k.value) for k in e.keywords]:
            if not (isinstance(a, ast.Call) and isinstance(a.func, ast.Name) and a.func.id == "mut"
                    and a.args and isinstance(a.args[0], ast.Name)):
                continue
            dl = self.dyn.get(a.args[0].id)
            if dl is not None and dl.elem is None:
                el = self.probe_append(callee, p)
                if el is not None and el != UNKNOWN:
                    dl.elem = el
                else:
                    self.mut_forwarded.setdefault(a.args[0].id, callee.name)

    def probe_append(self, callee: FuncInfo, pname: str) -> Optional[Type]:
        if callee.name in self.mod.probing:
            return None
        self.mod.probing.add(callee.name)
        saved = dict(self.mod.expr_types)
        try:
            tmp = FuncInfo(callee.name, callee.node, callee.exported)
            tmp.params = dict(initial_params(self.mod, callee))
            probe = DynList()
            tmp.params[pname] = probe
            FuncInferer(self.mod, callee.node, tmp, DiagnosticSink()).run()
            return probe.elem
        finally:
            self.mod.expr_types.clear()
            self.mod.expr_types.update(saved)
            self.mod.probing.discard(callee.name)

    def paw_call(self, e: ast.Call) -> Type:
        pm: PawModule = self.mod.paw[e.func.value.id]
        arg_ts = [resolve(self.expr(a)) for a in e.args]
        pf = pm.funcs.get(e.func.attr)
        if pf is None:
            self.sink.report("PA_E090", e, f"'{pm.name}.{e.func.attr}' is not a public typed function of {pm.name}.paw",
                             inferred_context={"available": sorted(pm.funcs)})
            return UNKNOWN
        if len(arg_ts) != len(pf.params) or e.keywords:
            self.sink.report("PA_E090", e, f"'{pm.name}.{pf.name}' expects {len(pf.params)} positional argument(s)",
                             inferred_context={"signature": [(n, t.fmt()) for n, t in pf.params]})
            return pf.ret
        for (pn, pt), at in zip(pf.params, arg_ts):
            if not paw_arg_compatible(pt, at):
                self.sink.report("PA_E090", e, f"argument '{pn}' of '{pm.name}.{pf.name}' expects {pt.fmt()}, got {at.fmt()}",
                                 inferred_context={"param": pn, "expected": pt.fmt(), "actual": at.fmt()})
        return pf.ret

    def local_call(self, n: str, arg_ts: list, kw_ts: dict) -> Type:
        names = list(self.mod.funcs[n].node.args.args)
        sites = self.mod.callsites.setdefault(n, {})
        for a, t in zip(names, arg_ts):
            sites[a.arg] = widen(sites.get(a.arg, UNKNOWN), resolve(t))
        for k, t in kw_ts.items():
            sites[k] = widen(sites.get(k, UNKNOWN), resolve(t))
        return self.mod.funcs[n].ret

    def builtin_call(self, n: str, e: ast.Call, arg_ts: list) -> Type:
        if n == "alloc" and e.args and isinstance(e.args[0], ast.Name):
            return Ptr(SCALARS.get(e.args[0].id) or self.mod.structs.get(e.args[0].id, UNKNOWN))
        if n == "span" and arg_ts:
            return Span(getattr(resolve(arg_ts[0]), "elem", UNKNOWN))
        if n == "mut" and arg_ts:
            return arg_ts[0]
        if n == "len":
            return I64
        if n == "print":
            return VOID
        if n in ("copy", "free"):
            return VOID
        if n in ("int", "i64"):
            return I64
        if n == "i32":
            return I32
        if n == "u8":
            return U8
        if n == "u32":
            return U32
        if n == "u64":
            return U64
        if n in ("float", "f64"):
            return F64
        if n == "f32":
            return F32
        if n == "bool":
            return BOOL
        if n in MATH_UNARY or n in MATH_BINARY:
            return F64
        if n in ("min", "max", "abs") and arg_ts:
            out = arg_ts[0]
            for t in arg_ts[1:]:
                out = promote(out, t)
            return out
        return UNKNOWN

    def method_call(self, e: ast.Call) -> Type:
        recv = e.func.value
        arg_ts = [self.expr(a) for a in e.args]
        if e.func.attr == "append" and isinstance(recv, ast.Name) and arg_ts:
            self.expr(recv)
            self.on_append(recv.id, arg_ts[0], e)
        else:
            self.expr(recv)
        return VOID if e.func.attr == "append" else UNKNOWN

    def on_append(self, name: str, t: Type, node) -> None:
        container = self.env.get(name)
        if isinstance(container, DynList):
            if container.elem is None:
                container.elem = resolve(t) if not isinstance(t, DynList) else t
            elif container.elem != t and not (is_numeric(t) and is_numeric(container.elem)):
                self.sink.report("PA_E030", node, f"append of {resolve(t).fmt()} into list[{container.elem.fmt()}]",
                                 inferred_context={"list_elem": container.elem.fmt(), "appended": resolve(t).fmt()},
                                 causal_chain=[f"'{name}' was inferred as list[{container.elem.fmt()}]",
                                               "appended value has a different type"])
            else:
                container.elem = widen(container.elem, t)

    # ---- escape / bound analysis ------------------------------------
    def finalize_lists(self) -> None:
        for name, dl in self.dyn.items():
            if dl.elem is None:
                via = self.mut_forwarded.get(name)
                summary = f"cannot infer element type of empty list '{name}'"
                if via:
                    summary += (f": it is only passed via mut() to '{via}', which has no direct append() on that "
                                "parameter (multi-level mut() forwarding is not followed)")
                self.sink.report("PA_E011", self.fn, summary,
                                 line=dl.line,
                                 inferred_context={"variable": name},
                                 causal_chain=[f"'{name} = []' at line {dl.line}",
                                               "no append() establishes an element type"],
                                 patches=[{"line": dl.line, "replacement": f"{name} = [0.0] * N  # or append a typed value"}])
                dl.final = Vec(UNKNOWN)
                continue
            bound, escapes = self.list_bound(name)
            elem = resolve(dl.elem)
            if not escapes and bound is not None and bound * elem.size <= STACK_LIMIT:
                dl.final = StackVec(elem, max(bound, 1))
                self.set_storage(name, dl, "stack", False)
            else:
                dl.final = Vec(elem)
                self.set_storage(name, dl, "heap", escapes)
                if not escapes:
                    self.info.raii_free_lines = sorted(
                        set(self.info.raii_free_lines) | {r.lineno for r, _ in self.returns} | {self.fn.end_lineno})

    def set_storage(self, name: str, dl: DynList, storage: str, escapes: bool) -> None:
        loc = self.info.locals[name]
        loc.storage, loc.escapes = storage, escapes
        loc.note = "Zero-Heap" if storage == "stack" else ("owned by caller" if escapes else "RAII")

    def list_bound(self, name: str):
        state = {"count": 0, "unbounded": False, "escape": False}
        self.scan(self.fn.body, name, 1, state)
        return (None if state["unbounded"] else state["count"]), state["escape"]

    def trip(self, it) -> Optional[int]:
        if isinstance(it, ast.Call) and isinstance(it.func, ast.Name) and it.func.id == "range":
            vals = [const_value(a, self.mod.consts) for a in it.args]
            if all(v is not None for v in vals) and vals and len(vals) <= 3:
                return len(range(*vals)) if vals[-1:] != [0] or len(vals) < 3 else None
            return None
        if isinstance(it, ast.Name):
            t = resolve(self.env.get(it.id, UNKNOWN))
            return t.n if isinstance(t, FixedList) else (t.max_n if isinstance(t, StackVec) else None)
        return None

    def scan(self, stmts, name: str, mult: Optional[int], st: dict) -> None:
        for s in stmts:
            self.scan_stmt(s, name, mult, st)

    def scan_stmt(self, s, name: str, mult: Optional[int], st: dict) -> None:
        if isinstance(s, ast.For):
            t = self.trip(s.iter)
            self.scan(s.body, name, None if mult is None or t is None else mult * t, st)
            return
        if isinstance(s, ast.While):
            self.scan(s.body, name, None, st)
            return
        if isinstance(s, ast.If):
            self.scan(s.body, name, mult, st)
            self.scan(s.orelse, name, mult, st)
            return
        if isinstance(s, ast.Expr) and self.is_append(s.value, name):
            if mult is None:
                st["unbounded"] = True
            else:
                st["count"] += mult
            return
        self.scan_uses(s, name, st)

    @staticmethod
    def is_append(v, name: str) -> bool:
        return (isinstance(v, ast.Call) and isinstance(v.func, ast.Attribute) and v.func.attr == "append"
                and isinstance(v.func.value, ast.Name) and v.func.value.id == name)

    def scan_uses(self, s, name: str, st: dict) -> None:
        def mentions(n) -> bool:
            return self.escapes_in(n, name)
        if isinstance(s, ast.Return) and s.value is not None and mentions(s.value):
            st["escape"] = True
        for n in ast.walk(s):
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name):
                if n.func.id == "mut" and any(mentions(a) for a in n.args):
                    st["unbounded"] = True
                if n.func.id in self.mod.structs and any(mentions(a) for a in list(n.args) + [k.value for k in n.keywords]):
                    st["escape"] = True
            if isinstance(n, ast.Assign) and mentions(n.value) and not all(isinstance(t, ast.Name) for t in n.targets):
                st["escape"] = True

    @staticmethod
    def escapes_in(root, name: str) -> bool:
        """True if `name` occurs in root other than as len(name) / name[i] / name.attr reads."""
        def visit(n, safe: bool) -> bool:
            if isinstance(n, ast.Name):
                return n.id == name and not safe
            for child in ast.iter_child_nodes(n):
                child_safe = (isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "len") or \
                    (isinstance(n, ast.Subscript) and child is n.value) or \
                    (isinstance(n, ast.Attribute) and child is n.value)
                if visit(child, child_safe):
                    return True
            return False
        return visit(root, False)

    # ---- alloc / span checks ----------------------------------------
    def check_allocs(self) -> None:
        for var, line in self.allocs.items():
            frees = [n for n in ast.walk(self.fn) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                     and n.func.id == "free" and n.args and isinstance(n.args[0], ast.Name) and n.args[0].id == var]
            returned = any(isinstance(n, ast.Return) and n.value is not None and
                           any(isinstance(x, ast.Name) and x.id == var for x in ast.walk(n.value))
                           for n in ast.walk(self.fn))
            if not frees and not returned:
                self.sink.report("PA_E004", self.fn, f"alloc'd pointer '{var}' is never freed (leak)", line=line,
                                 inferred_context={"variable": var, "type": self.env.get(var, UNKNOWN).fmt()},
                                 causal_chain=[f"'{var}' allocated at line {line}", "no free() on any path"],
                                 patches=[{"line": self.fn.end_lineno, "replacement": f"free({var})"}])
                continue
            if len(frees) > 1:
                self.sink.report("PA_E004", frees[1], f"double free of '{var}'",
                                 causal_chain=[f"free({var}) at lines {[f.lineno for f in frees]}"])
            if frees:
                self.check_free_order(var, line, min(f.lineno for f in frees))

    def check_free_order(self, var: str, alloc_line: int, free_line: int) -> None:
        for n in ast.walk(self.fn):
            if isinstance(n, ast.Return) and alloc_line < n.lineno < free_line and not any(
                    isinstance(x, ast.Name) and x.id == var for x in ast.walk(n)):
                self.sink.report("PA_E004", n, f"return before free({var}) leaks the allocation",
                                 causal_chain=[f"alloc line {alloc_line}", f"early return line {n.lineno}", f"free line {free_line}"])
        for n in ast.walk(self.fn):
            if isinstance(n, ast.Name) and n.id == var and n.lineno > free_line and isinstance(n.ctx, ast.Load):
                self.sink.report("PA_E004", n, f"use after free of '{var}'",
                                 causal_chain=[f"free({var}) at line {free_line}", f"read of '{var}' at line {n.lineno}"])
                return

    def check_span_escape(self) -> None:
        local_names = set(self.info.locals) - set(self.info.params)
        for r, _ in self.returns:
            if r.value is None:
                continue
            for n in ast.walk(r.value):
                base = None
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "span" and n.args \
                        and isinstance(n.args[0], ast.Name):
                    base = n.args[0].id
                elif isinstance(n, ast.Name) and n.id in self.span_locals:
                    base = self.span_locals[n.id]
                if base and base in local_names:
                    self.sink.report("PA_E040", r, f"returning a span over local '{base}' leaves a dangling pointer",
                                     inferred_context={"base": base, "storage": self.info.locals[base].storage},
                                     causal_chain=[f"'{base}' is a local container", "span borrows its memory",
                                                   "function return destroys/frees the local"],
                                     patches=[{"line": r.lineno, "replacement": "return an owned Vec<T> (copy into a result list)"}])
                    return


# ---------------------------------------------------------------------------
def collect_module(tree: ast.Module, sink: DiagnosticSink, search_dirs=None) -> ModuleInfo:
    mod = ModuleInfo()
    for node in tree.body:
        if isinstance(node, ast.Import):
            for a in node.names:
                path = find_paw(a.name, search_dirs or [])
                if path:
                    mod.paw[a.asname or a.name] = load_paw(path)
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            v = eval_const(node.value, mod)
            name = node.targets[0].id
            if isinstance(v, bool):
                mod.fconsts[name] = (v, BOOL)
            elif isinstance(v, int):
                mod.consts[name] = v
            elif isinstance(v, float):
                mod.fconsts[name] = (v, F64)
            elif isinstance(v, str):
                mod.fconsts[name] = (v, STR_VIEW)
        elif isinstance(node, ast.ClassDef) and "struct" in _decorators(node):
            fields = []
            for st in node.body:
                if isinstance(st, ast.AnnAssign) and isinstance(st.target, ast.Name):
                    fields.append((st.target.id, parse_annotation(st.annotation, mod.structs)))
            mod.structs[node.name] = StructT(node.name, tuple(fields))
    for node in tree.body:
        if isinstance(node, ast.FunctionDef):
            info = FuncInfo(node.name, node, exported="export" in _decorators(node))
            mod.funcs[node.name] = info
    return mod


def initial_params(mod: ModuleInfo, info: FuncInfo) -> dict:
    out = {}
    sites = mod.callsites.get(info.name, {})
    for a in info.node.args.args:
        if info.exported or a.annotation is not None:
            out[a.arg] = parse_annotation(a.annotation, mod.structs)
        else:
            out[a.arg] = sites.get(a.arg, UNKNOWN)
    return out


def analyze(tree: ast.Module, sink: Optional[DiagnosticSink] = None, source: str = "",
            filename: str = "<mem>", search_dirs=None) -> ModuleInfo:
    """Infer every function. Call-site types propagate to a fixed point; one final pass reports."""
    sink = sink or DiagnosticSink(source, filename)
    mod = collect_module(tree, sink, search_dirs)
    for fi in mod.funcs.values():
        if fi.node.returns is not None:
            fi.ret = parse_annotation(fi.node.returns, mod.structs)
    def run_pass(cur: DiagnosticSink) -> None:
        for fi in mod.funcs.values():
            fi.params = initial_params(mod, fi)
            fi.locals, fi.raii_free_lines, fi.copy_lines = {}, [], {}
            declared = parse_annotation(fi.node.returns, mod.structs) if fi.node.returns is not None else None
            FuncInferer(mod, fi.node, fi, cur).run()
            if declared is not None:
                check_declared_return(fi, declared, cur)
                fi.ret = declared

    for _ in range(16):
        prev = ({k: dict(v) for k, v in mod.callsites.items()}, {k: f.ret for k, f in mod.funcs.items()})
        run_pass(DiagnosticSink(source, filename))
        nxt = ({k: dict(v) for k, v in mod.callsites.items()}, {k: f.ret for k, f in mod.funcs.items()})
        if nxt == prev:
            break
    run_pass(sink)
    check_aliases(mod, sink)
    check_temp_strings(mod, sink)
    for fi in mod.funcs.values():
        build_hints(fi)
    return mod


def annotation_str(t: Type) -> str:
    """Pa annotation spelling of a type (for repair patches)."""
    t = resolve(t)
    if isinstance(t, FixedList):
        return f"list[{annotation_str(t.elem)}, {t.n}]"
    if isinstance(t, StrOwned):
        return "String"
    if isinstance(t, (Vec, StackVec)):
        return f"Vec[{annotation_str(t.elem)}]"
    return display(t)


def compatible_return(declared: Type, inferred: Type) -> bool:
    declared, inferred = resolve(declared), resolve(inferred)
    if declared == inferred or inferred == UNKNOWN:
        return True
    if is_numeric(declared) and is_numeric(inferred):
        return True                                   # numeric results are coerced to the declared type
    if isinstance(declared, (Vec, StackVec)) and isinstance(inferred, (Vec, StackVec)):
        return declared.elem == inferred.elem
    return False


def check_declared_return(fi: FuncInfo, declared: Type, sink: DiagnosticSink) -> None:
    node = fi.node
    if declared == UNKNOWN:
        sink.report("PA_E085", node.returns, f"unknown return type annotation '{ast.unparse(node.returns)}' on '{fi.name}'",
                    inferred_context={"function": fi.name, "inferred": annotation_str(fi.ret)},
                    patches=[{"line": node.lineno, "replacement": f"def {fi.name}(...) -> {annotation_str(fi.ret)}:"}])
        return
    if compatible_return(declared, fi.ret):
        return
    sink.report("PA_E085", node.returns, f"'{fi.name}' is annotated -> {annotation_str(declared)} but its body returns {annotation_str(fi.ret)}",
                inferred_context={"function": fi.name, "declared": annotation_str(declared), "inferred": annotation_str(fi.ret)},
                causal_chain=[f"annotation fixes the output to {annotation_str(declared)}",
                              f"return statements infer to {annotation_str(fi.ret)}"],
                patches=[{"line": node.lineno, "action": "replace_annotation",
                          "replacement": f"-> {annotation_str(fi.ret)}"}])


def _alias_root(e) -> Optional[str]:
    if isinstance(e, ast.Call) and isinstance(e.func, ast.Name) and e.func.id == "span" and e.args:
        return _alias_root(e.args[0])
    while isinstance(e, (ast.Attribute, ast.Subscript)):
        e = e.value
    return e.id if isinstance(e, ast.Name) else None


def check_aliases(mod: ModuleInfo, sink: DiagnosticSink) -> None:
    """PA_E089: same variable passed twice while one occurrence is mut().

    Scalar subscript/attribute reads (e.g. `halfedges[ar]`) are loaded by value before the call
    and do not borrow a pointer, so they are allowed alongside `mut(halfedges)`.
    """
    for fi in mod.funcs.values():
        for n in ast.walk(fi.node):
            if not (isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in mod.funcs):
                continue
            name = n.func.id
            args = list(n.args) + [k.value for k in n.keywords]
            muts, borrows = [], []
            for a in args:
                if isinstance(a, ast.Call) and isinstance(a.func, ast.Name) and a.func.id == "mut" \
                        and a.args and isinstance(a.args[0], ast.Name):
                    muts.append(a.args[0].id)
                    borrows.append(a.args[0].id)
                else:
                    r = _alias_root(a)
                    if r is not None:
                        t = resolve(mod.expr_types.get(id(a), UNKNOWN))
                        if is_aggregate(t) or isinstance(t, Span) or t == UNKNOWN:
                            borrows.append(r)
            for m in dict.fromkeys(muts):
                if borrows.count(m) > 1:
                    sink.report("PA_E089", n, f"'{m}' is passed to '{name}' more than once and one of them is mut()",
                                inferred_context={"variable": m, "callee": name},
                                causal_chain=[f"{name}() receives '{m}' through two parameters",
                                              "one of them may append/realloc",
                                              "the other view would dangle (use-after-free)"],
                                patches=[{"line": n.lineno,
                                          "replacement": f"copy_of_{m} = {m}\n{name}(... mut({m}) ..., copy_of_{m})"}])


def check_temp_strings(mod: ModuleInfo, sink: DiagnosticSink) -> None:
    """PA_E072: a fresh String (concat / call result) may only be assigned, returned, or fused into a concat."""
    for fi in mod.funcs.values():
        parents = {}
        for n in ast.walk(fi.node):
            for ch in ast.iter_child_nodes(n):
                parents[id(ch)] = n
        for n in ast.walk(fi.node):
            if not isinstance(n, (ast.BinOp, ast.Call)) or (isinstance(n, ast.BinOp) and not isinstance(n.op, ast.Add)):
                continue
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "mut":
                continue
            if not isinstance(resolve(mod.expr_types.get(id(n), UNKNOWN)), StrOwned):
                continue
            par = parents.get(id(n))
            fused = isinstance(par, ast.BinOp) and isinstance(par.op, ast.Add) and isinstance(n, ast.BinOp) \
                and isinstance(resolve(mod.expr_types.get(id(par), UNKNOWN)), StrOwned)
            ok = fused or isinstance(par, ast.Return) or (isinstance(par, ast.Assign) and par.value is n
                                                           and all(isinstance(t, ast.Name) for t in par.targets))
            if ok:
                continue
            what = "string concatenation" if isinstance(n, ast.BinOp) else f"call '{ast.unparse(n.func)}' returning a String"
            sink.report("PA_E072", n, f"temporary String from {what} is never freed here",
                        inferred_context={"expression": ast.unparse(n), "type": "String"},
                        causal_chain=["the expression allocates an owned heap String",
                                      "it is used as an operand/argument, not bound to a local",
                                      "RAII only frees locals, so the buffer would leak"],
                        patches=[{"line": n.lineno, "action": "bind_to_local",
                                  "replacement": f"tmp = {ast.unparse(n)}\n# then use `tmp` here"}])


def build_hints(fi: FuncInfo) -> None:
    fi.hints, fi.hints_simple = [], []
    for loc in fi.locals.values():
        if loc.storage == "param":
            continue
        t = loc.type
        if isinstance(t, FixedList):
            text = f": {display(t)} ({t.size}B flat)"
        elif isinstance(t, StackVec):
            text = f": {display(t)} (Zero-Heap)"
        elif isinstance(t, Vec):
            text = f": {display(t)} ({'RAII' if not loc.escapes else 'ownership -> caller'})"
        else:
            text = f": {display(t)}"
        fi.hints.append((loc.line, loc.name, text))
        fi.hints_simple.append((loc.line, loc.name, f": {simple_display(t)}"))
    for line, nbytes in fi.copy_lines.items():
        if nbytes:
            fi.hints.append((line, "", f"[memcpy {nbytes}B]"))
