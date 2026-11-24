---
name: cpp-to-pa
description: Modernise legacy C++ into pure Pa (.pa) by mapping std::array/std::vector/struct/std::span/const T& 1:1 and stripping macros and raw pointers, verified with byte-level differential fuzzing. Use when migrating C++ numeric/data code to Pa.
---

# cpp-to-pa

## Mapping
| C++ | Pa |
|---|---|
| `std::array<T,N>` / `T[N]` | `[x] * N` fixed list `list[T, N]` (stack, flat) |
| `std::vector<T>` | list built with `append` (`StackVec` if bound known, else `Vec<T>` RAII) |
| `struct` of PODs | `@struct` class |
| `std::span<T>` | `span(arr, start, len)` |
| `const T&` / `T&` | default read-only borrow / `mut(x)` at the call site |
| `T*` + `new[]`/`delete[]` | `alloc(T, n)` / `free(p)` (exactly one free on every path) |
| `Node*` pool / intrusive list or tree | `Vec[Node]` + `int`/`i32` index Arena (`-1` for `nullptr`; immune to realloc invalidation) |
| `int`/`long long`/`uint8_t`/`uint32_t`/`uint64_t`/`double`/`float`/`bool` | `i32`/`i64`/`u8`/`u32`/`u64`/`f64`/`f32`/`bool` |

## Comments
Every non-obvious converted function (pointer arithmetic, bit tricks, manual memory, intentional narrow types, former macros) gets exactly one single-line `# ...` comment directly above its `def` stating the intent or the trap (≤ 100 chars); trivial functions get none.

## Safety differences to remember
Pa bounds-checks indexes only in `--mode dev` (prod is C-like: no checks, negative indexes are out of range), traps integer division by zero in both modes, wraps 64-bit integer overflow like C, and never accepts a negative index as `len + i`. Do not rely on C++ undefined behaviour being preserved: it is reported as a fault instead. Also note that ternary `cond ? a : b` (`a if cond else b`) is scalar-only in Pa — convert struct/container ternaries to `if / else` assignments.

## Steps
1. Inline or expand macros first; remove `goto`, exceptions, virtuals, templates (monomorphise by hand).
2. Replace raw owning pointers with Vec/lists; keep `alloc/free` only where unavoidable.
3. Write the `.pa` (carry each C++ return type over as `-> T` on every function; `double`/`long long` become `float`/`int`, while `int`/`uint8_t`/`float` map to `i32`/`u8`/`f32` to preserve layout, e.g. `-> float`, `-> Vec[int]`), `@export` the entry points, `pac check --ai-diagnostic` until green.
4. Build with `pac build --shared --mode prod x.pa -o x.so` (`.pyd` on Windows — self-contained, directly importable in Python) or run standalone checks via `pac run x.pa`, and fuzz both implementations with identical byte inputs (libFuzzer-style or a seeded random generator); compare outputs byte for byte. Pa `//` and `%` truncate toward zero exactly like C++.
