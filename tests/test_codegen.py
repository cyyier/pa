import ctypes
import json
import subprocess
import textwrap

import pytest

from pa_lang.build import BuildError, compile_pa


def run_pa(tmp_path, src, paws=None, expect_code=0, env=None, mode="dev"):
    for name, body in (paws or {}).items():
        (tmp_path / f"{name}.paw").write_text(textwrap.dedent(body))
    main = tmp_path / "main.pa"
    main.write_text(textwrap.dedent(src))
    exe = compile_pa(str(main), out=str(tmp_path / "prog"), mode=mode)
    r = subprocess.run([exe], capture_output=True, text=True, cwd=tmp_path, env=env)
    assert r.returncode == expect_code, (r.stdout, r.stderr)
    return r


def test_scalars_loops_and_fixed_list(tmp_path):
    r = run_pa(tmp_path, """
        N = 5
        def total(xs):
            s = 0.0
            for i in range(N):
                s = s + xs[i]
            return s

        def main():
            a = [1.0, 2.0, 3.0, 4.0, 5.0]
            print(total(a))
            k = 0
            while k < 3:
                k += 1
            print(k, 7 // 2, 7 % 4, 7 / 2)
            return 0
    """)
    assert r.stdout.split("\n")[:2] == ["15.0", "3 3 3 3.5"]


def test_struct_and_stack_vec_and_heap_vec(tmp_path):
    r = run_pa(tmp_path, """
        @struct
        class P:
            x: f64
            n: i32

        def squares(n):
            out = []
            for i in range(n):
                out.append(i * i)
            return out

        def main():
            p = P(x=1.5, n=4)
            p.n = p.n + 1
            small = []
            for i in range(10):
                small.append(i * 2)
            big = squares(100)
            print(p.x, p.n, len(small), small[9], len(big), big[99])
            return 0
    """)
    assert r.stdout.strip() == "1.5 5 10 18 100 9801"


def test_vec_growth_past_initial_capacity_and_mut(tmp_path):
    r = run_pa(tmp_path, """
        def fill(buf, n):
            for i in range(n):
                buf.append(i + 1)
            return 0

        def main():
            v = []
            v.append(0)
            fill(mut(v), 999)
            t = 0
            for x in v:
                t += x
            print(len(v), t)
            return 0
    """)
    assert r.stdout.strip() == "1000 499500"


def test_value_semantics_deep_copy(tmp_path):
    r = run_pa(tmp_path, """
        def main():
            a = [1, 2, 3]
            b = a
            b[0] = 99
            print(a[0], b[0])
            return 0
    """)
    assert r.stdout.strip() == "1 99"


def test_alloc_free_and_span(tmp_path):
    r = run_pa(tmp_path, """
        def main():
            p = alloc(f64, 4)
            for i in range(4):
                p[i] = i * 1.5
            q = [1.0, 2.0, 3.0, 4.0]
            s = span(q, 1, 2)
            print(p[3], s[0], len(s))
            free(p)
            return 0
    """)
    assert r.stdout.strip() == "4.5 2.0 2"


def test_exit_code_from_main(tmp_path):
    run_pa(tmp_path, "def main():\n    return 3\n", expect_code=3)


def test_paw_numpy_zero_copy_roundtrip(tmp_path):
    r = run_pa(tmp_path, """
        import np_ops

        def main():
            arr = [1.0, 2.0, 3.0, 4.0]
            x = np_ops.sum_array(arr)
            ys = np_ops.scale(arr, 2.0)
            print(x, len(ys), ys[3])
            return 0
    """, paws={"np_ops": """
        import numpy as np

        def sum_array(arr: span[f64]) -> f64:
            return float(np.sum(np.frombuffer(arr, dtype=np.float64)))

        def scale(arr: span[f64], k: f64) -> Vec[f64]:
            return np.frombuffer(arr, dtype=np.float64) * k
    """})
    assert r.stdout.strip() == "10.0 4 8.0"


def test_paw_string_and_int_args(tmp_path):
    r = run_pa(tmp_path, """
        import text_ops

        def main():
            n = text_ops.count_chars("héllo", 2)
            print(n)
            return 0
    """, paws={"text_ops": """
        def count_chars(s: str_view, k: i32) -> i32:
            return len(s) * k
    """})
    assert r.stdout.strip() == "10"


