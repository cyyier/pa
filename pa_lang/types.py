"""Continuous-memory type system and Compiler Intrinsics registry."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

STACK_LIMIT = 64 * 1024          # Level-1 stack ceiling
COPY_WARN_LIMIT = 4 * 1024       # PA_W001 threshold for one value-copy


class Type:
    """Base class. size/align are in bytes, following the C++ layout."""
    def fmt(self) -> str:
        raise NotImplementedError

    def __str__(self) -> str:
        return self.fmt()


@dataclass(frozen=True)
class Scalar(Type):
    name: str
    size: int
    kind: str  # "int" | "float" | "bool" | "void"

    @property
    def align(self) -> int:  # type: ignore[override]
        return max(self.size, 1)

    def fmt(self) -> str:
        return self.name


I32 = Scalar("i32", 4, "int")
I64 = Scalar("i64", 8, "int")
U8 = Scalar("u8", 1, "int")
F32 = Scalar("f32", 4, "float")
F64 = Scalar("f64", 8, "float")
BOOL = Scalar("bool", 1, "bool")
VOID = Scalar("void", 0, "void")


@dataclass(frozen=True)
class ViewT(Type):
    """str_view / bytes_view: {u8*, i64}."""
    name: str
    size: int = 16
    align: int = 8

    def fmt(self) -> str:
        return self.name


STR_VIEW = ViewT("str_view")
BYTES_VIEW = ViewT("bytes_view")


@dataclass(frozen=True)
class UnknownT(Type):
    size = 0
    align = 1

    def fmt(self) -> str:
        return "unknown"


UNKNOWN = UnknownT()


@dataclass(frozen=True)
class FixedList(Type):
    elem: Type
    n: int

    @property
    def size(self) -> int:  # type: ignore[override]
        return self.elem.size * self.n

    @property
    def align(self) -> int:  # type: ignore[override]
        return self.elem.align

    def fmt(self) -> str:
        return f"list[{self.elem.fmt()}, {self.n}]"


@dataclass(frozen=True)
class StackVec(Type):
    elem: Type
    max_n: int

    @property
    def size(self) -> int:  # type: ignore[override]
        return self.elem.size * self.max_n + 8

    @property
    def align(self) -> int:  # type: ignore[override]
        return max(self.elem.align, 8)

    def fmt(self) -> str:
        return f"StackVec<{self.elem.fmt()}, max={self.max_n}>"


@dataclass(frozen=True)
class Vec(Type):
    elem: Type
    size: int = 24  # {ptr, len, cap}
    align: int = 8

    def fmt(self) -> str:
        return f"Vec<{self.elem.fmt()}>"


@dataclass(frozen=True)
class StrOwned(Vec):
    """Owned heap string produced by concatenation: {u8*, len, cap}, RAII like Vec<u8>."""
    elem: Type = U8

    def fmt(self) -> str:
        return "String"


def is_str(t: Type) -> bool:
    return t == STR_VIEW or isinstance(t, StrOwned)


@dataclass(frozen=True)
class Ptr(Type):
    elem: Type
    size: int = 8
    align: int = 8

    def fmt(self) -> str:
        return f"ptr[{self.elem.fmt()}]"


@dataclass(frozen=True)
class Span(Type):
    elem: Type
    size: int = 16
    align: int = 8

    def fmt(self) -> str:
        return f"span[{self.elem.fmt()}]"


@dataclass(frozen=True)
class StructT(Type):
    name: str
    fields: tuple = ()  # tuple[(field_name, Type), ...]

    @property
    def align(self) -> int:  # type: ignore[override]
        return max((t.align for _, t in self.fields), default=1)

    @property
    def size(self) -> int:  # type: ignore[override]
        off = 0
        for _, t in self.fields:
            off = _round_up(off, t.align) + t.size
        return _round_up(off, self.align)

    def field_type(self, fname: str) -> Optional[Type]:
        for n, t in self.fields:
            if n == fname:
                return t
        return None

    def fmt(self) -> str:
        return self.name


@dataclass
class DynList(Type):
    """Inference-time list whose storage class is decided by escape analysis."""
    elem: Optional[Type] = None
    final: Optional[Type] = None
    line: int = 0

    @property
    def size(self) -> int:  # type: ignore[override]
        return self.final.size if self.final else 0

    def fmt(self) -> str:
        if self.final:
            return self.final.fmt()
        return f"list[{self.elem.fmt() if self.elem else '?'}]"


def _round_up(x: int, a: int) -> int:
    return (x + a - 1) // a * a


_PY_NAMES = {"i64": "int", "f64": "float"}


def display(t: Type) -> str:
    """Python-flavoured spelling: defaults show as int/float; narrow widths (i32 u8 f32) keep their real name."""
    t = resolve(t)
    if isinstance(t, Scalar):
        return _PY_NAMES.get(t.name, t.name)
    if isinstance(t, StrOwned):
        return "String"
    if isinstance(t, FixedList):
        return f"list[{display(t.elem)}, {t.n}]"
    if isinstance(t, StackVec):
        return f"StackVec<{display(t.elem)}, max={t.max_n}>"
    if isinstance(t, Vec):
        return f"Vec<{display(t.elem)}>"
    if isinstance(t, (Ptr, Span)):
        return f"{'ptr' if isinstance(t, Ptr) else 'span'}[{display(t.elem)}]"
    return t.fmt()


def simple_display(t: Type) -> str:
    """Lowest-cognitive-load spelling for default inlay hints: Python names, no sizes / storage classes."""
    t = resolve(t)
    if isinstance(t, (StrOwned, ViewT)) and t.fmt() in ("String", "str_view"):
        return "str"
    if isinstance(t, (FixedList, Vec, StackVec)):
        return f"list[{simple_display(t.elem)}]"
    if isinstance(t, (Ptr, Span)):
        return f"{'ptr' if isinstance(t, Ptr) else 'span'}[{simple_display(t.elem)}]"
    return display(t)


def resolve(t: Type) -> Type:
    """Collapse DynList placeholders to their final storage type."""
    if isinstance(t, DynList):
        return t.final if t.final else Vec(t.elem or UNKNOWN)
    return t


SCALARS = {t.name: t for t in (I32, I64, U8, F32, F64, BOOL, VOID)}
SCALARS["str_view"] = STR_VIEW
SCALARS["bytes_view"] = BYTES_VIEW
SCALARS["int"] = I64          # Python-flavoured aliases of the default widths
SCALARS["float"] = F64

AGGREGATES = (FixedList, StackVec, Vec, StructT)


def is_aggregate(t: Type) -> bool:
    return isinstance(resolve(t), AGGREGATES)


def is_numeric(t: Type) -> bool:
    return isinstance(t, Scalar) and t.kind in ("int", "float")


def promote(a: Type, b: Type) -> Type:
    """Numeric promotion for binary arithmetic."""
    if not (isinstance(a, Scalar) and isinstance(b, Scalar)):
        return UNKNOWN
    if a.kind == "void" or b.kind == "void":
        return UNKNOWN
    if F64 in (a, b):
        return F64
    if F32 in (a, b):
        return F32
    return I64 if I64 in (a, b) else I32


# ---------------------------------------------------------------------------
# Compiler Intrinsics registry
# ---------------------------------------------------------------------------
INTRINSICS = {
    "mut": "mut(x) -> explicit mutable borrow",
    "span": "span(arr, start, len) -> zero-copy view",
    "copy": "copy(dst, src, count) -> llvm.memcpy",
    "alloc": "alloc(T, n) -> ptr[T]",
    "free": "free(p)",
}
MATH_UNARY = ("sin", "cos", "tan", "sqrt", "atan", "exp", "log", "floor", "ceil")
MATH_BINARY = ("atan2",)
BUILTINS_OK = {"range", "len", "min", "max", "abs", "int", "float", "bool", "print", *MATH_UNARY, *MATH_BINARY}
FORBIDDEN_CALLS = {
    "eval", "exec", "compile", "getattr", "setattr", "hasattr", "delattr",
    "globals", "locals", "vars", "type", "__import__", "isinstance",
}
LIST_MUTATORS = {"append", "extend", "insert", "pop", "remove", "clear", "sort", "reverse"}
ALLOWED_IMPORT_ROOTS = {"pa", "pa_lang"}  # plus project-local .paw modules


def parse_annotation(node, structs: dict) -> Type:
    """Translate a Python annotation AST node into a Pa Type."""
    import ast
    if node is None:
        return UNKNOWN
    if isinstance(node, ast.Constant) and node.value is None:
        return VOID
    if isinstance(node, ast.Name):
        if node.id in SCALARS:
            return SCALARS[node.id]
        return structs.get(node.id, UNKNOWN)
    if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name):
        head = node.value.id
        sl = node.slice
        args = list(sl.elts) if isinstance(sl, ast.Tuple) else [sl]
        elem = parse_annotation(args[0], structs)
        if head == "list" and len(args) == 2 and isinstance(args[1], ast.Constant):
            return FixedList(elem, int(args[1].value))
        if head == "list" and len(args) == 1:
            return Vec(elem)
        if head in ("Vec", "ptr", "span"):
            return {"Vec": Vec, "ptr": Ptr, "span": Span}[head](elem)
    return UNKNOWN
