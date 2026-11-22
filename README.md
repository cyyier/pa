# Pa

Python-syntax, C++-memory-layout, LLVM-compiled systems language. `*.pa` files compile to native code;
`*.paw` files are plain Python "crossing zones" with locked `In -> Out` signatures. Full spec: [ARCHITECTURE.md](https://github.com/cyyier/pa/blob/main/ARCHITECTURE.md);
AI skills: [skills/](https://github.com/cyyier/pa/tree/main/skills). Homepage: <https://palang.dev>.

## Quick start
See the full Quick Start, deployment, and troubleshooting guide at <https://palang.dev/quickstart>.

## Tests
```bash
pytest -q                          # compiler, codegen, LSP, hardening, relocation
(cd vscode-pa && npm test)         # TextMate grammar
```

## License
MIT, see [LICENSE](https://github.com/cyyier/pa/blob/main/LICENSE). Release process: [RELEASING.md](https://github.com/cyyier/pa/blob/main/RELEASING.md); history: [CHANGELOG.md](https://github.com/cyyier/pa/blob/main/CHANGELOG.md).
