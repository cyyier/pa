"""Four-Layer Nested E2E Integration Test backed by real libsqlite3 and libz.

Topology:
    main.pa -> helper.pa -> bridge.paw -> py_adapter.py -> sqlite3 (libsqlite3) & zlib (libz)

Also verifies repackaging helper.pa as a standalone C-ABI shared library (`libpa_repackaged.*`)
and calling its @export functions directly from Python via ctypes.CDLL (zero PyObject*).
"""
from __future__ import annotations

import ctypes
import os
import sqlite3
import struct
import subprocess
import sys
import zlib
from pathlib import Path

from pa_lang.build import compile_pa, default_python_paths
from pa_lang.validator import check_file

FIXTURE_DIR = Path(__file__).resolve().parent / "e2e_four_layer"
SHARED_EXT = ".dll" if sys.platform == "win32" else ".dylib" if sys.platform == "darwin" else ".so"


class PaSpan(ctypes.Structure):
    _fields_ = [("data", ctypes.c_void_p), ("len", ctypes.c_int64)]


def _python_oracle(n: int) -> tuple[int, float, float, int]:
    b1 = [((i * 17 + 13) % 997) * 0.125 + 0.5 for i in range(n)]
    b2 = [((i * 17 + 29) % 997) * 0.125 + 1.0 for i in range(n)]
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE t (batch_id INTEGER, idx INTEGER, val REAL)")
    conn.executemany("INSERT INTO t VALUES (?, ?, ?)", ((1, i, v) for i, v in enumerate(b1)))
    conn.executemany("INSERT INTO t VALUES (?, ?, ?)", ((2, i, v) for i, v in enumerate(b2)))
    total_sum = float(conn.execute("SELECT SUM(val) FROM t").fetchone()[0])
    top_rows = conn.execute(
        "SELECT val FROM t ORDER BY val DESC, batch_id ASC, idx ASC LIMIT 8"
    ).fetchall()
    top_sum = sum(float(r[0]) for r in top_rows)
    conn.close()
    raw1 = struct.pack(f"<{n}d", *b1)
    raw2 = struct.pack(f"<{n}d", *b2)
    crc = zlib.crc32(raw2, zlib.crc32(raw1, 0)) & 0xFFFFFFFF
    return 2 * n, total_sum, top_sum, crc


def test_four_layer_fixture_passes_static_checks():
    for fname in ("main.pa", "helper.pa", "bridge.paw"):
        diags = check_file(str(FIXTURE_DIR / fname))
        errors = [d for d in diags if d.severity == "error"]
        assert not errors, f"{fname} failed pac check: {[d.brief() for d in errors]}"


def test_four_layer_native_to_sqlite3_and_zlib_e2e(tmp_path):
    exe = compile_pa(str(FIXTURE_DIR / "main.pa"), out=str(tmp_path / "four_layer_app"))
    env = dict(os.environ)
    py_paths = [str(FIXTURE_DIR)] + default_python_paths()
    env["PA_PYPATH"] = os.pathsep.join(py_paths)

    r = subprocess.run([exe], capture_output=True, text=True, encoding="utf-8", cwd=tmp_path, env=env)
    assert r.returncode == 0, f"stdout={r.stdout!r}\nstderr={r.stderr!r}"

    parts = r.stdout.strip().split()
    assert len(parts) == 6, f"unexpected output: {r.stdout!r}"
    rows, ingested = int(parts[0]), int(parts[1])
    pa_total_sum, pa_top_sum = float(parts[2]), float(parts[3])
    pa_crc, pa_ok = int(parts[4]), int(parts[5])

    exp_rows, exp_total, exp_top, exp_crc = _python_oracle(2000)
    assert rows == exp_rows == 4000
    assert ingested == 4000
    assert abs(pa_total_sum - exp_total) < 1e-6
    assert abs(pa_top_sum - exp_top) < 1e-6
    assert pa_crc == exp_crc
    assert pa_ok == 1


def test_repackaged_helper_shared_library_via_ctypes(tmp_path):
    so_path = compile_pa(
        str(FIXTURE_DIR / "helper.pa"),
        out=str(tmp_path / f"libpa_repackaged{SHARED_EXT}"),
        shared=True,
    )
    assert os.path.isfile(so_path)

    lib = ctypes.CDLL(so_path)
    lib.native_batch_checksum.argtypes = [ctypes.c_int64, ctypes.c_int64, ctypes.c_int64]
    lib.native_batch_checksum.restype = ctypes.c_double
    lib.native_dot_scale.argtypes = [PaSpan, ctypes.c_double]
    lib.native_dot_scale.restype = ctypes.c_double
    lib.pa_error_code.argtypes = []
    lib.pa_error_code.restype = ctypes.c_int32

    got_sum = lib.native_batch_checksum(2000, 13, 1)
    assert lib.pa_error_code() == 0
    exp_sum = sum(((i * 17 + 13) % 997) * 0.125 + 0.5 for i in range(2000))
    assert abs(got_sum - exp_sum) < 1e-6

    arr = (ctypes.c_double * 4)(1.5, 2.5, 3.5, 4.5)
    got_dot = lib.native_dot_scale(PaSpan(ctypes.addressof(arr), 4), 2.0)
    assert lib.pa_error_code() == 0
    assert abs(got_dot - 24.0) < 1e-12


def test_direct_python_import_of_shared_module_zero_ctypes(tmp_path, monkeypatch):
    import array
    import importlib

    pyext = ".pyd" if sys.platform == "win32" else ".so"
    mod_name = "pa_kernel_direct"
    so_path = compile_pa(
        str(FIXTURE_DIR / "helper.pa"),
        out=str(tmp_path / f"{mod_name}{pyext}"),
        shared=True,
    )
    assert os.path.isfile(so_path)

    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()
    mod = importlib.import_module(mod_name)

    got_sum = mod.native_batch_checksum(2000, 13, 1)
    exp_sum = sum(((i * 17 + 13) % 997) * 0.125 + 0.5 for i in range(2000))
    assert abs(got_sum - exp_sum) < 1e-6

    buf = array.array("d", [1.5, 2.5, 3.5, 4.5])
    assert abs(mod.native_dot_scale(buf, 2.0) - 24.0) < 1e-12
