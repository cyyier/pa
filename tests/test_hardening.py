"""Edge-case hardening: runtime faults, new compile-time checks, Python-compatible printing."""
import ctypes
import json
import subprocess
import textwrap

import pytest

from pa_lang.build import BuildError, compile_pa
from pa_lang.validator import check_source
from tests.test_codegen import run_pa


def codes(src, name="t.pa", **kw):
    return [d.code for d in check_source(textwrap.dedent(src), name, **kw)]


def fault(tmp_path, name="pa_runtime_fault.json"):
    return json.loads((tmp_path / name).read_text())


# ---------------------------------------------------------------- runtime faults (dev mode)
def test_int_division_and_modulo_by_zero_fault(tmp_path):
    for op in ("//", "%"):
        run_pa(tmp_path, f"def main() -> int:\n    z = 0\n    print(10 {op} z)\n    return 0\n", expect_code=70)
        f = fault(tmp_path)
        assert f["kind"] == "ZeroDivisionError" and f["function"] == "main" and f["line"] == 3


def test_int_min_div_minus_one_does_not_trap(tmp_path):
    r = run_pa(tmp_path, """
        def main() -> int:
            m = -9223372036854775807 - 1
            d = -1
            print(m // d, m % d, 7 // d, 7 % d)
            return 0
    """)
    assert r.stdout.split() == ["-9223372036854775808", "0", "-7", "0"]


def test_index_out_of_range_and_negative_index_fault_in_dev(tmp_path):
    for idx in ("10", "-1"):
        run_pa(tmp_path, f"def main() -> int:\n    a = [1, 2, 3]\n    i = {idx}\n    print(a[i])\n    return 0\n", expect_code=70)
        f = fault(tmp_path)
        assert f["kind"] == "IndexError" and f"index {idx} out of range for length 3" in f["detail"]
    run_pa(tmp_path, """
        def main() -> int:
            v = []
            v.append(1)
            i = 1
            v[i] = 5
            return 0
    """, expect_code=70)
    run_pa(tmp_path, "def main() -> int:\n    m = [[0.0] * 3] * 2\n    j = 3\n    m[1, j] = 1.0\n    return 0\n", expect_code=70)
    run_pa(tmp_path, 'def main() -> int:\n    s = "abc"\n    i = 3\n    print(s[i])\n    return 0\n', expect_code=70)


def test_bounds_checks_are_dev_only(tmp_path):
    src = "def main() -> int:\n    a = [1, 2, 3]\n    b = [7, 8, 9]\n    i = 2\n    print(a[i], b[i - 1])\n    return 0\n"
    r = run_pa(tmp_path, src, mode="prod")
    assert r.stdout.strip() == "3 8"
    ir_dev = compile_pa(str(tmp_path / "main.pa"), emit_ir=True, mode="dev")
    ir_prod = compile_pa(str(tmp_path / "main.pa"), emit_ir=True, mode="prod")
    assert "pa_rt_index_fault" in ir_dev.split("define", 1)[1] and "pa_rt_index_fault" not in ir_prod.split("define", 1)[1]


def test_div_by_zero_still_faults_in_prod(tmp_path):
    run_pa(tmp_path, "def main() -> int:\n    z = 0\n    print(1 // z)\n    return 0\n", expect_code=70, mode="prod")
    assert fault(tmp_path)["mode"] == "prod"


def test_constant_index_errors_at_compile_time(tmp_path):
    assert "PA_E073" in codes("def f() -> int:\n    a = [1, 2]\n    return a[-1]\n")
    assert "PA_E073" in codes("def f() -> int:\n    m = [[1.0]*2]*2\n    return m[0, -1]\n")
    (tmp_path / "main.pa").write_text("def main() -> int:\n    a = [1, 2, 3]\n    print(a[3])\n    return 0\n")
    with pytest.raises(BuildError, match="out of range for a list of length 3"):
        compile_pa(str(tmp_path / "main.pa"))


# ---------------------------------------------------------------- range steps
def test_range_with_variable_step(tmp_path):
    r = run_pa(tmp_path, """
        def count(a, b, s) -> int:
            n = 0
            for i in range(a, b, s):
                n += 1
            return n

        def main() -> int:
            print(count(a=5, b=0, s=-1), count(a=0, b=10, s=3), count(a=0, b=5, s=1), count(a=5, b=0, s=1), count(a=0, b=5, s=-1), count(a=10, b=0, s=-4))
            return 0
    """)
    assert r.stdout.strip() == "5 4 5 0 0 3"


def test_range_zero_step(tmp_path):
    run_pa(tmp_path, "def main() -> int:\n    s = 0\n    for i in range(0, 5, s):\n        pass\n    return 0\n", expect_code=70)
    assert fault(tmp_path)["kind"] == "ValueError"
    (tmp_path / "main.pa").write_text("def main() -> int:\n    for i in range(0, 5, 0):\n        pass\n    return 0\n")
    with pytest.raises(BuildError, match="step must not be zero"):
        compile_pa(str(tmp_path / "main.pa"))


