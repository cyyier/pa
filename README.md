<p align="center">
  <img src="https://raw.githubusercontent.com/cyyier/pa/main/site/Pa.png" alt="Pa logo" width="72" />
</p>

<h1 align="center">Pa</h1>

<p align="center">
  AI-native LLVM compiler for ultra-fast, zero-copy Python acceleration (<code>--shared</code> self-contained <code>.so</code>/<code>.pyd</code> modules, up to <strong>530&times;</strong> faster) and effortless standalone execution (<code>pac run</code>, <strong>1.7 MB</strong> peak RSS).<br/>
  <a href="https://palang.dev">Website</a> ·
  <a href="https://palang.dev/quickstart">Quickstart</a> ·
  <a href="https://palang.dev/architecture">Architecture</a> ·
  <a href="https://github.com/cyyier/pa/blob/main/CHANGELOG.md">Changelog</a>
</p>

---

## Installation & Quickstart

Requires Linux x86-64, macOS, or Windows x64, CPython 3.10–3.14 (headers + shared Python library), and a C compiler (`gcc`, `clang`, or MSVC `cl`).

```bash
pip install pa-compiler        # installs `pac`
pac doctor                     # verify LLVM, C compiler, and Python headers
pac skill --install claude     # optional: install AI skills (also: cursor, codex, copilot, antigravity)
```

### 1. Instant Native Execution (`pac run`, zero leftover `.exe` / `.dll`)

Create `hello.pa`:

```python
def sum_squares(n) -> int:
    total = 0
    for i in range(n):
        total += i * i
    return total

def main() -> int:
    print(sum_squares(100000))
    return 0
```

Run directly in one step:

```bash
pac run hello.pa               # dev mode (bounds checks, auto-cleanup)
pac run --mode prod hello.pa   # prod mode (LLVM -O2)
```

### 2. Zero-Copy Python Extension (`pac build --shared`)

Compile `@export` functions into a self-contained `.so` / `.pyd` module (zero `ctypes`, zero extra Windows DLLs) and `import` directly from Python:

```bash
pac build --shared --mode prod kernel.pa -o pa_kernel.so   # or pa_kernel.pyd on Windows
```

For `.paw` Python bridges, `--relocatable` bundles, `pac doctor`, and the full AI workflow, see <https://palang.dev/quickstart>.

---

## Repository Layout

```text
pa/
├── pa_lang/
│   ├── types.py, validator.py, infer.py   # Front-end: subset checks, bidirectional inference, escape analysis
│   ├── paw.py, diagnostics.py             # .paw header parser/linter & AI JSON repair tickets
│   ├── codegen.py, build.py               # LLVM IR emission (llvmlite), -O2 object build, gcc link, @export
│   ├── runtime_core.c, runtime_bridge.c   # RAII fault/print core & embedded CPython zero-copy .paw bridge
│   ├── cli.py, doctor.py, skills_cmd.py   # `pac` CLI: run, check, build, doctor, bundle, skill
│   ├── lsp_server.py                      # Auto-reloading LSP server (inlay hints, hover memory audit cards)
│   └── skills/                            # Bundled SKILL.md assets shipped inside the wheel
├── skills/                                # Source AI skills (pa-coder, py-to-pa, cpp-to-pa)
├── vscode-pa/                             # VS Code extension (TextMate grammar, LSP client, grammar tests)
├── examples/                              # pipeline_demo, hashlib_pa (Python port), raytracer (C++ port)
├── site/                                  # palang.dev static site (index, quickstart, architecture)
└── tests/                                 # Pytest suites (front-end, codegen, hardening, LSP, doctor, skill)
```

---

## Building from Source & Testing

```bash
git clone https://github.com/cyyier/pa.git && cd pa
uv venv --python 3.12 .venv        # or any CPython 3.10–3.14 with Python.h + shared libpython
source .venv/bin/activate
pip install -e ".[dev]"

pac doctor                         # verify gcc, llvmlite, Python.h, shared libpython
pac check examples tests           # run Pa front-end checks
pytest -q                          # run full compiler, runtime, LSP, doctor, and CLI test suite
(cd vscode-pa && npm test)         # run VS Code TextMate grammar tests
```

---

## Status & Roadmap

**Current (`v0.1.1` alpha):**
- **Supported:** Linux x86-64, macOS (arm64 / x86-64), Windows x64, CPython 3.10–3.14, `gcc` / `clang` / MSVC `cl`, LLVM via `llvmlite`.
- **Included:** zero-annotation `.pa` compiler (`int`/`uint`/`float`/`bool`, explicit widths `int32`/`uint8`/`uint32`/`uint64`/`float32` + short aliases `i32`/`u8`/`u32`/`u64`/`f32`, `**`, zero-copy `str_view` slicing, `@struct`, stack/RAII `Vec`, flat CSR `Vec[Vec[T]]`, `String`, `libm`, multi-file `.pa` module imports via local dirs / `-I` / `PA_PATH` / pip packages), `.paw` zero-copy CPython bridge with two-way customs, `--shared` guarded C-ABI libraries, `pac doctor` / `pac bundle` (ELF / Mach-O / PE), `pac skill`, `pac lsp`, and VS Code extension.

**Planned:**
- Growing the [`libpa`](https://github.com/cyyier/libpa) ecosystem (`levenshtein-pa`, `utm-pa`, `kdtree-pa`, `bm25-pa`) for pre-compiled Python acceleration wheels and pure `.pa` libraries.
- *(Low priority)* Nested `Vec` fields inside `@struct` with recursive RAII destructors (Pa recommends flat SoA arrays or CSR `Vec[Vec[T]]`).

**Non-goals (won't do):**
- Static `libpython` linking for single-file self-contained binaries — standard Python distributions do not ship portable `libpython.a` (especially on Windows/macOS), and third-party Python C extensions (`.pyd`/`.so` such as NumPy) require dynamic `libpython`. Use `pac build --bundle --with-stdlib` for self-contained directory distribution.

---

## License

MIT — see [LICENSE](https://github.com/cyyier/pa/blob/main/LICENSE). Release process: [RELEASING.md](https://github.com/cyyier/pa/blob/main/RELEASING.md).
