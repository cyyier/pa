from pa_lang.lsp_server import DocAnalysis, build_server


def test_hints_and_hover(tmp_path):
    (tmp_path / "np_ops.paw").write_text("def sum_array(arr: span[f64]) -> f64:\n    return 0.0\n")
    src = ("import np_ops\n\ndef main():\n    arr = [0.0] * 16\n    x = np_ops.sum_array(arr)\n"
           "    res = []\n    for i in range(8):\n        res.append(i)\n    return 0\n")
    da = DocAnalysis(src, str(tmp_path / "main.pa"))
    labels = [t for _, _, t in da.hints()]
    assert ": list[f64, 16] (128B flat)" in labels
    assert any("[🌉 .paw FFI -> f64]" in t for t in labels)
    assert any("Zero-Heap" in t for t in labels)
    md = da.hover(3, 5)   # 'arr'
    assert "128 B" in md and "[16 x double]" in md and "complexity" in md


def test_server_builds():
    assert build_server() is not None
