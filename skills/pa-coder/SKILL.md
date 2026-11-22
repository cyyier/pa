---
name: pa-coder
description: Write new Pa (.pa) projects from scratch with Host-First architecture, zero-annotation bodies, contiguous-memory data layout and .paw crossing modules for the Python ecosystem. Use when creating or extending Pa code.
---

# pa-coder

## Workflow
1. **Skeleton** – Host-First: `main.pa` (+ `<domain>.pa` imported via `import domain` / `from domain import fn, Struct`) owns `main()`, the loop and all compute. Shared `.pa` libraries resolve from the local directory, `-I` / `PA_PATH`, or pip-installed packages. Python libraries live only in `<tool>.paw`. For FastAPI/Flask/Streamlit apps keep a thin `.py` router shell that calls `@export`-ed `.pa` functions (built with `pac build --shared`).
2. **Write `.pa`** – **always declare every function's Out by default, in Python names: `def total(xs) -> float:`** (also `-> int`, `-> uint`, `-> Vec[int]`, `-> list[float, 16]`, `-> void`, `-> MyStruct`). `int` (`int64`), `uint` (`uint64`), and `float` (`float64`) are the 64-bit defaults; write `int32` / `uint8` / `uint32` / `float32` (and casts `uint(x)`, `uint8(x)`, `uint32(x)`, `uint64(x)`, `int32(x)`, `float32(x)`; short aliases `u8`/`u32`/`u64`/`i32`/`f32` also work) **only** when you deliberately need a narrow or unsigned type (struct packing, bitwise/hashing, big arrays, C ABI) — the editor shows those loudly so reviewers notice them. The same names work in `@struct` fields, `@export` and `.paw` signatures (`def scale(a: span[float], k: int) -> Vec[float]:`). The compiler checks it against the inferred return type (`PA_E085`; numeric results are coerced to the declared type). Parameters stay unannotated (only `@export` and `.paw` annotate them, fully), and no annotations inside function bodies (only `@struct` fields); no `try`, `yield`, closures, `lambda`, `eval`, `*args`; no dict/set/tuple; functions ≤ 40 lines, complexity ≤ 8, nesting ≤ 3; > 2 args => keyword args.
3. **Data layout** – prefer `[0.0] * N` fixed lists (stack if constant `N` and ≤ 64KB), `[0.0] * n` runtime-sized heap `Vec` (auto RAII-freed, full dev bounds checks), `[0] * 0` / `[0.0] * 0` for typed empty dynamic `Vec`s, 1-level ragged `Vec[Vec[T]]` (automatically lowered to flat contiguous CSR `data + offsets`; build row-by-row with `g.append(row)` or `[[1, 2], [3, 4, 5]]`, read rows zero-copy as `g[i]` -> `span[T]`, mutate elements in-place with `g[i][j] = v`; no `g[i].append(...)` or 3D+ ragged lists), and `@struct` records (AoS, `Vec<Record>`). Never mix element types in one list (`PA_E030`). Built-in `libm` math functions are available directly without import: `sin`, `cos`, `tan`, `sqrt`, `atan`, `atan2`, `pow`, `exp`, `log`, `floor`, `ceil` (all `float -> float`, `atan2`/`pow` take 2 floats).
4. **Borrowing & call style** – containers are passed as read-only borrows; if the callee mutates (`append`, `x[i] = ...`, `p.f = ...`) wrap the argument in `mut(...)`. Assignment `b = a` deep-copies. Calls with > 2 args require keyword arguments (`PA_E051`); when a call has **> 4 keyword args**, break it across multiple vertically aligned lines for readability.
5. **Python ecosystem** – create `<tool>.paw`: standard Python, any imports, but every public function has a locked signature, e.g. `def f(arr: span[f64], k: f64) -> f64:`. Boundary types: `i32 i64 u8 u32 u64 f32 f64 bool str_view span[T] list[T,N] Vec[T]`; return scalar | `Vec[T]` | `void`. Call it from `.pa` as `import tool; x = tool.f(arr)`. **Never call a `.paw` function inside a loop** (`PA_E002`): pack the batch first. Because `.paw` sources are embedded inside compiled binaries (`__file__` is `<embedded>/<name>.paw` at runtime), **never** resolve output paths via `os.path.dirname(__file__)` inside a `.paw` — pass `out_path: str_view` from `.pa` or write relative to the current working directory.
6. **Modes** – develop with `pac build --mode dev` (default): deep static lint of every imported `.paw` (`PA_E083` wrong/missing return, `PA_E084` writes to read-only inputs) and full runtime boundary customs. Ship with `--mode prod`: signature check only and light runtime customs (no per-element NaN scans). Always get dev green before building prod.
7. **Deploy** – a plain `pac build` binary runs on the machine that built it. For other machines use `--relocatable`, then `pac doctor <bin>` to see what is missing and `pac bundle <bin> [--with-stdlib]` to ship `libpython` (and a stdlib) next to it; numpy & co. come from `PA_PYPATH` / `VIRTUAL_ENV`. A `STARTUP` fault in `pa_boundary_fault.json` means the Python home is unusable (set `PA_PYTHON_HOME` or bundle a stdlib).
8. **Gate** – run `pac check --ai-diagnostic <dir>` and apply every ticket's `repair_plan.suggested_patches` until it is green; then `pac build main.pa -o app`.

