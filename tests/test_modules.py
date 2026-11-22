"""Tests for multi-file .pa module imports, package discovery, mangling, and cross-module checks."""
from __future__ import annotations

import subprocess
import sys
import textwrap

from pa_lang.build import compile_pa
from pa_lang.validator import check_file, check_source


def _w(path, content: str) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(content).lstrip("\n"), encoding="utf-8")
    return str(path)


def test_import_pa_module_and_from_import(tmp_path):
    _w(tmp_path / "geom.pa", """
        PI2 = 6.283185307179586

        @struct
        class Vec2:
            x: float
            y: float

        def dot(a, b) -> float:
            return a.x * b.x + a.y * b.y

        def scale_in_place(xs, k) -> void:
            for i in range(len(xs)):
                xs[i] = xs[i] * k
    """)
    main = _w(tmp_path / "main.pa", """
        import geom
        from geom import Vec2 as V2, scale_in_place

        def main() -> int:
            a = geom.Vec2(3.0, 4.0)
            b = V2(1.0, 2.0)
            d = geom.dot(a, b)
            arr = [1.0, 2.0, 3.0]
            scale_in_place(mut(arr), 2.0)
            print(d, arr[2], geom.PI2 > 6.0)
            return 0
    """)
    exe = compile_pa(main, out=str(tmp_path / "prog"))
    r = subprocess.run([exe], capture_output=True, text=True, check=True)
    assert r.stdout.strip() == "11.0 6.0 True"


def test_diamond_imports_and_name_collision_isolation(tmp_path):
    # Both left.pa and right.pa define `K`, `helper()`, and `@struct Box` AND both import `base.pa`
    _w(tmp_path / "base.pa", """
        BASE = 100

        def add_base(x) -> int:
            return x + BASE
    """)
    _w(tmp_path / "left.pa", """
        import base

        K = 2

        @struct
        class Box:
            v: int

        def helper(x) -> int:
            return base.add_base(x) * K

        def compute(x) -> int:
            b = Box(helper(x))
            return b.v
    """)
    _w(tmp_path / "right.pa", """
        from base import add_base

        K = 3

        @struct
        class Box:
            v: int

        def helper(x) -> int:
            return add_base(x) * K

        def compute(x) -> int:
            b = Box(helper(x))
            return b.v
    """)
    main = _w(tmp_path / "main.pa", """
        import left
        import right as rt

        def main() -> int:
            # Shadowing test: local K must not be confused with imported constants
            K = 7
            print(left.compute(1), rt.compute(1), K)
            return 0
    """)
    exe = compile_pa(main, out=str(tmp_path / "prog"))
    r = subprocess.run([exe], capture_output=True, text=True, check=True)
    assert r.stdout.strip() == "202 303 7"


def test_package_dotted_relative_and_pa_path_imports(tmp_path, monkeypatch):
    lib_root = tmp_path / "ext_libs"
    _w(lib_root / "mylib" / "__init__.pa", """
        from . import core
        VERSION = 1
    """)
    _w(lib_root / "mylib" / "core.pa", """
        def square(x) -> int:
            return x * x
    """)
    _w(lib_root / "mylib" / "ops.pa", """
        from .core import square

        def sum_sq(a, b) -> int:
            return square(a) + square(b)
    """)
    main = _w(tmp_path / "app" / "main.pa", """
        import mylib.ops as ops
        from mylib import core

        def main() -> int:
            print(ops.sum_sq(3, 4), core.square(5))
            return 0
    """)
    monkeypatch.setenv("PA_PATH", str(lib_root))
    exe = compile_pa(main, out=str(tmp_path / "app" / "prog"))
    r = subprocess.run([exe], capture_output=True, text=True, check=True)
    assert r.stdout.strip() == "25 25"


def test_installed_python_package_pa_discovery(tmp_path, monkeypatch):
    site_dir = tmp_path / "fake_site"
    _w(site_dir / "pa_demo_pkg" / "__init__.py", "")
    _w(site_dir / "pa_demo_pkg" / "fastmath.pa", """
        def hypot2(a, b) -> float:
            return a * a + b * b
    """)
    monkeypatch.syspath_prepend(str(site_dir))
    main = _w(tmp_path / "main.pa", """
        from pa_demo_pkg import fastmath

        def main() -> int:
            print(fastmath.hypot2(3.0, 4.0))
            return 0
    """)
    exe = compile_pa(main, out=str(tmp_path / "prog"))
    r = subprocess.run([exe], capture_output=True, text=True, check=True)
    assert r.stdout.strip() == "25.0"


def test_transitive_pa_to_paw_import(tmp_path):
    sub = tmp_path / "libs"
    _w(sub / "py_helper.paw", """
        def double_sum(arr: span[f64]) -> f64:
            return float(sum(arr)) * 2.0
    """)
    _w(sub / "stats.pa", """
        import py_helper

        def score(xs) -> float:
            return py_helper.double_sum(xs)
    """)
    main = _w(tmp_path / "main.pa", """
        from libs import stats

        def main() -> int:
            xs = [1.0, 2.0, 3.0]
            print(stats.score(xs))
            return 0
    """)
    exe = compile_pa(main, out=str(tmp_path / "prog"))
    r = subprocess.run([exe], capture_output=True, text=True, check=True)
    assert r.stdout.strip() == "12.0"


def test_cross_module_safety_diagnostics(tmp_path):
    # 1. Missing mut() across module boundary -> PA_E050
    _w(tmp_path / "mutlib.pa", """
        def bump(xs) -> void:
            xs[0] = xs[0] + 1
    """)
    bad_mut = _w(tmp_path / "bad_mut.pa", """
        import mutlib

        def main() -> int:
            a = [1, 2]
            mutlib.bump(a)
            return 0
    """)
    assert "PA_E050" in [d.code for d in check_file(bad_mut)]

    # 2. Circular import -> PA_E001
    _w(tmp_path / "cyc_a.pa", "import cyc_b\ndef fa() -> int:\n    return 1\n")
    _w(tmp_path / "cyc_b.pa", "import cyc_a\ndef fb() -> int:\n    return 2\n")
    assert "PA_E001" in [d.code for d in check_file(str(tmp_path / "cyc_a.pa"))]

    # 3. Missing symbol -> PA_E001
    bad_sym = _w(tmp_path / "bad_sym.pa", """
        from mutlib import does_not_exist

        def main() -> int:
            return 0
    """)
    assert "PA_E001" in [d.code for d in check_file(bad_sym)]

    # 4. Transitive .paw call inside a loop -> PA_E002
    _w(tmp_path / "bridge.paw", "def ping(x: f64) -> f64:\n    return x\n")
    _w(tmp_path / "wrap.pa", "import bridge\ndef call_py(x) -> float:\n    return bridge.ping(x)\n")
    bad_loop = _w(tmp_path / "bad_loop.pa", """
        import wrap

        def main() -> int:
            s = 0.0
            for i in range(5):
                s = s + wrap.call_py(1.0)
            return 0
    """)
    assert "PA_E002" in [d.code for d in check_file(bad_loop)]
