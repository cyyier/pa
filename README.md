# Pa

Python-syntax, C++-memory-layout, LLVM-compiled systems language. `*.pa` files compile to native code;
`*.paw` files are plain Python "crossing zones" with locked `In -> Out` signatures. Full spec: [ARCHITECTURE.md](ARCHITECTURE.md);
AI skills: [skills/](skills/).

## Quick start
```bash
pip install -e ".[dev]"            # provides `pac`; needs gcc and a CPython with headers (e.g. `uv python install 3.12`)
pac check --ai-diagnostic examples # JSON repair tickets for AI agents
pac build examples/pipeline_demo/main.pa -o demo && ./demo
pac build lib.pa --shared -o lib.so     # @export functions for a Python/web shell (see examples/web_shell)
```
`--mode dev` (default): deep `.paw` lint, bounds checks, full boundary customs. `--mode prod`: lean runtime.

## Where things live at run time (no hard-coded source paths)
* **`.paw` sources are embedded in the binary** at build time — move or delete the source tree freely.
  `PA_PATH=dir1:dir2` makes the binary prefer live `.paw` files from those directories (hot-edit while developing).
* **CPython is located at run time**, in priority order:
  home `PA_PYTHON_HOME` > `PYTHONHOME` > build-time default (only if it still exists);
  `sys.path`: `PA_PYPATH` > `$VIRTUAL_ENV/lib/pythonX.Y/site-packages` > build-time defaults (only entries that exist).
* `pac build --python-home P --python-path S [--python-path S2]` set the baked defaults;
  `--relocatable` bakes none (then provide `PA_PYTHON_HOME`/`PA_PYPATH`/`VIRTUAL_ENV`, and put `libpython3.X.so` on `LD_LIBRARY_PATH`
  or next to the binary: the rpath includes `$ORIGIN` and `$ORIGIN/../lib`).
* Pure-native programs (no `.paw`) link no Python at all.

## When a binary cannot find its Python
A missing `libpython` makes the *dynamic loader* abort before any Pa code runs (`error while loading shared libraries:
libpython3.12.so.1.0`, exit 127). Pa cannot intercept that, so it ships detection and an assisted fix:
```bash
pac doctor                         # build machine: gcc, llvmlite, Python.h, shared libpython
pac doctor ./app                   # why ./app will not start: which libraries are MISSING, which Python home it will use
pac bundle ./app                   # copy libpython3.X.so next to the binary (the $ORIGIN rpath finds it)
pac bundle ./app --with-stdlib     # + a trimmed stdlib as ./python-home  -> runs with no environment variables at all
```
`pac build --relocatable` prints these next steps. Problems after the loader succeeds (no stdlib / bad `PA_PYTHON_HOME`)
become a friendly start-up fault (exit 70, `pa_boundary_fault.json` with `"direction": "STARTUP"`) that names the fix.
Third-party packages (numpy, ...) are never bundled: provide them with `PA_PYPATH` or `VIRTUAL_ENV`.
* Fault reports go to the current directory (`PA_FAULT_PATH` overrides). Examples read/write data in the current directory
  unless their `PA_*_DATA_DIR` variable is set.

## Tests
```bash
pytest -q                          # compiler, codegen, LSP, web shell, hardening, relocation
(cd vscode-pa && npm test)         # TextMate grammar
```