## Hard rules for strings and `mut()` (enforced by the compiler)
1. **No string concatenation inside call arguments** (`PA_E072`; also flagged: a String-returning call used as an operand, comparison, index or `s += a + b`). A concat used as a temporary (`f("a" + b)`, `tool.g(x + y)`) is never freed. Always bind it to a local first so RAII owns and frees it:
   ```python
   tmp = "a" + b      # owned String, freed automatically at function exit
   f(tmp)
   ```
2. **Empty lists + `mut()`: use `[0] * 0` / `[0.0] * 0` for multi-level `mut()`** (`PA_E011` names the callee it was forwarded to). `x = []` filled only through `mut(x)` gets its element type from a single-level callee's direct `append`. Whenever an empty dynamic `Vec` will be passed through multiple levels of `mut()`, declare it with `[0] * 0` (empty `Vec[int]`) or `[0.0] * 0` (empty `Vec[float]`) so the element type is known immediately and 100% Python-compatible.
3. **Zero-copy string slicing**: `s[a:b]` and `span(s, start, len)` on `str_view`/`String` return a zero-copy `str_view` (negative bounds and clamping work as in Python; stepped slicing `s[a:b:step]` is rejected with `PA_E071`, and returning a slice over a local owned `String` is rejected with `PA_E040`). List slicing `arr[a:b]` is still `PA_E071` — use `span(arr, start, len)`.
4. **A `.paw` function must not return a string** (`PA_E082`). Return the characters as `Vec[u8]` (a char/byte array) and let the `.pa` side own it; encode on the `.paw` side with e.g. `np.frombuffer(s.encode("utf-8"), dtype=np.uint8)` or `list(s.encode("utf-8"))`. Passing strings *into* a `.paw` as `str_view` is fine.

## Hard rule: one-line comment on every non-obvious function
Any function that is hard to understand or fiddly to operate **must** carry exactly **one** single-line comment directly above its `def` (`# ...`, ≤ 100 chars, no multi-line blocks). State the intent / the trap, not a restatement of the code. "Non-obvious" means any of:
- modular / bit / overflow-sensitive arithmetic, numerical tricks, unusual formulas or algorithms;
- manual memory: `alloc`/`free`, `span`/`copy`, `mut()` forwarding, ownership handed back to the caller;
- a `.paw` bridge call or a batching step (what is crossed, what shape goes in/out);
- narrow types (`i32`/`u8`/`f32`) chosen on purpose (say why: struct packing, ABI, memory);
- complexity ≥ 5, early-exit/guard-heavy logic, or anything a reviewer would need > 10 s to understand.
Trivial one-screen functions (a plain sum, a getter, `main` that just wires calls) get no comment. Good: `# 31-bit LCG: x*1103515245 can exceed 2^31, so keep it in int (i64) before % MOD`. Bad: `# loops over the list`.

