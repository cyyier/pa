"""Release hygiene: one version everywhere, real metadata, license, platform guard."""
import json
import os
import re
import subprocess
import sys
import tomllib

import pytest

import pa_lang
from pa_lang.build import BuildError, compile_pa

ROOT = os.path.dirname(os.path.dirname(__file__))


def read(*p):
    with open(os.path.join(ROOT, *p), encoding="utf-8") as f:
        return f.read()


def project():
    return tomllib.loads(read("pyproject.toml"))["project"]


def test_version_is_single_sourced_and_consistent():
    pj = tomllib.loads(read("pyproject.toml"))
    assert "version" in pj["project"]["dynamic"]
    assert pj["tool"]["setuptools"]["dynamic"]["version"] == {"attr": "pa_lang.__version__"}
    v = pa_lang.__version__
    assert re.fullmatch(r"\d+\.\d+\.\d+", v)
    assert json.loads(read("vscode-pa", "package.json"))["version"] == v
    assert re.search(rf"^## \[{re.escape(v)}\] - \d{{4}}-\d{{2}}-\d{{2}}", read("CHANGELOG.md"), re.M)


def test_pac_version_flag():
    out = subprocess.run([sys.executable, "-m", "pa_lang.cli", "--version"], capture_output=True, text=True)
    assert out.returncode == 0 and out.stdout.strip() == f"pac {pa_lang.__version__}"


def test_project_metadata_is_complete():
    p = project()
    assert p["name"] == "pa-lang" and p["license"] == "MIT" and p["license-files"] == ["LICENSE"]
    assert p["authors"] == [{"name": "cyyier"}] and p["readme"] == "README.md"
    assert p["urls"]["Homepage"] == "https://palang.dev"
    assert p["urls"]["Repository"] == "https://github.com/cyyier/pa"
    assert p["urls"]["Issues"] == "https://github.com/cyyier/pa/issues"
    assert "Operating System :: POSIX :: Linux" in p["classifiers"]
    assert p["requires-python"] == ">=3.11"
    assert {"dev", "web", "release"} <= set(p["optional-dependencies"])
    assert all(re.search(r">=\d", d) for d in p["dependencies"])           # no unbounded-below runtime deps


def test_license_file_is_mit_with_the_author():
    text = read("LICENSE")
    assert text.startswith("MIT License") and "Copyright (c) 2026 cyyier" in text
    assert read("vscode-pa", "LICENSE") == text


def test_vscode_extension_metadata_points_at_the_real_project():
    pj = json.loads(read("vscode-pa", "package.json"))
    assert pj["license"] == "MIT" and pj["publisher"] == "cyyier"
    assert pj["homepage"] == "https://palang.dev"
    assert pj["repository"]["url"] == "https://github.com/cyyier/pa.git"
    assert pj["bugs"]["url"] == "https://github.com/cyyier/pa/issues"
    assert "example.invalid" not in json.dumps(pj)


def test_readme_links_are_absolute_for_pypi():
    readme = read("README.md")
    for target in re.findall(r"\]\(([^)]+)\)", readme):
        assert target.startswith(("http://", "https://")) or target.startswith("#"), target


def test_ci_and_release_workflows_exist_and_are_consistent():
    ci, rel = read(".github", "workflows", "ci.yml"), read(".github", "workflows", "release.yml")
    for needle in ('"3.11", "3.12", "3.13"', "pac doctor", "pytest -q", "twine check", "npm test"):
        assert needle in ci, needle
    for needle in ("tags: [\"v*\"]", "id-token: write", "pypa/gh-action-pypi-publish", "softprops/action-gh-release", "vsce package"):
        assert needle in rel, needle
    assert re.search(r"__version__", rel)                                       # tag must match the package version


def test_non_linux_build_gets_a_clear_error(tmp_path, monkeypatch):
    (tmp_path / "main.pa").write_text("def main() -> int:\n    return 0\n")
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.delenv("PA_ALLOW_UNSUPPORTED_PLATFORM", raising=False)
    with pytest.raises(BuildError, match="Linux only"):
        compile_pa(str(tmp_path / "main.pa"))
    ir = compile_pa(str(tmp_path / "main.pa"), emit_ir=True)                    # IR emission is platform independent
    assert "pa_user_main" in ir
