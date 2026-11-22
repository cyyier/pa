# Changelog

All notable changes to this project are documented here. Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
versioning: [SemVer](https://semver.org/) (`0.x` = alpha, the language and ABI may still change).

## [0.1.0] - 2026-11-22

First public release.

### Language and compiler
- `.pa` front end: zero-annotation type inference, stack-bounded escape analysis, `Vec<T>` RAII, `@struct`, 2-D indexing,
  owned `String`, `libm` intrinsics, default `int`/`float` (`i64`/`f64`), `-> T` return annotations (`PA_E085`).
- `.paw` crossing zone: statically typed `In -> Out` signatures, embedded in the binary, two-way boundary customs,
  `@allow_nan`, dev/prod modes, `pa_boundary_fault.json` with replayable `in_payload_snapshot`.
- LLVM code generator (llvmlite), runtime faults (`ZeroDivisionError`, `IndexError`, `ValueError`, `OverflowError`),
  guarded `--shared` libraries that never exit the host process.
- Hardening diagnostics `PA_E073`, `PA_E086`-`PA_E089`; Python-compatible `print`.

### Tooling
- `pac check [--ai-diagnostic]`, `pac build [--mode dev|prod] [--shared] [--relocatable]`, `pac lsp`, `pac doctor`, `pac bundle [--with-stdlib]`, `pac skill`, `pac --version`.
- Relocatable binaries (runtime Python resolution via `PA_PYTHON_HOME` / `PA_PYPATH` / `VIRTUAL_ENV` / `PA_PATH`) and pure-Python ELF / Mach-O / PE loader diagnostics across Linux, macOS, and Windows.
- Language server (inlay hints, hover memory audit, diagnostics, auto-restart) and a VS Code extension (`pa` / `paw`).
- Skills: `pa-coder`, `py-to-pa`, `cpp-to-pa` (also installable via `pac skill --install`).

### Examples
- `pipeline_demo`, `hashlib_pa` (CRC-32 + SHA-256), and `raytracer` (`Ray Tracing in One Weekend` port).

### Known limits
- Needs CPython 3.11–3.13 with development headers/shared library and a C compiler (`gcc`, `clang`, or MSVC `cl`).
- String slicing, `str` returns from `.paw` and structs across the `.paw` boundary are not supported.
