"""pac lsp: diagnostics, Inlay Hints (inferred types / memory strategy / .paw FFI), Hover, and Go to Definition."""
from __future__ import annotations

import ast
import os
import re
import sys
import threading
import time
from pathlib import Path
from typing import Optional
from urllib.parse import unquote, urlparse
from urllib.request import url2pathname

from .diagnostics import DiagnosticSink
from .infer import FuncInfo, ModuleInfo, analyze
from .modules import find_pa, link_modules
from .paw import find_paw
from .types import FixedList, Scalar, StackVec, StructT, Vec, display, resolve, simple_display
from .validator import check_source, complexity, default_search_dirs

PAW_ICON = "[🌉 .paw FFI -> {ret}]"          # detailed
PAW_ICON_SIMPLE = "[🌉 .paw → {ret}]"         # default


def uri_to_path(uri: str) -> str:
    p = urlparse(uri)
    if p.scheme != "file":
        return uri
    raw = unquote(p.path)
    if len(raw) >= 3 and raw[0] == "/" and raw[2] == ":" and raw[1].isalpha():
        return url2pathname(raw) if sys.platform == "win32" else raw[1:]
    return url2pathname(raw)


def path_to_uri(path: str) -> str:
    return Path(os.path.abspath(path)).as_uri()


def _name_span(lines: list[str], lineno: int, kw: str, name: str, fallback_col: int) -> tuple[int, int, int, int]:
    line0 = max(0, lineno - 1)
    text = lines[line0] if line0 < len(lines) else ""
    m = re.search(rf"\b{kw}\s+({re.escape(name)})\b", text)
    if m:
        return line0, m.start(1), line0, m.end(1)
    return line0, fallback_col, line0, fallback_col + len(name)


def _index_module_defs(tree: ast.Module, path: str, lines: list[str]) -> dict[str, tuple[str, int, int, int, int]]:
    """Map top-level function, @struct class, and global variable/constant names to (path, l0, c0, l1, c1)."""
    defs: dict[str, tuple[str, int, int, int, int]] = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            l0, c0, l1, c1 = _name_span(lines, node.lineno, "def", node.name, node.col_offset)
            defs.setdefault(node.name, (path, l0, c0, l1, c1))
        elif isinstance(node, ast.ClassDef):
            l0, c0, l1, c1 = _name_span(lines, node.lineno, "class", node.name, node.col_offset)
            defs.setdefault(node.name, (path, l0, c0, l1, c1))
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                for sub in ast.walk(target):
                    if isinstance(sub, ast.Name):
                        defs.setdefault(
                            sub.id,
                            (path, sub.lineno - 1, sub.col_offset,
                             (sub.end_lineno or sub.lineno) - 1,
                             sub.end_col_offset if sub.end_col_offset is not None else sub.col_offset + len(sub.id)),
                        )
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            t = node.target
            defs.setdefault(
                t.id,
                (path, t.lineno - 1, t.col_offset,
                 (t.end_lineno or t.lineno) - 1,
                 t.end_col_offset if t.end_col_offset is not None else t.col_offset + len(t.id)),
            )
    return defs


def _index_struct_fields(tree: ast.Module, class_name: str, path: str) -> dict[str, tuple[str, int, int, int, int]]:
    fields: dict[str, tuple[str, int, int, int, int]] = {}
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            for stmt in node.body:
                if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                    t = stmt.target
                    fields.setdefault(
                        t.id,
                        (path, t.lineno - 1, t.col_offset,
                         (t.end_lineno or t.lineno) - 1,
                         t.end_col_offset if t.end_col_offset is not None else t.col_offset + len(t.id)),
                    )
    return fields


