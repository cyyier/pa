"""Layer 3 (Python Adapter): real stdlib sqlite3 (libsqlite3.so) and zlib (libz.so).

Zero third-party pip dependencies so offline CI runs deterministically on Linux, macOS, and Windows.
"""
from __future__ import annotations

import sqlite3
import struct
import zlib

_CONN: sqlite3.Connection | None = None
_CRC_STATE: int = 0


def _ensure_conn() -> sqlite3.Connection:
    global _CONN
    if _CONN is None:
        _CONN = sqlite3.connect(":memory:")
        _CONN.execute(
            "CREATE TABLE IF NOT EXISTS telemetry ("
            "  batch_id INTEGER NOT NULL,"
            "  idx INTEGER NOT NULL,"
            "  val REAL NOT NULL"
            ")"
        )
        _CONN.commit()
    return _CONN


def reset_session() -> int:
    """Reset the in-memory SQLite database (libsqlite3) and rolling zlib CRC32 (libz) state."""
    global _CONN, _CRC_STATE
    if _CONN is not None:
        _CONN.close()
        _CONN = None
    _ensure_conn()
    _CRC_STATE = 0
    return 1


def ingest_batch(batch_id: int, values) -> int:
    """Persist a contiguous f64 span into SQLite (:memory:) and fold bytes into zlib.crc32."""
    global _CRC_STATE
    conn = _ensure_conn()
    # Read zero-copy memoryview / buffer as IEEE-754 f64 without numpy
    raw = bytes(values)
    count = len(raw) // 8
    floats = struct.unpack(f"<{count}d", raw)
    conn.executemany(
        "INSERT INTO telemetry (batch_id, idx, val) VALUES (?, ?, ?)",
        ((int(batch_id), i, v) for i, v in enumerate(floats)),
    )
    conn.commit()
    _CRC_STATE = zlib.crc32(raw, _CRC_STATE) & 0xFFFFFFFF
    return count


def query_batch_sum(batch_id: int) -> float:
    """Aggregate SUM(val) for a batch directly inside SQLite (libsqlite3)."""
    conn = _ensure_conn()
    row = conn.execute(
        "SELECT COALESCE(SUM(val), 0.0) FROM telemetry WHERE batch_id = ?",
        (int(batch_id),),
    ).fetchone()
    return float(row[0])


def query_top_values(limit_n: int) -> list[float]:
    """Read back the top-N largest values across all ingested batches from SQLite."""
    conn = _ensure_conn()
    rows = conn.execute(
        "SELECT val FROM telemetry ORDER BY val DESC, batch_id ASC, idx ASC LIMIT ?",
        (int(limit_n),),
    ).fetchall()
    return [float(r[0]) for r in rows]


def total_row_count() -> int:
    """Return total rows stored in the live SQLite connection."""
    conn = _ensure_conn()
    row = conn.execute("SELECT COUNT(*) FROM telemetry").fetchone()
    return int(row[0])


def current_crc32() -> int:
    """Return the accumulated zlib.crc32 checksum across all ingested batches."""
    return int(_CRC_STATE)


def verify_against_python_ref(n: int, pa_total_sum: float, pa_top_sum: float, pa_crc: int) -> int:
    """Pure-Python differential oracle verifying Pa's native math + SQLite + zlib pipeline."""
    # Exact same mathematical recurrence as helper.pa:compute_batch()
    b1 = [((i * 17 + 13) % 997) * 0.125 + 1.0 * 0.5 for i in range(n)]
    b2 = [((i * 17 + 29) % 997) * 0.125 + 2.0 * 0.5 for i in range(n)]

    ref_conn = sqlite3.connect(":memory:")
    ref_conn.execute("CREATE TABLE t (batch_id INTEGER, idx INTEGER, val REAL)")
    ref_conn.executemany("INSERT INTO t VALUES (?, ?, ?)", ((1, i, v) for i, v in enumerate(b1)))
    ref_conn.executemany("INSERT INTO t VALUES (?, ?, ?)", ((2, i, v) for i, v in enumerate(b2)))
    ref_total = float(ref_conn.execute("SELECT SUM(val) FROM t").fetchone()[0])
    top8 = [float(r[0]) for r in ref_conn.execute("SELECT val FROM t ORDER BY val DESC, batch_id ASC, idx ASC LIMIT 8").fetchall()]
    ref_top_sum = sum(top8)
    ref_conn.close()

    raw1 = struct.pack(f"<{n}d", *b1)
    raw2 = struct.pack(f"<{n}d", *b2)
    ref_crc = zlib.crc32(raw2, zlib.crc32(raw1, 0)) & 0xFFFFFFFF

    sum_ok = abs(pa_total_sum - ref_total) < 1e-6
    top_ok = abs(pa_top_sum - ref_top_sum) < 1e-6
    crc_ok = int(pa_crc) == ref_crc
    return 1 if (sum_ok and top_ok and crc_ok) else 0
