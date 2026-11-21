"""Host-boundary isolation, batching, borrow, and readability iron laws."""
from __future__ import annotations

import ast
from typing import Iterator, Optional

from .diagnostics import Diagnostic, DiagnosticSink
from .infer import _decorators, analyze
from .paw import find_paw, parse_paw
from .types import ALLOWED_IMPORT_ROOTS, FORBIDDEN_CALLS, INTRINSICS, LIST_MUTATORS

MAX_FUNC_LINES = 40
MAX_COMPLEXITY = 8
MAX_NESTING = 3
BAD_ANN_NAMES = {"Any", "Union", "Optional"}
LOOPS = (ast.For, ast.While)
SKIP_FIELDS = {"annotation", "returns", "decorator_list"}


def walk_code(node) -> Iterator[ast.AST]:
    """ast.walk that skips annotation/decorator subtrees."""
    stack = [node]
    while stack:
        n = stack.pop()
        yield n
        for name, value in ast.iter_fields(n):
            if name in SKIP_FIELDS:
                continue
            for c in (value if isinstance(value, list) else [value]):
                if isinstance(c, ast.AST):
                    stack.append(c)


def complexity(fn: ast.FunctionDef) -> int:
    c = 1
    for n in walk_code(fn):
        if isinstance(n, (ast.If, ast.For, ast.While, ast.IfExp, ast.comprehension)):
            c += 1
        elif isinstance(n, ast.BoolOp):
            c += len(n.values) - 1
    return c


def max_nesting(stmts, depth: int = 0) -> tuple:
    """Return (deepest level, node at that level)."""
    best = (depth, None)
    for s in stmts:
        if isinstance(s, (ast.If, ast.For, ast.While)):
            inner = [s.body, getattr(s, "orelse", [])]
            for blk in inner:
                cand = max_nesting(blk, depth + 1)
                if cand[0] > best[0]:
                    best = (cand[0], s if cand[1] is None else cand[1])
            if depth + 1 > best[0]:
                best = (depth + 1, s)
    return best