def _index_func_locals(fn: ast.FunctionDef, path: str) -> dict[str, tuple[str, int, int, int, int]]:
    locs: dict[str, tuple[str, int, int, int, int]] = {}
    all_args = list(fn.args.posonlyargs) + list(fn.args.args) + list(fn.args.kwonlyargs)
    if fn.args.vararg:
        all_args.append(fn.args.vararg)
    if fn.args.kwarg:
        all_args.append(fn.args.kwarg)
    for arg in all_args:
        l0 = arg.lineno - 1
        c0 = arg.col_offset
        locs.setdefault(arg.arg, (path, l0, c0, l0, c0 + len(arg.arg)))

    def _visit(stmts):
        for stmt in stmts:
            if isinstance(stmt, ast.Assign):
                for target in stmt.targets:
                    for sub in ast.walk(target):
                        if isinstance(sub, ast.Name):
                            locs.setdefault(
                                sub.id,
                                (path, sub.lineno - 1, sub.col_offset,
                                 (sub.end_lineno or sub.lineno) - 1,
                                 sub.end_col_offset if sub.end_col_offset is not None else sub.col_offset + len(sub.id)),
                            )
            elif isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                t = stmt.target
                locs.setdefault(
                    t.id,
                    (path, t.lineno - 1, t.col_offset,
                     (t.end_lineno or t.lineno) - 1,
                     t.end_col_offset if t.end_col_offset is not None else t.col_offset + len(t.id)),
                )
            elif isinstance(stmt, ast.For):
                for sub in ast.walk(stmt.target):
                    if isinstance(sub, ast.Name):
                        locs.setdefault(
                            sub.id,
                            (path, sub.lineno - 1, sub.col_offset,
                             (sub.end_lineno or sub.lineno) - 1,
                             sub.end_col_offset if sub.end_col_offset is not None else sub.col_offset + len(sub.id)),
                        )
                _visit(stmt.body)
                _visit(stmt.orelse)
            elif isinstance(stmt, (ast.If, ast.While)):
                _visit(stmt.body)
                _visit(stmt.orelse)

    _visit(fn.body)
    return locs


def _resolve_module_path(mod_name: str, search_dirs: list[str], level: int = 0) -> Optional[str]:
    p = find_pa(mod_name, search_dirs, level=level)
    if p:
        return p
    if level == 0 and mod_name and "." not in mod_name:
        return find_paw(mod_name, search_dirs)
    return None


def _parse_and_index_file(file_path: str) -> tuple[Optional[ast.Module], dict[str, tuple[str, int, int, int, int]]]:
    try:
        with open(file_path, "r", encoding="utf-8", errors="replace") as f:
            src = f.read()
        tree = ast.parse(src, file_path)
    except (OSError, SyntaxError):
        return None, {}
    return tree, _index_module_defs(tree, file_path, src.splitlines())


def _is_in_comment(line: str, col: int) -> bool:
    in_quote: Optional[str] = None
    i = 0
    limit = min(col, len(line))
    while i < limit:
        ch = line[i]
        if in_quote:
            if ch == "\\" and i + 1 < limit:
                i += 2
                continue
            if ch == in_quote:
                in_quote = None
        else:
            if ch in ("'", '"'):
                in_quote = ch
            elif ch == "#":
                return True
        i += 1
    return False


