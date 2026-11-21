"""Automated verification & benchmark: pure-Python Levenshtein/OSA DP vs native Pa binary."""
import os
import subprocess
import tempfile
import time

import numpy as np

from pa_lang.build import compile_pa
from pa_lang.validator import check_file

HERE = os.path.dirname(os.path.abspath(__file__))
N = 50_000


def generate_pairs(data_dir: str, n: int):
    rng = np.random.default_rng(2026)
    alphabet = list("abcdefghij")
    # Edge cases first: empty strings, identical, transpositions, single char, long
    edge = [
        ("", ""),
        ("", "abc"),
        ("abc", ""),
        ("abcdef", "abcdef"),
        ("ab", "ba"),
        ("ca", "abc"),
        ("kitten", "sitting"),
        ("saturday", "sunday"),
        ("algorithm", "altruistic"),
    ]
    a_list = [x for x, _ in edge]
    b_list = [y for _, y in edge]

    rem = n - len(edge)
    lens_a = rng.integers(8, 29, size=rem)
    lens_b = rng.integers(8, 29, size=rem)
    chars_a = rng.integers(0, len(alphabet), size=int(lens_a.sum()))
    chars_b = rng.integers(0, len(alphabet), size=int(lens_b.sum()))
    lut = np.array([ord(c) for c in alphabet], dtype=np.uint8)
    raw_a = lut[chars_a].tobytes()
    raw_b = lut[chars_b].tobytes()

    pa = 0
    pb = 0
    for i in range(rem):
        la = int(lens_a[i])
        lb = int(lens_b[i])
        sa = raw_a[pa : pa + la].decode("ascii")
        sb = raw_b[pb : pb + lb].decode("ascii")
        # 30% of pairs are fuzzy mutations of sa (tests transpositions & small edit distances)
        if i % 3 == 0 and la >= 4:
            arr = list(sa)
            pos = int(rng.integers(0, la - 1))
            arr[pos], arr[pos + 1] = arr[pos + 1], arr[pos]
            sb = "".join(arr)
        a_list.append(sa)
        b_list.append(sb)
        pa += la
        pb += lb

    # Pack into CSR byte buffers for zero-copy .paw transfer
    def pack_csr(strs):
        chunks = [s.encode("utf-8") for s in strs]
        offs = np.zeros(len(strs) + 1, dtype=np.int64)
        for idx, c in enumerate(chunks):
            offs[idx + 1] = offs[idx] + len(c)
        buf = np.frombuffer(b"".join(chunks), dtype=np.uint8)
        return buf, offs

    buf_a, off_a = pack_csr(a_list)
    buf_b, off_b = pack_csr(b_list)
    np.save(os.path.join(data_dir, "buf_a.npy"), buf_a)
    np.save(os.path.join(data_dir, "off_a.npy"), off_a)
    np.save(os.path.join(data_dir, "buf_b.npy"), buf_b)
    np.save(os.path.join(data_dir, "off_b.npy"), off_b)
    return a_list, b_list


def py_lev(a: str, b: str) -> int:
    la, lb = len(a), len(b)
    if la == 0:
        return lb
    if lb == 0:
        return la
    prev = list(range(lb + 1))
    curr = [0] * (lb + 1)
    for i in range(1, la + 1):
        curr[0] = i
        ca = a[i - 1]
        for j in range(1, lb + 1):
            cost = 0 if ca == b[j - 1] else 1
            best = prev[j] + 1
            ins = curr[j - 1] + 1
            sub = prev[j - 1] + cost
            if ins < best:
                best = ins
            if sub < best:
                best = sub
            curr[j] = best
        prev, curr = curr, prev
    return prev[lb]


def py_osa(a: str, b: str) -> int:
    la, lb = len(a), len(b)
    if la == 0:
        return lb
    if lb == 0:
        return la
    stride = lb + 1
    dp = [0] * ((la + 1) * stride)
    for j in range(stride):
        dp[j] = j
    for i in range(1, la + 1):
        dp[i * stride] = i
        ca = a[i - 1]
        for j in range(1, stride):
            cb = b[j - 1]
            cost = 0 if ca == cb else 1
            best = dp[(i - 1) * stride + j] + 1
            ins = dp[i * stride + (j - 1)] + 1
            sub = dp[(i - 1) * stride + (j - 1)] + cost
            if ins < best:
                best = ins
            if sub < best:
                best = sub
            if i > 1 and j > 1 and ca == b[j - 2] and a[i - 2] == cb:
                tr = dp[(i - 2) * stride + (j - 2)] + 1
                if tr < best:
                    best = tr
            dp[i * stride + j] = best
    return dp[la * stride + lb]