# ---------------------------------------------------------------- big [x] * N
def test_big_repeat_goes_to_heap(tmp_path):
    r = run_pa(tmp_path, """
        def make() -> Vec[float]:
            a = [1.5] * 100000
            return a

        def main() -> int:
            a = [1.5] * 100000
            a[99999] = 2.5
            t = 0.0
            for x in a:
                t += x
            m = [[0.0] * 512] * 512
            m[511, 511] = 7.0
            b = make()
            print(len(a), t, m[511, 511], len(m), len(b), b[5])
            return 0
    """)
    assert r.stdout.strip() == "100000 150001.0 7.0 512 100000 1.5"


# ---------------------------------------------------------------- compile-time diagnostics
def test_variable_type_change_is_a_diagnostic_not_a_crash():
    assert "PA_E086" in codes('def f() -> int:\n    x = "a"\n    x = 5\n    return 0\n')
    assert "PA_E086" in codes("def f() -> int:\n    a = [1, 2]\n    a = [1, 2, 3]\n    return 0\n")
    assert "PA_E086" not in codes("def f() -> float:\n    x = 1\n    x = 2.5\n    return x\n")
    assert "PA_E086" not in codes('def f() -> int:\n    s = "a"\n    s = s + "b"\n    return 0\n')


def test_mutation_while_iterating_rejected():
    base = "def f() -> int:\n    v = []\n    v.append(1)\n    for x in v:\n        {}\n    return 0\n"
    for body in ("v.append(x)", "v = []", "v += 1"):
        assert "PA_E087" in codes(base.format(body)), body
    assert "PA_E087" in codes("def g(b) -> int:\n    b.append(1)\n    return 0\n\n" + base.format("g(mut(v))"))
    assert "PA_E087" not in codes(base.format("v[0] = x"))
    ok = "def f() -> int:\n    v = []\n    v.append(1)\n    for i in range(len(v)):\n        v.append(i)\n    return 0\n"
    assert "PA_E087" not in codes(ok)


def test_possibly_unassigned_variable_rejected():
    assert "PA_E088" in codes("def f(c) -> int:\n    if c > 0:\n        y = 1\n    return y\n")
    assert "PA_E088" in codes("def f(n) -> int:\n    for i in range(n):\n        last = i\n    return last\n")
    assert "PA_E088" in codes("def f(n) -> int:\n    while n > 0:\n        k = 1\n        n -= 1\n    return k\n")
    assert "PA_E088" in codes("def f() -> int:\n    x += 1\n    return 0\n")
    ok = ("def f(c) -> int:\n    if c > 0:\n        y = 1\n    else:\n        y = 2\n    z = 0\n"
          "    for i in range(3):\n        z += i\n    if c < 0:\n        return 0\n    else:\n        w = 1\n    return y + z + w\n")
    assert "PA_E088" not in codes(ok)
    assert "PA_E088" not in codes("def f(c) -> int:\n    if c > 0:\n        y = 1\n        return y\n    return 0\n")


def test_aliasing_with_mut_rejected():
    callee = "def f(a, b) -> int:\n    b.append(a[0])\n    return len(a)\n\n"
    assert "PA_E089" in codes(callee + "def g() -> int:\n    v = []\n    v.append(1)\n    return f(v, mut(v))\n")
    assert "PA_E089" in codes(callee + "def g() -> int:\n    v = []\n    v.append(1)\n    return f(mut(v), mut(v))\n")
    assert "PA_E089" not in codes(callee + "def g() -> int:\n    v = []\n    w = []\n    v.append(1)\n    w.append(1)\n    return f(v, mut(w))\n")
    assert "PA_E089" not in codes("def h(a, n) -> int:\n    a.append(n)\n    return 0\n\n"
                                  "def g() -> int:\n    v = []\n    v.append(1)\n    return h(mut(v), len(v))\n")


# ---------------------------------------------------------------- narrowing at boundaries
def test_paw_narrowing_is_checked_and_literals_rejected(tmp_path):
    paws = {"w": "def f(x: u8) -> i64:\n    return x\n\ndef g(x: i32) -> i64:\n    return x\n"}
    run_pa(tmp_path, "import w\n\ndef main() -> int:\n    x = 300\n    print(w.f(x))\n    return 0\n", paws=paws, expect_code=70)
    assert fault(tmp_path)["kind"] == "OverflowError"
    run_pa(tmp_path, "import w\n\ndef main() -> int:\n    x = 2147483648\n    print(w.g(x))\n    return 0\n", paws=paws, expect_code=70, mode="prod")
    r = run_pa(tmp_path, "import w\n\ndef main() -> int:\n    x = 255\n    y = -5\n    print(w.f(x), w.g(y))\n    return 0\n", paws=paws)
    assert r.stdout.strip() == "255 -5"
    (tmp_path / "main.pa").write_text("import w\n\ndef main() -> int:\n    print(w.f(300))\n    return 0\n")
    with pytest.raises(BuildError, match="literal 300 does not fit u8"):
        compile_pa(str(tmp_path / "main.pa"))


def test_float_never_passed_to_int_paw_param(tmp_path):
    (tmp_path / "w.paw").write_text("def f(x: i64) -> i64:\n    return x\n")
    d = codes("import w\n\ndef main() -> int:\n    return w.f(1.5)\n", "main.pa", search_dirs=[str(tmp_path)])
    assert d == ["PA_E090"]


