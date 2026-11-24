"""Tests for `pac run` CLI subcommand and Windows static runtime linking."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from pa_lang import build as build_mod


def test_pac_run_dev_and_prod_leaves_no_artifacts(tmp_path: Path):
    src = tmp_path / "hello.pa"
    src.write_text(
        "def sum_n(n) -> int:\n"
        "    s = 0\n"
        "    for i in range(n):\n"
        "        s += i\n"
        "    return s\n\n"
        "def main() -> int:\n"
        "    print(sum_n(10))\n"
        "    return 0\n",
        encoding="utf-8",
    )
    before = set(p.name for p in tmp_path.iterdir())

    for mode in ("dev", "prod"):
        res = subprocess.run(
            [sys.executable, "-m", "pa_lang.cli", "run", "--mode", mode, str(src)],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            check=True,
        )
        assert res.stdout.strip() == "45"
        assert set(p.name for p in tmp_path.iterdir()) == before


def test_pac_run_propagates_exit_code_and_double_dash_args(tmp_path: Path):
    src = tmp_path / "exit42.pa"
    src.write_text("def main() -> int:\n    return 42\n", encoding="utf-8")
    res = subprocess.run(
        [sys.executable, "-m", "pa_lang.cli", "run", str(src), "--", "arg1", "arg2"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    assert res.returncode == 42
    assert list(p.name for p in tmp_path.iterdir()) == ["exit42.pa"]


def test_windows_mingw_shared_links_static_runtime(monkeypatch: pytest.MonkeyPatch):
    calls: list[list[str]] = []

    def fake_run(cmd, capture_output=True, text=True, **kwargs):
        calls.append(list(cmd))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(build_mod.subprocess, "run", fake_run)

    build_mod._link_binary(
        kind="gcc",
        cc="gcc",
        objs=["a.obj", "b.obj"],
        out="mod.pyd",
        shared=True,
        paws=set(),
        relocatable=False,
        pyinfo={"libdir": "C:/py/libs", "lib_name": "python312", "lib_file": ""},
    )
    assert len(calls) == 1
    cmd = calls[0]
    assert "-shared" in cmd
    assert "-static-libgcc" in cmd
    assert "-static-libstdc++" in cmd
    assert "-Wl,-Bstatic" in cmd and "-lwinpthread" in cmd and "-Wl,-Bdynamic" in cmd
    # Python import library must remain dynamically linked after -Wl,-Bdynamic
    assert cmd.index("-Wl,-Bdynamic") < cmd.index("-lpython312")


def test_windows_mingw_fallback_when_winpthread_missing(monkeypatch: pytest.MonkeyPatch):
    calls: list[list[str]] = []

    def fake_run(cmd, capture_output=True, text=True, **kwargs):
        calls.append(list(cmd))
        if "-lwinpthread" in cmd:
            return SimpleNamespace(returncode=1, stdout="", stderr="ld.exe: cannot find -lwinpthread")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(build_mod.subprocess, "run", fake_run)

    build_mod._link_binary(
        kind="gcc",
        cc="gcc",
        objs=["a.obj"],
        out="mod.pyd",
        shared=True,
        paws=set(),
        relocatable=False,
        pyinfo={"libdir": "C:/py/libs", "lib_name": "python312", "lib_file": ""},
    )
    assert len(calls) == 2
    assert "-lwinpthread" in calls[0]
    assert "-lwinpthread" not in calls[1]
    assert "-static-libgcc" in calls[1] and "-static-libstdc++" in calls[1]


def test_shared_abi3_limited_api_builds_and_imports(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import array
    import importlib

    src = tmp_path / "abi3_mod.pa"
    src.write_text(
        "@export\n"
        "def scale_sum(xs: span[float], k: float) -> float:\n"
        "    acc = 0.0\n"
        "    for i in range(len(xs)):\n"
        "        acc += xs[i] * k\n"
        "    return acc\n\n"
        "@export\n"
        "def str_len(s: str_view) -> int:\n"
        "    return len(s)\n",
        encoding="utf-8",
    )
    ext = ".pyd" if sys.platform == "win32" else ".abi3.so"
    out = tmp_path / f"abi3_mod{ext}"
    built = build_mod.compile_pa(str(src), out=str(out), shared=True, mode="prod", abi3=True)
    assert Path(built).is_file()

    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()
    mod = importlib.import_module("abi3_mod")
    assert abs(mod.scale_sum(array.array("d", [1.0, 2.0, 3.0]), 2.0) - 12.0) < 1e-12
    assert mod.str_len("hello") == 5


def test_windows_prefers_mingw_gcc_over_bare_clang(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("PA_CC", raising=False)
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(
        build_mod.shutil,
        "which",
        lambda name: {"clang": "C:/LLVM/bin/clang.exe", "gcc": "C:/msys64/ucrt64/bin/gcc.exe"}.get(name),
    )
    kind, cc = build_mod.find_c_compiler()
    assert kind == "mingw" and cc.endswith("gcc.exe")
