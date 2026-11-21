import ast

from pa_lang.diagnostics import DiagnosticSink
from pa_lang.infer import analyze
from pa_lang.types import F64, I32, I64, FixedList, StackVec, StructT, Vec


def run(src):
    sink = DiagnosticSink(src, "t.pa")
    mod = analyze(ast.parse(src), sink)
    return mod, sink


def codes(sink):
    return [d.code for d in sink.items]


def test_scalar_inference_without_annotations():
    mod, sink = run("def f():\n    a = 1\n    b = 2.5\n    c = a + b\n    d = a / 2\n    return c\n")
    loc = mod.funcs["f"].locals
    assert loc["a"].type == I64 and loc["b"].type == F64
    assert loc["c"].type == F64 and loc["d"].type == F64
    assert mod.funcs["f"].ret == F64 and not sink.items


def test_int_literals_default_to_i64_even_when_small():
    mod, _ = run("def f():\n    s = 1\n    return s\n")
    assert mod.funcs["f"].locals["s"].type == I64


def test_big_int_literal_is_i64():
    mod, _ = run("def f():\n    a = 5000000000\n    return a\n")
    assert mod.funcs["f"].locals["a"].type == I64


def test_fixed_list_literal_and_repeat():
    mod, sink = run("N = 16\ndef f():\n    a = [1.0, 2.0, 3.0]\n    b = [0.0] * N\n    return 0\n")
    loc = mod.funcs["f"].locals
    assert loc["a"].type == FixedList(F64, 3)
    assert loc["b"].type == FixedList(F64, 16) and loc["b"].type.size == 128
    assert not sink.items


def test_multi_dim_flattened_size():
    mod, _ = run("def f():\n    m = [[1.0, 2.0], [3.0, 4.0]]\n    return 0\n")
    t = mod.funcs["f"].locals["m"].type
    assert isinstance(t, FixedList) and t.size == 32


def test_stack_bounded_vec_const_range():
    src = "def f():\n    res = []\n    for i in range(64):\n        res.append(i)\n    return len(res)\n"
    mod, sink = run(src)
    loc = mod.funcs["f"].locals["res"]
    assert loc.type == StackVec(I64, 64) and loc.storage == "stack" and loc.note == "Zero-Heap"
    assert not sink.items


def test_stack_vec_bound_multiplies_nested_loops_and_consts():
    src = ("M = 4\ndef f():\n    res = []\n    for i in range(M):\n        for j in range(8):\n"
           "            res.append(i)\n            res.append(j)\n    return 0\n")
    mod, _ = run(src)
    assert mod.funcs["f"].locals["res"].type == StackVec(I64, 64)


def test_unbounded_append_upgrades_to_heap_vec_with_raii():
    src = "def f(n):\n    res = []\n    for i in range(n):\n        res.append(1.5)\n    return len(res)\n"
    mod, _ = run(src)
    fi = mod.funcs["f"]
    assert fi.locals["res"].type == Vec(F64) and fi.locals["res"].storage == "heap"
    assert fi.raii_free_lines and fi.node.end_lineno in fi.raii_free_lines


def test_escaping_list_transfers_ownership():
    src = "def f():\n    res = []\n    for i in range(4):\n        res.append(i)\n    return res\n"
    mod, _ = run(src)
    fi = mod.funcs["f"]
    assert fi.ret == Vec(I64) and fi.locals["res"].escapes
    assert fi.raii_free_lines == []


def test_over_64kb_falls_back_to_heap():
    src = "def f():\n    res = []\n    for i in range(10000):\n        res.append(1.0)\n    return len(res)\n"
    mod, _ = run(src)
    assert mod.funcs["f"].locals["res"].type == Vec(F64)


def test_while_loop_is_unbounded():
    src = "def f(x):\n    res = []\n    while x > 0:\n        res.append(x)\n        x = x - 1\n    return 0\n"
    mod, _ = run(src)
    assert isinstance(mod.funcs["f"].locals["res"].type, Vec)


def test_e030_heterogeneous_list_with_surgery():
    mod, sink = run('def f():\n    rows = [[1, "apple", True], [2, "pear", False]]\n    return 0\n')
    d = [x for x in sink.items if x.code == "PA_E030"][0]
    patches = d.repair_plan["suggested_patches"]
    assert "class Record" in patches[0]["replacement"]
    assert "f1: str_view" in patches[0]["replacement"]
    assert "Record(f0=1" in patches[1]["replacement"]
    assert d.inferred_context["target_layout"].startswith("Vec<Record>")


