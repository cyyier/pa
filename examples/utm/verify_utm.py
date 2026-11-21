"""Automated verification & benchmark: original `utm` library (CPython) vs native Pa binary."""
import os
import subprocess
import sys
import tempfile
import time

import numpy as np
import utm

from pa_lang.build import compile_pa
from pa_lang.validator import check_file

HERE = os.path.dirname(os.path.abspath(__file__))
N = 100_000


def generate_inputs(data_dir: str, n: int) -> tuple[np.ndarray, np.ndarray]:
    rng = np.random.default_rng(2026)
    lats = rng.uniform(-80.0, 84.0, size=n)
    lons = rng.uniform(-180.0, 180.0, size=n)
    # Inject tricky boundary & special-zone coordinates (Norway, Svalbard, equator, edges)
    special = [
        (60.0, 5.0),      # Norway 32V
        (78.0, 5.0),      # Svalbard 31X
        (78.0, 15.0),     # Svalbard 33X
        (78.0, 25.0),     # Svalbard 35X
        (78.0, 38.0),     # Svalbard 37X
        (-80.0, -180.0),  # SW corner
        (84.0, 179.9999), # NE corner
        (0.0, 0.0),       # Equator / prime meridian
        (-0.0001, 179.999),
    ]
    for i, (la, lo) in enumerate(special):
        lats[i], lons[i] = la, lo
    np.save(os.path.join(data_dir, "in_lats.npy"), lats)
    np.save(os.path.join(data_dir, "in_lons.npy"), lons)
    return lats, lons


def run_python_oracle(lats: np.ndarray, lons: np.ndarray):
    n = len(lats)
    eastings = np.empty(n, dtype=np.float64)
    northings = np.empty(n, dtype=np.float64)
    zones = np.empty(n, dtype=np.int64)
    letters = np.empty(n, dtype=np.uint8)
    rt_lats = np.empty(n, dtype=np.float64)
    rt_lons = np.empty(n, dtype=np.float64)

    t0 = time.perf_counter()
    for i in range(n):
        e, no, zn, zl = utm.from_latlon(float(lats[i]), float(lons[i]))
        eastings[i] = e
        northings[i] = no
        zones[i] = zn
        letters[i] = ord(zl)
    t1 = time.perf_counter()
    for i in range(n):
        la, lo = utm.to_latlon(float(eastings[i]), float(northings[i]), int(zones[i]), chr(int(letters[i])))
        rt_lats[i] = la
        rt_lons[i] = lo
    t2 = time.perf_counter()
    return (eastings, northings, zones, letters, rt_lats, rt_lons), (t1 - t0, t2 - t1)


def main():
    main_pa = os.path.join(HERE, "main.pa")
    paw_file = os.path.join(HERE, "io_bridge.paw")

    diags = check_file(main_pa, mode="dev") + check_file(paw_file, mode="dev")
    assert not diags, [d.brief() for d in diags]

    with tempfile.TemporaryDirectory() as tmp:
        lats, lons = generate_inputs(tmp, N)

        # 1. Verify dev build runs cleanly with full bounds & overflow checks
        dev_bin = compile_pa(main_pa, out=os.path.join(tmp, "utm_dev"), mode="dev")
        env = dict(os.environ, PA_UTM_DATA_DIR=tmp)
        r_dev = subprocess.run([dev_bin], capture_output=True, text=True, env=env, check=True)
        assert r_dev.returncode == 0

        # 2. Build prod native binary and time end-to-end + pure compute
        t_b0 = time.perf_counter()
        prod_bin = compile_pa(main_pa, out=os.path.join(tmp, "utm_prod"), mode="prod")
        build_t = time.perf_counter() - t_b0

        t_p0 = time.perf_counter()
        r_prod = subprocess.run([prod_bin], capture_output=True, text=True, env=env, check=True)
        pa_e2e = time.perf_counter() - t_p0
        pa_fwd, pa_inv = map(float, r_prod.stdout.strip().split())

        pa_e = np.load(os.path.join(tmp, "pa_eastings.npy"))
        pa_n = np.load(os.path.join(tmp, "pa_northings.npy"))
        pa_z = np.load(os.path.join(tmp, "pa_zones.npy"))
        pa_l = np.load(os.path.join(tmp, "pa_letters.npy"))
        pa_rt_lat = np.load(os.path.join(tmp, "pa_rt_lats.npy"))
        pa_rt_lon = np.load(os.path.join(tmp, "pa_rt_lons.npy"))

        # 3. Run original Python utm library oracle
        (py_e, py_n, py_z, py_l, py_rt_lat, py_rt_lon), (py_fwd, py_inv) = run_python_oracle(lats, lons)

    # 4. Numerical consistency checks
    max_de = float(np.max(np.abs(pa_e - py_e)))
    max_dn = float(np.max(np.abs(pa_n - py_n)))
    max_dlat = float(np.max(np.abs(pa_rt_lat - py_rt_lat)))
    max_dlon = float(np.max(np.abs(pa_rt_lon - py_rt_lon)))
    zone_eq = bool(np.array_equal(pa_z, py_z))
    letter_eq = bool(np.array_equal(pa_l, py_l))

    print(f"=== UTM Differential Verification (N = {N:,}) ===")
    print(f"max |Δeasting|  = {max_de:.3e} m   (< 1e-9: {max_de < 1e-9})")
    print(f"max |Δnorthing| = {max_dn:.3e} m   (< 1e-9: {max_dn < 1e-9})")
    print(f"zone_number     = {'100% exact' if zone_eq else 'MISMATCH'}")
    print(f"zone_letter     = {'100% exact' if letter_eq else 'MISMATCH'}")
    print(f"max |Δrt_lat|   = {max_dlat:.3e} deg (< 1e-9: {max_dlat < 1e-9})")
    print(f"max |Δrt_lon|   = {max_dlon:.3e} deg (< 1e-9: {max_dlon < 1e-9})")

    assert max_de < 1e-9, f"easting diff {max_de}"
    assert max_dn < 1e-9, f"northing diff {max_dn}"
    assert zone_eq, "zone_number mismatch"
    assert letter_eq, "zone_letter mismatch"
    assert max_dlat < 1e-9, f"rt_lat diff {max_dlat}"
    assert max_dlon < 1e-9, f"rt_lon diff {max_dlon}"

    py_total = py_fwd + py_inv
    pa_compute = pa_fwd + pa_inv
    print()
    print(f"pac build (--mode prod): {build_t:.2f}s")
    print("| 阶段 (100,000 点) | CPython (utm) | Pa (prod) | 加速比 |")
    print("|---|---:|---:|---:|")
    print(f"| from_latlon (纯计算) | {py_fwd:.3f}s | {pa_fwd:.4f}s | {py_fwd / pa_fwd:.1f}x |")
    print(f"| to_latlon (纯计算) | {py_inv:.3f}s | {pa_inv:.4f}s | {py_inv / pa_inv:.1f}x |")
    print(f"| 合计 (纯计算) | {py_total:.3f}s | {pa_compute:.4f}s | {py_total / pa_compute:.1f}x |")
    print(f"| 端到端进程 (含 .paw I/O + 启动) | {py_total:.3f}s | {pa_e2e:.3f}s | {py_total / pa_e2e:.1f}x |")


if __name__ == "__main__":
    main()
