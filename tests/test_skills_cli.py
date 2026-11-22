"""Tests for `pac skill` CLI and bundled SKILL.md integrity."""
from pathlib import Path

from pa_lang.cli import main as pac_main
from pa_lang.skills_cmd import SKILL_ORDER, discover_skills


def test_bundled_skills_match_repo_root():
    repo_root = Path(__file__).resolve().parent.parent
    pkg_skills = repo_root / "pa_lang" / "skills"
    root_skills = repo_root / "skills"
    for name in SKILL_ORDER:
        a = (pkg_skills / name / "SKILL.md").read_text(encoding="utf-8")
        b = (root_skills / name / "SKILL.md").read_text(encoding="utf-8")
        assert a == b, f"pa_lang/skills/{name}/SKILL.md drifted from skills/{name}/SKILL.md"


def test_skill_list(capsys):
    rc = pac_main(["skill", "--list"])
    assert rc == 0
    out = capsys.readouterr().out
    for name in SKILL_ORDER:
        assert name in out


def test_skill_stdout_single_and_all(capsys):
    rc = pac_main(["skill", "pa-coder"])
    assert rc == 0
    out = capsys.readouterr().out
    assert out.startswith("---\nname: pa-coder\n")
    assert "# pa-coder" in out

    rc = pac_main(["skill"])
    assert rc == 0
    all_out = capsys.readouterr().out
    for name in SKILL_ORDER:
        assert f"name: {name}" in all_out


def test_skill_install_and_output_file(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    # Install all skills to a named agent target (.claude/skills/<name>/SKILL.md)
    rc = pac_main(["skill", "--install", "claude"])
    assert rc == 0
    out = capsys.readouterr().out
    for name in SKILL_ORDER:
        dest = tmp_path / ".claude" / "skills" / name / "SKILL.md"
        assert dest.is_file()
        assert f"name: {name}" in dest.read_text(encoding="utf-8")
        assert str(name) in out

    # Install a single skill to a custom directory
    custom = tmp_path / "my-agent-skills"
    rc = pac_main(["skill", "cpp-to-pa", "--install", str(custom)])
    assert rc == 0
    assert (custom / "cpp-to-pa" / "SKILL.md").is_file()
    assert not (custom / "pa-coder").exists()

    # Write combined skills to a single file via -o
    out_file = tmp_path / "PA_SKILLS.md"
    rc = pac_main(["skill", "-o", str(out_file)])
    assert rc == 0
    txt = out_file.read_text(encoding="utf-8")
    assert all(f"name: {n}" in txt for n in SKILL_ORDER)


def test_skill_unknown_name_returns_error(capsys):
    rc = pac_main(["skill", "nonexistent"])
    assert rc == 1
    assert "unknown skill" in capsys.readouterr().err
