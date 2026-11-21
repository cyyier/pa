"""llvmlite IR generation for Pa.

Memory model: scalars are SSA values; FixedList / Vec / StackVec / @struct live in memory and are
handled by address. StackVec and Vec share the {T*, len, cap} header (StackVec's pointer targets an
entry-block alloca). .paw calls are lowered to marshaling stubs around runtime_bridge.c.
"""
from __future__ import annotations

import ast
from typing import Optional

from llvmlite import ir

from .infer import FuncInferer, ModuleInfo, const_value, analyze
from .paw import ELEM_CODES, PawFunc
from .types import (BOOL, F32, F64, I32, I64, MATH_BINARY, MATH_UNARY, STR_VIEW, U8, UNKNOWN, VOID, FixedList, Ptr, Span, StackVec,
                    StructT, Type, Vec, resolve, is_numeric, StrOwned, is_str, Scalar)

PTR = ir.PointerType()
I1, I8, I32T, I64T = ir.IntType(1), ir.IntType(8), ir.IntType(32), ir.IntType(64)
F32T, F64T, VOIDT = ir.FloatType(), ir.DoubleType(), ir.VoidType()
VEC = ir.LiteralStructType([PTR, I64T, I64T])
VIEW = ir.LiteralStructType([PTR, I64T])
PAARG = ir.LiteralStructType([I32T, I32T, I64T, F64T, PTR, I64T])
PARET = ir.LiteralStructType([I64T, F64T, PTR, I64T])
TAG_INT, TAG_FLOAT, TAG_BOOL, TAG_STR, TAG_BUF, TAG_VOID = range(6)


class PBuilder(ir.IRBuilder):
    """IRBuilder that reports every pointer as the opaque `ptr` type (llvmlite tracks typed ones)."""

    def gep(self, *a, **k):
        r = super().gep(*a, **k)
        r.type = PTR
        return r

    def alloca(self, *a, **k):
        r = super().alloca(*a, **k)
        r.type = PTR
        return r


class CodegenError(Exception):
    def __init__(self, msg: str, node=None):
        super().__init__(msg)
        self.line = getattr(node, "lineno", 0)


def is_agg(t: Type) -> bool:
    return isinstance(resolve(t), (FixedList, Vec, StackVec, StructT))


def is_int(t: Type) -> bool:
    return t in (I32, I64, U8)


def ci(n: int, ty=I32T) -> ir.Constant:
    return ir.Constant(ty, n)


class ModuleGen:
    def __init__(self, tree: ast.Module, info: ModuleInfo, filename: str = "<mem>", shared: bool = False,
                 mode: str = "dev"):
        self.tree, self.info, self.shared, self.mode = tree, info, shared, mode
        self.m = ir.Module(name=filename)
        self.struct_cache: dict = {}
        self.strings: dict = {}
        self.fns: dict = {}
        self.paw_used: set = set()
        d = lambda name, ret, args, va=False: ir.Function(self.m, ir.FunctionType(ret, args, var_arg=va), name)
        self.malloc = d("malloc", PTR, [I64T])
        self.realloc = d("realloc", PTR, [PTR, I64T])
        self.free = d("free", VOIDT, [PTR])
        self.printf = d("printf", I32T, [PTR], True)
        self.bcmp = d("bcmp", I32T, [PTR, PTR, I64T])
        self.rt_fault = d("pa_rt_fault", VOIDT, [PTR, PTR, I64T, PTR])
        self.rt_index_fault = d("pa_rt_index_fault", VOIDT, [I64T, I64T, I64T, PTR])
        self.p_i64 = d("pa_print_i64", VOIDT, [I64T])
        self.p_f64 = d("pa_print_f64", VOIDT, [F64T])
        self.p_bool = d("pa_print_bool", VOIDT, [I32T])
        self.p_str = d("pa_print_str", VOIDT, [PTR, I64T])
        self.p_sep = d("pa_print_sep", VOIDT, [])
        self.p_nl = d("pa_print_nl", VOIDT, [])
        self.memcpy = d("llvm.memcpy.p0.p0.i64", VOIDT, [PTR, PTR, I64T, I1])
        self.call_paw = d("pa_call_paw", VOIDT, [PTR, PTR, PTR, I32T, I32T, I32T, I32T, PTR])
        self.math_fns = {n: d(n, F64T, [F64T]) for n in MATH_UNARY}
        self.math_fns.update({n: d(n, F64T, [F64T, F64T]) for n in MATH_BINARY})

    # ---- type lowering ----------------------------------------------
    def lt(self, t: Type, node=None) -> ir.Type:
        t = resolve(t)
        if t == I32: return I32T
        if t == I64: return I64T
        if t == U8: return I8
        if t == BOOL: return I1
        if t == F32: return F32T
        if t == F64: return F64T
        if t == VOID: return VOIDT
        if isinstance(t, (Vec, StackVec)): return VEC
        if isinstance(t, FixedList): return ir.ArrayType(self.lt(t.elem, node), t.n)
        if isinstance(t, (Ptr,)): return PTR
        if isinstance(t, Span) or t == STR_VIEW: return VIEW
        if isinstance(t, StructT):
            if t.name not in self.struct_cache:
                self.struct_cache[t.name] = ir.LiteralStructType([self.lt(ft, node) for _, ft in t.fields])
            return self.struct_cache[t.name]
        raise CodegenError(f"cannot lower type '{t.fmt()}' (inference could not determine it)", node)

    def param_lt(self, t: Type, node=None) -> ir.Type:
        return PTR if is_agg(t) else self.lt(t, node)

    def cstr(self, s: str) -> ir.Value:
        if s not in self.strings:
            data = bytearray(s.encode("utf-8") + b"\0")
            g = ir.GlobalVariable(self.m, ir.ArrayType(I8, len(data)), f".str.{len(self.strings)}")
            g.global_constant, g.linkage = True, "private"
            g.initializer = ir.Constant(ir.ArrayType(I8, len(data)), data)
            g.type = PTR
            self.strings[s] = g
        return self.strings[s]

    def symbol(self, name: str, exported: bool) -> str:
        if name == "main":
            return "pa_user_main"
        if exported:
            return f"pa_impl_{name}" if self.shared else name     # shared libs expose guarded C wrappers
        return f"pa_fn_{name}"

    # ---- driver ------------------------------------------------------
    def reachable(self) -> list:
        funcs = self.info.funcs
        roots = [n for n, f in funcs.items() if f.exported or (n == "main" and not self.shared)]
        seen, stack = [], list(roots)
        while stack:
            n = stack.pop()
            if n in seen:
                continue
            seen.append(n)
            for x in ast.walk(funcs[n].node):
                if isinstance(x, ast.Call) and isinstance(x.func, ast.Name) and x.func.id in funcs:
                    stack.append(x.func.id)
        return seen

    def emit(self) -> str:
        names = self.reachable()
        if not self.shared and "main" not in self.info.funcs:
            raise CodegenError("executable build needs a main() function")
        for n in names:
            fi = self.info.funcs[n]
            ft = ir.FunctionType(self.lt_ret(fi), [self.param_lt(t, fi.node) for t in fi.params.values()])
            self.fns[n] = ir.Function(self.m, ft, self.symbol(n, fi.exported))
            if ft.return_type == I1:
                self.fns[n].return_value.add_attribute("zeroext")
        for n in names:
            FnGen(self, self.info.funcs[n], self.fns[n]).emit()
        if not self.shared:
            self.emit_c_main()
        return str(self.m)

    def lt_ret(self, fi) -> ir.Type:
        return VOIDT if fi.ret == VOID or fi.ret == UNKNOWN else self.lt(fi.ret, fi.node)

    def emit_c_main(self) -> None:
        fi = self.info.funcs["main"]
        f = ir.Function(self.m, ir.FunctionType(I32T, [I32T, PTR]), "main")
        b = PBuilder(f.append_basic_block("entry"))
        r = b.call(self.fns["main"], [])
        if fi.ret in (VOID, UNKNOWN):
            b.ret(ci(0))
        elif fi.ret in (I32, I64, U8, BOOL):
            w = r.type.width
            b.ret(r if w == 32 else b.trunc(r, I32T) if w > 32 else b.zext(r, I32T))
        else:
            raise CodegenError("main() must return an integer or nothing", fi.node)


