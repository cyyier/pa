"""Thin HTTP router shell: extract params -> call @export-ed .pa function -> serialize.

Build the native library first:   pac build stats.pa --shared -o libstats.so
Run:                              uvicorn app:app
"""
import ctypes
import json
import os

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

LIB_PATH = os.environ.get("PA_STATS_LIB", os.path.join(os.path.dirname(__file__), "libstats.so"))
lib = ctypes.CDLL(LIB_PATH)


class Span(ctypes.Structure):          # Pa span[f64]: {ptr, len}
    _fields_ = [("ptr", ctypes.c_void_p), ("n", ctypes.c_int64)]


lib.mean.argtypes, lib.mean.restype = [Span], ctypes.c_double
lib.variance.argtypes, lib.variance.restype = [Span], ctypes.c_double
lib.moving_average.argtypes = [Span, ctypes.c_int64, ctypes.c_void_p]
lib.moving_average.restype = ctypes.c_int64
lib.trace.argtypes, lib.trace.restype = [Span, ctypes.c_int64], ctypes.c_double
lib.ratio.argtypes, lib.ratio.restype = [ctypes.c_int64, ctypes.c_int64], ctypes.c_int64
lib.at.argtypes, lib.at.restype = [Span, ctypes.c_int64], ctypes.c_double
lib.pa_error_message.restype = ctypes.c_char_p
lib.pa_error_json.restype = ctypes.c_char_p


def checked(call, *args):
    """Run an exported Pa function; a Pa fault (ZeroDivisionError, IndexError, boundary...) becomes HTTP 400.

    The shared library never exits the process: it records the error and returns, so the server survives.
    """
    result = call(*args)
    if lib.pa_error_code():
        info = json.loads(lib.pa_error_json() or b"{}")
        lib.pa_error_clear()
        raise HTTPException(400, detail={"pa_error": info.get("kind", "fault"), "message": info.get("detail", "")})
    return result


def as_span(values):
    buf = (ctypes.c_double * len(values))(*values)
    return Span(ctypes.cast(buf, ctypes.c_void_p), len(values)), buf   # keep buf alive


class Series(BaseModel):
    values: list[float]


class Smooth(Series):
    window: int


class Matrix(BaseModel):
    rows: list[list[float]]


app = FastAPI(title="Pa stats")


@app.post("/stats")
def stats(body: Series):
    if not body.values:
        raise HTTPException(422, "values must not be empty")
    span, _keep = as_span(body.values)
    return {"mean": lib.mean(span), "variance": lib.variance(span)}


@app.post("/smooth")
def smooth(body: Smooth):
    if not 1 <= body.window <= len(body.values):
        raise HTTPException(422, "window must be in [1, len(values)]")
    span, _keep = as_span(body.values)
    out = (ctypes.c_double * len(body.values))()
    n = lib.moving_average(span, body.window, ctypes.cast(out, ctypes.c_void_p))
    return {"values": list(out[:n])}


@app.post("/trace")
def trace(body: Matrix):
    n = len(body.rows)
    if n == 0 or any(len(r) != n for r in body.rows):
        raise HTTPException(422, "matrix must be square")
    span, _keep = as_span([x for r in body.rows for x in r])
    return {"trace": lib.trace(span, n)}


class Ratio(BaseModel):
    a: int
    b: int


class Pick(Series):
    index: int


@app.post("/ratio")
def ratio(body: Ratio):
    return {"ratio": checked(lib.ratio, body.a, body.b)}


@app.post("/at")
def at(body: Pick):
    span, _keep = as_span(body.values)
    return {"value": checked(lib.at, span, body.index)}
