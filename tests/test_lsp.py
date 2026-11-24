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


def test_goto_definition_same_file_and_cross_module(tmp_path):
    fetcher_paw = tmp_path / "data_fetcher.paw"
    fetcher_paw.write_text(
        "def helper() -> int:\n"
        "    return 1\n\n"
        "def fetch_series(n: int) -> list[f64]:\n"
        "    return [0.0] * n\n"
    )
    stats_pa = tmp_path / "stats.pa"
    stats_pa.write_text(
        "SCALE = 2.0\n\n"
        "def mean(xs):\n"
        "    return 0.0\n\n"
        "def median(xs):\n"
        "    return 0.0\n"
    )
    main_pa = tmp_path / "main.pa"
    src = (
        "import data_fetcher\n"                      # 0
        "import stats as st\n"                       # 1
        "from stats import median as med\n"          # 2
        "\n"                                         # 3
        "N = 64\n"                                   # 4
        "WINDOW = 4\n"                               # 5
        "\n"                                         # 6
        "@struct\n"                                  # 7
        "class Sample:\n"                            # 8
        "    val: f64\n"                             # 9
        "\n"                                         # 10
        "def moving_average(xs, w):\n"               # 11
        "    total = 0.0\n"                          # 12
        "    for i in range(w):\n"                   # 13
        "        total += xs[i]\n"                   # 14
        "    return total / w\n"                     # 15
        "\n"                                         # 16
        "def main():\n"                              # 17
        "    data = data_fetcher.fetch_series(N)\n"  # 18
        "    avg = moving_average(data, WINDOW)\n"   # 19
        "    m1 = st.mean(data)\n"                   # 20
        "    m2 = med(data)\n"                       # 21
        "    s = Sample(avg + m1 + m2 + st.SCALE)\n" # 22
        "    # comment with moving_average\n"        # 23
        "    return int(s.val)\n"                    # 24
    )
    main_pa.write_text(src)
    da = DocAnalysis(src, str(main_pa))

    # Same-file function call: `moving_average` on line 19
    loc = da.definition(19, 12)
    assert loc == (str(main_pa), 11, 4, 11, 18)

    # Same-file module constants: `N` (line 18) and `WINDOW` (line 19)
    assert da.definition(18, 38) == (str(main_pa), 4, 0, 4, 1)
    assert da.definition(19, 34) == (str(main_pa), 5, 0, 5, 6)

    # Same-file @struct class: `Sample` on line 22, and struct field `s.val` on line 24
    assert da.definition(22, 10) == (str(main_pa), 8, 6, 8, 12)
    assert da.definition(24, 19) == (str(main_pa), 9, 4, 9, 7)

    # Same-file local variable & parameter inside `moving_average`
    assert da.definition(14, 10) == (str(main_pa), 12, 4, 12, 9)   # `total`
    assert da.definition(15, 20) == (str(main_pa), 11, 23, 11, 24) # param `w`

    # Cross-file .paw function call: `data_fetcher.fetch_series` on line 18
    loc_paw = da.definition(18, 28)
    assert loc_paw == (str(fetcher_paw), 3, 4, 3, 16)

    # Clicking module name `data_fetcher` on call site or `import` line -> line 1 of .paw
    assert da.definition(18, 14) == (str(fetcher_paw), 0, 0, 0, 0)
    assert da.definition(0, 10) == (str(fetcher_paw), 0, 0, 0, 0)

    # Cross-file .pa module via `import stats as st`: `st.mean` & `st.SCALE`
    assert da.definition(20, 13) == (str(stats_pa), 2, 4, 2, 8)
    assert da.definition(22, 36) == (str(stats_pa), 0, 0, 0, 5)
    assert da.definition(1, 10) == (str(stats_pa), 0, 0, 0, 0)

    # Cross-file .pa symbol via `from stats import median as med`
    assert da.definition(21, 10) == (str(stats_pa), 5, 4, 5, 10)
    assert da.definition(2, 7) == (str(stats_pa), 0, 0, 0, 0)     # `stats` in `from stats ...`
    assert da.definition(2, 20) == (str(stats_pa), 5, 4, 5, 10)   # `median` in `from stats import median`

    # Edge cases: builtins, blank lines, comments, out-of-range -> safely return None
    assert da.definition(13, 15) is None  # `range`
    assert da.definition(3, 0) is None    # blank line
    assert da.definition(23, 22) is None  # inside comment
    assert da.definition(999, 0) is None  # out-of-range line
