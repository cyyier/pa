"""Multi-file .pa module discovery, cycle detection, and whole-program AST linking.

Because Pa uses whole-program call-site type inference (non-export functions keep
unannotated parameters), imported .pa modules are linked at the AST level with
deterministic name mangling before type inference and LLVM IR generation.
"""
from __future__ import annotations

import ast
import copy
import importlib.metadata
import importlib.util
import os
from dataclasses import dataclass, field
from typing import Optional

from .diagnostics import DiagnosticSink
from .paw import find_paw
from .types import ALLOWED_IMPORT_ROOTS


def extra_pa_search_dirs() -> list[str]:
    """Return search directories from PA_PATH and installed pa_lang.libs entry points."""
    dirs: list[str] = []
    env_path = os.environ.get("PA_PATH", "")
    if env_path:
        for p in env_path.split(os.pathsep):
            p = p.strip()
            if p and os.path.isdir(p) and p not in dirs:
                dirs.append(os.path.abspath(p))
    try:
        eps = importlib.metadata.entry_points(group="pa_lang.libs")
    except Exception:
        eps = ()
    for ep in eps:
        try:
            val = ep.load()
            if callable(val):
                val = val()
            paths = [val] if isinstance(val, (str, os.PathLike)) else list(val)
            for p in paths:
                sp = os.path.abspath(os.fspath(p))
                if os.path.isdir(sp) and sp not in dirs:
                    dirs.append(sp)
        except Exception:
            continue
    return dirs


def _probe_pa_in_dir(base: str, parts: list[str]) -> Optional[str]:
    if not parts:
        p = os.path.join(base, "__init__.pa")
        return os.path.abspath(p) if os.path.isfile(p) else None
    cand1 = os.path.join(base, *parts) + ".pa"
    if os.path.isfile(cand1):
        return os.path.abspath(cand1)
    cand2 = os.path.join(base, *parts, "__init__.pa")
    if os.path.isfile(cand2):
        return os.path.abspath(cand2)
    return None


def find_pa(name: str, search_dirs: list[str], level: int = 0) -> Optional[str]:
    """Locate a .pa module by dotted name (or relative import level) across local dirs, PA_PATH, and installed packages."""
    parts = [p for p in name.split(".") if p] if name else []
    if level > 0:
        if not search_dirs:
            return None
        base = os.path.abspath(search_dirs[0])
        for _ in range(level - 1):
            base = os.path.dirname(base)
        return _probe_pa_in_dir(base, parts)

    if not parts:
        return None
    all_dirs = list(search_dirs) + [d for d in extra_pa_search_dirs() if d not in search_dirs]
    for d in all_dirs:
        hit = _probe_pa_in_dir(d, parts)
        if hit:
            return hit

    # Discover .pa files shipped inside installed Python packages (e.g. `pip install pa-libs`)
    try:
        spec = importlib.util.find_spec(parts[0])
    except (ImportError, ValueError, ModuleNotFoundError):
        spec = None
    if spec and spec.submodule_search_locations:
        subparts = parts[1:]
        for loc in spec.submodule_search_locations:
            hit = _probe_pa_in_dir(loc, subparts)
            if hit:
                return hit
    return None


def is_pa_import_node(node: ast.AST, search_dirs: list[str]) -> bool:
    """Return True if an Import / ImportFrom node resolves completely to .pa modules."""
    if isinstance(node, ast.Import):
        return bool(node.names) and all(find_pa(a.name, search_dirs) is not None for a in node.names)
    if isinstance(node, ast.ImportFrom):
        mod = node.module or ""
        if mod and find_pa(mod, search_dirs, level=node.level):
            return True
        if node.names:
            all_sub = True
            for a in node.names:
                if a.name == "*":
                    return False
                sub = f"{mod}.{a.name}" if mod else a.name
                if find_pa(sub, search_dirs, level=node.level) is None:
                    all_sub = False
                    break
            if all_sub:
                return True
    return False