@pytest.mark.parametrize("body,direction,needle", [
    ("def f(x: f64) -> f64:\n    return None\n", "PY_TO_PA_OUT", "None"),
    ("def f(x: f64) -> f64:\n    return float('nan')\n", "PY_TO_PA_OUT", "NaN"),
    ("def f(x: f64) -> f64:\n    return 1\n", "PY_TO_PA_OUT", "drifted"),
    ("def f(x: f64) -> f64:\n    raise ValueError('boom')\n", "PY_TO_PA_OUT", "boom"),
])
def test_inbound_customs_fault_report(tmp_path, body, direction, needle):
    run_pa(tmp_path, "import w\n\ndef main():\n    y = w.f(1.0)\n    print(y)\n    return 0\n",
           paws={"w": body}, expect_code=70, mode="prod")   # dev would reject these workers statically
    fault = json.loads((tmp_path / "pa_boundary_fault.json").read_text())
    assert fault["direction"] == direction and fault["worker"] == "w.f"
    assert needle in fault["violation"]
    assert fault["in_payload_snapshot"][0]["value"] == 1.0


def test_outbound_customs_rejects_nan(tmp_path):
    run_pa(tmp_path, """
        import w

        def main():
            z = 0.0
            bad = z / z
            y = w.f(bad)
            return 0
    """, paws={"w": "def f(x: f64) -> f64:\n    return x\n"}, expect_code=70)
    fault = json.loads((tmp_path / "pa_boundary_fault.json").read_text())
    assert fault["direction"] == "PA_TO_PY_IN" and "NaN" in fault["violation"]
    assert fault["in_payload_snapshot"][0]["value"] == "NaN"


def test_bisection_replay_of_snapshot(tmp_path):
    """5-second bisection: replay in_payload_snapshot against the bare .paw worker."""
    worker = "def f(x: f64) -> f64:\n    return None\n"
    run_pa(tmp_path, "import w\n\ndef main():\n    y = w.f(2.5)\n    return 0\n", paws={"w": worker}, expect_code=70, mode="prod")
    snap = json.loads((tmp_path / "pa_boundary_fault.json").read_text())["in_payload_snapshot"]
    ns = {}
    exec("f64 = float\n" + worker, ns)
    assert ns["f"](*[a["value"] for a in snap]) is None      # valid input, bad output => the .paw is at fault


def test_shared_library_export(tmp_path):
    src = tmp_path / "lib.pa"
    src.write_text(textwrap.dedent("""
        @export
        def dot(a: i32, b: i32) -> i32:
            return a * b + 1

        @export
        def mean(xs: list[f64, 4]) -> f64:
            s = 0.0
            for i in range(4):
                s = s + xs[i]
            return s / 4
    """))
    so = compile_pa(str(src), out=str(tmp_path / "lib.so"), shared=True)
    lib = ctypes.CDLL(so)
    lib.dot.argtypes, lib.dot.restype = [ctypes.c_int, ctypes.c_int], ctypes.c_int
    assert lib.dot(6, 7) == 43
    arr = (ctypes.c_double * 4)(1, 2, 3, 6)
    lib.mean.argtypes, lib.mean.restype = [ctypes.c_void_p], ctypes.c_double
    assert lib.mean(arr) == 3.0


def test_frontend_errors_block_build(tmp_path):
    (tmp_path / "main.pa").write_text("import requests\n\ndef main():\n    return 0\n")
    with pytest.raises(BuildError, match="PA_E001"):
        compile_pa(str(tmp_path / "main.pa"))


def test_pipeline_demo_builds_and_runs(tmp_path):
    import os
    demo = os.path.join(os.path.dirname(__file__), "..", "examples", "pipeline_demo")
    exe = compile_pa(os.path.join(demo, "main.pa"), out=str(tmp_path / "demo"))
    r = subprocess.run([exe], capture_output=True, text=True)
    assert r.returncode == 0 and r.stdout.split("\n")[0] == "64 60"


