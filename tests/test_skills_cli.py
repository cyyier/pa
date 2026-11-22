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


def test_skill_install_idempotent_preserves_custom_and_supports_copilot(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    # Create a user custom skill alongside managed skills
    custom_skill = tmp_path / ".claude" / "skills" / "my-custom" / "SKILL.md"
    custom_skill.parent.mkdir(parents=True)
    custom_skill.write_text("# user custom skill\n", encoding="utf-8")

    # 1st install -> installed
    assert pac_main(["skill", "--install", "claude"]) == 0
    out1 = capsys.readouterr().out
    assert out1.count("installed") == 3

    # 2nd install -> up-to-date, custom skill untouched
    assert pac_main(["skill", "--install", "claude"]) == 0
    out2 = capsys.readouterr().out
    assert out2.count("up-to-date") == 3
    assert custom_skill.read_text(encoding="utf-8") == "# user custom skill\n"

    # Simulate stale managed skill -> updated
    managed = tmp_path / ".claude" / "skills" / "pa-coder" / "SKILL.md"
    managed.write_text("# old version\n", encoding="utf-8")
    assert pac_main(["skill", "--install", "claude"]) == 0
    out3 = capsys.readouterr().out
    assert "updated" in out3
    assert out3.count("up-to-date") == 2
    assert "# pa-coder" in managed.read_text(encoding="utf-8")
    assert custom_skill.read_text(encoding="utf-8") == "# user custom skill\n"

    # Copilot / GitHub target -> .github/skills/<name>/SKILL.md
    assert pac_main(["skill", "--install", "copilot"]) == 0
    capsys.readouterr()
    for name in SKILL_ORDER:
        assert (tmp_path / ".github" / "skills" / name / "SKILL.md").is_file()
