---
name: pa-coder
description: Write new Pa (.pa) projects from scratch with Host-First architecture, zero-annotation bodies, contiguous-memory data layout and .paw crossing modules for the Python ecosystem. Use when creating or extending Pa code.
---

# pa-coder

## Workflow
1. **Skeleton** – Host-First: `main.pa` (+ `<domain>.pa`) owns `main()`, the loop and all compute. Python libraries live only in `<tool>.paw`. For FastAPI/Flask/Streamlit apps keep a thin `.py` router shell that calls `@export`-ed `.pa` functions (built with `pac build --shared`).
2. **Write `.pa`** – **always declare every function's Out by default: `def total(xs) -> f64:`** (also `-> Vec[i64]`, `-> list[f64, 16]`, `-> void`, `-> i64`, `-> MyStruct`). The compiler checks it against the inferred return type (`PA_E085`; numeric results are coerced to the declared type). Parameters stay unannotated (only `@export` and `.paw` annotate them, fully), and no annotations inside function bodies (only `@struct` fields); no `try`, `yield`, closures, `lambda`, `eval`, `*args`; no dict/set/tuple; functions ≤ 40 lines, complexity ≤ 8, nesting ≤ 3; > 2 args => keyword args.
3. **Data layout** – prefer `[0.0] * N` fixed lists and `@struct` records (AoS, `Vec<Record>`); build dynamic lists with `append` in loops with constant bounds so they stay on the stack. Never mix element types in one list (`PA_E030`).
4. **Borrowing** – containers are passed as read-only borrows; if the callee mutates (`append`, `x[i] = ...`, `p.f = ...`) wrap the argument in `mut(...)`. Assignment `b = a` deep-copies.
5. **Python ecosystem** – create `<tool>.paw`: standard Python, any imports, but every public function has a locked signature, e.g. `def f(arr: span[f64], k: f64) -> f64:`. Boundary types: `i32 i64 u8 f32 f64 bool str_view span[T] list[T,N] Vec[T]`; return scalar | `Vec[T]` | `void`. Call it from `.pa` as `import tool; x = tool.f(arr)`. **Never call a `.paw` function inside a loop** (`PA_E002`): pack the batch first.
6. **Modes** – develop with `pac build --mode dev` (default): deep static lint of every imported `.paw` (`PA_E083` wrong/missing return, `PA_E084` writes to read-only inputs) and full runtime boundary customs. Ship with `--mode prod`: signature check only and light runtime customs (no per-element NaN scans). Always get dev green before building prod.
7. **Gate** – run `pac check --ai-diagnostic <dir>` and apply every ticket's `repair_plan.suggested_patches` until it is green; then `pac build main.pa -o app`.

## Hard rules for strings and `mut()` (enforced by the compiler)
1. **No string concatenation inside call arguments** (`PA_E072`; also flagged: a String-returning call used as an operand, comparison, index or `s += a + b`). A concat used as a temporary (`f("a" + b)`, `tool.g(x + y)`) is never freed. Always bind it to a local first so RAII owns and frees it:
   ```python
   tmp = "a" + b      # owned String, freed automatically at function exit
   f(tmp)
   ```
2. **Empty lists + `mut()`: keep it to one level** (`PA_E011` names the callee it was forwarded to). `x = []` filled only through `mut(x)` gets its element type from the callee's direct `append`. If the list must be passed through several functions via `mut()`, either give it an element type at the top level first (`x = []; x.append(first_value)` or `x = [0.0] * N`) or fill it in a single-level `mut()` call. Never hand an untyped empty list down a chain of `mut()` forwards.
3. **No string slicing** (`PA_E071`). Use `s[i]` (a `u8`), `for ch in s`, `span` over a char array, or copy bytes with an explicit loop.
4. **A `.paw` function must not return a string** (`PA_E082`). Return the characters as `Vec[u8]` (a char/byte array) and let the `.pa` side own it; encode on the `.paw` side with e.g. `np.frombuffer(s.encode("utf-8"), dtype=np.uint8)` or `list(s.encode("utf-8"))`. Passing strings *into* a `.paw` as `str_view` is fine.

## Debugging a boundary fault
`pa_boundary_fault.json` has `direction`, `worker`, `violation`, `in_payload_snapshot`. Replay the snapshot directly against the `.paw` function: valid input + bad output => the `.paw` is wrong; correct worker output but wrong final state => the `.pa` core is wrong.

## Quick reference
| Code | Meaning |
|---|---|
| PA_E001 | third-party import in `.pa` -> move to `.paw` |
| PA_E002 | boundary call in loop -> batch |
| PA_E011 / E030 | empty list uninferable / heterogeneous list -> `@struct` |
| PA_E040 / E004 / E070 | span escape / alloc leak or UAF / ptr alias |
| PA_E050 / E051 | missing `mut()` / use keyword args |
| PA_E080 / E082 / E090 | `.paw` signature missing / bad type / call mismatch |