class DocAnalysis:
    """Everything the LSP needs for one document version (pure Python, unit-testable)."""

    def __init__(self, source: str, path: str, detail: str = "simple"):
        self.source, self.path = source, path
        self.detail = "detailed" if detail == "detailed" else "simple"
        self.lines = source.splitlines()
        self.diags = check_source(source, path)
        self.info: Optional[ModuleInfo] = None
        self.tree = None
        self.search_dirs = default_search_dirs(path)
        try:
            self.tree = ast.parse(source, path)
        except SyntaxError:
            return
        if path.endswith(".paw"):
            return
        linked_tree, _, paw_paths = link_modules(self.tree, path, self.search_dirs)
        self.info = analyze(linked_tree, DiagnosticSink(source, path), source, path, self.search_dirs, paw_paths=paw_paths)

    # ---- inlay hints: list of (line0, col0, label) --------------------
    def hints(self) -> list:
        out = []
        if not self.info:
            return out
        detailed = self.detail == "detailed"
        for fi in self.info.funcs.values():
            if fi.name.startswith("__pa_m"):
                continue
            for line, name, text in (fi.hints if detailed else fi.hints_simple):
                out.append((line - 1, self.hint_col(line, name), text))
            if not detailed:          # simple mode: memory-strategy notes live in the hover card
                continue
            for line in fi.raii_free_lines:
                if 1 <= line <= len(self.lines):
                    out.append((line - 1, len(self.lines[line - 1]), "[free: RAII]"))
        out += self.paw_hints()
        return sorted(out)

    def hint_col(self, line: int, name: str) -> int:
        text = self.lines[line - 1] if 0 < line <= len(self.lines) else ""
        m = re.search(rf"\b{re.escape(name)}\b", text) if name else None
        return m.end() if m else len(text)

    def paw_hints(self) -> list:
        out = []
        for n in ast.walk(self.tree):
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and isinstance(n.func.value, ast.Name) \
                    and n.func.value.id in self.info.paw:
                pm = self.info.paw[n.func.value.id]
                pf = pm.funcs.get(n.func.attr)
                detailed = self.detail == "detailed"
                ret = (display if detailed else simple_display)(pf.ret) if pf else "?"
                icon = PAW_ICON if detailed else PAW_ICON_SIMPLE
                out.append((n.end_lineno - 1, n.end_col_offset, " " + icon.format(ret=ret)))
        return out

    # ---- hover --------------------------------------------------------
    def word_at(self, line0: int, col0: int) -> str:
        text = self.lines[line0] if line0 < len(self.lines) else ""
        for m in re.finditer(r"[A-Za-z_]\w*", text):
            if m.start() <= col0 <= m.end():
                return m.group(0)
        return ""

    def func_at(self, line: int) -> Optional[FuncInfo]:
        if not self.info:
            return None
        for fi in self.info.funcs.values():
            if fi.name.startswith("__pa_m"):
                continue
            if fi.node.lineno <= line <= fi.node.end_lineno:
                return fi
        return None

    def hover(self, line0: int, col0: int) -> Optional[str]:
        fi = self.func_at(line0 + 1)
        word = self.word_at(line0, col0)
        if not fi or not word:
            return None
        loc = fi.locals.get(word)
        if loc is None:
            return None
        return self.card(fi, loc)

    def card(self, fi: FuncInfo, loc) -> str:
        from .codegen import ModuleGen
        t = resolve(loc.type)
        try:
            llvm_t = str(ModuleGen(self.tree, self.info).lt(t))
        except Exception:
            llvm_t = "n/a"
        stride = getattr(getattr(t, "elem", None), "size", None)
        where = {"stack": "stack (Zero-Heap)", "heap": "heap Vec<T> (RAII)", "param": "borrowed parameter",
                 "scalar": "register / stack slot"}.get(loc.storage, loc.storage)
        short = {"stack": "stack", "scalar": "stack", "heap": "heap", "param": "param"}.get(loc.storage, loc.storage)
        shown, real = display(t), t.fmt()
        head = f"{shown} ({real}, {t.size}B, {short})" if shown != real else f"{shown} ({t.size}B, {short})"
        rows = [f"**{loc.name}**: `{head}`", ""]
        if isinstance(t, Scalar) and t.name in ("i32", "u8", "u32", "f32"):
            rows += [f"> narrow type `{display(t)}` (`{t.name}`): watch for overflow / precision loss (chosen for size or C ABI)", ""]
        rows += [f"- LLVM layout: `{llvm_t}`",
                f"- size: **{t.size} B**" + (f", element stride: **{stride} B**" if stride else ""),
                f"- storage: {where}" + (" -- ownership transferred to caller" if loc.escapes else "")]
        if loc.storage == "heap" and not loc.escapes and fi.raii_free_lines:
            rows.append(f"- auto `free` inserted at line(s): {', '.join(map(str, fi.raii_free_lines))}")
        rows.append(f"- function `{fi.name}`: cyclomatic complexity **{complexity(fi.node)}**")
        return "\n".join(rows)

    # ---- go to definition ---------------------------------------------
    def _collect_imports(self) -> tuple[dict[str, str], dict[str, tuple[str, Optional[str]]]]:
        """Return (imported_modules, imported_symbols) for the current AST."""
        imported_modules: dict[str, str] = {}
        imported_symbols: dict[str, tuple[str, Optional[str]]] = {}
        if not self.tree:
            return imported_modules, imported_symbols
        for stmt in self.tree.body:
            if isinstance(stmt, ast.Import):
                for alias in stmt.names:
                    target = _resolve_module_path(alias.name, self.search_dirs)
                    if target:
                        local = alias.asname or alias.name
                        imported_modules[local] = target
                        if not alias.asname and "." in alias.name:
                            root = alias.name.split(".", 1)[0]
                            root_path = _resolve_module_path(root, self.search_dirs)
                            if root_path:
                                imported_modules.setdefault(root, root_path)
            elif isinstance(stmt, ast.ImportFrom):
                mod_name = stmt.module or ""
                mod_path = _resolve_module_path(mod_name, self.search_dirs, level=stmt.level)
                for alias in stmt.names:
                    if alias.name == "*":
                        continue
                    local = alias.asname or alias.name
                    sub_name = f"{mod_name}.{alias.name}" if mod_name else alias.name
                    sub_path = _resolve_module_path(sub_name, self.search_dirs, level=stmt.level)
                    if mod_path:
                        _, mod_defs = _parse_and_index_file(mod_path)
                        if alias.name in mod_defs or not sub_path:
                            imported_symbols[local] = (mod_path, alias.name)
                            continue
                    if sub_path:
                        imported_modules[local] = sub_path
                        imported_symbols[local] = (sub_path, None)
        return imported_modules, imported_symbols

    def _enclosing_func_node(self, line1: int) -> Optional[ast.FunctionDef]:
        if not self.tree:
            return None
        for node in self.tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                end = node.end_lineno or node.lineno
                if node.lineno <= line1 <= end:
                    return node
        return None

    def definition(self, line0: int, col0: int) -> Optional[tuple[str, int, int, int, int]]:
        """Resolve symbol under cursor to (file_path, start_line0, start_col0, end_line0, end_col0) or None."""
        if not self.tree or line0 < 0 or line0 >= len(self.lines):
            return None
        line_text = self.lines[line0]
        match = None
        for m in re.finditer(r"[A-Za-z_]\w*", line_text):
            if m.start() <= col0 <= m.end():
                match = m
                break
        if match is None or _is_in_comment(line_text, match.start()):
            return None

        word = match.group(0)
        prefix_m = re.search(r"([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*)\.\s*$", line_text[:match.start()])
        prefix = prefix_m.group(1) if prefix_m else None

        imported_modules, imported_symbols = self._collect_imports()

        # 1. Cursor on an `import` / `from ... import ...` statement line
        line1 = line0 + 1
        for stmt in self.tree.body:
            if isinstance(stmt, ast.Import) and stmt.lineno <= line1 <= (stmt.end_lineno or stmt.lineno):
                for alias in stmt.names:
                    if word == alias.asname or word == alias.name or word in alias.name.split("."):
                        target = _resolve_module_path(alias.name, self.search_dirs)
                        if target:
                            return (target, 0, 0, 0, 0)
                return None
            if isinstance(stmt, ast.ImportFrom) and stmt.lineno <= line1 <= (stmt.end_lineno or stmt.lineno):
                imp_m = re.search(r"\bimport\b", line_text)
                if imp_m and match.start() < imp_m.start():
                    target = _resolve_module_path(stmt.module or "", self.search_dirs, level=stmt.level)
                    return (target, 0, 0, 0, 0) if target else None
                for alias in stmt.names:
                    local = alias.asname or alias.name
                    if word in (alias.name, local):
                        entry = imported_symbols.get(local)
                        if entry:
                            target_path, orig_sym = entry
                            if orig_sym is None:
                                return (target_path, 0, 0, 0, 0)
                            _, tdefs = _parse_and_index_file(target_path)
                            return tdefs.get(orig_sym, (target_path, 0, 0, 0, 0))
                return None

        # 2. Dotted attribute access: `mod.func` / `mod.Struct` / `mod.CONST` / `struct_var.field`
        if prefix is not None:
            if prefix in imported_modules:
                target_path = imported_modules[prefix]
                _, tdefs = _parse_and_index_file(target_path)
                return tdefs.get(word)
            fi = self.func_at(line1)
            if fi and "." not in prefix and prefix in fi.locals:
                t = resolve(fi.locals[prefix].type)
                if isinstance(t, StructT):
                    fields = _index_struct_fields(self.tree, t.name, self.path)
                    if word in fields:
                        return fields[word]
            return None

        # 3. Module alias reference (e.g. clicking `data_fetcher` in `data_fetcher.fetch_series(...)`)
        if word in imported_modules:
            return (imported_modules[word], 0, 0, 0, 0)

        # 4. Symbol imported via `from mod import sym [as alias]`
        if word in imported_symbols:
            target_path, orig_sym = imported_symbols[word]
            if orig_sym is None:
                return (target_path, 0, 0, 0, 0)
            _, tdefs = _parse_and_index_file(target_path)
            return tdefs.get(orig_sym, (target_path, 0, 0, 0, 0))

        # 5. Enclosing function parameter or local variable
        fn_node = self._enclosing_func_node(line1)
        if fn_node is not None:
            fn_locals = _index_func_locals(fn_node, self.path)
            if word in fn_locals:
                return fn_locals[word]

        # 6. Top-level module definitions (functions, @struct classes, constants/globals)
        mod_defs = _index_module_defs(self.tree, self.path, self.lines)
        if word in mod_defs:
            return mod_defs[word]

        return None


