"""AI self-healing diagnostics: structured JSON repair tickets + @struct surgery."""
from __future__ import annotations

import ast
import json
from dataclasses import dataclass, field, asdict
from typing import Optional

# code -> (category, severity, skill_rule_ref, default strategy, anti-patterns)
CATALOG = {
    "PA_E000": ("syntax", "error", "pa-coder#python-syntax",
                "Fix the Python syntax error; Pa files must be valid Python.", []),
    "PA_E001": ("host-isolation", "error", "pa-coder#host-first-isolation",
                "Move the third-party usage into py_workers/<tool>.py and call it via py_call.",
                ["Do not import third-party libs in .pa", "Do not leak dynamic objects into .pa"]),
    "PA_E002": ("batching", "error", "pa-coder#batching-rule",
                "Pack inputs into one batch before the loop, cross the boundary once, then iterate the result.",
                ["Do not call py_call inside for/while", "Do not hide py_call in a helper called from a loop"]),
    "PA_E003": ("syntax-subset", "error", "pa-coder#clean-subset",
                "Rewrite with if/for/while and plain functions only.",
                ["No try/except, yield, closures, lambda, eval/exec, metaclass, reflection, *args/**kwargs"]),
    "PA_E004": ("memory-safety", "error", "pa-coder#alloc-free",
                "Pair every alloc with exactly one free on all exits; never use the pointer afterwards.",
                ["Do not free twice", "Do not return before free"]),
    "PA_E005": ("annotations", "error", "pa-coder#zero-annotation-bodies",
                "Remove annotations in function bodies; annotate only @struct fields and @export signatures.",
                ["No Any/Union/Optional", "No redundant local annotations"]),
    "PA_E011": ("inference", "error", "pa-coder#empty-list-inference",
                "Make the element type inferable: append a value, or use a fixed list like [0.0] * N.",
                ["Do not leave an empty list that is never appended"]),
    "PA_E030": ("layout", "error", "pa-coder#no-heterogeneous-list",
                "Promote the mixed row(s) to a @struct and use a contiguous Vec<Record> (AoS).",
                ["Do not mix element types in a list", "Do not use tuple/dict as a row substitute"]),
    "PA_E031": ("data-type", "error", "pa-coder#data-type-whitelist",
                "Replace dict/set/tuple with a @struct or a homogeneous list.",
                ["No dynamic dict", "No set", "No tuple"]),
    "PA_E040": ("memory-safety", "error", "pa-coder#span-escape",
                "Return an owned Vec<T>/value instead of a span over a local.",
                ["Do not return span() of a local container"]),
    "PA_E050": ("borrow", "error", "pa-coder#mut-borrow",
                "Wrap the argument in mut(...) at the call site because the callee mutates it.",
                ["Do not mutate through an implicit read-only borrow"]),
    "PA_E051": ("readability", "error", "pa-coder#keyword-args",
                "Pass arguments by keyword when a call has more than 2 arguments.",
                ["No long positional argument lists"]),
    "PA_E060": ("readability", "error", "pa-coder#function-size",
                "Split the function into smaller helpers (<= 40 lines).", []),
    "PA_E061": ("readability", "error", "pa-coder#complexity",
                "Reduce branches with guard clauses / helpers (cyclomatic complexity <= 8).", []),
    "PA_E062": ("readability", "error", "pa-coder#nesting",
                "Flatten nesting to <= 3 levels using early returns.", []),
    "PA_E070": ("memory-safety", "error", "pa-coder#no-ptr-alias",
                "Use copy(dst, src, count) or keep a single owner of the pointer.",
                ["Do not alias ptr[T] with bare assignment (double free)"]),
    "PA_E071": ("layout", "error", "pa-coder#no-implicit-heap",
                "Use span(arr, start, len), copy(...) or an explicit loop with append.",
                ["No arr[1:5] slices", "No a + b list concatenation", "No .extend()"]),
    "PA_E072": ("memory-safety", "error", "pa-coder#string-temporaries",
                "Bind the String temporary to a local first (tmp = a + b; f(tmp)) so RAII frees it.",
                ["Do not concatenate or return-by-call a String inside call arguments, comparisons or indexes"]),
    "PA_E073": ("memory-safety", "error", "pa-coder#no-negative-index",
                "Negative indexes are out of range in Pa: write a[len(a) - 1] instead of a[-1].",
                ["No Python-style negative indexing"]),
    "PA_E086": ("inference", "error", "pa-coder#stable-variable-types",
                "Use a new variable name for a value of a different type (a variable keeps one type).",
                ["Do not rebind a name to a different type or a different fixed length"]),
    "PA_E087": ("memory-safety", "error", "pa-coder#no-mutation-while-iterating",
                "Do not modify the list you are iterating over; iterate by index over a fixed range(len(x)) or build a second list.",
                ["No append/mut()/rebind of the iterated list inside the loop body"]),
    "PA_E088": ("definite-assignment", "error", "pa-coder#definite-assignment",
                "Assign the variable on every path before using it (initialise it before the if/loop).",
                ["Do not read a variable that is only assigned inside a branch or a loop body"]),
    "PA_E089": ("borrow", "error", "pa-coder#no-aliasing",
                "Do not pass the same variable twice in one call when one of them is mut(); copy it to a local first.",
                ["No f(v, mut(v))", "No f(mut(v), mut(v))"]),
    "PA_E080": ("boundary", "error", "pa-coder#paw-signature",
                "Give every public .paw function a static In -> Out signature (the 'header').", []),
    "PA_E082": ("boundary", "error", "pa-coder#paw-types",
                "Use only boundary types: scalars, str_view, span[T], list[T, N], Vec[T] (T numeric); return scalar | Vec[T] | void.", []),
    "PA_E083": ("boundary", "error", "pa-coder#paw-return",
                "Make every return path produce the declared return type (no implicit/explicit None, no int for f64).", []),
    "PA_E084": ("boundary", "error", "pa-coder#paw-readonly-input",
                "span/list/Vec parameters are read-only zero-copy views: copy first (np.array(arr)) before writing.", []),
    "PA_E085": ("annotations", "error", "pa-coder#return-annotation",
                "Make the `-> T` annotation match what the body actually returns (or fix the body).", []),
    "PA_E090": ("boundary", "error", "pa-coder#paw-call",
                "Call the .paw function with arguments matching its declared signature.", []),
    "PA_E081": ("annotations", "error", "pa-coder#export-annotations",
                "Annotate every parameter and the return type of @export functions.", []),
    "PA_W001": ("performance", "warning", "pa-coder#large-copy",
                "Pass by read-only borrow or use span(...) to avoid a >4KB memcpy.", []),
}