def test_narrow_struct_field_assignment_checked_in_dev(tmp_path):
    src = "@struct\nclass P:\n    b: u8\n\ndef main() -> int:\n    p = P(b=1)\n    x = 300\n    p.b = x\n    print(p.b)\n    return 0\n"
    run_pa(tmp_path, src, expect_code=70)
    assert fault(tmp_path)["kind"] == "OverflowError"


# ---------------------------------------------------------------- module-level constants & printing
def test_module_level_float_str_bool_constants(tmp_path):
    r = run_pa(tmp_path, """
        PI = 3.14159
        TAU = 2.0 * PI
        NEG = -2.5
        NAME = "pa"
        ON = True
        N = 4

        def area(r) -> float:
            return PI * r * r

        def main() -> int:
            print(area(2.0), TAU, NEG, NAME, ON, N * 2)
            return 0
    """)
    assert r.stdout.strip() == "12.56636 6.28318 -2.5 pa True 8"


def test_print_matches_python_repr(tmp_path):
    values = ["15.0", "0.1 + 0.2", "1.0 / 3.0", "1e20", "1.5e-07", "-0.0", "100.0", "1e16", "12345678.9", "123456789012345.6",
              "0.0001", "0.00001", "2.5e-05", "1e15", "5e-324", "1.7976931348623157e308"]
    expect = [repr(eval(v)) for v in values]
    r = run_pa(tmp_path, "def main() -> int:\n    print(" + ", ".join(values) + ")\n    return 0\n")
    assert r.stdout.split() == expect


# ---------------------------------------------------------------- shared libs survive faults
def test_shared_library_fault_returns_to_host(tmp_path):
    src = tmp_path / "lib.pa"
    src.write_text(textwrap.dedent("""
        @export
        def ratio(a: int, b: int) -> int:
            return a // b

        @export
        def at(xs: span[float], i: int) -> float:
            return xs[i]
    """))
    so = compile_pa(str(src), out=str(tmp_path / "lib.so"), shared=True)
    lib = ctypes.CDLL(so)
    lib.pa_error_message.restype = ctypes.c_char_p
    lib.pa_error_json.restype = ctypes.c_char_p
    lib.ratio.argtypes, lib.ratio.restype = [ctypes.c_int64, ctypes.c_int64], ctypes.c_int64

    class Span(ctypes.Structure):
        _fields_ = [("p", ctypes.c_void_p), ("n", ctypes.c_int64)]
    lib.at.argtypes, lib.at.restype = [Span, ctypes.c_int64], ctypes.c_double

    assert lib.ratio(7, 2) == 3 and lib.pa_error_code() == 0
    assert lib.ratio(7, 0) == 0                      # process is still alive
    assert lib.pa_error_code() == 70 and b"ZeroDivisionError" in lib.pa_error_message()
    assert json.loads(lib.pa_error_json())["kind"] == "ZeroDivisionError"
    assert lib.ratio(9, 3) == 3 and lib.pa_error_code() == 0     # the next call starts clean
    buf = (ctypes.c_double * 3)(1.0, 2.0, 3.0)
    assert lib.at(Span(ctypes.cast(buf, ctypes.c_void_p), 3), 1) == 2.0 and lib.pa_error_code() == 0
    lib.at(Span(ctypes.cast(buf, ctypes.c_void_p), 3), 7)
    assert lib.pa_error_code() == 70 and b"IndexError" in lib.pa_error_message()


def test_shared_library_boundary_fault_returns_to_host_and_releases_gil(tmp_path):
    (tmp_path / "w.paw").write_text("def f(x: f64) -> f64:\n    return None\n")
    src = tmp_path / "lib.pa"
    src.write_text("import w\n\n@export\ndef go(x: float) -> float:\n    return w.f(x)\n")
    so = compile_pa(str(src), out=str(tmp_path / "lib.so"), shared=True, mode="prod")
    lib = ctypes.CDLL(so)
    lib.go.argtypes, lib.go.restype = [ctypes.c_double], ctypes.c_double
    lib.pa_error_message.restype = ctypes.c_char_p
    lib.go(1.0)
    assert lib.pa_error_code() == 70 and b"None" in lib.pa_error_message()
    lib.go(2.0)                                       # a second call would deadlock if the GIL leaked
    assert lib.pa_error_code() == 70


def test_math_intrinsics_and_deep_call_chain(tmp_path):
    r = run_pa(tmp_path, """
        def l4(x) -> float:
            return sqrt(x) + floor(1.9) + ceil(1.1)

        def l3(x) -> float:
            return l4(x) + sin(0.0) + cos(0.0) + tan(0.0) + atan(0.0) + atan2(0.0, 1.0)

        def l2(x) -> float:
            return l3(x) + log(exp(1.0))

        def l1(x) -> float:
            return l2(x)

        def main() -> int:
            print(l1(9.0), int(5000000000.5))
            return 0
    """)
    assert r.stdout.strip() == "8.0 5000000000"