# ---------------------------------------------------------------------------
def build_server():
    from lsprotocol import types
    from pygls.lsp.server import LanguageServer

    server = LanguageServer("pac-lsp", "0.1.0")
    cache: dict = {}
    options = {"detail": "simple"}

    @server.feature(types.INITIALIZE)
    def on_initialize(params):
        opts = params.initialization_options or {}
        if isinstance(opts, dict) and opts.get("hintDetail") in ("off", "simple", "detailed"):
            options["detail"] = opts["hintDetail"]

    def analysis(uri: str) -> DocAnalysis:
        doc = server.workspace.get_text_document(uri)
        key = (uri, doc.version)
        if key not in cache:
            cache.clear()
            cache[key] = DocAnalysis(doc.source, uri_to_path(uri), options["detail"])
        return cache[key]

    def publish(uri: str) -> None:
        da = analysis(uri)
        diags = []
        for d in da.diags:
            ln = d.location["line"] - 1
            col = d.location["col"] - 1
            sev = types.DiagnosticSeverity.Error if d.severity == "error" else types.DiagnosticSeverity.Warning
            diags.append(types.Diagnostic(
                range=types.Range(types.Position(ln, col), types.Position(ln, max(col + 1, len(da.lines[ln]) if ln < len(da.lines) else col + 1))),
                message=f"{d.summary}\n-> {d.repair_plan['strategy']}", severity=sev, code=d.code, source="pac"))
        server.text_document_publish_diagnostics(types.PublishDiagnosticsParams(uri=uri, diagnostics=diags))

    for feat in (types.TEXT_DOCUMENT_DID_OPEN, types.TEXT_DOCUMENT_DID_CHANGE, types.TEXT_DOCUMENT_DID_SAVE):
        server.feature(feat)(lambda params: publish(params.text_document.uri))

    @server.feature(types.TEXT_DOCUMENT_INLAY_HINT)
    def inlay(params: types.InlayHintParams):
        if options["detail"] == "off":
            return []
        da = analysis(params.text_document.uri)
        return [types.InlayHint(position=types.Position(l, c), label=text, padding_left=True,
                                kind=types.InlayHintKind.Type) for l, c, text in da.hints()]

    @server.feature(types.TEXT_DOCUMENT_HOVER)
    def hover(params: types.HoverParams):
        md = analysis(params.text_document.uri).hover(params.position.line, params.position.character)
        if not md:
            return None
        return types.Hover(contents=types.MarkupContent(kind=types.MarkupKind.Markdown, value=md))

    @server.feature(types.TEXT_DOCUMENT_DEFINITION)
    def definition(params: types.DefinitionParams):
        loc = analysis(params.text_document.uri).definition(params.position.line, params.position.character)
        if not loc:
            return None
        path, l0, c0, l1, c1 = loc
        return types.Location(
            uri=path_to_uri(path),
            range=types.Range(types.Position(l0, c0), types.Position(l1, c1)),
        )

    return server


