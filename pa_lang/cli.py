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
        diags += check_file(f, mode=args.mode, include_dirs=args.include or None)
    if args.ai_diagnostic:
        print(json.dumps([d.to_dict() for d in diags], ensure_ascii=False, indent=2))
    else:
        for d in diags:
            print(d.brief())
    return 1 if any(d.severity == "error" for d in diags) else 0


def cmd_build(args) -> int:
    from .build import BuildError, compile_pa
    try:
        out = compile_pa(args.file, out=args.output, shared=args.shared, emit_ir=args.emit_ir, mode=args.mode,
                         python_home=args.python_home, python_path=args.python_path or None,
                         relocatable=args.relocatable, include_dirs=args.include or None)
    except BuildError as e:
        print(e, file=sys.stderr)
        return 1
    print(out)
    if args.relocatable and not args.emit_ir and not args.shared:
        from .doctor import relocatable_notice
        note = relocatable_notice(out)
        if note:
            print(note, file=sys.stderr)
    return 0


def cmd_doctor(args) -> int:
    from .doctor import check_binary, check_environment
    rep = check_binary(args.binary) if args.binary else check_environment()
    print(rep.text())
    return 0 if rep.ok else 1


def cmd_bundle(args) -> int:
    from .doctor import bundle
    rep = bundle(args.binary, dest=args.dest, with_stdlib=args.with_stdlib)
    print(rep.text())
    return 0 if rep.ok else 1


def cmd_lsp(args) -> int:
    from .lsp_server import main as lsp_main
    lsp_main()
    return 0


def cmd_skill(args) -> int:
    from .skills_cmd import run_skill_cmd
    rc, out, err = run_skill_cmd(
        name=args.name,
        list_only=args.list,
        output=args.output,
        install=args.install,
        global_install=args.global_install,
    )
    if out:
        sys.stdout.write(out)
    if err:
        sys.stderr.write(err)
    return rc


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="pac", description="Pa language compiler")
    from . import __version__
    ap.add_argument("--version", action="version", version=f"pac {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check", help="front-end checks")
    c.add_argument("paths", nargs="+")
    c.add_argument("--ai-diagnostic", action="store_true", help="emit JSON repair tickets")
    c.add_argument("--mode", choices=("dev", "prod"), default="dev", help="dev: deep .paw In->Out lint (default)")
    c.add_argument("-I", "--include", action="append", help="extra directory to search for .pa modules (repeatable)")
    c.set_defaults(fn=cmd_check)
    b = sub.add_parser("build", help="compile a .pa file")
    b.add_argument("file")
    b.add_argument("-o", "--output")
    b.add_argument("--mode", choices=("dev", "prod"), default="dev",
                   help="dev: deep .paw lint + full boundary customs; prod: signature check + light customs")
    b.add_argument("-I", "--include", action="append", help="extra directory to search for .pa modules (repeatable)")
    b.add_argument("--python-home", help="default CPython prefix baked as a run-time fallback (PA_PYTHON_HOME overrides)")
    b.add_argument("--python-path", action="append", help="default sys.path entry baked as a fallback (repeatable; PA_PYPATH overrides)")
    b.add_argument("--relocatable", action="store_true", help="bake no Python paths at all (rely on PA_PYTHON_HOME / PA_PYPATH / VIRTUAL_ENV)")
    b.add_argument("--shared", action="store_true", help="build a shared library of @export functions")
    b.add_argument("--emit-ir", action="store_true", help="print LLVM IR instead of linking")
    b.set_defaults(fn=cmd_build)
    d = sub.add_parser("doctor", help="diagnose the build environment, or why a built binary cannot start")
    d.add_argument("binary", nargs="?", help="a binary built by pac (omit to check this machine)")
    d.set_defaults(fn=cmd_doctor)
    bu = sub.add_parser("bundle", help="copy libpython (and optionally the stdlib) next to a binary")
    bu.add_argument("binary")
    bu.add_argument("--dest", help="directory to bundle into (default: the binary's directory)")
    bu.add_argument("--with-stdlib", action="store_true", help="also ship a trimmed stdlib as <dest>/python-home")
    bu.set_defaults(fn=cmd_bundle)
    l = sub.add_parser("lsp", help="start the language server (stdio)")
    l.set_defaults(fn=cmd_lsp)
    sk = sub.add_parser("skill", help="print or install Pa's AI agent SKILL.md files")
    sk.add_argument("name", nargs="?", default="all",
                    help="skill name: pa-coder | py-to-pa | cpp-to-pa | all (default: all)")
    sk.add_argument("--list", action="store_true", help="list bundled skills")
    sk.add_argument("-o", "--output", help="write SKILL.md content to a file instead of stdout")
    sk.add_argument("--install", metavar="TARGET",
                    help="install into an agent skills dir: claude | cursor | codex | copilot | github | antigravity | <dir>")
    sk.add_argument("--global", dest="global_install", action="store_true",
                    help="with --install claude|cursor|codex|copilot|antigravity, install to user home (~/) instead of project")
    sk.set_defaults(fn=cmd_skill)
    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