def test_e030_single_mixed_list():
    _, sink = run('def f():\n    a = [1, "a", True]\n    return 0\n')
    assert codes(sink) == ["PA_E030"]


def test_e011_empty_list_never_appended():
    _, sink = run("def f():\n    x = []\n    return 0\n")
    assert codes(sink) == ["PA_E011"]


def test_struct_fields_and_attribute_inference():
    src = ("@struct\nclass P:\n    x: f64\n    y: i32\n\ndef f():\n    p = P(x=1.0, y=2)\n    z = p.x\n    w = p.y\n    return z\n")
    mod, sink = run(src)
    loc = mod.funcs["f"].locals
    assert isinstance(loc["p"].type, StructT) and loc["z"].type == F64 and loc["w"].type == I32
    assert mod.structs["P"].size == 16


def test_param_types_flow_from_callsites():
    src = "def g(a, b):\n    return a + b\ndef f():\n    return g(1, 2.5)\n"
    mod, _ = run(src)
    assert mod.funcs["g"].ret == F64 and mod.funcs["f"].ret == F64


def test_span_escape_e040():
    src = "def f():\n    a = [1, 2, 3, 4]\n    s = span(a, 1, 2)\n    return s\n"
    _, sink = run(src)
    assert "PA_E040" in codes(sink)


def test_span_of_param_is_fine():
    src = "def f(a):\n    s = span(a, 1, 2)\n    return s\n"
    _, sink = run(src)
    assert "PA_E040" not in codes(sink)


def test_alloc_leak_and_uaf_and_ok():
    _, leak = run("def f():\n    p = alloc(i32, 4)\n    return 0\n")
    assert codes(leak) == ["PA_E004"]
    _, uaf = run("def f():\n    p = alloc(i32, 4)\n    free(p)\n    x = p\n    return 0\n")
    assert "PA_E004" in codes(uaf)
    _, ok = run("def f():\n    p = alloc(i32, 4)\n    free(p)\n    return 0\n")
    assert codes(ok) == []


def test_ptr_alias_e070():
    _, sink = run("def f():\n    p = alloc(i32, 4)\n    q = p\n    free(p)\n    return 0\n")
    assert "PA_E070" in codes(sink)


def test_large_copy_warns_w001():
    src = "def f():\n    a = [0.0] * 1024\n    b = a\n    return 0\n"
    _, sink = run(src)
    assert codes(sink) == ["PA_W001"]


def test_implicit_heap_slice_and_concat():
    _, sink = run("def f():\n    a = [1, 2, 3]\n    b = a[1:2]\n    c = a + a\n    return 0\n")
    assert codes(sink).count("PA_E071") == 2


def test_hints_for_lsp():
    mod, _ = run("def f():\n    a = [0.0] * 16\n    return 0\n")
    texts = [t for _, _, t in mod.funcs["f"].hints]
    assert ": list[f64, 16] (128B flat)" in texts


def test_paw_call_typed_statically(tmp_path):
    (tmp_path / "np_ops.paw").write_text("def sum_array(arr: span[f64]) -> f64:\n    return 0.0\n")
    src = "import np_ops\n\ndef main():\n    arr = [1.0, 2.0, 3.0, 4.0]\n    x = np_ops.sum_array(arr)\n    return 0\n"
    sink = DiagnosticSink(src, "main.pa")
    mod = analyze(ast.parse(src), sink, search_dirs=[str(tmp_path)])
    assert mod.funcs["main"].locals["x"].type == F64 and not sink.items


def test_paw_call_mismatch_e090(tmp_path):
    (tmp_path / "w.paw").write_text("def f(a: span[f64]) -> f64:\n    return 0.0\n")
    src = "import w\n\ndef main():\n    a = [1, 2]\n    x = w.f(a)\n    y = w.g(a)\n    return 0\n"
    sink = DiagnosticSink(src, "main.pa")
    analyze(ast.parse(src), sink, search_dirs=[str(tmp_path)])
    assert [d.code for d in sink.items] == ["PA_E090", "PA_E090"]
