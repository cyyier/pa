"""pac lsp: diagnostics, Inlay Hints (inferred types / memory strategy / .paw FFI) and memory-audit Hover."""
from __future__ import annotations

import ast
import os
import re
import sys
import threading
import time
from typing import Optional
from urllib.parse import unquote, urlparse
from urllib.request import url2pathname

from .diagnostics import DiagnosticSink
from .infer import FuncInfo, ModuleInfo, analyze
from .modules import link_modules
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


class DocAnalysis:
    """Everything the LSP needs for one document version (pure Python, unit-testable)."""

    def __init__(self, source: str, path: str, detail: str = "simple"):
        self.source, self.path = source, path
        self.detail = "detailed" if detail == "detailed" else "simple"
        self.lines = source.splitlines()
        self.diags = check_source(source, path)
        self.info: Optional[ModuleInfo] = None
        self.tree = None
        if path.endswith(".paw"):
            return
        try:
            self.tree = ast.parse(source, path)
        except SyntaxError:
            return
        dirs = default_search_dirs(path)
        linked_tree, _, paw_paths = link_modules(self.tree, path, dirs)
        self.info = analyze(linked_tree, DiagnosticSink(source, path), source, path, dirs, paw_paths=paw_paths)

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
        if isinstance(opts, dict) and opts.get("hintDetail") in ("simple", "detailed"):
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
        da = analysis(params.text_document.uri)
        return [types.InlayHint(position=types.Position(l, c), label=text, padding_left=True,
                                kind=types.InlayHintKind.Type) for l, c, text in da.hints()]

    @server.feature(types.TEXT_DOCUMENT_HOVER)
    def hover(params: types.HoverParams):
        md = analysis(params.text_document.uri).hover(params.position.line, params.position.character)
        if not md:
            return None
        return types.Hover(contents=types.MarkupContent(kind=types.MarkupKind.Markdown, value=md))

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