@dataclass
class _ModRecord:
    path: str
    real_path: str
    prefix: str
    tree: ast.Module
    source: str
    exports: dict[str, str] = field(default_factory=dict)          # public name -> mangled name
    own_defs: dict[str, str] = field(default_factory=dict)         # local top-level def -> mangled name
    direct_imports: dict[str, str] = field(default_factory=dict)   # imported symbol -> mangled name
    mod_aliases: dict[tuple[str, ...], "_ModRecord"] = field(default_factory=dict)
    paw_remap: dict[str, str] = field(default_factory=dict)        # local paw alias -> linked paw alias


def _decorators(node) -> set[str]:
    return {d.id for d in getattr(node, "decorator_list", ()) if isinstance(d, ast.Name)}


def _collect_local_bindings(fn: ast.FunctionDef) -> set[str]:
    names = {a.arg for a in fn.args.posonlyargs + fn.args.args + fn.args.kwonlyargs}
    for n in ast.walk(fn):
        if isinstance(n, ast.Assign):
            for t in n.targets:
                if isinstance(t, ast.Name):
                    names.add(t.id)
        elif isinstance(n, ast.AugAssign) and isinstance(n.target, ast.Name):
            names.add(n.target.id)
        elif isinstance(n, ast.For) and isinstance(n.target, ast.Name):
            names.add(n.target.id)
    return names


def _attr_chain(node: ast.AST) -> Optional[list[str]]:
    parts: list[str] = []
    cur = node
    while isinstance(cur, ast.Attribute):
        parts.append(cur.attr)
        cur = cur.value
    if isinstance(cur, ast.Name):
        parts.append(cur.id)
        parts.reverse()
        return parts
    return None


class _ModuleRewriter(ast.NodeTransformer):
    def __init__(self, rec: _ModRecord, is_entry: bool, sink: Optional[DiagnosticSink]):
        self.rec = rec
        self.is_entry = is_entry
        self.sink = sink
        self.scopes: list[set[str]] = []

    def _is_shadowed(self, name: str) -> bool:
        return any(name in sc for sc in self.scopes)

    def _rewrite_type_expr(self, node: Optional[ast.AST]) -> Optional[ast.AST]:
        if node is None:
            return None
        # Type annotations are resolved at module scope, not shadowed by function locals
        saved = self.scopes
        self.scopes = []
        try:
            return self.visit(node)
        finally:
            self.scopes = saved

    def visit_FunctionDef(self, node: ast.FunctionDef) -> ast.FunctionDef:
        if not self.is_entry:
            node.decorator_list = [
                d for d in node.decorator_list
                if not (isinstance(d, ast.Name) and d.id == "export")
            ]
            if node.name in self.rec.own_defs:
                node.name = self.rec.own_defs[node.name]
        node.returns = self._rewrite_type_expr(node.returns)
        for a in node.args.posonlyargs + node.args.args + node.args.kwonlyargs:
            a.annotation = self._rewrite_type_expr(a.annotation)
        self.scopes.append(_collect_local_bindings(node))
        try:
            node.body = [self.visit(s) for s in node.body]
        finally:
            self.scopes.pop()
        return node

    def visit_ClassDef(self, node: ast.ClassDef) -> ast.ClassDef:
        if not self.is_entry and node.name in self.rec.own_defs:
            node.name = self.rec.own_defs[node.name]
        for st in node.body:
            if isinstance(st, ast.AnnAssign):
                st.annotation = self._rewrite_type_expr(st.annotation)
        return node

    def visit_Attribute(self, node: ast.Attribute) -> ast.AST:
        chain = _attr_chain(node)
        if chain and len(chain) >= 2 and not self._is_shadowed(chain[0]):
            mod_key = tuple(chain[:-1])
            sym = chain[-1]
            target = self.rec.mod_aliases.get(mod_key)
            if target is not None:
                if sym in target.exports:
                    rep = ast.Name(id=target.exports[sym], ctx=node.ctx)
                    return ast.copy_location(rep, node)
                if self.sink is not None:
                    self.sink.report(
                        "PA_E001", node,
                        f"module '{'.'.join(mod_key)}' has no symbol '{sym}'",
                        inferred_context={"module": ".".join(mod_key), "symbol": sym},
                    )
                return node
        node.value = self.visit(node.value)
        return node

    def visit_Name(self, node: ast.Name) -> ast.AST:
        if self._is_shadowed(node.id):
            return node
        if node.id in self.rec.direct_imports:
            node.id = self.rec.direct_imports[node.id]
        elif node.id in self.rec.paw_remap:
            node.id = self.rec.paw_remap[node.id]
        elif not self.is_entry and node.id in self.rec.own_defs:
            node.id = self.rec.own_defs[node.id]
        return node


