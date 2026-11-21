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
2. Strip annotations from function bodies; keep them only on `@struct` fields and `@export` signatures.
3. Remove `try/except`, generators, lambdas, closures, `*args`; use plain loops and guard clauses.
4. Each library interaction becomes a `.paw` function operating on whole batches (`span[f64]` in, scalar / `Vec[f64]` out). DataFrames/Responses/ORM objects must be created and destroyed inside the `.paw`.
5. Run `pac check --ai-diagnostic .` until green.

## ⚠ Semantic differences that silently change results
**Integer `//` and `%` on negative operands differ from Python.** Pa compiles them to C semantics (`sdiv`/`srem`, truncation toward zero); Python floors toward negative infinity.

| expr | Python | Pa |
|---|---|---|
| `-7 // 2` | `-4` | `-3` |
| `-7 % 3` | `2` | `-1` |
| `7 % -3` | `-2` | `1` |

Rules for the AI doing the port:
1. Whenever a source expression uses `//` or `%` and an operand *can* be negative (signed data, offsets, modular arithmetic, hash/ring indices, time deltas), do NOT translate it verbatim. Emit a floor-correct form, e.g. `r = a % m; if r < 0: r += m` (for `m > 0`) and `q = a // m; if a % m != 0 and (a < 0) != (m < 0): q -= 1`; put it in a small helper function.
2. If both operands are provably non-negative (loop counters from `range`, lengths, unsigned data) the verbatim translation is safe.
3. Float `//` is unsupported by Pa; use `int(x / y)` only after deciding truncation vs floor explicitly.
4. Add at least one negative-operand case to the differential test fixtures for every converted `//` or `%`.

## Step 3 – Prove it
Generate a differential test: run the original Python and the compiled Pa binary on the same inputs (dump inputs to JSON fixtures, compare outputs with a tolerance for floats) and record wall-clock timings of both in a short report. Integer `//` and `%` differ for negative operands (Pa truncates toward zero) – call this out when relevant.
