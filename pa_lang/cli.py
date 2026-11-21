"""pac: check [--ai-diagnostic] | build [-o bin | --shared] | lsp"""
from __future__ import annotations

import argparse
import json
import os
import sys

from .validator import check_file


def collect(paths: list) -> list:
    out = []
    for p in paths:
        if os.path.isdir(p):
            for root, _, files in os.walk(p):
                if any(x in root for x in (".venv", "__pycache__", ".git")):
                    continue
                out += [os.path.join(root, f) for f in sorted(files) if f.endswith((".pa", ".paw"))]
        else:
            out.append(p)
    return out


def cmd_check(args) -> int:
    diags = []
    for f in collect(args.paths):
        diags += check_file(f, mode=args.mode)
    if args.ai_diagnostic:
        print(json.dumps([d.to_dict() for d in diags], ensure_ascii=False, indent=2))
    else:
        for d in diags:
            print(d.brief())
    return 1 if any(d.severity == "error" for d in diags) else 0


def cmd_build(args) -> int:
    from .build import BuildError, compile_pa
    try:
        out = compile_pa(args.file, out=args.output, shared=args.shared, emit_ir=args.emit_ir, mode=args.mode)
    except BuildError as e:
        print(e, file=sys.stderr)
        return 1
    print(out)
    return 0


def cmd_lsp(args) -> int:
    from .lsp_server import main as lsp_main
    lsp_main()
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="pac", description="Pa language compiler")
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check", help="front-end checks")
    c.add_argument("paths", nargs="+")
    c.add_argument("--ai-diagnostic", action="store_true", help="emit JSON repair tickets")
    c.add_argument("--mode", choices=("dev", "prod"), default="dev", help="dev: deep .paw In->Out lint (default)")
    c.set_defaults(fn=cmd_check)
    b = sub.add_parser("build", help="compile a .pa file")
    b.add_argument("file")
    b.add_argument("-o", "--output")
    b.add_argument("--mode", choices=("dev", "prod"), default="dev",
                   help="dev: deep .paw lint + full boundary customs; prod: signature check + light customs")
    b.add_argument("--shared", action="store_true", help="build a shared library of @export functions")
    b.add_argument("--emit-ir", action="store_true", help="print LLVM IR instead of linking")
    b.set_defaults(fn=cmd_build)
    l = sub.add_parser("lsp", help="start the language server (stdio)")
    l.set_defaults(fn=cmd_lsp)
    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
