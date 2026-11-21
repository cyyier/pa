"""Automated verification & benchmark: pure-Python 2D KD-Tree vs native Pa binary + PIL render."""
import os
import subprocess
import sys
import tempfile
import time

import numpy as np
from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from oracle_kdtree import batch_query_py, build_kd_py
from pa_lang.build import compile_pa
from pa_lang.validator import check_file

N_PTS = 50_000
N_QUERIES = 50_000


def main():
    main_pa = os.path.join(HERE, "main.pa")
    paw_file = os.path.join(HERE, "io_bridge.paw")

    diags = check_file(main_pa, mode="dev") + check_file(paw_file, mode="dev")
    assert not diags, [d.brief() for d in diags]

    rng = np.random.default_rng(2026)
    px = rng.uniform(0.0, 1000.0, size=N_PTS)
    py = rng.uniform(0.0, 1000.0, size=N_PTS)
    qx = rng.uniform(0.0, 1000.0, size=N_QUERIES)
    qy = rng.uniform(0.0, 1000.0, size=N_QUERIES)
    png_out = os.path.join(HERE, "kdtree_voronoi.png")

    with tempfile.TemporaryDirectory() as tmp:
        np.save(os.path.join(tmp, "px.npy"), px)
        np.save(os.path.join(tmp, "py.npy"), py)
        np.save(os.path.join(tmp, "qx.npy"), qx)
        np.save(os.path.join(tmp, "qy.npy"), qy)
        env = dict(os.environ, PA_KD_DATA_DIR=tmp, PA_KD_PNG=png_out)

        # 1. Verify dev build runs cleanly with full runtime bounds & narrowing checks
        dev_bin = compile_pa(main_pa, out=os.path.join(tmp, "kd_dev"), mode="dev")
        subprocess.run([dev_bin], capture_output=True, text=True, env=env, check=True)

        # 2. Build prod binary and time it
        t_b0 = time.perf_counter()
        prod_bin = compile_pa(main_pa, out=os.path.join(tmp, "kd_prod"), mode="prod")
        build_t = time.perf_counter() - t_b0

        t_p0 = time.perf_counter()
        r_prod = subprocess.run([prod_bin], capture_output=True, text=True, env=env, check=True)
        pa_e2e = time.perf_counter() - t_p0
        pa_build_t, pa_query_t = map(float, r_prod.stdout.strip().split())

        pa_idx = np.load(os.path.join(tmp, "pa_idx.npy"))
        pa_d2 = np.load(os.path.join(tmp, "pa_d2.npy"))

    # 3. Run pure-Python KD-Tree oracle
    px_l, py_l, qx_l, qy_l = px.tolist(), py.tolist(), qx.tolist(), qy.tolist()
    t0 = time.perf_counter()
    nodes, root = build_kd_py(px_l, py_l)
    py_build_t = time.perf_counter() - t0
    t1 = time.perf_counter()
    py_idx, py_d2 = batch_query_py(nodes, root, qx_l, qy_l)
    py_query_t = time.perf_counter() - t1

    py_idx = np.array(py_idx, dtype=np.int64)
    py_d2 = np.array(py_d2, dtype=np.float64)

    # 4. Exact nearest-neighbor & brute-force spot verification + PNG check
    assert np.array_equal(pa_idx, py_idx), "nearest-neighbor index mismatch!"
    max_dd2 = float(np.max(np.abs(pa_d2 - py_d2)))
    assert max_dd2 < 1e-12, f"dist2 diff {max_dd2}"
    # Brute-force check on first 200 queries to prove KD-Tree itself is exact
    for i in range(200):
        bf_d2 = float(np.min((px - qx[i]) ** 2 + (py - qy[i]) ** 2))
        assert abs(pa_d2[i] - bf_d2) < 1e-9

    img = Image.open(png_out)
    assert img.size == (512, 512) and img.mode == "RGB"

    py_total = py_build_t + py_query_t
    pa_total = pa_build_t + pa_query_t
    print(f"=== 2D KD-Tree Verification ({N_PTS:,} pts, {N_QUERIES:,} NN queries) ===")
    print(f"NN index match      : 100% exact ({N_QUERIES:,} / {N_QUERIES:,})")
    print(f"max |Δdist²|        : {max_dd2:.3e} (< 1e-12: True)")
    print(f"Brute-force check   : 200 / 200 exact")
    print(f"PIL Voronoi PNG     : {png_out} ({img.size[0]}x{img.size[1]} RGB, 262,144 NN queries)")
    print()
    print(f"pac build (--mode prod): {build_t:.2f}s")
    print("| 阶段 (50k 建树 + 50k 查询) | CPython (pure) | Pa (prod) | 加速比 |")
    print("|---|---:|---:|---:|")
    print(f"| 递归建树 (Quickselect) | {py_build_t:.3f}s | {pa_build_t:.4f}s | {py_build_t / pa_build_t:.1f}x |")
    print(f"| 50,000 次最近邻查询 (手动栈) | {py_query_t:.3f}s | {pa_query_t:.4f}s | {py_query_t / pa_query_t:.1f}x |")
    print(f"| 合计 (纯计算) | {py_total:.3f}s | {pa_total:.4f}s | {py_total / pa_total:.1f}x |")
    print(f"| 端到端进程 (含 512² 渲染 + PIL 出图) | {py_total:.3f}s | {pa_e2e:.3f}s | {py_total / pa_e2e:.1f}x |")


if __name__ == "__main__":
    main()