# ---------------------------------------------------------------------------
class Validator:
    def __init__(self, tree: ast.Module, sink: DiagnosticSink, search_dirs=None):
        self.tree, self.sink = tree, sink
        self.search_dirs = search_dirs or []
        self.paw_aliases = self.collect_paw_aliases()
        self.funcs = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
        for n in ast.walk(tree):
            if isinstance(n, ast.Subscript) and isinstance(n.slice, ast.Tuple):
                n.slice._in_subscript = True
        self.mutated = self.compute_mutated()
        self.pycalling = self.compute_pycalling()

    def run(self) -> None:
        self.check_imports()
        for n in walk_code(self.tree):
            self.check_syntax_node(n)
        for fn in self.funcs.values():
            self.check_function(fn)
        for n in self.tree.body:
            if isinstance(n, ast.ClassDef):
                self.check_class(n)

    # ---- imports -----------------------------------------------------
    def collect_paw_aliases(self) -> set:
        out = set()
        for n in self.tree.body:
            if isinstance(n, ast.Import):
                out |= {a.asname or a.name for a in n.names if find_paw(a.name, self.search_dirs)}
        return out

    def check_imports(self) -> None:
        for n in ast.walk(self.tree):
            mods = []
            if isinstance(n, ast.Import):
                mods = [a.name for a in n.names]
            elif isinstance(n, ast.ImportFrom):
                mods = [n.module or ""] if n.level == 0 else ["."]
            for m in mods:
                root = m.split(".")[0]
                if root in ALLOWED_IMPORT_ROOTS:
                    continue
                if isinstance(n, ast.Import) and find_paw(m, self.search_dirs):
                    continue
                self.sink.report("PA_E001", n, f"'{m}' import is forbidden in a .pa module (only pa, pa_lang and project .paw modules)",
                                 inferred_context={"module": m},
                                 causal_chain=[f"{m} is a dynamic Python library",
                                               ".pa modules must compile to pure native code",
                                               "only .paw modules may import third-party libs"],
                                 patches=[{"action": "create_file", "path": f"{root}_ops.paw",
                                           "replacement": f"import {m}\n\ndef run(batch: span[f64]) -> f64:\n    ...\n"},
                                          {"line": n.lineno, "replacement": f"import {root}_ops"}])

    # ---- node-level syntax ------------------------------------------
    def check_syntax_node(self, n) -> None:
        ban = {ast.Try: "try/except", ast.Lambda: "lambda", ast.Yield: "yield", ast.YieldFrom: "yield from",
               ast.With: "with", ast.Await: "await", ast.Global: "global", ast.Nonlocal: "nonlocal",
               ast.Delete: "del"}
        if hasattr(ast, "TryStar"):
            ban[ast.TryStar] = "try/except*"
        for t, label in ban.items():
            if isinstance(n, t):
                self.sink.report("PA_E003", n, f"'{label}' is outside the clean Pa subset",
                                 inferred_context={"construct": label},
                                 causal_chain=[f"{label} needs a runtime/GC model Pa does not have"])
        if isinstance(n, ast.Starred):
            self.sink.report("PA_E003", n, "'*args' unpacking is outside the clean Pa subset",
                             inferred_context={"construct": "starred"})
        if isinstance(n, ast.Call):
            self.check_call(n)
        if isinstance(n, (ast.Dict, ast.Set, ast.Tuple, ast.DictComp, ast.SetComp)):
            self.check_data_literal(n)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "extend":
            self.sink.report("PA_E071", n, ".extend() performs an implicit heap re-allocation",
                             causal_chain=["extend() grows a list from another list with hidden realloc"],
                             patches=[{"line": n.lineno, "replacement": "for x in src:\n    dst.append(x)"}])

    def check_data_literal(self, n) -> None:
        if isinstance(n, ast.Tuple) and getattr(n, "_in_subscript", False):
            return
        label = type(n).__name__.lower()
        self.sink.report("PA_E031", n, f"{label} is not an allowed Pa data type",
                         inferred_context={"construct": label},
                         causal_chain=[f"{label} requires heterogeneous/dynamic storage"])

    def check_call(self, n: ast.Call) -> None:
        if isinstance(n.func, ast.Name):
            name = n.func.id
            if name in FORBIDDEN_CALLS:
                self.sink.report("PA_E003", n, f"'{name}()' (eval/reflection) is forbidden",
                                 inferred_context={"construct": name})
            if name in ("dict", "set", "tuple"):
                self.sink.report("PA_E031", n, f"{name}() is not an allowed Pa data type",
                                 inferred_context={"construct": name})
            if name not in INTRINSICS and len(n.args) + len(n.keywords) > 2 and len(n.args) > 2 \
                    and name in self.funcs:
                self.sink.report("PA_E051", n, f"call to '{name}' passes {len(n.args)} positional arguments",
                                 inferred_context={"callee": name})
            if name in self.funcs:
                self.check_mut_args(n, name)
                self.check_alias(n, name)

    def check_mut_args(self, n: ast.Call, name: str) -> None:
        params = [a.arg for a in self.funcs[name].args.args]
        bound = list(zip(params, n.args)) + [(k.arg, k.value) for k in n.keywords]
        for p, arg in bound:
            if p in self.mutated.get(name, set()) and not self.is_mut_call(arg):
                self.sink.report("PA_E050", n, f"'{name}' mutates parameter '{p}' but the call passes it read-only",
                                 inferred_context={"callee": name, "param": p},
                                 causal_chain=[f"{name}() writes to '{p}'", "default container borrow is const T&"],
                                 patches=[{"line": n.lineno, "original": ast.unparse(arg),
                                           "replacement": f"mut({ast.unparse(arg)})"}])

    @staticmethod
    def root_name(e) -> Optional[str]:
        while isinstance(e, (ast.Attribute, ast.Subscript)):
            e = e.value
        return e.id if isinstance(e, ast.Name) else None

    def check_alias(self, n: ast.Call, name: str) -> None:
        """PA_E089: the same variable passed twice while one occurrence is mut(...)."""
        args = list(n.args) + [k.value for k in n.keywords]
        muts = [a.args[0].id for a in args if self.is_mut_call(a) and a.args and isinstance(a.args[0], ast.Name)]
        roots = [a.args[0].id if self.is_mut_call(a) and a.args and isinstance(a.args[0], ast.Name) else self.root_name(a)
                 for a in args]
        for m in dict.fromkeys(muts):
            if roots.count(m) > 1:
                self.sink.report("PA_E089", n, f"'{m}' is passed to '{name}' more than once and one of them is mut()",
                                 inferred_context={"variable": m, "callee": name},
                                 causal_chain=[f"{name}() receives '{m}' through two parameters", "one of them may append/realloc",
                                               "the other view would dangle (use-after-free)"],
                                 patches=[{"line": n.lineno, "replacement": f"copy_of_{m} = {m}\n{name}(... mut({m}) ..., copy_of_{m})"}])

    def check_negative_index(self, fn) -> None:
        for n in walk_code(fn):
            if not isinstance(n, ast.Subscript) or isinstance(n.slice, ast.Slice):
                continue
            idxs = n.slice.elts if isinstance(n.slice, ast.Tuple) else [n.slice]
            for ix in idxs:
                if isinstance(ix, ast.UnaryOp) and isinstance(ix.op, ast.USub) and isinstance(ix.operand, ast.Constant) \
                        and isinstance(ix.operand.value, (int, float)):
                    self.sink.report("PA_E073", n, f"negative index -{ix.operand.value} is out of range in Pa",
                                     causal_chain=["Pa does not fold negative indexes into len + i"],
                                     patches=[{"line": n.lineno, "replacement":
                                               f"{ast.unparse(n.value)}[len({ast.unparse(n.value)}) - {ix.operand.value}]"}])

    def check_mutation_while_iterating(self, fn) -> None:
        for loop in (n for n in walk_code(fn) if isinstance(n, ast.For) and isinstance(n.iter, ast.Name)):
            name = loop.iter.id
            for st in loop.body:
                for n in walk_code(st):
                    why = None
                    if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr in LIST_MUTATORS \
                            and isinstance(n.func.value, ast.Name) and n.func.value.id == name:
                        why = f".{n.func.attr}() on"
                    elif isinstance(n, ast.Call) and self.is_mut_call(n) and n.args and isinstance(n.args[0], ast.Name) \
                            and n.args[0].id == name:
                        why = "mut(...) of"
                    elif isinstance(n, (ast.Assign, ast.AugAssign)) and any(
                            isinstance(t, ast.Name) and t.id == name
                            for t in (n.targets if isinstance(n, ast.Assign) else [n.target])):
                        why = "rebinding"
                    if why:
                        self.sink.report("PA_E087", n, f"{why} '{name}' inside 'for ... in {name}' (iteration can dangle)",
                                         inferred_context={"list": name, "loop_line": loop.lineno},
                                         causal_chain=[f"loop over '{name}' at line {loop.lineno} caches its data pointer",
                                                       "growing or replacing the list reallocates it"])

    # ---- definite assignment (PA_E088) -----------------------------------
    def check_definite_assignment(self, fn) -> None:
        local = set()
        for n in walk_code(fn):
            if isinstance(n, ast.Assign):
                local |= {t.id for t in n.targets if isinstance(t, ast.Name)}
            elif isinstance(n, ast.AugAssign) and isinstance(n.target, ast.Name):
                local.add(n.target.id)
            elif isinstance(n, ast.For) and isinstance(n.target, ast.Name):
                local.add(n.target.id)
        params = {a.arg for a in fn.args.posonlyargs + fn.args.args + fn.args.kwonlyargs}
        reported = set()

        def use(expr, A) -> None:
            for x in walk_code(expr):
                if isinstance(x, ast.Name) and isinstance(x.ctx, ast.Load) and x.id in local \
                        and x.id not in params and x.id not in A and (x.id, x.lineno) not in reported:
                    reported.add((x.id, x.lineno))
                    self.sink.report("PA_E088", x, f"'{x.id}' may be used before it is assigned",
                                     inferred_context={"variable": x.id},
                                     causal_chain=[f"'{x.id}' is only assigned inside a branch or loop body (or later)",
                                                   "another path reaches this use with it uninitialised"],
                                     patches=[{"line": fn.lineno + 1, "replacement": f"{x.id} = 0  # initialise before the branch/loop"}])

        def flow(stmts, A):
            A = set(A)
            for s in stmts:
                if isinstance(s, ast.Assign):
                    use(s.value, A)
                    for t in s.targets:
                        if isinstance(t, ast.Name):
                            A.add(t.id)
                        else:
                            use(t, A)
                elif isinstance(s, ast.AugAssign):
                    use(s.value, A)
                    use(s.target if not isinstance(s.target, ast.Name) else ast.Name(s.target.id, ast.Load(), lineno=s.lineno, col_offset=s.col_offset), A)
                elif isinstance(s, ast.Expr):
                    use(s.value, A)
                elif isinstance(s, ast.Return):
                    if s.value is not None:
                        use(s.value, A)
                    return A, True
                elif isinstance(s, (ast.Break, ast.Continue)):
                    return A, True
                elif isinstance(s, ast.If):
                    use(s.test, A)
                    a1, t1 = flow(s.body, A)
                    a2, t2 = flow(s.orelse, A)
                    if t1 and t2:
                        return A, True
                    A = a2 if t1 else a1 if t2 else (a1 & a2)
                elif isinstance(s, ast.While):
                    use(s.test, A)
                    flow(s.body, A)
                elif isinstance(s, ast.For):
                    use(s.iter, A)
                    flow(s.body, A | ({s.target.id} if isinstance(s.target, ast.Name) else set()))
            return A, False

        flow(fn.body, set(params))

    @staticmethod
    def is_mut_call(e) -> bool:
        return isinstance(e, ast.Call) and isinstance(e.func, ast.Name) and e.func.id == "mut"

    # ---- per-function ------------------------------------------------
    def check_function(self, fn: ast.FunctionDef) -> None:
        self.check_negative_index(fn)
        self.check_mutation_while_iterating(fn)
        self.check_definite_assignment(fn)
        self.check_signature(fn)
        self.check_body_annotations(fn)
        self.check_size(fn)
        self.check_py_call_loops(fn)
        self.check_closures(fn)

    def check_signature(self, fn) -> None:
        exported = "export" in _decorators(fn)
        a = fn.args
        if a.vararg or a.kwarg:
            self.sink.report("PA_E003", fn, "*args/**kwargs are outside the clean Pa subset",
                             inferred_context={"function": fn.name})
        params = a.posonlyargs + a.args + a.kwonlyargs
        if exported:
            if any(p.annotation is None for p in params) or fn.returns is None:
                self.sink.report("PA_E081", fn, f"@export '{fn.name}' needs full parameter/return annotations",
                                 inferred_context={"function": fn.name})
            self.check_ann_nodes([p.annotation for p in params] + [fn.returns], fn)
        else:
            for p in params:
                if p.annotation is not None:
                    self.sink.report("PA_E005", p, f"parameter '{p.arg}' of non-export '{fn.name}' is annotated",
                                     inferred_context={"function": fn.name},
                                     patches=[{"line": p.lineno, "replacement": p.arg}])
            # a return annotation (the function's "Out") is allowed everywhere and checked against inference
            self.check_ann_nodes([fn.returns], fn)

    def check_ann_nodes(self, anns, fn) -> None:
        for ann in anns:
            if ann is None:
                continue
            for x in ast.walk(ann):
                bad = (isinstance(x, ast.Name) and x.id in BAD_ANN_NAMES) or \
                      (isinstance(x, ast.BinOp) and isinstance(x.op, ast.BitOr))
                if bad:
                    self.sink.report("PA_E005", x, "Any/Union/Optional is not allowed in annotations",
                                     inferred_context={"function": fn.name})

    def check_body_annotations(self, fn) -> None:
        for n in walk_code(fn):
            if isinstance(n, ast.AnnAssign):
                self.sink.report("PA_E005", n, "annotation inside a function body is redundant noise",
                                 inferred_context={"target": ast.unparse(n.target), "annotation": ast.unparse(n.annotation)},
                                 causal_chain=["types in bodies are statically inferred"],
                                 patches=[{"line": n.lineno, "replacement":
                                           f"{ast.unparse(n.target)} = {ast.unparse(n.value)}" if n.value else "# removed"}])

    def check_size(self, fn) -> None:
        lines = fn.end_lineno - fn.lineno + 1
        if lines > MAX_FUNC_LINES:
            self.sink.report("PA_E060", fn, f"function '{fn.name}' is {lines} lines (max {MAX_FUNC_LINES})",
                             inferred_context={"lines": lines})
        cx = complexity(fn)
        if cx > MAX_COMPLEXITY:
            self.sink.report("PA_E061", fn, f"function '{fn.name}' has cyclomatic complexity {cx} (max {MAX_COMPLEXITY})",
                             inferred_context={"complexity": cx})
        depth, node = max_nesting(fn.body)
        if depth > MAX_NESTING:
            self.sink.report("PA_E062", node or fn, f"function '{fn.name}' nests {depth} levels (max {MAX_NESTING})",
                             inferred_context={"depth": depth})

    def check_closures(self, fn) -> None:
        for n in ast.walk(fn):
            if n is not fn and isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self.sink.report("PA_E003", n, f"nested function '{n.name}' (closure) is forbidden",
                                 inferred_context={"outer": fn.name})
            if isinstance(n, ast.ClassDef):
                self.sink.report("PA_E003", n, "class definition inside a function is forbidden")

    def check_class(self, c: ast.ClassDef) -> None:
        for kw in c.keywords:
            self.sink.report("PA_E003", c, f"class keyword '{kw.arg}=' (metaclass) is forbidden")
        if "struct" not in _decorators(c):
            self.sink.report("PA_E003", c, f"class '{c.name}' must be a @struct (no methods/inheritance)")
            return
        for st in c.body:
            if not isinstance(st, ast.AnnAssign) and not (isinstance(st, ast.Expr) and isinstance(st.value, ast.Constant)):
                self.sink.report("PA_E003", st, "@struct bodies may contain only annotated fields")
        self.check_ann_nodes([s.annotation for s in c.body if isinstance(s, ast.AnnAssign)], c)

    # ---- batching rule -----------------------------------------------
    def compute_pycalling(self) -> set:
        direct = {n for n, f in self.funcs.items() if any(self.is_py_call(x) for x in walk_code(f))}
        changed = True
        while changed:
            changed = False
            for n, f in self.funcs.items():
                if n in direct:
                    continue
                if any(isinstance(x, ast.Call) and isinstance(x.func, ast.Name) and x.func.id in direct
                       for x in walk_code(f)):
                    direct.add(n)
                    changed = True
        return direct

    def is_py_call(self, n) -> bool:
        """A boundary crossing: py_call(...) or a call into an imported .paw module."""
        if not isinstance(n, ast.Call):
            return False
        f = n.func
        if isinstance(f, ast.Name):
            return f.id == "py_call"
        return isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name) and f.value.id in self.paw_aliases

    def check_py_call_loops(self, fn) -> None:
        for loop in (n for n in walk_code(fn) if isinstance(n, LOOPS + (ast.ListComp,))):
            for n in walk_code(loop):
                if not isinstance(n, ast.Call):
                    continue
                if self.is_py_call(n) or (isinstance(n.func, ast.Name) and n.func.id in self.pycalling):
                    via = "" if self.is_py_call(n) else f" (via '{n.func.id}')"
                    self.sink.report("PA_E002", n, f"boundary call (py_call/.paw) inside a loop{via} in '{fn.name}'",
                                     inferred_context={"function": fn.name, "loop_line": loop.lineno},
                                     causal_chain=[f"loop at line {loop.lineno}", f"py_call at line {n.lineno}{via}",
                                                   "each iteration crosses the Pa<->Python boundary"],
                                     patches=[{"line": loop.lineno, "replacement":
                                               "batch = [...]  # pack inputs first\nresult = py_call(\"worker.fn\", batch)  # single crossing"}])
                    break

    # ---- mutation analysis ------------------------------------------
    def compute_mutated(self) -> dict:
        direct = {n: self.direct_mutations(f) for n, f in self.funcs.items()}
        changed = True
        while changed:
            changed = False
            for n, f in self.funcs.items():
                for c in walk_code(f):
                    if isinstance(c, ast.Call) and isinstance(c.func, ast.Name) and c.func.id in direct:
                        params = [a.arg for a in self.funcs[c.func.id].args.args]
                        for p, arg in list(zip(params, c.args)) + [(k.arg, k.value) for k in c.keywords]:
                            if p in direct[c.func.id] and self.is_mut_call(arg) and arg.args \
                                    and isinstance(arg.args[0], ast.Name):
                                base = arg.args[0].id
                                if base in {a.arg for a in f.args.args} and base not in direct[n]:
                                    direct[n].add(base)
                                    changed = True
        return direct

    @staticmethod
    def direct_mutations(fn) -> set:
        params = {a.arg for a in fn.args.args}
        out = set()
        for n in walk_code(fn):
            tgt = []
            if isinstance(n, ast.Assign):
                tgt = n.targets
            elif isinstance(n, ast.AugAssign):
                tgt = [n.target]
            for t in tgt:
                if isinstance(t, (ast.Subscript, ast.Attribute)) and isinstance(t.value, ast.Name) \
                        and t.value.id in params:
                    out.add(t.value.id)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr in LIST_MUTATORS \
                    and isinstance(n.func.value, ast.Name) and n.func.value.id in params:
                out.add(n.func.value.id)
        return out


