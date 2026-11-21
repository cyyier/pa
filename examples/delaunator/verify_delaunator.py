"""Automated verification & benchmark: pure-Python Delaunator vs native Pa binary."""
import os
import subprocess
import sys
import tempfile
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from oracle_delaunator import _in_circle, _orient2d, delaunator_py
from pa_lang.build import compile_pa
from pa_lang.validator import check_file

N = 20_000


def canonical_tris(triangles) -> set:
    out = set()
    for i in range(0, len(triangles), 3):
        a, b, c = int(triangles[i]), int(triangles[i + 1]), int(triangles[i + 2])
        # Canonicalize cyclic rotation (preserving orientation)
        if b < a and b < c:
            out.add((b, c, a))
        elif c < a and c < b:
            out.add((c, a, b))
        else:
            out.add((a, b, c))
    return out


def verify_geometric_invariants(xs, ys, triangles, halfedges):
    m = len(triangles)
    assert m > 0 and m % 3 == 0 and len(halfedges) == m
    # 1. Half-edge symmetry & opposite vertices
    for e in range(m):
        opp = int(halfedges[e])
        if opp != -1:
            assert int(halfedges[opp]) == e, f"asymmetric halfedge {e} <-> {opp}"
            e_next = e - e % 3 + (e + 1) % 3
            opp_next = opp - opp % 3 + (opp + 1) % 3
            assert int(triangles[e]) == int(triangles[opp_next])
            assert int(triangles[e_next]) == int(triangles[opp])
    # 2. Consistent orientation on every triangle
    for t in range(0, m, 3):
        a, b, c = int(triangles[t]), int(triangles[t + 1]), int(triangles[t + 2])
        assert _orient2d(xs[a], ys[a], xs[b], ys[b], xs[c], ys[c]) >= -1e-12
    # 3. Empty-circumcircle Delaunay condition across every interior edge
    for e in range(m):
        opp = int(halfedges[e])
        if opp == -1 or opp < e:
            continue
        t0 = e - e % 3
        a, b, c = int(triangles[t0]), int(triangles[t0 + 1]), int(triangles[t0 + 2])
        d = int(triangles[opp - opp % 3 + (opp + 2) % 3])
        assert not _in_circle(xs[a], ys[a], xs[b], ys[b], xs[c], ys[c], xs[d], ys[d]), f"non-Delaunay edge {e}"


def main():
    main_pa = os.path.join(HERE, "main.pa")
    paw_file = os.path.join(HERE, "io_bridge.paw")

    diags = check_file(main_pa, mode="dev") + check_file(paw_file, mode="dev")
    assert not diags, [d.brief() for d in diags]

    rng = np.random.default_rng(42)
    xs = rng.uniform(0.0, 1000.0, size=N)
    ys = rng.uniform(0.0, 1000.0, size=N)

    with tempfile.TemporaryDirectory() as tmp:
        np.save(os.path.join(tmp, "in_xs.npy"), xs)
        np.save(os.path.join(tmp, "in_ys.npy"), ys)
        env = dict(os.environ, PA_DELAUNATOR_DATA_DIR=tmp)

        # 1. Verify dev build passes with full runtime bounds checking on every half-edge jump
        dev_bin = compile_pa(main_pa, out=os.path.join(tmp, "del_dev"), mode="dev")
        subprocess.run([dev_bin], capture_output=True, text=True, env=env, check=True)

        # 2. Build prod binary and time it
        t_b0 = time.perf_counter()
        prod_bin = compile_pa(main_pa, out=os.path.join(tmp, "del_prod"), mode="prod")
        build_t = time.perf_counter() - t_b0

        t_p0 = time.perf_counter()
        r_prod = subprocess.run([prod_bin], capture_output=True, text=True, env=env, check=True)
        pa_e2e = time.perf_counter() - t_p0
        pa_compute_s, pa_ntris = r_prod.stdout.strip().split()
        pa_compute = float(pa_compute_s)

        pa_tris = np.load(os.path.join(tmp, "pa_triangles.npy"))
        pa_half = np.load(os.path.join(tmp, "pa_halfedges.npy"))

    # 3. Run pure-Python Delaunator oracle
    xs_list, ys_list = xs.tolist(), ys.tolist()
    t0 = time.perf_counter()
    py_tris, py_half = delaunator_py(xs_list, ys_list)
    py_compute = time.perf_counter() - t0

    # 4. Verify geometric invariants & 100% triangle-set match
    verify_geometric_invariants(xs_list, ys_list, pa_tris, pa_half)
    pa_set = canonical_tris(pa_tris)
    py_set = canonical_tris(py_tris)
    assert pa_set == py_set, f"triangle set mismatch: pa={len(pa_set)} py={len(py_set)} diff={len(pa_set ^ py_set)}"
    assert len(pa_tris) == len(py_tris)

    print(f"=== Delaunator 2D Triangulation Verification (N = {N:,} points) ===")
    print(f"Triangles generated : {int(pa_ntris):,} (Python: {len(py_tris) // 3:,})")
    print(f"Triangle set match  : 100% exact ({len(pa_set):,} / {len(py_set):,})")
    print(f"Half-edge symmetry  : 100% valid")
    print(f"Delaunay empty-circ : 100% valid")
    print()
    print(f"pac build (--mode prod): {build_t:.2f}s")
    print("| 阶段 (20,000 点三角剖分) | CPython (pure) | Pa (prod) | 加速比 |")
    print("|---|---:|---:|---:|")
    print(f"| 核心三角剖分 (纯计算) | {py_compute:.3f}s | {pa_compute:.4f}s | {py_compute / pa_compute:.1f}x |")
    print(f"| 端到端进程 (含 .paw I/O + 启动) | {py_compute:.3f}s | {pa_e2e:.3f}s | {py_compute / pa_e2e:.1f}x |")


if __name__ == "__main__":
    main()
