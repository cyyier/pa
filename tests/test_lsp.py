from pa_lang.lsp_server import DocAnalysis, build_server


def test_hints_and_hover(tmp_path):
    (tmp_path / "np_ops.paw").write_text("def sum_array(arr: span[f64]) -> f64:\n    return 0.0\n")
    src = ("import np_ops\n\ndef main():\n    arr = [0.0] * 16\n    x = np_ops.sum_array(arr)\n"
           "    res = []\n    for i in range(8):\n        res.append(i)\n    return 0\n")
    simple = [t for _, _, t in DocAnalysis(src, str(tmp_path / "main.pa")).hints()]
    assert ": list[float]" in simple and ": float" in simple and ": list[int]" in simple
    assert any("[🌉 .paw → float]" in t for t in simple)
    assert not any("flat" in t or "Zero-Heap" in t or "RAII" in t or "memcpy" in t for t in simple)
    da = DocAnalysis(src, str(tmp_path / "main.pa"), detail="detailed")
    labels = [t for _, _, t in da.hints()]
    assert ": list[float, 16] (128B flat)" in labels
    assert any("[🌉 .paw FFI -> float]" in t for t in labels)
    assert any("Zero-Heap" in t for t in labels)
    md = da.hover(3, 5)   # 'arr'
    assert "128 B" in md and "[16 x double]" in md and "complexity" in md
    assert "list[float, 16] (list[f64, 16], 128B, stack)" in md
    scalar = da.hover(4, 4)                  # 'x' (float result of the .paw call)
    assert "float (f64, 8B, stack)" in scalar


def test_server_builds():
    assert build_server() is not None
