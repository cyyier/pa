"""Tests for 1-level jagged nested lists Vec[Vec[T]] lowered to flat CSR (data + offsets)."""
from __future__ import annotations

import textwrap

from pa_lang.validator import check_source
from tests.test_codegen import run_pa


def codes(src: str) -> list[str]:
    return [d.code for d in check_source(textwrap.dedent(src).lstrip("\n"), "t.pa")]


def test_csr_build_append_and_access(tmp_path):
    r = run_pa(tmp_path, """
        def sum_span(row) -> int:
            s = 0
            for x in row:
                s = s + x
            return s

        def build_adj(n) -> Vec[Vec[int]]:
            g = []
            for i in range(n):
                row = []
                for j in range(i):
                    row.append(i * 10 + j)
                g.append(row)
            return g

        def main() -> int:
            g = build_adj(4)
            total = 0
            for row in g:
                total = total + sum_span(row)
            print(len(g), len(g[0]), len(g[1]), len(g[3]), g[3][2], g[2, 1], total)
            return 0
    """)
    assert r.returncode == 0, r.stderr
    # rows: [], [10], [20, 21], [30, 31, 32] -> sum = 10 + 41 + 93 = 144
    assert r.stdout.strip() == "4 0 1 3 32 21 144"


def test_csr_jagged_literal_copy_and_inplace_mutation(tmp_path):
    r = run_pa(tmp_path, """
        def set_val(g, r, c, v) -> void:
            g[r][c] = v

        def main() -> int:
            a = [[1, 2], [3, 4, 5], []]
            b = a
            set_val(g=mut(b), r=0, c=1, v=99)
            b[1, 2] = 77
            b.append([8, 9])
            print(len(a), a[0][1], a[1][2], len(b), b[0][1], b[1][2], len(b[2]), b[3][1])
            return 0
    """)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "3 2 5 4 99 77 0 9"


def test_csr_bounds_checks_in_dev_mode(tmp_path):
    # Out-of-bounds row index
    r1 = run_pa(tmp_path, """
        def main() -> int:
            g = [[1, 2], [3]]
            row = g[2]
            print(len(row))
            return 0
    """, expect_code=70)
    assert "IndexError" in r1.stderr

    # Out-of-bounds column index inside a valid row
    r2 = run_pa(tmp_path, """
        def main() -> int:
            g = [[1, 2], [3]]
            print(g[1][1])
            return 0
    """, expect_code=70)
    assert "IndexError" in r2.stderr


def test_csr_diagnostics_and_safety():
    # Cannot append to an interior row of CSR
    assert "PA_E071" in codes("""
        def f() -> int:
            g = [[1, 2], [3]]
            g[0].append(4)
            return 0
    """)
    # Cannot replace a whole row of CSR
    assert "PA_E071" in codes("""
        def f() -> int:
            g = [[1, 2], [3]]
            g[0] = [9, 9]
            return 0
    """)
    # Returning a row view over a local CSR leaves a dangling span -> PA_E040
    assert "PA_E040" in codes("""
        def f() -> span[int]:
            g = [[1, 2], [3]]
            return g[0]
    """)
    assert "PA_E040" in codes("""
        def f() -> span[int]:
            g = [[1, 2], [3]]
            r = g[0]
            return r
    """)
    # Returning an element scalar g[0][1] is safe
    assert codes("""
        def f() -> int:
            g = [[1, 2], [3]]
            return g[0][1]
    """) == []
    # 3D+ ragged list is rejected -> PA_E031
    assert "PA_E031" in codes("""
        def f() -> int:
            g = [[[1]], [[2, 3]]]
            return 0
    """)
    assert "PA_E031" in codes("""
        def f() -> int:
            inner = [[1, 2], [3]]
            outer = []
            outer.append(inner)
            return 0
    """)
    # Missing mut() when helper mutates g[i][j] -> PA_E050
    assert "PA_E050" in codes("""
        def bump(g) -> void:
            g[0][0] = 9

        def main() -> int:
            g = [[1, 2], [3]]
            bump(g)
            return 0
    """)
