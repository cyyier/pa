import importlib
import os
import subprocess
import sys
import textwrap

import pytest

from pa_lang.build import compile_pa
from tests.test_codegen import run_pa


def test_mut_only_list_infers_element_type(tmp_path):
    r = run_pa(tmp_path, """
        def fill(buf, n):
            for i in range(n):
                buf.append(i * 2)
            return 0

        def fill_f(buf):
            buf.append(1.5)
            return 0

        def main():
            v = []
            fill(mut(v), 1000)
            w = []
            fill_f(mut(w))
            t = 0
            for x in v:
                t += x
            print(len(v), t, w[0])
            return 0
    """)
    assert r.stdout.strip() == "1000 999000 1.5"


def test_empty_list_never_filled_still_errors():
    from pa_lang.validator import check_source
    codes = [d.code for d in check_source("def f():\n    x = []\n    return 0\n", "t.pa")]
    assert codes == ["PA_E011"]


def test_2d_index_matrix(tmp_path):
    r = run_pa(tmp_path, """
        W = 4
        H = 3

        def total(m):
            s = 0.0
            for i in range(H):
                for j in range(W):
                    s = s + m[i, j]
            return s

        def main():
            img = [[0.0] * W] * H
            for i in range(H):
                for j in range(W):
                    img[i, j] = i * 10 + j
            img[1, 2] += 100
            print(img[2, 3], img[1][2], total(img))
            big = [[1.0] * 64] * 64
            big[63, 63] = 5.0
            print(total2(big))
            return 0

        def total2(m):
            s = 0.0
            for i in range(64):
                for j in range(64):
                    s += m[i, j]
            return s
    """)
    assert r.stdout.split("\n")[:2] == ["23 112 238", "4100"]


def test_string_concat_index_compare(tmp_path):
    r = run_pa(tmp_path, """
        def greet(name):
            return "hello, " + name + "!"

        def main():
            s = greet("pa")
            print(s, len(s))
            print(s[0], s[1])
            acc = ""
            for i in range(3):
                acc += "ab"
            acc += s
            print(acc)
            t = 0
            for ch in "abc":
                t += ch
            print(t)
            g2 = greet("pa")
            same = g2 == s
            other = s != "hello"
            print(same, other, "x" == "x", "x" == "y")
            copy = s
            copy += "?"
            print(s, copy)
            return 0
    """)
    lines = r.stdout.split("\n")
    assert lines[0] == "hello, pa! 10"
    assert lines[1] == "104 101"
    assert lines[2] == "abababhello, pa!"
    assert lines[3] == str(97 + 98 + 99)
    assert lines[4] == "True True True False"
    assert lines[5] == "hello, pa! hello, pa!?"


def test_string_passed_to_paw(tmp_path):
    r = run_pa(tmp_path, """
        import text_ops

        def main():
            s = "ab" + "cde"
            print(text_ops.length(s))
            return 0
    """, paws={"text_ops": "def length(s: str_view) -> i64:\n    return len(s)\n"})
    assert r.stdout.strip() == "5"


def test_string_loop_has_no_leak_growth(tmp_path):
    r = run_pa(tmp_path, """
        def main():
            n = 0
            for i in range(200000):
                s = "abc" + "defghijklmnop"
                n += len(s)
            print(n)
            return 0
    """)
    assert r.stdout.strip() == str(200000 * 16)


# ---------------------------------------------------------------- FastAPI shell
@pytest.fixture(scope="module")
def web_client(tmp_path_factory):
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient
    shell = os.path.join(os.path.dirname(__file__), "..", "examples", "web_shell")
    so = compile_pa(os.path.join(shell, "stats.pa"), out=str(tmp_path_factory.mktemp("web") / "libstats.so"),
                    shared=True)
    os.environ["PA_STATS_LIB"] = so
    sys.path.insert(0, shell)
    sys.modules.pop("app", None)
    app_module = importlib.import_module("app")
    yield TestClient(app_module.app)
    sys.path.remove(shell)
    sys.modules.pop("app", None)


def test_web_stats_endpoint(web_client):
    r = web_client.post("/stats", json={"values": [1.0, 2.0, 3.0, 4.0]})
    assert r.status_code == 200
    assert r.json() == {"mean": 2.5, "variance": 1.25}