# ---------------------------------------------------------------------------
def default_search_dirs(filename: str) -> list:
    import os
    d = os.path.dirname(os.path.abspath(filename)) if filename and not filename.startswith("<") else os.getcwd()
    return [d]


def check_source(source: str, filename: str = "<mem>", kind: Optional[str] = None, search_dirs=None,
                 mode: str = "dev") -> list:
    """Run all front-end checks. kind: 'pa' (default) or 'paw' (inferred from *.paw).

    mode 'dev' additionally runs the deep static In -> Out lint of .paw bodies; 'prod' only checks signatures.
    """
    sink = DiagnosticSink(source, filename)
    try:
        tree = ast.parse(source, filename)
    except SyntaxError as e:
        sink.report("PA_E000", None, f"syntax error: {e.msg}", line=e.lineno or 1)
        return sink.items
    kind = kind or ("paw" if filename.endswith(".paw") else "pa")
    if kind == "paw":
        parse_paw(source, filename, sink, lint=(mode == "dev"))
    else:
        dirs = search_dirs if search_dirs is not None else default_search_dirs(filename)
        Validator(tree, sink, dirs).run()
        analyze(tree, sink, source, filename, dirs)
    sink.items.sort(key=lambda d: (d.location["line"], d.code))
    return sink.items


def check_file(path: str, mode: str = "dev") -> list:
    with open(path, encoding="utf-8") as f:
        return check_source(f.read(), path, mode=mode)
