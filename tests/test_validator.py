import json

from pa_lang.validator import check_source


def codes(src, filename="t.pa"):
    return [d.code for d in check_source(src, filename)]


def test_clean_program_is_green():
    src = ("N = 4\n"
           "def total(xs):\n    s = 0.0\n    for i in range(N):\n        s = s + xs[i]\n    return s\n\n"
           "def main():\n    a = [1.0, 2.0, 3.0, 4.0]\n    t = total(a)\n    return 0\n")
    assert codes(src) == []


def test_third_party_import_blocked_but_paw_allowed(tmp_path):
    (tmp_path / "np_ops.paw").write_text("import numpy as np\n\ndef f(a: f64) -> f64:\n    return a\n")
    for imp in ("import requests", "import pandas as pd", "from sqlalchemy import create_engine", "import os"):
        d = check_source(imp + "\n", "main.pa", search_dirs=[str(tmp_path)])
        assert [x.code for x in d] == ["PA_E001"], imp
    assert codes("from pa import struct\n") == []
    assert check_source("import np_ops\n", "main.pa", search_dirs=[str(tmp_path)]) == []


def test_py_call_in_loop_blocked():
    src = 'def f(n):\n    for i in range(n):\n        r = py_call("w.fn", i)\n    return 0\n'
    assert "PA_E002" in codes(src)
    src = 'def f(n):\n    i = 0\n    while i < n:\n        r = py_call("w.fn", i)\n        i = i + 1\n    return 0\n'
    assert "PA_E002" in codes(src)


def test_py_call_in_loop_via_helper_blocked():
    src = ('def helper(x):\n    return py_call("w.fn", x)\n'
           'def f(n):\n    for i in range(n):\n        r = helper(i)\n    return 0\n')
    assert "PA_E002" in codes(src)


def test_batched_py_call_ok():
    src = ('def f(n):\n    batch = [1.0, 2.0]\n    r = py_call("w.fn", batch)\n    for i in range(n):\n        x = i\n    return 0\n')
    assert codes(src) == []


def test_banned_syntax():
    cases = {
        "try:\n        x = 1\n    except Exception:\n        x = 2": "PA_E003",
        "x = lambda a: a": "PA_E003",
        "x = eval('1')": "PA_E003",
        "x = getattr(1, 'a')": "PA_E003",
        "x = {1: 2}": "PA_E031",
        "x = {1, 2}": "PA_E031",
        "x = (1, 2)": "PA_E031",
        "x = dict()": "PA_E031",
        "x: int = 1": "PA_E005",
        "x = [1]\n    x.extend(x)": "PA_E071",
    }
    for body, code in cases.items():
        assert code in codes(f"def f():\n    {body}\n    return 0\n"), body


def test_closure_yield_varargs_metaclass():
    assert "PA_E003" in codes("def f():\n    def g():\n        return 1\n    return g()\n")
    assert "PA_E003" in codes("def f():\n    yield 1\n")
    assert "PA_E003" in codes("def f(*args):\n    return 0\n")
    assert "PA_E003" in codes("class A(metaclass=type):\n    x: i32\n")


def test_annotation_policy():
    assert "PA_E005" in codes("def f(a: i32):\n    return a\n")
    assert codes("@export\ndef f(a: i32) -> i32:\n    return a\n") == []
    assert "PA_E081" in codes("@export\ndef f(a):\n    return a\n")
    assert "PA_E005" in codes("@export\ndef f(a: Optional[i32]) -> i32:\n    return 0\n")
    assert codes("@struct\nclass P:\n    x: f64\n    y: i32\n") == []


def test_mut_borrow_rule():
    src = ("def fill(buf):\n    buf.append(1)\n    return 0\n"
           "def f():\n    a = []\n    fill(a)\n    return 0\n")
    assert "PA_E050" in codes(src)
    ok = src.replace("fill(a)\n", "fill(mut(a))\n")
    assert "PA_E050" not in codes(ok)
    sub = ("def setv(buf):\n    buf[0] = 1\n    return 0\n"
           "def f():\n    a = [0, 0]\n    setv(a)\n    return 0\n")
    assert "PA_E050" in codes(sub)


def test_mut_borrow_is_transitive():
    src = ("def inner(b):\n    b.append(1)\n    return 0\n"
           "def outer(b):\n    inner(mut(b))\n    return 0\n"
           "def f():\n    a = []\n    outer(a)\n    return 0\n")
    assert "PA_E050" in codes(src)


def test_keyword_args_over_two():
    src = "def g(a, b, c):\n    return a\ndef f():\n    return g(1, 2, 3)\n"
    assert "PA_E051" in codes(src)
    assert "PA_E051" not in codes(src.replace("g(1, 2, 3)", "g(a=1, b=2, c=3)"))
    assert "PA_E051" not in codes("def g(a, b):\n    return a\ndef f():\n    return g(1, 2)\n")


def test_readability_limits():
    long_body = "\n".join(f"    x{i} = {i}" for i in range(42))
    assert "PA_E060" in codes(f"def f():\n{long_body}\n    return 0\n")
    branches = "\n".join(f"    if a == {i}:\n        return {i}" for i in range(9))
    assert "PA_E061" in codes(f"def f(a):\n{branches}\n    return 0\n")
    deep = ("def f(a):\n    if a:\n        if a:\n            if a:\n                if a:\n"
            "                    return 1\n    return 0\n")
    assert "PA_E062" in codes(deep)
    ok3 = "def f(a):\n    if a:\n        if a:\n            if a:\n                return 1\n    return 0\n"
    assert "PA_E062" not in codes(ok3)


def test_subscript_tuple_index_not_flagged():
    assert "PA_E031" not in codes("def f(m):\n    return m[1, 2]\n")


def test_paw_signature_rules():
    ok = "import numpy as np\n\ndef f(arr: span[f64]) -> f64:\n    return float(np.sum(arr))\n"
    assert codes(ok, "a.paw") == []
    assert codes("def f(x):\n    return x\n", "a.paw") == ["PA_E080"]
    assert codes("def f(x: f64):\n    return x\n", "a.paw") == ["PA_E080"]
    assert codes("def f(x: dict) -> f64:\n    return 1.0\n", "a.paw") == ["PA_E082"]
    assert codes("def _private(x):\n    return x\n", "a.paw") == []
    assert codes("def f(x: f64) -> list[f64, 4]:\n    return x\n", "a.paw") == ["PA_E082"]


def test_paw_call_in_loop_blocked(tmp_path):
    (tmp_path / "w.paw").write_text("def f(a: f64) -> f64:\n    return a\n")
    src = "import w\ndef g(n):\n    for i in range(n):\n        x = w.f(1.0)\n    return 0\n"
    d = check_source(src, "main.pa", search_dirs=[str(tmp_path)])
    assert "PA_E002" in [x.code for x in d]


def test_syntax_error_reports_e000():
    assert codes("def f(:\n") == ["PA_E000"]


def test_ai_diagnostic_ticket_shape():
    src = "def f():\n    x = [1, \"a\"]\n    return 0\n"
    d = check_source(src, "main.pa")[0].to_dict()
    for key in ("code", "category", "severity", "skill_rule_ref", "location", "summary",
                "inferred_context", "causal_chain", "source_context", "repair_plan"):
        assert key in d
    for key in ("strategy", "anti_patterns_to_avoid", "suggested_patches"):
        assert key in d["repair_plan"]
    assert any(l["mark"] and "[1," in l["text"] for l in d["source_context"])
    json.dumps(d)
