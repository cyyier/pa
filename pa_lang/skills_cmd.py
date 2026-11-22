"""pac skill: print or one-click install Pa's AI agent SKILL.md files."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

SKILL_ORDER = ("pa-coder", "py-to-pa", "cpp-to-pa")

AGENT_TARGETS = {
    "claude": (".claude/skills", "~/.claude/skills"),
    "cursor": (".cursor/skills", "~/.cursor/skills"),
    "codex": (".codex/skills", "~/.codex/skills"),
    "antigravity": (".agent/skills", "~/.gemini/antigravity/skills"),
}


def _skills_root() -> Path:
    pkg = Path(__file__).resolve().parent / "skills"
    if pkg.is_dir():
        return pkg
    return Path(__file__).resolve().parent.parent / "skills"


def discover_skills() -> dict[str, Path]:
    root = _skills_root()
    found: dict[str, Path] = {}
    if not root.is_dir():
        return found
    for name in SKILL_ORDER:
        p = root / name / "SKILL.md"
        if p.is_file():
            found[name] = p
    for child in sorted(root.iterdir()):
        p = child / "SKILL.md"
        if child.is_dir() and p.is_file() and child.name not in found:
            found[child.name] = p
    return found


def _frontmatter_desc(text: str) -> str:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return ""
    for line in lines[1:]:
        if line.strip() == "---":
            break
        if line.startswith("description:"):
            return line.split(":", 1)[1].strip()
    return ""


def read_skill(name: str) -> str:
    skills = discover_skills()
    if name not in skills:
        avail = ", ".join(skills) or "(none)"
        raise KeyError(f"unknown skill '{name}' (available: {avail}, all)")
    return skills[name].read_text(encoding="utf-8")


def render_skills(names: list[str]) -> str:
    parts = [read_skill(n).rstrip() for n in names]
    if len(parts) == 1:
        return parts[0] + "\n"
    return ("\n\n---\n\n").join(parts) + "\n"


def resolve_target_dir(target: str, global_install: bool = False) -> Path:
    if target in AGENT_TARGETS:
        local_rel, global_rel = AGENT_TARGETS[target]
        return Path(os.path.expanduser(global_rel)) if global_install else Path(local_rel)
    return Path(os.path.expanduser(target))


def install_skills(names: list[str], target: str, global_install: bool = False) -> list[Path]:
    base = resolve_target_dir(target, global_install=global_install)
    written: list[Path] = []
    for n in names:
        content = read_skill(n)
        dest = base / n / "SKILL.md"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(content, encoding="utf-8")
        written.append(dest)
    return written


def list_skills_text() -> str:
    skills = discover_skills()
    lines = []
    for name, path in skills.items():
        desc = _frontmatter_desc(path.read_text(encoding="utf-8"))
        lines.append(f"{name:12s}  {desc}" if desc else name)
    return "\n".join(lines)


def run_skill_cmd(name: Optional[str], list_only: bool, output: Optional[str],
                  install: Optional[str], global_install: bool) -> tuple[int, str, str]:
    skills = discover_skills()
    if list_only:
        return 0, list_skills_text() + "\n", ""
    if not name or name == "all":
        selected = list(skills.keys())
    else:
        if name not in skills:
            avail = ", ".join(list(skills.keys()) + ["all"])
            return 1, "", f"error: unknown skill '{name}' (choose from: {avail})\n"
        selected = [name]
    if not selected:
        return 1, "", "error: no bundled SKILL.md files found\n"
    if install:
        written = install_skills(selected, install, global_install=global_install)
        lines = [f"installed {p}" for p in written]
        return 0, "\n".join(lines) + "\n", ""
    text = render_skills(selected)
    if output:
        out_path = Path(output)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(text, encoding="utf-8")
        return 0, f"wrote {out_path}\n", ""
    return 0, text, ""