@dataclass
class Diagnostic:
    code: str
    category: str
    severity: str
    skill_rule_ref: str
    location: dict
    summary: str
    inferred_context: dict
    causal_chain: list
    source_context: list
    repair_plan: dict

    def to_dict(self) -> dict:
        return asdict(self)

    def brief(self) -> str:
        loc = self.location
        return f"{loc['file']}:{loc['line']}:{loc['col']}: {self.severity} {self.code}: {self.summary}"


class DiagnosticSink:
    def __init__(self, source: str = "", filename: str = "<mem>"):
        self.filename = filename
        self.lines = source.splitlines()
        self.items: list[Diagnostic] = []

    def _context(self, line: int) -> list:
        lo, hi = max(1, line - 3), min(len(self.lines), line + 3)
        return [{"line": n, "text": self.lines[n - 1], "mark": n == line} for n in range(lo, hi + 1)]

    def report(self, code: str, node, summary: str, *, inferred_context: Optional[dict] = None,
               causal_chain: Optional[list] = None, strategy: Optional[str] = None,
               patches: Optional[list] = None, line: Optional[int] = None) -> Diagnostic:
        cat, sev, rule, default_strategy, avoid = CATALOG[code]
        ln = line if line is not None else getattr(node, "lineno", 1)
        col = getattr(node, "col_offset", 0) + 1 if node is not None else 1
        d = Diagnostic(
            code=code, category=cat, severity=sev, skill_rule_ref=rule,
            location={"file": self.filename, "line": ln, "col": col},
            summary=summary,
            inferred_context=inferred_context or {},
            causal_chain=causal_chain or [],
            source_context=self._context(ln),
            repair_plan={"strategy": strategy or default_strategy,
                         "anti_patterns_to_avoid": list(avoid),
                         "suggested_patches": patches or []},
        )
        self.items.append(d)
        return d

    @property
    def errors(self) -> list:
        return [d for d in self.items if d.severity == "error"]

    def dump_json(self) -> str:
        return json.dumps([d.to_dict() for d in self.items], ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# @struct surgery for PA_E030
# ---------------------------------------------------------------------------
def struct_surgery(rows: list, struct_name: str = "Record") -> Optional[dict]:
    """Build a deterministic @struct patch from rows of AST nodes (list of list[ast.expr]).

    Each row is a list of element expressions. Columns become fields f0..fn typed by
    their literal values. Returns None if the column types are not consistent across rows.
    """
    from .types import I32, I64, F64, BOOL, STR_VIEW
    def lit_type(e):
        if isinstance(e, ast.Constant):
            v = e.value
            if isinstance(v, bool):
                return BOOL.name
            if isinstance(v, int):
                return I32.name if -2**31 <= v < 2**31 else I64.name
            if isinstance(v, float):
                return F64.name
            if isinstance(v, str):
                return STR_VIEW.name
        if isinstance(e, ast.UnaryOp) and isinstance(e.operand, ast.Constant):
            return lit_type(e.operand)
        return None
    if not rows or not rows[0]:
        return None
    width = len(rows[0])
    cols: list = [None] * width
    for row in rows:
        if len(row) != width:
            return None
        for i, e in enumerate(row):
            t = lit_type(e)
            if t is None or (cols[i] not in (None, t)):
                return None
            cols[i] = t
    fields = "\n".join(f"    f{i}: {t}" for i, t in enumerate(cols))
    struct_src = f"@struct\nclass {struct_name}:\n{fields}\n"
    ctor = ", ".join(
        f"{struct_name}(" + ", ".join(f"f{i}={ast.unparse(e)}" for i, e in enumerate(row)) + ")"
        for row in rows)
    return {"struct_definition": struct_src, "columns": cols,
            "literal": f"[{ctor}]", "target_layout": f"Vec<{struct_name}> (AoS, contiguous)"}