## Hard rules: runtime safety and Python-semantics gaps (the compiler enforces most of these)
1. **Indexing**: `0 <= i < len` only. **Negative indexes are out of range** (never `a[-1]`; write `a[len(a) - 1]`). A constant negative index is `PA_E073`, a constant index past a fixed list is a compile error, and in `--mode dev` every variable index is bounds-checked (`IndexError` fault, exit 70). `--mode prod` removes the checks: only ship code that is clean in dev. Pointers from `alloc` are never checked.
2. **Division & modulo**: integer `//` and `%` by zero raise `ZeroDivisionError` in both modes (fault, exit 70). **Both integer `%`/`//` AND float `%` follow C semantics (`sdiv`/`srem`/`frem`, truncated toward zero / `fmod`)**, NOT Python's floored modulo — on negative dividends (e.g. angle wrapping `mod_angle`, ring indices), adjust with `r = a % m; if r < 0.0: r = r + m`. **Float** division by zero does *not* raise: it yields `inf`/`nan` (IEEE), unlike Python — guard divisors yourself when porting. Integer results overflow-wrap in 64 bits (Python has big ints): use floats or split the computation when values can exceed ±9.2e18.
3. **Variables keep one type** (`PA_E086`): never rebind a name to another type or another fixed list length; numeric `int`→`float` widening is fine. **Assign before use on every path** (`PA_E088`): initialise accumulators and "last value" variables *before* the `if`/loop; a loop variable is not defined after its loop.
4. **Never modify a list while iterating it** (`PA_E087`: no `append`, `mut(v)` or rebinding of `v` inside `for x in v`); iterate `range(len(v))` over a fixed length or build a second list.
5. **No aliasing with `mut()`** (`PA_E089`): never pass the same container twice in one call when one occurrence is `mut(...)` (e.g. `f(v, mut(v))`, `f(mut(v), span(v, 0, 2))`); copy it into a local first. Scalar subscript/attribute reads (e.g. `f(mut(v), v[i])`) are loaded by value before the call and are allowed.
6. **`range(a, b, step)`** accepts a variable step: `step == 0` raises `ValueError`; `step > 0` counts up, `step < 0` counts down (as Python). A constant step of 0 is a compile error.
7. **Narrowing never truncates silently**: an `int` passed to a `.paw` `u8`/`i32` parameter is range-checked (`OverflowError` fault; a literal that does not fit is a compile error); in dev mode the same check guards assignments to narrow fields/variables/arguments. A float is never accepted for an integer `.paw` parameter.
8. **Big & runtime-sized lists**: `[x] * n` works for any compile-time or **runtime** `n` (above 64KB or runtime `n` or `n <= 0`, it allocates a contiguous heap `Vec` freed automatically by RAII with full dev bounds checks); `[0] * 0` / `[0.0] * 0` creates a typed empty dynamic `Vec`. 2-D `[[0.0] * W] * H` is also supported (`m[i, j]`).
   - **Monomorphic helper ABI**: functions are not generic across container layouts — do **not** call the same helper with a stack `FixedList` (`[0] * 32`) at one site and a heap `Vec` (`[0] * n`) at another (`[32 x i64]*` vs `{ptr, len, cap}*`). Unify call sites to the same layout (e.g. pass `span(arr, 0, len(arr))` for read-only views, or allocate with a runtime `sz = 32; buf = [0] * sz` so both sites pass `Vec`).
9. **Strings are UTF-8 bytes**: `len("héllo")` is 6 and `s[i]` is a byte (`u8`), unlike Python's characters; do character-level work in a `.paw`.
10. **`print` matches Python's `repr`** for ints, bools, strs and floats (`15.0`, `0.30000000000000004`, `1e+20`); `f32` values are printed as the widened `f64`.
11. **Module-level constants**: `int`, `float`, `bool` and `str` constants (and arithmetic on them) are supported (`PI = 3.14159`, `TAU = 2.0 * PI`); other module-level statements are not.
12. **Shared libraries never exit the host**: a fault in an `@export` function returns 0 and sets an error; the host caller should check `pa_error_code()` after each call (`pa_error_message()`, `pa_error_json()`, `pa_error_clear()`).
13. **Ternary `a if cond else b` is scalar-only**: conditional expressions (`IfExp`) do not support aggregates (`@struct` or lists); use a regular `if / else` assignment when choosing between structs or containers.

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