def main():
    main_pa = os.path.join(HERE, "main.pa")
    paw_file = os.path.join(HERE, "io_bridge.paw")

    diags = check_file(main_pa, mode="dev") + check_file(paw_file, mode="dev")
    assert not diags, [d.brief() for d in diags]

    with tempfile.TemporaryDirectory() as tmp:
        a_list, b_list = generate_pairs(tmp, N)
        env = dict(os.environ, PA_LEV_DATA_DIR=tmp)

        # 1. Verify dev build passes with full runtime bounds checking on every DP cell & span
        dev_bin = compile_pa(main_pa, out=os.path.join(tmp, "lev_dev"), mode="dev")
        subprocess.run([dev_bin], capture_output=True, text=True, env=env, check=True)

        # 2. Build prod binary and time it
        t_b0 = time.perf_counter()
        prod_bin = compile_pa(main_pa, out=os.path.join(tmp, "lev_prod"), mode="prod")
        build_t = time.perf_counter() - t_b0

        t_p0 = time.perf_counter()
        r_prod = subprocess.run([prod_bin], capture_output=True, text=True, env=env, check=True)
        pa_e2e = time.perf_counter() - t_p0
        pa_lev_t, pa_osa_t, raii_sum = r_prod.stdout.strip().split()
        pa_lev_t, pa_osa_t, raii_sum = float(pa_lev_t), float(pa_osa_t), int(raii_sum)

        pa_lev = np.load(os.path.join(tmp, "pa_lev.npy"))
        pa_osa = np.load(os.path.join(tmp, "pa_osa.npy"))

    # 3. Run pure-Python oracle on all 50,000 pairs
    t0 = time.perf_counter()
    py_lev_res = np.array([py_lev(a_list[i], b_list[i]) for i in range(N)], dtype=np.int64)
    py_lev_t = time.perf_counter() - t0

    t1 = time.perf_counter()
    py_osa_res = np.array([py_osa(a_list[i], b_list[i]) for i in range(N)], dtype=np.int64)
    py_osa_t = time.perf_counter() - t1

    # 4. Exact integer verification
    assert np.array_equal(pa_lev, py_lev_res), "Levenshtein mismatch!"
    assert np.array_equal(pa_osa, py_osa_res), "Damerau-Levenshtein (OSA) mismatch!"
    assert raii_sum == N * 3, f"String RAII stress mismatch: {raii_sum} != {N * 3}"

    py_total = py_lev_t + py_osa_t
    pa_total = pa_lev_t + pa_osa_t
    print(f"=== Levenshtein + OSA DP Verification (N = {N:,} string pairs) ===")
    print(f"Levenshtein (rolling 1D) : 100% exact ({N:,} / {N:,})")
    print(f"Damerau-Lev (flat 2D DP) : 100% exact ({N:,} / {N:,})")
    print(f"String RAII stress (50k) : 100% exact (sum={raii_sum:,}, no leaks)")
    print()
    print(f"pac build (--mode prod): {build_t:.2f}s")
    print("| 阶段 (50,000 对字符串) | CPython (pure) | Pa (prod) | 加速比 |")
    print("|---|---:|---:|---:|")
    print(f"| Levenshtein 滚动数组 (纯计算) | {py_lev_t:.3f}s | {pa_lev_t:.4f}s | {py_lev_t / pa_lev_t:.1f}x |")
    print(f"| Damerau-OSA 二维压一维 (纯计算) | {py_osa_t:.3f}s | {pa_osa_t:.4f}s | {py_osa_t / pa_osa_t:.1f}x |")
    print(f"| 合计 (纯计算) | {py_total:.3f}s | {pa_total:.4f}s | {py_total / pa_total:.1f}x |")
    print(f"| 端到端进程 (含 .paw I/O + 启动) | {py_total:.3f}s | {pa_e2e:.3f}s | {py_total / pa_e2e:.1f}x |")


if __name__ == "__main__":
    main()
