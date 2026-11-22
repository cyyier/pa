# Pa Quickstart

Go from zero to a native LLVM-compiled binary with a zero-copy Python `.paw` bridge in five minutes.

> **Platform:** Linux x86-64, CPython 3.11–3.13 (headers + shared `libpython`), `gcc`.
> Full specification: [ARCHITECTURE.md](ARCHITECTURE.md).

---

## 1. Install & Verify

Install `pa-lang` (which provides the `pac` CLI) and verify your local toolchain:

```bash
pip install pa-lang
pac doctor
```

`pac doctor` checks `gcc`, `llvmlite`, `Python.h`, and a shared `libpython3.X.so`. If you use `uv`, `uv python install 3.12` provides a CPython build with headers and a shared library out of the box.

---

## 2. Your First Native Binary

Create `hello.pa`. Every `.pa` file is 100% valid Python syntax (`ast.parse` compatible). Function bodies carry no type annotations—only declare return types (`-> T`):

```python
# hello.pa
def sum_squares(xs) -> float:
    total = 0.0
    for x in xs:
        total = total + x * x
    return total


def main() -> int:
    data = [1.0, 2.0, 3.0, 4.0]
    print(sum_squares(data))
    return 0
```

Check, compile to native machine code (`LLVM -O2`), and run:

```bash
pac check hello.pa
pac build hello.pa -o hello && ./hello
```

Output:

```text
30.0
```

Because `hello.pa` imports no `.paw` modules, `./hello` is a pure native ELF executable and links **zero** `libpython`.

---

## 3. Structs, Borrowing & `mut()`

Containers and `@struct` records are passed as zero-copy **read-only borrows** by default. If a function mutates a parameter, the caller must wrap it in `mut(...)`. Calls with more than 2 arguments require keyword arguments (`PA_E051`):

```python
# vec_ops.pa
@struct
class Point:
    x: float
    y: float


def scale_all(pts, factor) -> void:
    for i in range(len(pts)):
        pts[i].x = pts[i].x * factor
        pts[i].y = pts[i].y * factor


def main() -> int:
    pts = [Point(x=1.0, y=2.0), Point(x=3.0, y=4.0)]
    scale_all(mut(pts), 2.5)
    print(pts[0].x, pts[1].y)
    return 0
```

Key memory rules:
- `[0.0] * 16` (constant `N`, total ≤ 64 KB) lives on the **stack** (`[16 x f64]`).
- `[0.0] * n` (runtime `n` or > 64 KB) allocates a contiguous heap `Vec` freed automatically by **RAII** at scope exit.
- `[0] * 0` / `[0.0] * 0` declares a typed empty dynamic `Vec`.
- Assignment `b = a` deep-copies; negative indexes (`a[-1]`) are rejected—use `a[len(a) - 1]`.

---

## 4. Calling Python Libraries (`.paw` Crossing Zone)

`.pa` files cannot import third-party Python packages (`PA_E001`). Instead, place library calls in a sibling `.paw` file with locked `In -> Out` signatures.

Create `stats.paw`:

```python
# stats.paw — standard Python, locked boundary signatures
import numpy as np


def percentile(arr: span[float], q: float) -> float:
    return float(np.percentile(np.frombuffer(arr, dtype=np.float64), q))
```

Call it from `main.pa` like a normal module:

```python
# main.pa
import stats


def main() -> int:
    values = [10.0, 20.0, 30.0, 40.0, 50.0]
    p90 = stats.percentile(values, 90.0)
    print(p90)
    return 0
```

Build and run:

```bash
pac build main.pa -o app && ./app
```

How the boundary works:
- `span[float]` crosses as a read-only `PyMemoryView_FromMemory` (**zero copy**—`np.frombuffer` reads Pa memory directly).
- **Batching rule (`PA_E002`):** calling a `.paw` function inside a `for`/`while` loop is a compile error. Pack inputs on the Pa side and cross once.
- `.paw` sources are embedded inside the compiled binary at build time. Set `PA_PATH=dir1:dir2` to hot-override them during development.

---

## 5. Dev vs. Prod Modes & AI Diagnostics

Use `--mode dev` (default) while developing and testing, then build `--mode prod` to ship:

```bash
# Machine-readable JSON repair tickets for AI coding agents
pac check --ai-diagnostic .

# Development build: bounds checks, deep .paw lint, full boundary customs
pac build main.pa --mode dev -o app_dev

# Production build: C-like indexing speed, lean boundary checks
pac build main.pa --mode prod -o app
```

If a `.paw` function violates its contract at run time (e.g., returns `None` or `NaN` where `float` was declared), the binary exits with code `70` and writes `pa_boundary_fault.json` containing the exact `in_payload_snapshot` for deterministic replay.

---

## 6. Shipping & Relocatable Binaries

A default `pac build` bakes your machine's CPython paths as fallbacks. To deploy a `.paw`-using binary to another Linux machine:

```bash
pac build main.pa --mode prod --relocatable -o app
pac doctor ./app                         # inspect missing shared libs & Python home
pac bundle ./app --with-stdlib           # copy libpython + trimmed stdlib next to ./app
```

Provide third-party packages (such as `numpy`) at run time via `PA_PYPATH` or an active `VIRTUAL_ENV`.

### Embedding Pa in FastAPI / Python Apps

Export functions with `@export` and compile a guarded shared library:

```bash
pac build lib.pa --shared -o lib.so
```

Faults inside `@export` functions are caught by a `setjmp` guard and stored in thread-local storage (`pa_error_code()`, `pa_error_message()`, `pa_error_json()`) instead of terminating the host process. See [examples/web_shell/](examples/web_shell/).

---

## 7. Next Steps

- **Run the examples:** `examples/pipeline_demo/`, `examples/utm/`, `examples/levenshtein/`, `examples/kdtree/`, `examples/delaunator/`.
- **AI coding skills:** Load [skills/pa-coder/SKILL.md](skills/pa-coder/SKILL.md) for new projects, [skills/py-to-pa/SKILL.md](skills/py-to-pa/SKILL.md) to accelerate existing Python repos, or [skills/cpp-to-pa/SKILL.md](skills/cpp-to-pa/SKILL.md) to migrate C++.
- **VS Code extension:** Install from [vscode-pa/](vscode-pa/) for `pac lsp` diagnostics, hover memory audit cards, and inlay hints.
- **Full specification:** [ARCHITECTURE.md](ARCHITECTURE.md).