def _has_pa_imports(tree: ast.Module, search_dirs: list[str]) -> bool:
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            if isinstance(node, ast.Import):
                for a in node.names:
                    if a.name.split(".")[0] in ALLOWED_IMPORT_ROOTS:
                        continue
                    if find_pa(a.name, search_dirs):
                        return True
            else:
                root = (node.module or "").split(".")[0]
                if node.level == 0 and root in ALLOWED_IMPORT_ROOTS:
                    continue
                if is_pa_import_node(node, search_dirs):
                    return True
    return False


def link_modules(
    tree: ast.Module,
    filename: str,
    search_dirs: list[str],
    sink: Optional[DiagnosticSink] = None,
) -> tuple[ast.Module, list[str], dict[str, str]]:
    """Resolve and link all transitive .pa imports into a single whole-program AST.

    Returns (linked_tree, imported_pa_paths, paw_paths_by_alias).
    If the module has no .pa imports, returns (tree, [], {}) untouched.
    """
    if not _has_pa_imports(tree, search_dirs):
        return tree, [], {}

    loaded: dict[str, _ModRecord] = {}
    order: list[_ModRecord] = []
    visiting: list[str] = []
    paw_paths: dict[str, str] = {}
    counter = [0]

    def collect_own_defs(mod_tree: ast.Module, prefix: str, is_entry: bool) -> dict[str, str]:
        defs: dict[str, str] = {}
        for st in mod_tree.body:
            if isinstance(st, ast.FunctionDef):
                defs[st.name] = st.name if is_entry else f"{prefix}{st.name}"
            elif isinstance(st, ast.ClassDef) and "struct" in _decorators(st):
                defs[st.name] = st.name if is_entry else f"{prefix}{st.name}"
            elif isinstance(st, ast.Assign) and len(st.targets) == 1 and isinstance(st.targets[0], ast.Name):
                n = st.targets[0].id
                defs[n] = n if is_entry else f"{prefix}{n}"
        return defs

    def load_one(mod_path: str, mod_tree: Optional[ast.Module], is_entry: bool) -> _ModRecord:
        real = os.path.realpath(mod_path) if not mod_path.startswith("<") else mod_path
        if real in loaded:
            return loaded[real]
        if real in visiting:
            cycle = " -> ".join(os.path.basename(p) for p in visiting + [real])
            if sink is not None:
                sink.report("PA_E001", None, f"circular .pa import detected: {cycle}", line=1)
            empty = ast.Module(body=[], type_ignores=[])
            return _ModRecord(mod_path, real, "", empty, "")

        visiting.append(real)
        if mod_tree is None:
            with open(mod_path, encoding="utf-8") as f:
                src = f.read()
            try:
                parsed = ast.parse(src, mod_path)
            except SyntaxError as e:
                if sink is not None:
                    sink.report("PA_E000", None, f"syntax error in '{mod_path}': {e.msg}", line=e.lineno or 1)
                parsed = ast.Module(body=[], type_ignores=[])
        else:
            src = ""
            parsed = copy.deepcopy(mod_tree)

        prefix = ""
        if not is_entry:
            prefix = f"__pa_m{counter[0]}__"
            counter[0] += 1

        rec = _ModRecord(mod_path, real, prefix, parsed, src)
        rec.own_defs = collect_own_defs(parsed, prefix, is_entry)
        rec.exports.update(rec.own_defs)

        mod_dir = os.path.dirname(os.path.abspath(mod_path)) if not mod_path.startswith("<") else (
            search_dirs[0] if search_dirs else os.getcwd()
        )
        local_dirs = [mod_dir] + [d for d in search_dirs if d != mod_dir]

        new_body: list[ast.stmt] = []
        for st in parsed.body:
            if isinstance(st, ast.Import):
                kept_aliases = []
                for a in st.names:
                    root = a.name.split(".")[0]
                    if root in ALLOWED_IMPORT_ROOTS:
                        kept_aliases.append(a)
                        continue
                    pa_hit = find_pa(a.name, local_dirs)
                    if pa_hit:
                        dep = load_one(pa_hit, None, is_entry=False)
                        key = (a.asname,) if a.asname else tuple(a.name.split("."))
                        rec.mod_aliases[key] = dep
                        continue
                    paw_hit = find_paw(a.name, local_dirs)
                    if paw_hit:
                        local_alias = a.asname or a.name
                        linked_alias = local_alias if is_entry else f"{prefix}paw_{local_alias}"
                        paw_paths[linked_alias] = os.path.abspath(paw_hit)
                        if linked_alias != local_alias:
                            rec.paw_remap[local_alias] = linked_alias
                        continue
                    kept_aliases.append(a)
                if kept_aliases:
                    st.names = kept_aliases
                    new_body.append(st)
                continue

            if isinstance(st, ast.ImportFrom):
                mod = st.module or ""
                root = mod.split(".")[0]
                if st.level == 0 and root in ALLOWED_IMPORT_ROOTS:
                    new_body.append(st)
                    continue
                pa_hit = find_pa(mod, local_dirs, level=st.level) if mod else None
                if pa_hit:
                    dep = load_one(pa_hit, None, is_entry=False)
                    for a in st.names:
                        local_name = a.asname or a.name
                        if a.name in dep.exports:
                            rec.direct_imports[local_name] = dep.exports[a.name]
                            rec.exports[local_name] = dep.exports[a.name]
                        else:
                            # Also check if a.name is a submodule under mod
                            sub = f"{mod}.{a.name}" if mod else a.name
                            sub_hit = find_pa(sub, local_dirs, level=st.level)
                            if sub_hit:
                                sub_dep = load_one(sub_hit, None, is_entry=False)
                                rec.mod_aliases[(local_name,)] = sub_dep
                            elif sink is not None:
                                sink.report(
                                    "PA_E001", st,
                                    f"module '{mod}' has no symbol '{a.name}'",
                                    inferred_context={"module": mod, "symbol": a.name},
                                )
                    continue
                # Try `from pkg import submod` or `from . import submod`
                resolved_any = False
                for a in st.names:
                    if a.name == "*":
                        continue
                    sub = f"{mod}.{a.name}" if mod else a.name
                    sub_hit = find_pa(sub, local_dirs, level=st.level)
                    if sub_hit:
                        sub_dep = load_one(sub_hit, None, is_entry=False)
                        rec.mod_aliases[(a.asname or a.name,)] = sub_dep
                        resolved_any = True
                if resolved_any:
                    continue
                new_body.append(st)
                continue

            new_body.append(st)

        parsed.body = new_body
        rewriter = _ModuleRewriter(rec, is_entry=is_entry, sink=sink)
        rewritten_body: list[ast.stmt] = []
        for st in parsed.body:
            if isinstance(st, ast.Assign) and len(st.targets) == 1 and isinstance(st.targets[0], ast.Name):
                st.value = rewriter.visit(st.value)
                if not is_entry and st.targets[0].id in rec.own_defs:
                    st.targets[0].id = rec.own_defs[st.targets[0].id]
                rewritten_body.append(st)
            else:
                rewritten_body.append(rewriter.visit(st))
        parsed.body = rewritten_body

        visiting.pop()
        loaded[real] = rec
        if not is_entry:
            order.append(rec)
        return rec

    entry_rec = load_one(filename, tree, is_entry=True)
    merged_body: list[ast.stmt] = []
    for dep in order:
        merged_body.extend(dep.tree.body)
    merged_body.extend(entry_rec.tree.body)
    merged = ast.Module(body=merged_body, type_ignores=[])
    ast.fix_missing_locations(merged)
    return merged, [dep.path for dep in order], paw_paths