def test_break_and_continue(tmp_path):
    r = run_pa(tmp_path, """
        def main():
            t = 0
            for i in range(100):
                if i % 2 == 1:
                    continue
                if i > 10:
                    break
                t += i
            k = 0
            n = 0
            while True:
                k += 1
                if k < 3:
                    continue
                n += k
                if k >= 5:
                    break
            print(t, k, n)
            return 0
    """)
    assert r.stdout.strip() == "30 5 12"


def test_allow_nan_passes_both_directions(tmp_path):
    r = run_pa(tmp_path, """
        import w

        def main():
            z = 0.0
            bad = z / z
            y = w.f(bad)
            xs = w.g([1.0, 2.0])
            print(len(xs))
            return 0
    """, paws={"w": """
        import math

        @allow_nan
        def f(x: f64) -> f64:
            assert math.isnan(x)
            return float('inf')

        @allow_nan
        def g(a: span[f64]) -> Vec[f64]:
            return [float('nan'), 1.0]
    """})
    assert r.stdout.strip() == "2"


def test_allow_nan_is_per_function(tmp_path):
    run_pa(tmp_path, """
        import w

        def main():
            a = w.ok()
            b = w.strict()
            return 0
    """, paws={"w": """
        @allow_nan
        def ok() -> f64:
            return float('nan')

        def strict() -> f64:
            return float('nan')
    """}, expect_code=70)
    fault = json.loads((tmp_path / "pa_boundary_fault.json").read_text())
    assert fault["worker"] == "w.strict" and "NaN" in fault["violation"]


def test_no_silent_i32_overflow_with_default_i64(tmp_path):
    r = run_pa(tmp_path, """
        def main():
            x = 12345
            for i in range(10):
                x = (x * 1103515245 + 12345) % 2147483648
            big = 100000 * 100000
            print(x, big)
            return 0
    """)
    x = 12345
    for _ in range(10):
        x = (x * 1103515245 + 12345) % 2147483648
    assert r.stdout.split() == [str(x), "10000000000"]


def test_u8_unsigned_shift_div_and_compare(tmp_path):
    r = run_pa(tmp_path, """
        def main() -> int:
            buf = [u8(200), u8(50), u8(255), u8(128)]
            a = buf[0]
            b = buf[1]
            c = buf[2]
            d = buf[3]
            gt = 1 if a > b else 0
            lt = 1 if b < d else 0
            sh = a >> u8(4)
            dv = a // u8(10)
            rm = c % u8(10)
            print(gt, lt, int(sh), int(dv), int(rm))
            return 0
    """)
    assert r.stdout.split() == ["1", "1", "12", "20", "5"]


def test_u32_and_u64_unsigned_ops_and_paw(tmp_path):
    (tmp_path / "uhelper.paw").write_text(
        "def echo_u64(x: u64, arr: list[u64]) -> u64:\n"
        "    assert x == 11400714819323198485\n"
        "    assert list(arr) == [18446744073709551615, 4294967295]\n"
        "    return x ^ 1\n",
        encoding="utf-8",
    )
    r = run_pa(tmp_path, """
        import uhelper

        def main() -> int:
            # 0x9E3779B97F4A7C15 = 11400714819323198485 (> 2^63)
            big = u64(11400714819323198485)
            hi = u64(18446744073709551615)
            small = u64(10)
            gt = 1 if big > small else 0
            sh = big >> u64(60)
            dv = hi // u64(3)
            rm = hi % u64(10)
            a32 = u32(4000000000)
            b32 = u32(100)
            gt32 = 1 if a32 > b32 else 0
            sh32 = a32 >> u32(28)
            arr = [hi, u64(4294967295)]
            back = uhelper.echo_u64(big, arr)
            print(gt, sh, dv, rm, gt32, sh32, back)
            return 0
    """)
    assert r.stdout.split() == [
        "1",
        str(0x9E3779B97F4A7C15 >> 60),
        str(0xFFFFFFFFFFFFFFFF // 3),
        str(0xFFFFFFFFFFFFFFFF % 10),
        "1",
        str(4000000000 >> 28),
        str(0x9E3779B97F4A7C15 ^ 1),
    ]
