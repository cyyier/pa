<p align="center">
  <img src="https://raw.githubusercontent.com/cyyier/pa/main/site/Pa.png" alt="Pa logo" width="72" />
</p>

<h1 align="center">Pa</h1>

<p align="center">
  Python-syntax, C++-memory-layout, LLVM-compiled systems language with typed <code>.paw</code> bridges to the Python ecosystem.<br/>
  <a href="https://palang.dev">Website</a> ·
  <a href="https://palang.dev/quickstart">Quickstart</a> ·
  <a href="https://palang.dev/architecture">Architecture</a> ·
  <a href="https://github.com/cyyier/pa/blob/main/CHANGELOG.md">Changelog</a>
</p>

---

## Installation & Minimal Example

Requires Linux x86-64, macOS, or Windows x64, CPython 3.11–3.13 (headers + shared Python library), and a C compiler (`gcc`, `clang`, or MSVC `cl`).

```bash
pip install pa-lang            # installs `pac`
pac skill --install claude     # optional: install AI skills (also: cursor, codex, copilot, antigravity)
```

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

Compile and run:

```bash
pac build hello.pa --mode prod -o hello && ./hello
```

For `.paw` Python bridges, `--relocatable` bundles, `pac doctor`, and `--shared` C-ABI builds, see the full guide at <https://palang.dev/quickstart>.

---

## Repository Layout

```text
pa/
├── pa_lang/
│   ├── types.py, validator.py, infer.py   # Front-end: subset checks, bidirectional inference, escape analysis
│   ├── paw.py, diagnostics.py             # .paw header parser/linter & AI JSON repair tickets
│   ├── codegen.py, build.py               # LLVM IR emission (llvmlite), -O2 object build, gcc link, @export
│   ├── runtime_core.c, runtime_bridge.c   # RAII fault/print core & embedded CPython zero-copy .paw bridge
│   ├── cli.py, doctor.py, skills_cmd.py   # `pac` CLI: check, build, doctor, bundle, skill
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
uv venv --python 3.12 .venv        # or any CPython 3.11–3.13 with Python.h + shared libpython
source .venv/bin/activate
pip install -e ".[dev]"

pac doctor                         # verify gcc, llvmlite, Python.h, shared libpython
pac check examples tests           # run Pa front-end checks
pytest -q                          # run full compiler, runtime, LSP, doctor, and CLI test suite
(cd vscode-pa && npm test)         # run VS Code TextMate grammar tests
```

---

## Status & Roadmap

**Current (`v0.1.0` alpha):**
- **Supported:** Linux x86-64, macOS (arm64 / x86-64), Windows x64, CPython 3.11–3.13, `gcc` / `clang` / MSVC `cl`, LLVM via `llvmlite`.
- **Included:** zero-annotation `.pa` compiler (`i32`/`i64`/`u8`/`u32`/`u64`/`f32`/`f64`/`bool`, `**`, zero-copy `str_view` slicing, `@struct`, stack/RAII `Vec`, `String`, `libm`, multi-file `.pa` module imports via local dirs / `-I` / `PA_PATH` / pip packages), `.paw` zero-copy CPython bridge with two-way customs, `--shared` guarded C-ABI libraries, `pac doctor` / `pac bundle` (ELF / Mach-O / PE), `pac skill`, `pac lsp`, and VS Code extension.

**Planned:**
- Standalone ecosystem packages (`pa-libs`) for common data, geometry, and parsing libraries (powered by the new multi-file `.pa` module linker).
- *(Low priority)* Nested `Vec` fields inside `@struct` with recursive RAII destructors (Pa recommends flat SoA / parallel arrays).

**Non-goals (won't do):**
- Static `libpython` linking for single-file self-contained binaries — standard Python distributions do not ship portable `libpython.a` (especially on Windows/macOS), and third-party Python C extensions (`.pyd`/`.so` such as NumPy) require dynamic `libpython`. Use `pac build --bundle --with-stdlib` for self-contained directory distribution.

---

## License

MIT — see [LICENSE](https://github.com/cyyier/pa/blob/main/LICENSE). Release process: [RELEASING.md](https://github.com/cyyier/pa/blob/main/RELEASING.md).