def test_web_smooth_and_trace(web_client):
    r = web_client.post("/smooth", json={"values": [1, 2, 3, 4, 5], "window": 2})
    assert r.json() == {"values": [1.5, 2.5, 3.5, 4.5]}
    r = web_client.post("/trace", json={"rows": [[1, 2], [3, 4]]})
    assert r.json() == {"trace": 5.0}


def test_web_validation_errors_stay_in_python_shell(web_client):
    assert web_client.post("/stats", json={"values": []}).status_code == 422
    assert web_client.post("/smooth", json={"values": [1.0], "window": 5}).status_code == 422
    assert web_client.post("/trace", json={"rows": [[1, 2]]}).status_code == 422


# ---------------------------------------------------------------- compiler checks for the Skill rules
from pa_lang.validator import check_source


def codes(src, name="t.pa", **kw):
    return [d.code for d in check_source(textwrap.dedent(src), name, **kw)]


def test_e072_string_temporaries():
    base = "def f(x):\n    return len(x)\n\ndef g(a):\n    return a + \"!\"\n\n"
    assert "PA_E072" in codes(base + 'def main():\n    b = "b"\n    f("a" + b)\n    return 0\n')
    assert "PA_E072" in codes(base + 'def main():\n    b = "b"\n    print("a" + b)\n    return 0\n')
    assert "PA_E072" in codes(base + 'def main():\n    b = "b"\n    x = g(b) == b\n    return 0\n')
    assert "PA_E072" in codes(base + 'def main():\n    b = "b"\n    s = "x"\n    s += b + b\n    return 0\n')
    assert "PA_E072" in codes(base + 'def main():\n    b = "b"\n    s = g(b) + b\n    return 0\n')
    ok = base + ('def main():\n    b = "b"\n    tmp = "a" + b\n    n = f(tmp)\n    t = "a" + b + "c"\n'
                 '    u = g(b)\n    s = "x"\n    s += b\n    return 0\n')
    assert "PA_E072" not in codes(ok)
    d = [x for x in check_source(base + 'def main():\n    b = "b"\n    f("a" + b)\n    return 0\n', "t.pa")
         if x.code == "PA_E072"][0]
    assert "tmp = " in d.repair_plan["suggested_patches"][0]["replacement"]


def test_e011_message_for_multi_level_mut():
    src = """
        def inner(b):
            b.append(1)
            return 0

        def outer(b):
            inner(mut(b))
            return 0

        def main():
            v = []
            outer(mut(v))
            return 0
    """
    d = [x for x in check_source(textwrap.dedent(src), "t.pa") if x.code == "PA_E011"]
    assert d and "mut()" in d[0].summary and "outer" in d[0].summary
    single = src.replace("v = []\n            outer(mut(v))", "v = []\n            inner(mut(v))")
    assert "PA_E011" not in codes(single)
    top = src.replace("v = []\n", "v = []\n            v.append(0)\n")
    assert "PA_E011" not in codes(top)


def test_string_slice_rejected():
    assert "PA_E071" in codes('def f():\n    s = "hello"\n    t = s[1:3]\n    return 0\n')
    d = [x for x in check_source('def f():\n    s = "hello"\n    t = s[1:3]\n    return 0\n', "t.pa")
         if x.code == "PA_E071"][0]
    assert "string slicing" in d.summary


def test_paw_cannot_return_string():
    for ann in ("str", "str_view", "bytes"):
        d = check_source(f"def f(x: f64) -> {ann}:\n    return 'a'\n", "w.paw")
        assert [x.code for x in d] == ["PA_E082"] and "Vec[u8]" in d[0].summary
    ok = "import numpy as np\n\ndef f(s: str_view) -> Vec[u8]:\n    return np.frombuffer(bytes(s, 'utf-8'), dtype=np.uint8)\n"
    assert codes(ok, "w.paw") == []


def test_paw_returns_char_array_end_to_end(tmp_path):
    r = run_pa(tmp_path, """
        import w

        def main():
            b = w.shout("hi")
            print(len(b), b[0], b[1])
            return 0
    """, paws={"w": """
        def shout(s: str_view) -> Vec[u8]:
            return list(s.upper().encode("utf-8"))
    """})
    assert r.stdout.strip() == "2 72 73"