class SourceWatcher:
    """Detects edits to the compiler's own source files (*.py, *.c) so a stale server never lingers."""

    def __init__(self, directory: Optional[str] = None):
        self.dir = directory or os.path.dirname(os.path.abspath(__file__))
        self.snapshot = self.scan()

    def scan(self) -> dict:
        out = {}
        for name in sorted(os.listdir(self.dir)):
            if name.endswith((".py", ".c")):
                try:
                    st = os.stat(os.path.join(self.dir, name))
                    out[name] = (st.st_mtime_ns, st.st_size)
                except OSError:
                    pass
        return out

    def changed(self) -> bool:
        return self.scan() != self.snapshot

    def run(self, on_change, interval: float = 1.0, debounce: float = 0.5) -> None:
        while True:
            time.sleep(interval)
            if self.changed():
                time.sleep(debounce)           # let multi-file edits / saves settle
                on_change()
                return


def _exit_for_restart() -> None:
    sys.stderr.write("pac lsp: compiler sources changed, exiting so the client restarts a fresh server\n")
    sys.stderr.flush()
    os._exit(0)


def main() -> None:
    """Start the server. Auto-exits when pa_lang sources change (VS Code's client restarts it).

    Disable with PA_LSP_AUTORELOAD=0; PA_LSP_WATCH_DIR overrides the watched directory (tests).
    """
    if os.environ.get("PA_LSP_AUTORELOAD", "1") != "0":
        watcher = SourceWatcher(os.environ.get("PA_LSP_WATCH_DIR"))
        threading.Thread(target=watcher.run, args=(_exit_for_restart,), daemon=True).start()
    build_server().start_io()


if __name__ == "__main__":
    main()
