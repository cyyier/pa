# Pa Language (VS Code)

Minimal local extension for `.pa` / `.paw` files.

- **Languages**: `pa` (`.pa`) and `paw` (`.paw`). Both TextMate grammars *inherit Python's* (`source.pa` → `source.python`, `source.paw` → `source.pa`).
- **Dedicated colours**: compiler intrinsics `mut span copy alloc free py_call`, decorators `@struct @export @allow_nan`, and the Pa types `i32 i64 u8 f32 f64 str_view bytes_view void Vec[...] ptr[...] span[...]`.
- **Language server** (`pac lsp`): inferred-type **inlay hints** (`: list[f64, 16] (128B flat)`, `: StackVec<i64, max=64> (Zero-Heap)`, `[🌉 .paw FFI -> f64]`), **hover memory-audit card** (LLVM layout, bytes, stride, stack/heap, RAII free lines, complexity) and live **diagnostics**.
- **Commands**: *Pa: Restart Language Server*, *Pa: Build Current File (dev|prod)*, *Pa: Check Current File (--ai-diagnostic)*.

## Install
```bash
pip install -e ..                 # provides `pac`  (or use the project's .venv)
npm install && npx @vscode/vsce package --skip-license --allow-missing-repository
code --install-extension pa-lang-0.1.0.vsix
```
`pa.pacPath` defaults to `pac`; `<workspace>/.venv/bin/pac` is preferred automatically when present.

## Tests
`npm test` (TextMate scopes via vscode-textmate) and, from the repo root, `pytest tests/test_lsp_stdio.py`
(real LSP JSON-RPC against `pac lsp`).
