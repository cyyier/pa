"""pac lsp: diagnostics, Inlay Hints (inferred types / memory strategy / .paw FFI) and memory-audit Hover."""
from __future__ import annotations

import ast
import re
from typing import Optional
from urllib.parse import unquote, urlparse

from .diagnostics import DiagnosticSink
from .infer import FuncInfo, ModuleInfo, analyze
from .types import FixedList, StackVec, StructT, Vec, resolve
from .validator import check_source, complexity, default_search_dirs

PAW_ICON = "[🌉 .paw FFI -> {ret}]"


def uri_to_path(uri: str) -> str:
    p = urlparse(uri)
    return unquote(p.path) if p.scheme == "file" else uri


class DocAnalysis:
    """Everything the LSP needs for one document version (pure Python, unit-testable)."""

    def __init__(self, source: str, path: str):
        self.source, self.path = source, path
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
        self.info = analyze(self.tree, DiagnosticSink(source, path), source, path, default_search_dirs(path))

    # ---- inlay hints: list of (line0, col0, label) --------------------
    def hints(self) -> list:
        out = []
        if not self.info:
            return out
        for fi in self.info.funcs.values():
            for line, name, text in fi.hints:
                out.append((line - 1, self.hint_col(line, name), text))
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
                ret = pf.ret.fmt() if pf else "?"
                out.append((n.end_lineno - 1, n.end_col_offset, " " + PAW_ICON.format(ret=ret)))
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
        rows = [f"**{loc.name}**: `{t.fmt()}`", "",
                f"- LLVM layout: `{llvm_t}`",
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

    def analysis(uri: str) -> DocAnalysis:
        doc = server.workspace.get_text_document(uri)
        key = (uri, doc.version)
        if key not in cache:
            cache.clear()
            cache[key] = DocAnalysis(doc.source, uri_to_path(uri))
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


def main() -> None:
    build_server().start_io()


if __name__ == "__main__":
    main()
