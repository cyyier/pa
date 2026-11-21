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
| `int`/`long long`/`double`/`float`/`bool`/`uint8_t` | `i32`/`i64`/`f64`/`f32`/`bool`/`u8` |

## Steps
1. Inline or expand macros first; remove `goto`, exceptions, virtuals, templates (monomorphise by hand).
2. Replace raw owning pointers with Vec/lists; keep `alloc/free` only where unavoidable.
3. Write the `.pa` (carry each C++ return type over as `-> T` on every function, e.g. `-> f64`, `-> Vec[i64]`), `@export` the entry points, `pac check --ai-diagnostic` until green.
4. Build `pac build x.pa --shared`, and fuzz both binaries with identical byte inputs (libFuzzer-style or a seeded random generator); compare outputs byte for byte. Pa `//` and `%` truncate toward zero exactly like C++.
