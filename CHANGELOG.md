# Changelog

All notable changes to this project are documented here. Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
versioning: [SemVer](https://semver.org/) (`0.x` = alpha, the language and ABI may still change).

## [0.1.0] - 2026-11-25

First public release.

### Language and compiler
- `.pa` front end: zero-annotation type inference, stack-bounded escape analysis, `Vec<T>` RAII, `@struct`, 2-D indexing,
  1-level ragged `Vec[Vec[T]]` lowered to flat contiguous CSR (`data + offsets`), owned `String` + zero-copy `str_view` slicing (`s[a:b]`),
  `**` exponentiation, `libm` intrinsics, default `int`/`uint`/`float` (`i64`/`u64`/`f64`) plus explicit narrow/unsigned types (`int32`, `uint8`, `uint32`, `uint64`, `float32`, `float64`), and `-> T` return annotations (`PA_E085`).
- Multi-file `.pa` modules: `import mod` / `from mod import ...` resolved via local directory, `-I` / `PA_PATH`, and `pa_lang.libs` pip entry points.
- `.paw` crossing zone: statically typed `In -> Out` signatures, embedded in the binary, two-way boundary customs,
  `@allow_nan`, dev/prod modes, `pa_boundary_fault.json` with replayable `in_payload_snapshot`.
- LLVM code generator (`llvmlite`), runtime faults (`ZeroDivisionError`, `IndexError`, `ValueError`, `OverflowError`),
  and `pac build --shared` emitting both guarded C-ABI wrappers and CPython `PyInit_<mod>` entry points for direct Python `import` with zero `ctypes` boilerplate.
- Hardening diagnostics `PA_E073`, `PA_E086`–`PA_E089`; Python-compatible `print`.

### Tooling
- `pac check [--ai-diagnostic] [-I DIR]`, `pac build [--mode dev|prod] [--shared] [--relocatable] [-I DIR]`, `pac lsp`, `pac doctor`, `pac bundle [--with-stdlib]`, `pac skill`, `pac --version`.
- Relocatable binaries (runtime Python resolution via `PA_PYTHON_HOME` / `PA_PYPATH` / `VIRTUAL_ENV` / `PA_PATH`), pure-Python ELF / Mach-O / PE loader diagnostics across Linux, macOS, and Windows, and macOS Framework Python symlink / `@rpath` + re-signing fixes.
- Language server (`pac lsp`): diagnostics, hover memory-audit cards, inlay hints, Go to Definition (`textDocument/definition`) across `.pa` and `.paw` modules, and auto-restart.
- VS Code extension (`pa` / `paw`): hover-only default (`pa.inlayHints.detail = "off"`), `#DCDCAA` function highlighting, and transparent RGBA Pa/Paw icons.
- Skills: `pa-coder`, `py-to-pa`, `cpp-to-pa` (installable via `pac skill --install claude|cursor|codex|copilot|antigravity`).

### Examples
- `pipeline_demo`, `hashlib_pa` (CRC-32 + SHA-256), and `raytracer` (`Ray Tracing in One Weekend` port).

### Known limits
- Needs CPython 3.11–3.13 with development headers/shared library and a C compiler (`gcc`, `clang`, or MSVC `cl`).
- Stepped string slicing (`s[a:b:step]`), list slicing (`arr[a:b]` — use `span()`), `str` returns from `.paw`, and structs across the `.paw` boundary are not supported.
