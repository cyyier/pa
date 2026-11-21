"""Compare CPython and compiled Pa on the same loops:  python run_bench.py"""
import os, subprocess, sys, time

here = os.path.dirname(os.path.abspath(__file__))
from pa_lang.build import compile_pa

def timed(cmd):
    t = time.perf_counter()
    r = subprocess.run(cmd, capture_output=True, text=True, check=True)
    return time.perf_counter() - t, r.stdout.split()

t0 = time.perf_counter()
exe = compile_pa(os.path.join(here, "bench.pa"), out=os.path.join(here, "bench_pa"))
print(f"pac build: {time.perf_counter() - t0:.2f}s")
py_t, py_out = timed([sys.executable, os.path.join(here, "bench.py")])
pa_t, pa_out = timed([exe])
print("python:", py_out, f"{py_t:.2f}s")
print("pa    :", pa_out, f"{pa_t:.3f}s")
assert py_out == pa_out, "results differ!"
print(f"speedup: {py_t / pa_t:.0f}x")
