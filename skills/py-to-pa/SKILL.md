---
name: py-to-pa
description: Refactor an existing Python repository to Pa for speed, using a three-colour feasibility gate (green/yellow/red) and .paw crossing modules. Use when asked to port or accelerate Python code with Pa.
---

# py-to-pa

## Step 1 – Feasibility Gate (do this first, per module)
- 🟢 **GREEN**: pure computation, no third-party imports -> convert the whole module to `.pa`.
- 🟡 **YELLOW**: uses third-party libs or a web framework -> invert control: `.pa` host owns logic; move each library call into a `.paw` file with a locked In -> Out signature. For Web frameworks keep a *thin* `.py` router shell: extract raw params -> call an `@export` `.pa` function -> serialize result.
- 🔴 **RED**: heavy metaprogramming / dynamic framework coupling that cannot be peeled off -> stop and answer exactly `STATUS: RED - Keep in .py worker layer + <one-sentence blocker>`.

## Step 2 – Convert
1. Replace classes with `@struct` (annotated fields only); dict/set/tuple -> `@struct` or homogeneous lists.
2. Strip annotations from function bodies and parameters, but **write the return type (`-> T`) on every converted function by default** (e.g. `def mean(xs) -> float:`, `-> int`, `-> Vec[float]`; use Python's `int`/`float` — they are aliases of the default `i64`/`f64`, and `i32`/`u8`/`f32` are reserved for deliberate narrow storage); `@export` and `.paw` functions keep full parameter annotations too. Run `pac check` — `PA_E085` tells you when the declared Out disagrees with the body.
3. Remove `try/except`, generators, lambdas, closures, `*args`; use plain loops and guard clauses.
4. Each library interaction becomes a `.paw` function operating on whole batches (`span[f64]` in, scalar / `Vec[f64]` out). DataFrames/Responses/ORM objects must be created and destroyed inside the `.paw`.
5. Run `pac check --ai-diagnostic .` until green.

## Hard rules for strings and `mut()` (enforced by the compiler)
1. **No string concatenation inside call arguments** (`PA_E072`; also flagged: a String-returning call used as an operand, comparison, index or `s += a + b`). A concat used as a temporary (`f("a" + b)`, `tool.g(x + y)`) is never freed. Always bind it to a local first so RAII owns and frees it:
   ```python
   tmp = "a" + b      # owned String, freed automatically at function exit
   f(tmp)
   ```
2. **Empty lists + `mut()`: keep it to one level** (`PA_E011` names the callee it was forwarded to). `x = []` filled only through `mut(x)` gets its element type from the callee's direct `append`. If the list must be passed through several functions via `mut()`, either give it an element type at the top level first (`x = []; x.append(first_value)` or `x = [0.0] * N`) or fill it in a single-level `mut()` call. Never hand an untyped empty list down a chain of `mut()` forwards.
3. **No string slicing** (`PA_E071`). Use `s[i]` (a `u8`), `for ch in s`, `span` over a char array, or copy bytes with an explicit loop.
4. **A `.paw` function must not return a string** (`PA_E082`). Return the characters as `Vec[u8]` (a char/byte array) and let the `.pa` side own it; encode on the `.paw` side with e.g. `np.frombuffer(s.encode("utf-8"), dtype=np.uint8)` or `list(s.encode("utf-8"))`. Passing strings *into* a `.paw` as `str_view` is fine.

## Hard rule: one-line comment on every non-obvious function
Any function that is hard to understand or fiddly to operate **must** carry (carry the original docstring's intent over, shortened) exactly **one** single-line comment directly above its `def` (`# ...`, ≤ 100 chars, no multi-line blocks). State the intent / the trap, not a restatement of the code. "Non-obvious" means any of:
- modular / bit / overflow-sensitive arithmetic, numerical tricks, unusual formulas or algorithms;
- manual memory: `alloc`/`free`, `span`/`copy`, `mut()` forwarding, ownership handed back to the caller;
- a `.paw` bridge call or a batching step (what is crossed, what shape goes in/out);
- narrow types (`i32`/`u8`/`f32`) chosen on purpose (say why: struct packing, ABI, memory);
- complexity ≥ 5, early-exit/guard-heavy logic, or anything a reviewer would need > 10 s to understand.
Trivial one-screen functions (a plain sum, a getter, `main` that just wires calls) get no comment. Good: `# 31-bit LCG: x*1103515245 can exceed 2^31, so keep it in int (i64) before % MOD`. Bad: `# loops over the list`.

## Python-semantics gaps and safety rules to apply while porting (the compiler enforces most of them)
1. **Indexing**: `0 <= i < len` only. **Negative indexes are out of range** (never `a[-1]`; write `a[len(a) - 1]`). A constant negative index is `PA_E073`, a constant index past a fixed list is a compile error, and in `--mode dev` every variable index is bounds-checked (`IndexError` fault, exit 70). `--mode prod` removes the checks: only ship code that is clean in dev. Pointers from `alloc` are never checked.
2. **Division**: integer `//` and `%` by zero raise `ZeroDivisionError` in both modes (fault, exit 70). **Float** division by zero does *not* raise: it yields `inf`/`nan` (IEEE), unlike Python — guard divisors yourself when porting. Integer results overflow-wrap in 64 bits (Python has big ints): use floats or split the computation when values can exceed ±9.2e18.
3. **Variables keep one type** (`PA_E086`): never rebind a name to another type or another fixed list length; numeric `int`→`float` widening is fine. **Assign before use on every path** (`PA_E088`): initialise accumulators and "last value" variables *before* the `if`/loop; a loop variable is not defined after its loop.
4. **Never modify a list while iterating it** (`PA_E087`: no `append`, `mut(v)` or rebinding of `v` inside `for x in v`); iterate `range(len(v))` over a fixed length or build a second list.
5. **No aliasing with `mut()`** (`PA_E089`): never pass the same variable twice in one call when one occurrence is `mut(...)`; copy it into a local first.
6. **`range(a, b, step)`** accepts a variable step: `step == 0` raises `ValueError`; `step > 0` counts up, `step < 0` counts down (as Python). A constant step of 0 is a compile error.
7. **Narrowing never truncates silently**: an `int` passed to a `.paw` `u8`/`i32` parameter is range-checked (`OverflowError` fault; a literal that does not fit is a compile error); in dev mode the same check guards assignments to narrow fields/variables/arguments. A float is never accepted for an integer `.paw` parameter.
8. **Big & runtime-sized lists**: `[x] * n` works for any compile-time or runtime `n` (above 64KB or runtime `n` or `n <= 0`, it lives on the heap as a `Vec` and is freed automatically); `[0] * 0` / `[0.0] * 0` creates a typed empty dynamic `Vec`; 2-D `[[0.0] * W] * H` too, index it `m[i, j]`. Keep container layouts consistent across call sites of the same helper (do not mix stack `[0] * 32` with heap `[0] * n` for the same parameter — unify via `span(...)` or runtime-sized `Vec`).
9. **Strings are UTF-8 bytes**: `len("héllo")` is 6 and `s[i]` is a byte (`u8`), unlike Python's characters; do character-level work in a `.paw`.
10. **`print` matches Python's `repr`** for ints, bools, strs and floats (`15.0`, `0.30000000000000004`, `1e+20`); `f32` values are printed as the widened `f64`.
11. **Module-level constants**: `int`, `float`, `bool` and `str` constants (and arithmetic on them) are supported (`PI = 3.14159`, `TAU = 2.0 * PI`); other module-level statements are not.
12. **Shared libraries never exit the host**: a fault in an `@export` function returns 0 and sets an error; the shell must check `pa_error_code()` after each call (`pa_error_message()`, `pa_error_json()`, `pa_error_clear()`) and turn it into an HTTP error (see `examples/web_shell/app.py: checked`).

## ⚠ Semantic differences that silently change results
**Both integer `//`/`%` AND float `%` on negative operands differ from Python.** Pa compiles them to C semantics (`sdiv`/`srem`/`frem`, truncation toward zero / `fmod`); Python floors toward negative infinity.

| expr | Python | Pa |
|---|---|---|
| `-7 // 2` | `-4` | `-3` |
| `-7 % 3` | `2` | `-1` |
| `7 % -3` | `-2` | `1` |
| `-1.0 % 6.283` | `5.283` | `-1.0` |

Rules for the AI doing the port (also apply the string/`mut()` rules above when converting `str` code: Python slicing, string-returning library calls and concatenation in arguments must be rewritten):
1. Whenever a source expression uses `//` or `%` (integer **or float**, e.g. angle wrapping `(x + pi) % (2 * pi)`) and an operand *can* be negative (signed data, offsets, modular arithmetic, hash/ring indices, time deltas), do NOT translate it verbatim. Emit a floor-correct form, e.g. `r = a % m; if r < 0: r += m` (or `0.0` for floats, when `m > 0`) and `q = a // m; if a % m != 0 and (a < 0) != (m < 0): q -= 1`; put it in a small helper function.
2. If both operands are provably non-negative (loop counters from `range`, lengths, unsigned data) the verbatim translation is safe.
3. Float `//` is unsupported by Pa; use `int(x / y)` only after deciding truncation vs floor explicitly.
4. Add at least one negative-operand case to the differential test fixtures for every converted `//` or `%`.

## Step 3 – Prove it
Generate a differential test: run the original Python and the compiled Pa binary on the same inputs (dump inputs to JSON fixtures, compare outputs with a tolerance for floats) and record wall-clock timings of both in a short report. Integer `//` and `%` differ for negative operands (Pa truncates toward zero) – call this out when relevant.