class FnGen:
    def __init__(self, mg: ModuleGen, fi, fn: ir.Function):
        self.mg, self.fi, self.fn, self.info = mg, fi, fn, mg.info
        self.entry_b = PBuilder(fn.append_basic_block("entry"))
        self.b = PBuilder(fn.append_basic_block("body"))
        self.slots: dict = {}          # name -> (ptr, Type)
        self.loops: list = []          # (continue_block, break_block)
        self.returned = self.returned_names()

    # ---- helpers -----------------------------------------------------
    def err(self, msg, node):
        raise CodegenError(msg, node)

    def ty(self, node) -> Type:
        if isinstance(node, ast.Name) and node.id in self.slots:
            return resolve(self.slots[node.id][1])      # final local type wins over per-node snapshot
        t = self.info.expr_types.get(id(node))
        if t is None:
            self.err(f"no inferred type for expression '{ast.unparse(node)}'", node)
        t = resolve(t)
        if t == UNKNOWN:
            self.err(f"cannot statically infer the type of '{ast.unparse(node)}'", node)
        return t

    def lt(self, t, node=None):
        return self.mg.lt(t, node)

    def returned_names(self) -> set:
        out = set()
        for n in ast.walk(self.fi.node):
            if isinstance(n, ast.Return) and n.value is not None:
                for name in list(self.fi.locals):
                    if FuncInferer.escapes_in(n.value, name):
                        out.add(name)
        return out

    def alloca(self, ty, name=""):
        return self.entry_b.alloca(ty, name=name)

    def memcpy(self, dst, src, nbytes):
        self.b.call(self.mg.memcpy, [dst, src, ci(nbytes, I64T) if isinstance(nbytes, int) else nbytes, ir.Constant(I1, 0)])

    def fault(self, kind: str, detail: str, node) -> None:
        b = self.b
        b.call(self.mg.rt_fault, [self.mg.cstr(kind), self.mg.cstr(detail), ci(getattr(node, "lineno", 0), I64T),
                                  self.mg.cstr(self.fi.name)])
        b.unreachable()

    def guard(self, ok, kind: str, detail: str, node) -> None:
        """Continue when `ok` (i1) holds, otherwise raise a runtime fault."""
        good, bad = self.fn.append_basic_block("ok"), self.fn.append_basic_block("fault")
        self.b.cbranch(ok, good, bad)
        self.b.position_at_end(bad)
        self.fault(kind, detail, node)
        self.b.position_at_end(good)

    def check_index(self, idx, n, node) -> None:
        """dev mode: 0 <= idx < n (a negative index is out of range by design)."""
        if self.mg.mode != "dev":
            return
        good, bad = self.fn.append_basic_block("idx.ok"), self.fn.append_basic_block("idx.bad")
        self.b.cbranch(self.b.icmp_unsigned("<", idx, n), good, bad)
        self.b.position_at_end(bad)
        self.b.call(self.mg.rt_index_fault, [idx, n, ci(getattr(node, "lineno", 0), I64T), self.mg.cstr(self.fi.name)])
        self.b.unreachable()
        self.b.position_at_end(good)

    def narrow_check(self, v, frm: Type, to: Type, node, always: bool, what: str) -> None:
        """Integer narrowing (i64 -> i32/u8 ...) must not silently truncate."""
        frm, to = resolve(frm), resolve(to)
        if not (isinstance(frm, Scalar) and isinstance(to, Scalar) and frm.kind == "int" and to.kind == "int"
                and to.size < frm.size):
            return
        lo, hi = (0, 255) if to == U8 else (-2**31, 2**31 - 1) if to == I32 else (None, None)
        cv = const_value(node, self.info.consts) if node is not None else None
        if cv is not None and lo is not None and not (lo <= cv <= hi):
            raise CodegenError(f"{what}: literal {cv} does not fit {to.name} ({lo}..{hi})", node)
        if not (always or self.mg.mode == "dev"):
            return
        t = self.b.trunc(v, self.lt(to))
        back = self.b.zext(t, v.type) if to == U8 else self.b.sext(t, v.type)
        self.guard(self.b.icmp_signed("==", back, v), "OverflowError", f"{what}: value does not fit {to.name}", node)

    def terminated(self) -> bool:
        return self.b.block.is_terminated

    # ---- function ----------------------------------------------------
    def emit(self) -> None:
        fi = self.fi
        for arg, (name, t) in zip(self.fn.args, fi.params.items()):
            if t == UNKNOWN:
                self.err(f"cannot infer the type of parameter '{name}' of '{fi.name}'", fi.node)
            if is_agg(t):
                self.slots[name] = (arg, t)
            else:
                slot = self.alloca(self.lt(t, fi.node), name)
                self.entry_b.store(arg, slot)
                self.slots[name] = (slot, t)
        for name, loc in fi.locals.items():
            if name not in self.slots:
                self.init_local(name, loc.type)
        self.block(fi.node.body)
        if not self.terminated():
            self.emit_return_value(None)
        self.entry_b.branch(self.b.function.basic_blocks[1])

    def init_local(self, name: str, t: Type) -> None:
        t = resolve(t)
        if t == UNKNOWN:
            self.err(f"cannot infer the type of local '{name}'", self.fi.node)
        slot = self.alloca(self.lt(t, self.fi.node), name)
        self.slots[name] = (slot, t)
        if isinstance(t, (Vec, StackVec)):
            buf = PTR(None) if False else ir.Constant(PTR, None)
            if isinstance(t, StackVec):
                arr = self.alloca(ir.ArrayType(self.lt(t.elem), t.max_n), name + ".buf")
                buf, cap = arr, t.max_n
            else:
                cap = 0
            self.store_vec_header(self.entry_b, slot, buf, 0, cap)

    def store_vec_header(self, b, slot, ptr, n, cap) -> None:
        vals = [x if isinstance(x, ir.Value) else ci(x, I64T) for x in (ptr, n, cap)]
        for i, v in enumerate(vals):
            b.store(v, b.gep(slot, [ci(0), ci(i)], inbounds=True, source_etype=VEC))

    # ---- statements --------------------------------------------------
    def block(self, stmts) -> None:
        for s in stmts:
            if self.terminated():
                return
            self.stmt(s)

    def stmt(self, s) -> None:
        h = getattr(self, "s_" + type(s).__name__, None)
        if h is None:
            self.err(f"statement '{type(s).__name__}' is not supported by the code generator", s)
        h(s)

    def s_Pass(self, s): pass

    def s_Break(self, s) -> None:
        if not self.loops:
            self.err("'break' outside a loop", s)
        self.b.branch(self.loops[-1][1])

    def s_Continue(self, s) -> None:
        if not self.loops:
            self.err("'continue' outside a loop", s)
        self.b.branch(self.loops[-1][0])

    def s_Expr(self, s) -> None:
        if isinstance(s.value, ast.Constant):
            return
        self.call_stmt(s.value)

    def call_stmt(self, e) -> None:
        if isinstance(e, ast.Call) and isinstance(e.func, ast.Attribute) and e.func.attr == "append":
            return self.append(e)
        if is_agg(self.ty_or_void(e)):
            self.addr(e)
        else:
            self.value(e)

    def ty_or_void(self, e) -> Type:
        t = resolve(self.info.expr_types.get(id(e), VOID))
        return VOID if t == UNKNOWN else t

    def s_Assign(self, s) -> None:
        tgt = s.targets[0]
        if isinstance(tgt, ast.Name):
            return self.assign_name(tgt.id, s.value, s)
        ptr, t = self.lvalue(tgt)
        self.store_to(ptr, t, s.value)

    def assign_name(self, name: str, val, s) -> None:
        slot, t = self.slots[name]
        if isinstance(val, ast.List) and not val.elts:
            return self.reset_vec(slot, t)
        self.store_to(slot, t, val, fresh_ok=True)

    def reset_vec(self, slot, t) -> None:
        if isinstance(t, Vec):
            self.free_vec(slot)
            self.store_vec_header(self.b, slot, ir.Constant(PTR, None), 0, 0)
        else:
            self.b.store(ci(0, I64T), self.b.gep(slot, [ci(0), ci(1)], inbounds=True, source_etype=VEC))

    def free_vec(self, slot) -> None:
        p = self.b.load(self.b.gep(slot, [ci(0), ci(0)], inbounds=True, source_etype=VEC), typ=PTR)
        self.b.call(self.mg.free, [p])

    def store_to(self, ptr, t: Type, val, fresh_ok: bool = False) -> None:
        """Store expression `val` into memory at `ptr` of declared type t (value semantics)."""
        t = resolve(t)
        if not is_agg(t):
            v, vt = self.value(val), self.ty(val)
            self.narrow_check(v, vt, t, val, False, "assignment")
            self.b.store(self.coerce(v, vt, t), ptr)
            return
        if isinstance(t, StrOwned) and self.ty(val) == STR_VIEW:
            return self.string_from_view(ptr, self.value(val))
        src = self.addr(val)
        if isinstance(t, (Vec, StackVec)):
            fresh = (isinstance(val, ast.Call) and not (isinstance(val.func, ast.Name) and val.func.id == "mut")) \
                or (isinstance(val, ast.BinOp) and isinstance(t, Vec))
            if isinstance(t, Vec) and fresh and fresh_ok:
                self.free_vec(ptr)
                self.memcpy(ptr, src, 24)
            else:
                self.vec_assign(ptr, t, src, resolve(self.ty(val)))
        else:
            self.memcpy(ptr, src, t.size)

    def string_from_view(self, slot, view) -> None:
        """Copy a str_view into an owned String slot (frees the previous buffer)."""
        b = self.b
        n, sp = b.extract_value(view, 1), b.extract_value(view, 0)
        self.free_vec(slot)
        dp = b.call(self.mg.malloc, [b.add(n, ci(1, I64T))])
        self.memcpy(dp, sp, n)
        self.store_vec_header(b, slot, dp, n, n)

    def view_of(self, e):
        """str_view value of any string-like expression (String is viewed in place)."""
        t = self.ty(e)
        if isinstance(t, StrOwned):
            return self.view_from_header(self.addr(e))
        if t == STR_VIEW:
            return self.value(e)
        self.err(f"'{ast.unparse(e)}' is not a string", e)

    def view_from_header(self, h):
        b = self.b
        p = b.load(b.gep(h, [ci(0), ci(0)], inbounds=True, source_etype=VEC), typ=PTR)
        n = b.load(b.gep(h, [ci(0), ci(1)], inbounds=True, source_etype=VEC), typ=I64T)
        return b.insert_value(b.insert_value(ir.Constant(VIEW, ir.Undefined), p, 0), n, 1)

    def concat_views(self, views: list):
        """Allocate one buffer for all views (no intermediate temporaries) -> fresh String header address."""
        b = self.b
        lens = [b.extract_value(v, 1) for v in views]
        total = lens[0]
        for ln in lens[1:]:
            total = b.add(total, ln)
        p = b.call(self.mg.malloc, [b.add(total, ci(1, I64T))])
        off = ci(0, I64T)
        for v, ln in zip(views, lens):
            self.memcpy(b.gep(p, [off], inbounds=True, source_etype=I8), b.extract_value(v, 0), ln)
            off = b.add(off, ln)
        tmp = self.alloca(VEC, "str")
        self.store_vec_header(b, tmp, p, total, total)
        return tmp

    def vec_assign(self, dst, dt, src, st) -> None:
        """Deep copy a Vec/StackVec header + elements into dst (value semantics)."""
        b = self.b
        n = b.load(b.gep(src, [ci(0), ci(1)], inbounds=True, source_etype=VEC), typ=I64T)
        sp = b.load(b.gep(src, [ci(0), ci(0)], inbounds=True, source_etype=VEC), typ=PTR)
        nbytes = b.mul(n, ci(dt.elem.size, I64T))
        if isinstance(dt, StackVec):
            dp = b.load(b.gep(dst, [ci(0), ci(0)], inbounds=True, source_etype=VEC), typ=PTR)
        else:
            self.free_vec(dst)
            dp = b.call(self.mg.malloc, [b.add(nbytes, ci(1, I64T))])
            b.store(dp, b.gep(dst, [ci(0), ci(0)], inbounds=True, source_etype=VEC))
            b.store(n, b.gep(dst, [ci(0), ci(2)], inbounds=True, source_etype=VEC))
        self.memcpy(dp, sp, nbytes)
        b.store(n, b.gep(dst, [ci(0), ci(1)], inbounds=True, source_etype=VEC))

    def s_AugAssign(self, s) -> None:
        if isinstance(s.target, ast.Name) and isinstance(self.slots[s.target.id][1], StrOwned) \
                and isinstance(s.op, ast.Add):
            slot = self.slots[s.target.id][0]
            tmp = self.concat_views([self.view_from_header(slot), self.view_of(s.value)])
            self.free_vec(slot)
            self.memcpy(slot, tmp, 24)
            return
        ptr, t = self.lvalue(s.target)
        cur = self.b.load(ptr, typ=self.lt(t))
        rt = self.ty(s.value)
        res_t = self.binop_type(t, rt, s.op)
        r = self.binop(s.op, self.coerce(cur, t, res_t), self.coerce(self.value(s.value), rt, res_t), res_t, s)
        self.b.store(self.coerce(r, res_t, t), ptr)

    def lvalue(self, node):
        """Address + Pa type of an assignable scalar/aggregate location."""
        if isinstance(node, ast.Name):
            return self.slots[node.id]
        if isinstance(node, (ast.Subscript, ast.Attribute)):
            return self.addr_of(node), self.ty(node)
        self.err("unsupported assignment target", node)

    def s_Return(self, s) -> None:
        if s.value is None:
            return self.emit_return_value(None)
        rt = resolve(self.fi.ret)
        if is_agg(rt):
            src = self.addr(s.value)
            v = self.b.load(src, typ=self.lt(rt))
        else:
            v = self.coerce(self.value(s.value), self.ty(s.value), rt)
        self.emit_return_value(v)

    def emit_return_value(self, v) -> None:
        for name, (slot, t) in self.slots.items():
            loc = self.fi.locals.get(name)
            if loc and name not in self.fi.params and isinstance(t, Vec) and not loc.escapes \
                    and name not in self.returned:
                self.free_vec(slot)
        rt = self.fn.function_type.return_type
        if rt == VOIDT:
            self.b.ret_void()
        else:
            self.b.ret(v if v is not None else ir.Constant(rt, None))

    def s_If(self, s) -> None:
        c = self.cond(s.test)
        then_b = self.fn.append_basic_block("then")
        else_b = self.fn.append_basic_block("else")
        end_b = self.fn.append_basic_block("endif")
        self.b.cbranch(c, then_b, else_b)
        for blk, body in ((then_b, s.body), (else_b, s.orelse)):
            self.b.position_at_end(blk)
            self.block(body)
            if not self.terminated():
                self.b.branch(end_b)
        self.b.position_at_end(end_b)

    def s_While(self, s) -> None:
        cb, bb, eb = (self.fn.append_basic_block(n) for n in ("wcond", "wbody", "wend"))
        self.b.branch(cb)
        self.b.position_at_end(cb)
        self.b.cbranch(self.cond(s.test), bb, eb)
        self.b.position_at_end(bb)
        self.loops.append((cb, eb))
        self.block(s.body)
        self.loops.pop()
        if not self.terminated():
            self.b.branch(cb)
        self.b.position_at_end(eb)

    def s_For(self, s) -> None:
        if not isinstance(s.target, ast.Name):
            self.err("for-target must be a simple name", s)
        slot, vt = self.slots[s.target.id]
        it = s.iter
        if isinstance(it, ast.Call) and isinstance(it.func, ast.Name) and it.func.id == "range":
            return self.for_range(s, slot, vt)
        ptr, n, elem = self.seq_view(it)
        idx = self.alloca(I64T, "i")
        self.b.store(ci(0, I64T), idx)
        self.loop(s, lambda: self.b.icmp_signed("<", self.b.load(idx, typ=I64T), n),
                  lambda: self.bind_elem(slot, vt, ptr, idx, elem),
                  lambda: self.b.store(self.b.add(self.b.load(idx, typ=I64T), ci(1, I64T)), idx))

    def bind_elem(self, slot, vt, ptr, idx, elem) -> None:
        ep = self.b.gep(ptr, [self.b.load(idx, typ=I64T)], inbounds=True, source_etype=self.lt(elem))
        if is_agg(elem):
            self.memcpy(slot, ep, elem.size)
        else:
            self.b.store(self.coerce(self.b.load(ep, typ=self.lt(elem)), elem, vt), slot)

    def for_range(self, s, slot, vt) -> None:
        args = [self.coerce(self.value(a), self.ty(a), vt) for a in s.iter.args]
        lt = self.lt(vt)
        start, stop, step = (args[0], args[1], args[2]) if len(args) == 3 else \
            (ir.Constant(lt, 0), args[0], ir.Constant(lt, 1)) if len(args) == 1 else (args[0], args[1], ir.Constant(lt, 1))
        self.b.store(start, slot)
        cur = lambda: self.b.load(slot, typ=lt)
        if len(s.iter.args) < 3:
            cond = lambda: self.b.icmp_signed("<", cur(), stop)
        else:
            cv = const_value(s.iter.args[2], self.info.consts)
            if cv == 0:
                self.err("range() step must not be zero", s)
            if cv is not None:
                op = "<" if cv > 0 else ">"
                cond = lambda: self.b.icmp_signed(op, cur(), stop)
            else:                    # runtime step: decide the direction once, before the loop
                self.guard(self.b.icmp_signed("!=", step, ir.Constant(lt, 0)), "ValueError",
                           "range() arg 3 must not be zero", s)
                up = self.b.icmp_signed(">", step, ir.Constant(lt, 0))
                cond = lambda: self.b.select(up, self.b.icmp_signed("<", cur(), stop),
                                             self.b.icmp_signed(">", cur(), stop))
        self.loop(s, cond, lambda: None, lambda: self.b.store(self.b.add(cur(), step), slot))

    def loop(self, s, cond_fn, prologue, step_fn, body=None) -> None:
        cb, bb, sb, eb = (self.fn.append_basic_block(n) for n in ("fcond", "fbody", "fstep", "fend"))
        self.b.branch(cb)
        self.b.position_at_end(cb)
        self.b.cbranch(cond_fn(), bb, eb)
        self.b.position_at_end(bb)
        prologue()
        self.loops.append((sb, eb))
        self.block(s.body if body is None else body)
        self.loops.pop()
        if not self.terminated():
            self.b.branch(sb)
        self.b.position_at_end(sb)
        step_fn()
        self.b.branch(cb)
        self.b.position_at_end(eb)

    # ---- append ------------------------------------------------------
    def append(self, e: ast.Call) -> None:
        recv = e.func.value
        t = self.ty(recv)
        if not isinstance(t, (Vec, StackVec)):
            self.err("append() requires a list built from []", e)
        base = self.addr(recv)
        b = self.b
        hp = lambda i: b.gep(base, [ci(0), ci(i)], inbounds=True, source_etype=VEC)
        n, ptr = b.load(hp(1), typ=I64T), None
        if isinstance(t, Vec):
            self.grow_if_full(base, hp, n, t)
        ptr = b.load(hp(0), typ=PTR)
        ep = b.gep(ptr, [n], inbounds=True, source_etype=self.lt(t.elem))
        if is_agg(t.elem):
            self.memcpy(ep, self.addr(e.args[0]), t.elem.size)
        else:
            b.store(self.coerce(self.value(e.args[0]), self.ty(e.args[0]), t.elem), ep)
        b.store(b.add(n, ci(1, I64T)), hp(1))

    def grow_if_full(self, base, hp, n, t) -> None:
        b = self.b
        cap = b.load(hp(2), typ=I64T)
        grow, cont = self.fn.append_basic_block("grow"), self.fn.append_basic_block("grown")
        b.cbranch(b.icmp_signed(">=", n, cap), grow, cont)
        b.position_at_end(grow)
        newcap = b.select(b.icmp_signed("==", cap, ci(0, I64T)), ci(16, I64T), b.mul(cap, ci(2, I64T)))
        newp = b.call(self.mg.realloc, [b.load(hp(0), typ=PTR), b.mul(newcap, ci(t.elem.size, I64T))])
        b.store(newp, hp(0))
        b.store(newcap, hp(2))
        b.branch(cont)
        b.position_at_end(cont)

    # ---- conditions & arithmetic -------------------------------------
    def cond(self, e) -> ir.Value:
        v, t = self.value(e), self.ty(e)
        if v.type == I1:
            return v
        if isinstance(v.type, (ir.FloatType, ir.DoubleType)):
            return self.b.fcmp_ordered("!=", v, ir.Constant(v.type, 0.0))
        return self.b.icmp_signed("!=", v, ir.Constant(v.type, 0))

    def coerce(self, v, frm: Type, to: Type):
        frm, to = resolve(frm), resolve(to)
        want = self.lt(to)
        if v.type == want or to == UNKNOWN:
            return v
        b = self.b
        fi_, ti = isinstance(v.type, ir.IntType), isinstance(want, ir.IntType)
        if want == I1:
            return b.fcmp_ordered("!=", v, ir.Constant(v.type, 0.0)) if not fi_ else b.icmp_signed("!=", v, ir.Constant(v.type, 0))
        if fi_ and ti:
            if v.type.width < want.width:
                return b.zext(v, want) if frm in (U8, BOOL) else b.sext(v, want)
            return b.trunc(v, want)
        if fi_ and not ti:
            return b.uitofp(v, want) if frm in (U8, BOOL) else b.sitofp(v, want)
        if not fi_ and ti:
            return b.fptosi(v, want)
        return b.fpext(v, want) if want == F64T else b.fptrunc(v, want)

    def binop_type(self, a: Type, b: Type, op) -> Type:
        from .types import promote
        if isinstance(op, ast.Div):
            return F32 if (F32 in (a, b) and F64 not in (a, b)) else F64
        return promote(a, b)

    def binop(self, op, l, r, t: Type, node):
        b, fl = self.b, t in (F32, F64)
        if isinstance(op, ast.Add): return b.fadd(l, r) if fl else b.add(l, r)
        if isinstance(op, ast.Sub): return b.fsub(l, r) if fl else b.sub(l, r)
        if isinstance(op, ast.Mult): return b.fmul(l, r) if fl else b.mul(l, r)
        if isinstance(op, ast.Div): return b.fdiv(l, r)
        if isinstance(op, ast.FloorDiv):
            if fl:
                self.err("float // is not supported", node)
            return self.int_divmod(l, r, node, True)
        if isinstance(op, ast.Mod):
            return b.frem(l, r) if fl else self.int_divmod(l, r, node, False)
        if fl:
            self.err(f"operator {type(op).__name__} on floats is not supported", node)
        for cls, f in ((ast.BitAnd, b.and_), (ast.BitOr, b.or_), (ast.BitXor, b.xor), (ast.LShift, b.shl), (ast.RShift, b.ashr)):
            if isinstance(op, cls):
                return f(l, r)
        self.err(f"operator {type(op).__name__} is not supported", node)

    def int_divmod(self, l, r, node, is_div: bool):
        """Integer // and %: divisor 0 raises ZeroDivisionError; x // -1 and x % -1 cannot trap on INT_MIN."""
        b = self.b
        self.guard(b.icmp_signed("!=", r, ir.Constant(r.type, 0)), "ZeroDivisionError",
                   "integer division or modulo by zero", node)
        m1 = b.icmp_signed("==", r, ir.Constant(r.type, -1))
        safe = b.select(m1, ir.Constant(r.type, 1), r)
        if is_div:
            return b.select(m1, b.neg(l), b.sdiv(l, safe))
        return b.select(m1, ir.Constant(r.type, 0), b.srem(l, safe))

    # ---- expressions (scalars / views by value) ----------------------
    def value(self, e) -> ir.Value:
        h = getattr(self, "v_" + type(e).__name__, None)
        if h is None:
            self.err(f"expression '{type(e).__name__}' is not supported by the code generator", e)
        return h(e)

    def v_Constant(self, e):
        v = e.value
        if isinstance(v, bool): return ir.Constant(I1, int(v))
        if isinstance(v, int): return ci(v, I32T if self.ty(e) == I32 else I64T)
        if isinstance(v, float): return ir.Constant(F64T, v)
        if isinstance(v, str):
            raw = v.encode("utf-8")
            view = ir.Constant(VIEW, ir.Undefined)
            view = self.b.insert_value(view, self.mg.cstr(v), 0)
            return self.b.insert_value(view, ci(len(raw), I64T), 1)
        self.err("unsupported constant", e)

    def v_Name(self, e):
        if e.id in self.slots:
            slot, t = self.slots[e.id]
            return self.b.load(slot, typ=self.lt(t, e))
        if e.id in self.info.consts:
            return ci(self.info.consts[e.id], I32T if self.ty(e) == I32 else I64T)
        if e.id in self.info.fconsts:
            val, t = self.info.fconsts[e.id]
            if t == BOOL:
                return ir.Constant(I1, int(val))
            if t == F64:
                return ir.Constant(F64T, val)
            return self.v_Constant(ast.Constant(value=val))
        self.err(f"unknown name '{e.id}'", e)

    def v_UnaryOp(self, e):
        v, t = self.value(e.operand), self.ty(e.operand)
        if isinstance(e.op, ast.Not):
            return self.b.xor(self.cond(e.operand), ir.Constant(I1, 1))
        if t == BOOL:
            v, t = self.coerce(v, t, I32), I32
        if isinstance(e.op, ast.USub):
            return self.b.fneg(v) if t in (F32, F64) else self.b.neg(v)
        if isinstance(e.op, ast.Invert):
            return self.b.not_(v)
        return v

    def v_BinOp(self, e):
        lt_, rt = self.ty(e.left), self.ty(e.right)
        t = self.binop_type(lt_, rt, e.op)
        l, r = self.coerce(self.value(e.left), lt_, t), self.coerce(self.value(e.right), rt, t)
        return self.binop(e.op, l, r, t, e)

    def v_Compare(self, e):
        res, left = None, e.left
        for op, right in zip(e.ops, e.comparators):
            c = self.compare_one(op, left, right, e)
            res = c if res is None else self.b.and_(res, c)
            left = right
        return res

    def compare_one(self, op, a, bnode, e):
        from .types import promote
        at, bt = self.ty(a), self.ty(bnode)
        if is_str(at) and is_str(bt) and isinstance(op, (ast.Eq, ast.NotEq)):
            return self.str_equal(a, bnode, isinstance(op, ast.NotEq))
        t = promote(at, bt)
        l, r = self.coerce(self.value(a), at, t), self.coerce(self.value(bnode), bt, t)
        name = {ast.Lt: "<", ast.LtE: "<=", ast.Gt: ">", ast.GtE: ">=", ast.Eq: "==", ast.NotEq: "!="}.get(type(op))
        if name is None:
            self.err("unsupported comparison operator", e)
        if t in (F32, F64):
            return self.b.fcmp_ordered(name, l, r) if name != "!=" else self.b.fcmp_unordered("!=", l, r)
        return self.b.icmp_signed(name, l, r)

    def str_equal(self, a, bnode, negate: bool):
        b = self.b
        va, vb = self.view_of(a), self.view_of(bnode)
        la, lb = b.extract_value(va, 1), b.extract_value(vb, 1)
        same_len = b.icmp_signed("==", la, lb)
        n = b.select(same_len, la, ci(0, I64T))
        r = b.call(self.mg.bcmp, [b.extract_value(va, 0), b.extract_value(vb, 0), n])
        eq = b.and_(same_len, b.icmp_signed("==", r, ci(0)))
        return b.xor(eq, ir.Constant(I1, 1)) if negate else eq

    def v_BoolOp(self, e):
        is_and = isinstance(e.op, ast.And)
        res = self.alloca(I1, "bool")
        self.b.store(self.cond(e.values[0]), res)
        end = self.fn.append_basic_block("bend")
        for v in e.values[1:]:
            nxt = self.fn.append_basic_block("bnext")
            cur = self.b.load(res, typ=I1)
            self.b.cbranch(cur, nxt, end) if is_and else self.b.cbranch(cur, end, nxt)
            self.b.position_at_end(nxt)
            self.b.store(self.cond(v), res)
        self.b.branch(end)
        self.b.position_at_end(end)
        return self.b.load(res, typ=I1)

    def v_IfExp(self, e):
        t = self.ty(e)
        if is_agg(t):
            self.err("conditional expression of aggregates is not supported", e)
        slot = self.alloca(self.lt(t), "ifx")
        tb, eb, nb = (self.fn.append_basic_block(n) for n in ("ift", "ife", "ifend"))
        self.b.cbranch(self.cond(e.test), tb, eb)
        for blk, x in ((tb, e.body), (eb, e.orelse)):
            self.b.position_at_end(blk)
            self.b.store(self.coerce(self.value(x), self.ty(x), t), slot)
            self.b.branch(nb)
        self.b.position_at_end(nb)
        return self.b.load(slot, typ=self.lt(t))

    def v_Subscript(self, e):
        t = self.ty(e)
        return self.b.load(self.addr_of(e), typ=self.lt(t, e))

    def v_Attribute(self, e):
        return self.b.load(self.addr_of(e), typ=self.lt(self.ty(e), e))

    def v_Call(self, e):
        f = e.func
        if isinstance(f, ast.Attribute):
            if isinstance(f.value, ast.Name) and f.value.id in self.info.paw:
                return self.paw_call(e)
            self.err(f"method call '.{f.attr}()' is not supported", e)
        name = f.id
        if name in self.info.funcs:
            return self.local_call(e, name)
        if name in self.mg.math_fns:
            want = 2 if name in MATH_BINARY else 1
            if len(e.args) != want or e.keywords:
                self.err(f"{name}() expects {want} positional argument(s)", e)
            args = [self.coerce(self.value(a), self.ty(a), F64) for a in e.args]
            return self.b.call(self.mg.math_fns[name], args)
        h = getattr(self, "i_" + name, None)
        if h is None:
            self.err(f"call to unknown function '{name}'", e)
        return h(e)

    # ---- intrinsics --------------------------------------------------
    def i_len(self, e):
        _, n, _ = self.seq_view(e.args[0])
        return n

    def i_int(self, e): return self.coerce(self.value(e.args[0]), self.ty(e.args[0]), I64)
    def i_float(self, e): return self.coerce(self.value(e.args[0]), self.ty(e.args[0]), F64)
    def i_bool(self, e): return self.cond(e.args[0])
    def i_mut(self, e): return self.value(e.args[0])

    def i_abs(self, e):
        t = self.ty(e.args[0])
        v = self.value(e.args[0])
        neg = self.b.fneg(v) if t in (F32, F64) else self.b.neg(v)
        lt0 = self.b.fcmp_ordered("<", v, ir.Constant(v.type, 0.0)) if t in (F32, F64) else self.b.icmp_signed("<", v, ir.Constant(v.type, 0))
        return self.b.select(lt0, neg, v)

    def minmax(self, e, op):
        t = self.ty(e)
        vals = [self.coerce(self.value(a), self.ty(a), t) for a in e.args]
        out = vals[0]
        for v in vals[1:]:
            c = self.b.fcmp_ordered(op, v, out) if t in (F32, F64) else self.b.icmp_signed(op, v, out)
            out = self.b.select(c, v, out)
        return out

    def i_min(self, e): return self.minmax(e, "<")
    def i_max(self, e): return self.minmax(e, ">")

    def i_alloc(self, e):
        from .types import SCALARS
        tname = e.args[0].id
        elem = SCALARS.get(tname) or self.info.structs.get(tname)
        if elem is None:
            self.err(f"alloc: unknown type '{tname}'", e)
        n = self.coerce(self.value(e.args[1]), self.ty(e.args[1]), I64)
        return self.b.call(self.mg.malloc, [self.b.mul(n, ci(elem.size, I64T))])

    def i_free(self, e):
        self.b.call(self.mg.free, [self.value(e.args[0])])
        return ir.Constant(I32T, 0)

    def i_span(self, e):
        ptr, n, elem = self.seq_view(e.args[0])
        start = self.coerce(self.value(e.args[1]), self.ty(e.args[1]), I64)
        ln = self.coerce(self.value(e.args[2]), self.ty(e.args[2]), I64)
        p = self.b.gep(ptr, [start], inbounds=True, source_etype=self.lt(elem))
        v = self.b.insert_value(ir.Constant(VIEW, ir.Undefined), p, 0)
        return self.b.insert_value(v, ln, 1)

    def i_copy(self, e):
        dst, _, elem = self.seq_view(e.args[0])
        src, _, _ = self.seq_view(e.args[1])
        cnt = self.coerce(self.value(e.args[2]), self.ty(e.args[2]), I64)
        self.memcpy(dst, src, self.b.mul(cnt, ci(elem.size, I64T)))
        return ir.Constant(I32T, 0)

    def i_print(self, e):
        b, mg = self.b, self.mg
        for k, a in enumerate(e.args):
            if k:
                b.call(mg.p_sep, [])
            t = self.ty(a)
            if is_str(t):
                v = self.view_of(a)
                b.call(mg.p_str, [b.extract_value(v, 0), b.extract_value(v, 1)])
            elif t == BOOL:
                b.call(mg.p_bool, [b.zext(self.value(a), I32T)])
            elif t in (F32, F64):
                b.call(mg.p_f64, [self.coerce(self.value(a), t, F64)])
            elif t in (I64, I32, U8):
                b.call(mg.p_i64, [self.coerce(self.value(a), t, I64)])
            else:
                self.err(f"print() cannot print a value of type {t.fmt()}", e)
        b.call(mg.p_nl, [])
        return ir.Constant(I32T, 0)

    # ---- local calls & paw FFI ---------------------------------------
    def local_call(self, e, name: str):
        callee = self.info.funcs[name]
        names = list(callee.params)
        bound = list(zip(names, e.args)) + [(k.arg, k.value) for k in e.keywords]
        by_name = dict(bound)
        args = []
        for pname in names:
            a = by_name.get(pname)
            if a is None:
                self.err(f"missing argument '{pname}' in call to '{name}'", e)
            pt = callee.params[pname]
            if is_agg(pt):
                args.append(self.addr(a))
            else:
                v, at = self.value(a), self.ty(a)
                self.narrow_check(v, at, pt, a, False, f"argument '{pname}' of {name}()")
                args.append(self.coerce(v, at, pt))
        return self.b.call(self.mg.fns[name], args)

    def paw_call(self, e):
        mod = self.info.paw[e.func.value.id]
        pf: PawFunc = mod.funcs[e.func.attr]
        self.mg.paw_used.add(mod.name)
        b = self.b
        arr = self.alloca(ir.ArrayType(PAARG, max(len(pf.params), 1)), "pawargs")
        for i, ((pn, pt), a) in enumerate(zip(pf.params, e.args)):
            self.fill_arg(b.gep(arr, [ci(0), ci(i)], inbounds=True, source_etype=ir.ArrayType(PAARG, max(len(pf.params), 1))), pt, a)
        ret = self.alloca(PARET, "pawret")
        rt, relem = self.ret_codes(pf.ret)
        b.call(self.mg.call_paw, [self.mg.cstr(mod.name), self.mg.cstr(pf.name), arr, ci(len(pf.params)),
                                  ci(rt), ci(relem), ci(1 if pf.allow_nan else 0), ret])
        return self.unbox_ret(pf.ret, ret)

    @staticmethod
    def ret_codes(t: Type):
        if t == VOID: return TAG_VOID, 0
        if isinstance(t, Vec): return TAG_BUF, ELEM_CODES[t.elem.name]
        tag = TAG_BOOL if t == BOOL else TAG_FLOAT if t in (F32, F64) else TAG_INT
        return tag, ELEM_CODES[t.name]

    def fill_arg(self, p, pt: Type, a) -> None:
        b = self.b
        fld = lambda i: b.gep(p, [ci(0), ci(i)], inbounds=True, source_etype=PAARG)
        at = self.ty(a)
        if pt == STR_VIEW:
            v = self.view_of(a)
            tag, elem, ptr, n = TAG_STR, 0, b.extract_value(v, 0), b.extract_value(v, 1)
            vals = (tag, elem, 0, 0.0, ptr, n)
        elif isinstance(pt, (FixedList, Vec, Span)):
            ptr, n, _ = self.seq_view(a)
            vals = (TAG_BUF, ELEM_CODES[pt.elem.name], 0, 0.0, ptr, n)
        else:
            v0 = self.value(a)
            self.narrow_check(v0, at, pt, a, True, f"argument {pt.name} of .paw call")
            v = self.coerce(v0, at, pt)
            elem = ELEM_CODES[pt.name]
            if pt == BOOL:
                vals = (TAG_BOOL, elem, b.zext(v, I64T), 0.0, None, 0)
            elif pt in (F32, F64):
                vals = (TAG_FLOAT, elem, 0, self.coerce(v, pt, F64), None, 0)
            else:
                vals = (TAG_INT, elem, v if pt == I64 else (b.zext(v, I64T) if pt == U8 else b.sext(v, I64T)), 0.0, None, 0)
        tys = (I32T, I32T, I64T, F64T, PTR, I64T)
        for i, (v, ty) in enumerate(zip(vals, tys)):
            if not isinstance(v, ir.Value):
                v = ir.Constant(ty, None if ty == PTR and v is None else v)
            b.store(v, fld(i))

    def unbox_ret(self, t: Type, ret):
        b = self.b
        fld = lambda i, ty: b.load(b.gep(ret, [ci(0), ci(i)], inbounds=True, source_etype=PARET), typ=ty)
        if t == VOID:
            return ir.Constant(I32T, 0)
        if isinstance(t, Vec):
            tmp = self.alloca(VEC, "pawvec")
            n = fld(3, I64T)
            for i, v in enumerate((fld(2, PTR), n, n)):
                b.store(v, b.gep(tmp, [ci(0), ci(i)], inbounds=True, source_etype=VEC))
            return tmp
        if t in (F32, F64):
            return self.coerce(fld(1, F64T), F64, t)
        v = fld(0, I64T)
        if t == BOOL:
            return b.icmp_signed("!=", v, ci(0, I64T))
        return v if t == I64 else b.trunc(v, self.lt(t))

    # ---- addresses (aggregates & lvalues) ----------------------------
    def addr(self, e) -> ir.Value:
        """Address of an aggregate-typed expression (a temp is materialized if needed)."""
        h = getattr(self, "a_" + type(e).__name__, None)
        if h is None:
            self.err(f"aggregate expression '{type(e).__name__}' is not supported by the code generator", e)
        return h(e)

    def a_Name(self, e):
        if e.id not in self.slots:
            self.err(f"unknown name '{e.id}'", e)
        return self.slots[e.id][0]

    def a_Subscript(self, e): return self.addr_of(e)
    def a_Attribute(self, e): return self.addr_of(e)

    def a_Call(self, e):
        f = e.func
        if isinstance(f, ast.Name) and f.id == "mut":
            return self.addr(e.args[0])
        t = self.ty(e)
        if isinstance(f, ast.Name) and f.id in self.info.structs:
            return self.struct_ctor(e, self.info.structs[f.id])
        if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name) and f.value.id in self.info.paw:
            return self.paw_call(e)       # Vec result: already an address
        v = self.value(e)
        tmp = self.alloca(self.lt(t, e), "ret")
        self.b.store(v, tmp)
        return tmp

    def struct_ctor(self, e, st: StructT):
        tmp = self.alloca(self.lt(st), st.name)
        names = [n for n, _ in st.fields]
        bound = list(zip(names, e.args)) + [(k.arg, k.value) for k in e.keywords]
        for fname, val in bound:
            idx = names.index(fname)
            ft = st.fields[idx][1]
            self.store_to(self.b.gep(tmp, [ci(0), ci(idx)], inbounds=True, source_etype=self.lt(st)), ft, val)
        return tmp

    def a_List(self, e):
        t = self.ty(e)
        if not isinstance(t, FixedList):
            self.err("list literal exceeds the 64KB stack limit; build it with append()", e)
        tmp = self.alloca(self.lt(t, e), "lit")
        for i, x in enumerate(e.elts):
            self.store_to(self.b.gep(tmp, [ci(0), ci(i)], inbounds=True, source_etype=self.lt(t)), t.elem, x)
        return tmp

    def flatten_concat(self, n) -> list:
        if isinstance(n, ast.BinOp) and isinstance(n.op, ast.Add) and isinstance(self.ty(n), StrOwned):
            return self.flatten_concat(n.left) + self.flatten_concat(n.right)
        return [n]

    def a_BinOp(self, e):
        if isinstance(self.ty(e), StrOwned) and isinstance(e.op, ast.Add):
            return self.concat_views([self.view_of(x) for x in self.flatten_concat(e)])
        if not (isinstance(e.op, ast.Mult) and isinstance(e.left, ast.List)):
            self.err("aggregate arithmetic is not supported", e)
        t = self.ty(e)
        if isinstance(t, Vec):
            return self.heap_repeat(e, t)
        if not isinstance(t, FixedList):
            self.err("unsupported list repetition", e)
        tmp = self.alloca(self.lt(t, e), "rep")
        k = len(e.left.elts)
        reps = t.n // k
        if reps * k <= 16:
            for rep in range(reps):
                self.fill_repeat(tmp, t, e, ci(rep, I64T), k)
            return tmp
        i = self.alloca(I64T, "rep.i")
        self.b.store(ci(0, I64T), i)
        self.loop(ast.Pass(), lambda: self.b.icmp_signed("<", self.b.load(i, typ=I64T), ci(reps, I64T)),
                  lambda: self.fill_repeat(tmp, t, e, self.b.load(i, typ=I64T), k),
                  lambda: self.b.store(self.b.add(self.b.load(i, typ=I64T), ci(1, I64T)), i), body=[])
        return tmp

    def heap_repeat(self, e, t: Vec):
        """`[x] * n` with runtime n, n <= 0, or above the 64KB stack limit: contiguous heap Vec."""
        b = self.b
        k = len(e.left.elts)
        raw_reps = self.coerce(self.value(e.right), self.ty(e.right), I64)
        reps = b.select(b.icmp_signed("<", raw_reps, ci(0, I64T)), ci(0, I64T), raw_reps)
        total = b.mul(reps, ci(k, I64T))
        raw_bytes = b.mul(total, ci(t.elem.size, I64T))
        nbytes = b.select(b.icmp_signed(">", raw_bytes, ci(0, I64T)), raw_bytes, ci(1, I64T))
        data = b.call(self.mg.malloc, [nbytes])
        tmp = self.alloca(VEC, "bigrep")
        self.store_vec_header(b, tmp, data, total, total)
        i = self.alloca(I64T, "rep.i")
        b.store(ci(0, I64T), i)

        def body():
            rep = b.load(i, typ=I64T)
            for j, x in enumerate(e.left.elts):
                idx = b.add(b.mul(rep, ci(k, I64T)), ci(j, I64T))
                self.store_to(b.gep(data, [idx], inbounds=True, source_etype=self.lt(t.elem)), t.elem, x)

        self.loop(ast.Pass(), lambda: b.icmp_signed("<", b.load(i, typ=I64T), reps), body,
                  lambda: b.store(b.add(b.load(i, typ=I64T), ci(1, I64T)), i), body=[])
        return tmp

    def fill_repeat(self, tmp, t, e, rep, k) -> None:
        for j, x in enumerate(e.left.elts):
            idx = self.b.add(self.b.mul(rep, ci(k, I64T)), ci(j, I64T))
            p = self.b.gep(tmp, [ci(0), idx], inbounds=True, source_etype=self.lt(t))
            self.store_to(p, t.elem, x)

    def addr_of(self, e):
        """Address of a Subscript / Attribute location."""
        if isinstance(e, ast.Attribute):
            bt = self.ty(e.value)
            if not isinstance(bt, StructT):
                self.err("attribute access requires a @struct", e)
            idx = [n for n, _ in bt.fields].index(e.attr)
            return self.b.gep(self.addr(e.value), [ci(0), ci(idx)], inbounds=True, source_etype=self.lt(bt))
        if isinstance(e.slice, ast.Slice):
            self.err("slices are not supported (use span(arr, start, len))", e)
        base_t = self.ty(e.value)
        ptr, n, elem = self.seq_view(e.value)
        idxs = e.slice.elts if isinstance(e.slice, ast.Tuple) else [e.slice]
        idx = self.index_value(idxs[0])
        self.static_index_check(idxs[0], base_t, e)
        if not isinstance(base_t, Ptr):
            self.check_index(idx, n, e)
        ptr = self.b.gep(ptr, [idx], inbounds=True, source_etype=self.lt(elem))
        for ix in idxs[1:]:                     # m[i, j]: descend one dimension per extra index
            ptr, elem = self.index_into(ptr, elem, ix, e)
        return ptr

    def static_index_check(self, ix, container: Type, node) -> None:
        cv = const_value(ix, self.info.consts)
        if cv is not None and isinstance(container, FixedList) and not (0 <= cv < container.n):
            raise CodegenError(f"constant index {cv} is out of range for a list of length {container.n}", node)

    def index_value(self, ix):
        return self.coerce(self.value(ix), self.ty(ix), I64)

    def index_into(self, ptr, cur: Type, ix, node):
        b = self.b
        if isinstance(cur, FixedList):
            idx = self.index_value(ix)
            self.static_index_check(ix, cur, node)
            self.check_index(idx, ci(cur.n, I64T), node)
            return b.gep(ptr, [idx], inbounds=True, source_etype=self.lt(cur.elem)), cur.elem
        if isinstance(cur, (Vec, StackVec)):
            data = b.load(b.gep(ptr, [ci(0), ci(0)], inbounds=True, source_etype=VEC), typ=PTR)
            n = b.load(b.gep(ptr, [ci(0), ci(1)], inbounds=True, source_etype=VEC), typ=I64T)
            idx = self.index_value(ix)
            self.check_index(idx, n, node)
            return b.gep(data, [idx], inbounds=True, source_etype=self.lt(cur.elem)), cur.elem
        self.err(f"cannot index into {cur.fmt()} with an extra subscript", node)

    def seq_view(self, e):
        """(data pointer, length i64, element Type) of a list-like expression."""
        t = self.ty(e)
        b = self.b
        if isinstance(t, FixedList):
            return self.addr(e), ci(t.n, I64T), t.elem
        if isinstance(t, (Vec, StackVec)):
            base = self.addr(e)
            p = b.load(b.gep(base, [ci(0), ci(0)], inbounds=True, source_etype=VEC), typ=PTR)
            n = b.load(b.gep(base, [ci(0), ci(1)], inbounds=True, source_etype=VEC), typ=I64T)
            return p, n, t.elem
        if isinstance(t, Span) or t == STR_VIEW:
            v = self.value(e)
            return b.extract_value(v, 0), b.extract_value(v, 1), (U8 if t == STR_VIEW else t.elem)
        if isinstance(t, Ptr):
            return self.value(e), ci(0, I64T), t.elem
        self.err(f"'{ast.unparse(e)}' of type {t.fmt()} is not list-like", e)


def generate(tree: ast.Module, info: ModuleInfo, filename: str = "<mem>", shared: bool = False,
             mode: str = "dev") -> tuple:
    """Return (llvm_ir_text, set_of_paw_modules_used)."""
    mg = ModuleGen(tree, info, filename, shared, mode)
    text = mg.emit()
    return text, mg.paw_used
