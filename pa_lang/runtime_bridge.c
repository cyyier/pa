/* Pa runtime bridge: embedded CPython + two-way boundary customs for .paw calls.
 *
 * Out-bound (PA_TO_PY_IN): scalars/buffers are checked for NaN/Inf, NULL, negative length.
 * In-bound  (PY_TO_PA_OUT): None / NaN / Inf / type drift / range overflow are rejected.
 * Any violation writes pa_boundary_fault.json (with the full In snapshot) and exits(70).
 */
#define PY_SSIZE_T_CLEAN
#include <Python.h>
#include <math.h>
#include <stdarg.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

enum { T_INT = 0, T_FLOAT = 1, T_BOOL = 2, T_STR = 3, T_BUF = 4, T_VOID = 5 };
enum { E_I32 = 0, E_I64 = 1, E_U8 = 2, E_F32 = 3, E_F64 = 4, E_BOOL = 5 };

typedef struct { int32_t tag, elem; int64_t i; double f; void *p; int64_t n; } PaArg;
typedef struct { int64_t i; double f; void *p; int64_t n; } PaRet;

static const char *ELEM_NAME[] = {"i32", "i64", "u8", "f32", "f64", "bool"};
static const char *ELEM_FMT[] = {"i", "q", "B", "f", "d", "?"};
static const int ELEM_SIZE[] = {4, 8, 1, 4, 8, 1};

static PyObject *g_bridge = NULL;
static int g_allow_nan = 0;   /* @allow_nan: per-call, set from the flags argument */

/* ---- tiny string builder ------------------------------------------------ */
typedef struct { char *s; size_t n, cap; } SB;
static void sb_printf(SB *b, const char *fmt, ...) {
    va_list ap;
    va_start(ap, fmt);
    va_list ap2;
    va_copy(ap2, ap);
    int need = vsnprintf(NULL, 0, fmt, ap);
    va_end(ap);
    if (b->n + need + 1 > b->cap) {
        b->cap = (b->n + need + 1) * 2;
        b->s = (char *)realloc(b->s, b->cap);
    }
    vsnprintf(b->s + b->n, need + 1, fmt, ap2);
    va_end(ap2);
    b->n += need;
}
static void sb_json_str(SB *b, const char *s, size_t len) {
    sb_printf(b, "\"");
    for (size_t i = 0; i < len; i++) {
        unsigned char c = (unsigned char)s[i];
        if (c == '"' || c == '\\') sb_printf(b, "\\%c", c);
        else if (c < 0x20) sb_printf(b, "\\u%04x", c);
        else sb_printf(b, "%c", c);
    }
    sb_printf(b, "\"");
}
static void sb_num(SB *b, double v) {
    if (isnan(v)) sb_printf(b, "\"NaN\"");
    else if (isinf(v)) sb_printf(b, v > 0 ? "\"Inf\"" : "\"-Inf\"");
    else sb_printf(b, "%.17g", v);
}
static double elem_at(const PaArg *a, int64_t k) {
    const char *p = (const char *)a->p;
    switch (a->elem) {
    case E_I32: return ((const int32_t *)p)[k];
    case E_I64: return (double)((const int64_t *)p)[k];
    case E_U8: return ((const uint8_t *)p)[k];
    case E_F32: return ((const float *)p)[k];
    case E_F64: return ((const double *)p)[k];
    default: return ((const uint8_t *)p)[k] != 0;
    }
}

/* ---- fault reporting ---------------------------------------------------- */
static void snapshot(SB *b, const PaArg *args, int nargs) {
    sb_printf(b, "[");
    for (int i = 0; i < nargs; i++) {
        const PaArg *a = &args[i];
        sb_printf(b, "%s{\"index\":%d,", i ? "," : "", i);
        if (a->tag == T_INT) sb_printf(b, "\"type\":\"%s\",\"value\":%lld}", ELEM_NAME[a->elem], (long long)a->i);
        else if (a->tag == T_FLOAT) { sb_printf(b, "\"type\":\"%s\",\"value\":", ELEM_NAME[a->elem]); sb_num(b, a->f); sb_printf(b, "}"); }
        else if (a->tag == T_BOOL) sb_printf(b, "\"type\":\"bool\",\"value\":%s}", a->i ? "true" : "false");
        else if (a->tag == T_STR) { sb_printf(b, "\"type\":\"str_view\",\"value\":"); sb_json_str(b, (const char *)a->p, a->p ? (size_t)a->n : 0); sb_printf(b, "}"); }
        else {
            int64_t lim = a->n < 1024 ? a->n : 1024;
            sb_printf(b, "\"type\":\"buffer<%s>\",\"len\":%lld,\"truncated\":%s,\"values\":[", ELEM_NAME[a->elem],
                      (long long)a->n, a->n > lim ? "true" : "false");
            for (int64_t k = 0; k < lim && a->p; k++) { if (k) sb_printf(b, ","); sb_num(b, elem_at(a, k)); }
            sb_printf(b, "]}");
        }
    }
    sb_printf(b, "]");
}

static void fault(const char *direction, const char *worker, const char *violation, const PaArg *args, int nargs) {
    SB b = {0};
    sb_printf(&b, "{\n  \"direction\": \"%s\",\n  \"worker\": ", direction);
    sb_json_str(&b, worker, strlen(worker));
    sb_printf(&b, ",\n  \"violation\": ");
    sb_json_str(&b, violation, strlen(violation));
    sb_printf(&b, ",\n  \"in_payload_snapshot\": ");
    snapshot(&b, args, nargs);
    sb_printf(&b, "\n}\n");
    const char *path = getenv("PA_FAULT_PATH");
    FILE *f = fopen(path ? path : "pa_boundary_fault.json", "w");
    if (f) { fputs(b.s, f); fclose(f); }
    fprintf(stderr, "[pa boundary fault] %s %s: %s (see pa_boundary_fault.json)\n", direction, worker, violation);
    fflush(NULL);
    exit(70);
}

/* ---- init --------------------------------------------------------------- */
static const char PRELUDE[] =
    "import sys, os, importlib.util, builtins\n"
    "class _Gen:\n"
    "    def __class_getitem__(cls, k):\n"
    "        return (cls.__name__, k)\n"
    "class span(_Gen): pass\n"
    "class Vec(_Gen): pass\n"
    "class ptr(_Gen): pass\n"
    "for _n in ('i32', 'i64', 'u8'): setattr(builtins, _n, int)\n"
    "for _n in ('f32', 'f64'): setattr(builtins, _n, float)\n"
    "builtins.allow_nan = lambda f: f\n"
    "builtins.str_view = str; builtins.bytes_view = bytes; builtins.void = type(None)\n"
    "for _c in (span, Vec, ptr): setattr(builtins, _c.__name__, _c)\n"
    "PAW_DIRS = []\n"
    "_cache = {}\n"
    "def load(name):\n"
    "    if name in _cache: return _cache[name]\n"
    "    for d in PAW_DIRS:\n"
    "        p = os.path.join(d, name + '.paw')\n"
    "        if os.path.isfile(p):\n"
    "            from importlib.machinery import SourceFileLoader\n"
    "            loader = SourceFileLoader('paw_' + name, p)\n"
    "            spec = importlib.util.spec_from_loader('paw_' + name, loader)\n"
    "            m = importlib.util.module_from_spec(spec)\n"
    "            sys.modules['paw_' + name] = m\n"
    "            loader.exec_module(m)\n"
    "            _cache[name] = m\n"
    "            return m\n"
    "    raise ImportError('no .paw module named ' + name + ' in ' + repr(PAW_DIRS))\n";

#ifndef PA_PYHOME
#define PA_PYHOME ""
#endif
#ifndef PA_PYPATH
#define PA_PYPATH ""
#endif
#ifndef PA_PAWDIRS
#define PA_PAWDIRS ""
#endif

void pa_bridge_init(const char *pyhome, const char *pypath, const char *pawdirs) {
    if (g_bridge) return;
    if (!Py_IsInitialized()) {
        if (pyhome && *pyhome && !getenv("PYTHONHOME")) setenv("PYTHONHOME", pyhome, 0);
        Py_InitializeEx(0);
    }
    PyGILState_STATE g = PyGILState_Ensure();
    g_bridge = PyModule_New("_pa_bridge");
    PyObject *d = PyModule_GetDict(g_bridge);
    PyDict_SetItemString(d, "__builtins__", PyEval_GetBuiltins());
    PyObject *r = PyRun_String(PRELUDE, Py_file_input, d, d);
    if (!r) { PyErr_Print(); exit(70); }
    Py_DECREF(r);
    PyObject *sys_path = PySys_GetObject("path");
    char *dup = strdup(pypath ? pypath : "");
    for (char *tok = strtok(dup, ":"); tok; tok = strtok(NULL, ":")) {
        PyObject *s = PyUnicode_FromString(tok);
        if (PySequence_Contains(sys_path, s) == 0) PyList_Append(sys_path, s);
        Py_DECREF(s);
    }
    free(dup);
    PyObject *dirs = PyDict_GetItemString(d, "PAW_DIRS");
    dup = strdup(pawdirs ? pawdirs : "");
    const char *env = getenv("PA_PATH");
    if (env) { char *e2 = strdup(env); for (char *tok = strtok(e2, ":"); tok; tok = strtok(NULL, ":")) { PyObject *s = PyUnicode_FromString(tok); PyList_Append(dirs, s); Py_DECREF(s);} free(e2); }
    for (char *tok = strtok(dup, ":"); tok; tok = strtok(NULL, ":")) {
        PyObject *s = PyUnicode_FromString(tok);
        PyList_Append(dirs, s);
        Py_DECREF(s);
    }
    free(dup);
    PyGILState_Release(g);
}

/* ---- out-bound customs -------------------------------------------------- */
static void check_out(const char *worker, const PaArg *args, int nargs) {
    char msg[200];
    for (int i = 0; i < nargs; i++) {
        const PaArg *a = &args[i];
        if (!g_allow_nan && a->tag == T_FLOAT && !isfinite(a->f)) {
            snprintf(msg, sizeof msg, "args[%d]: non-finite value %s", i, isnan(a->f) ? "NaN" : "Inf");
            fault("PA_TO_PY_IN", worker, msg, args, nargs);
        }
        if (a->tag == T_STR || a->tag == T_BUF) {
            if (a->n < 0) { snprintf(msg, sizeof msg, "args[%d]: negative length %lld", i, (long long)a->n); fault("PA_TO_PY_IN", worker, msg, args, nargs); }
            if (a->n > 0 && !a->p) { snprintf(msg, sizeof msg, "args[%d]: uninitialized (NULL) pointer with length %lld", i, (long long)a->n); fault("PA_TO_PY_IN", worker, msg, args, nargs); }
        }
        if (!g_allow_nan && a->tag == T_BUF && (a->elem == E_F32 || a->elem == E_F64)) {
            for (int64_t k = 0; k < a->n; k++) {
                double v = elem_at(a, k);
                if (!isfinite(v)) {
                    snprintf(msg, sizeof msg, "args[%d][%lld]: non-finite value %s", i, (long long)k, isnan(v) ? "NaN" : "Inf");
                    fault("PA_TO_PY_IN", worker, msg, args, nargs);
                }
            }
        }
    }
}

static PyObject *build_arg(const PaArg *a) {
    static char dummy[8];
    switch (a->tag) {
    case T_INT: return PyLong_FromLongLong(a->i);
    case T_FLOAT: return PyFloat_FromDouble(a->f);
    case T_BOOL: return PyBool_FromLong(a->i != 0);
    case T_STR: return PyUnicode_DecodeUTF8(a->p ? (const char *)a->p : "", a->p ? a->n : 0, "strict");
    default: {
        PyObject *mv = PyMemoryView_FromMemory(a->p ? (char *)a->p : dummy, a->n * ELEM_SIZE[a->elem], PyBUF_READ);
        if (!mv) return NULL;
        PyObject *cast = PyObject_CallMethod(mv, "cast", "s", ELEM_FMT[a->elem]);
        Py_DECREF(mv);
        return cast;
    }
    }
}

/* ---- in-bound customs --------------------------------------------------- */
#define IN_FAULT(...) do { snprintf(msg, sizeof msg, __VA_ARGS__); fault("PY_TO_PA_OUT", worker, msg, args, nargs); } while (0)

static int is_np_bool(PyObject *o) {
    const char *n = Py_TYPE(o)->tp_name;
    return strcmp(n, "numpy.bool_") == 0 || strcmp(n, "numpy.bool") == 0;
}

static void unbox_buffer(PyObject *o, int elem, PaRet *out, const char *worker, const PaArg *args, int nargs) {
    char msg[200];
    int is_float = elem == E_F32 || elem == E_F64;
    Py_buffer view;
    int64_t n;
    if (PyObject_GetBuffer(o, &view, PyBUF_FORMAT | PyBUF_C_CONTIGUOUS) == 0) {
        const char *f = view.format ? view.format : "B";
        while (*f == '<' || *f == '=' || *f == '@' || *f == '>') f++;
        int f_float = *f == 'd' || *f == 'f';
        int f_bool = *f == '?';
        if (f_float != is_float || (elem == E_BOOL) != f_bool || view.itemsize != ELEM_SIZE[elem]) {
            PyBuffer_Release(&view);
            IN_FAULT("return: buffer format '%s' (itemsize %zd) drifted from Vec[%s]", view.format ? view.format : "?", view.itemsize, ELEM_NAME[elem]);
        }
        n = view.len / view.itemsize;
        out->p = malloc(view.len ? view.len : 1);
        memcpy(out->p, view.buf, view.len);
        PyBuffer_Release(&view);
    } else {
        PyErr_Clear();
        PyObject *seq = PySequence_Fast(o, "x");
        if (!seq) { PyErr_Clear(); IN_FAULT("return: %s is not a Vec[%s] (no buffer/sequence)", Py_TYPE(o)->tp_name, ELEM_NAME[elem]); }
        n = PySequence_Fast_GET_SIZE(seq);
        out->p = malloc((size_t)(n ? n : 1) * ELEM_SIZE[elem]);
        for (int64_t k = 0; k < n; k++) {
            PyObject *it = PySequence_Fast_GET_ITEM(seq, k);
            if (it == Py_None) IN_FAULT("return[%lld]: None in Vec[%s]", (long long)k, ELEM_NAME[elem]);
            if (is_float) {
                if (!PyFloat_Check(it)) IN_FAULT("return[%lld]: %s drifted from %s", (long long)k, Py_TYPE(it)->tp_name, ELEM_NAME[elem]);
                double v = PyFloat_AsDouble(it);
                if (elem == E_F32) ((float *)out->p)[k] = (float)v; else ((double *)out->p)[k] = v;
            } else {
                if (!PyLong_Check(it) || (PyBool_Check(it) && elem != E_BOOL)) IN_FAULT("return[%lld]: %s drifted from %s", (long long)k, Py_TYPE(it)->tp_name, ELEM_NAME[elem]);
                long long v = PyLong_AsLongLong(it);
                if (elem == E_I32) ((int32_t *)out->p)[k] = (int32_t)v; else if (elem == E_I64) ((int64_t *)out->p)[k] = v; else ((uint8_t *)out->p)[k] = (uint8_t)v;
            }
        }
        Py_DECREF(seq);
    }
    if (is_float && !g_allow_nan) {
        for (int64_t k = 0; k < n; k++) {
            double v = elem == E_F32 ? ((float *)out->p)[k] : ((double *)out->p)[k];
            if (!isfinite(v)) IN_FAULT("return[%lld]: non-finite value %s", (long long)k, isnan(v) ? "NaN" : "Inf");
        }
    }
    out->n = n;
}

static void unbox(PyObject *o, int ret_tag, int ret_elem, PaRet *out, const char *worker, const PaArg *args, int nargs) {
    char msg[200];
    memset(out, 0, sizeof *out);
    if (ret_tag == T_VOID) {
        if (o != Py_None) IN_FAULT("return: expected None (void), got %s", Py_TYPE(o)->tp_name);
        return;
    }
    if (o == Py_None) IN_FAULT("return: None where %s was declared", ret_tag == T_BUF ? "Vec" : ELEM_NAME[ret_elem]);
    if (ret_tag == T_BOOL) {
        if (!PyBool_Check(o) && !is_np_bool(o)) IN_FAULT("return: %s drifted from bool", Py_TYPE(o)->tp_name);
        out->i = PyObject_IsTrue(o);
    } else if (ret_tag == T_INT) {
        if (PyBool_Check(o) || PyFloat_Check(o) || !(PyLong_Check(o) || PyIndex_Check(o)))
            IN_FAULT("return: %s drifted from %s", Py_TYPE(o)->tp_name, ELEM_NAME[ret_elem]);
        int ovf = 0;
        PyObject *as_long = PyNumber_Index(o);
        long long v = as_long ? PyLong_AsLongLongAndOverflow(as_long, &ovf) : 0;
        Py_XDECREF(as_long);
        if (ovf || (ret_elem == E_I32 && (v < INT32_MIN || v > INT32_MAX)) || (ret_elem == E_U8 && (v < 0 || v > 255)))
            IN_FAULT("return: integer out of range for %s", ELEM_NAME[ret_elem]);
        out->i = v;
    } else if (ret_tag == T_FLOAT) {
        if (PyBool_Check(o) || PyLong_Check(o) || PyComplex_Check(o) || !(PyFloat_Check(o) || PyNumber_Check(o)))
            IN_FAULT("return: %s drifted from %s", Py_TYPE(o)->tp_name, ELEM_NAME[ret_elem]);
        double v = PyFloat_AsDouble(o);
        if (PyErr_Occurred()) { PyErr_Clear(); IN_FAULT("return: %s not convertible to %s", Py_TYPE(o)->tp_name, ELEM_NAME[ret_elem]); }
        if (!isfinite(v) && !g_allow_nan) IN_FAULT("return: non-finite value %s", isnan(v) ? "NaN" : "Inf");
        out->f = v;
    } else {
        unbox_buffer(o, ret_elem, out, worker, args, nargs);
    }
}

void pa_call_paw(const char *mod, const char *fn, PaArg *args, int32_t nargs, int32_t ret_tag, int32_t ret_elem, int32_t flags, PaRet *out) {
    char worker[256], msg[512];
    g_allow_nan = flags & 1;
    snprintf(worker, sizeof worker, "%s.%s", mod, fn);
    check_out(worker, args, nargs);
    if (!g_bridge) pa_bridge_init(PA_PYHOME, PA_PYPATH, PA_PAWDIRS);
    PyGILState_STATE g = PyGILState_Ensure();
    PyObject *m = PyObject_CallMethod(g_bridge, "load", "s", mod);
    PyObject *f = m ? PyObject_GetAttrString(m, fn) : NULL;
    PyObject *tup = f ? PyTuple_New(nargs) : NULL;
    for (int i = 0; tup && i < nargs; i++) {
        PyObject *a = build_arg(&args[i]);
        if (!a) { Py_CLEAR(tup); break; }
        PyTuple_SET_ITEM(tup, i, a);
    }
    PyObject *res = tup ? PyObject_CallObject(f, tup) : NULL;
    if (!res) {
        PyObject *t, *v, *tb;
        PyErr_Fetch(&t, &v, &tb);
        PyObject *s = v ? PyObject_Str(v) : NULL;
        const char *cs = s ? PyUnicode_AsUTF8(s) : "unknown error";
        snprintf(msg, sizeof msg, "python exception %s: %s", t ? ((PyTypeObject *)t)->tp_name : "?", cs ? cs : "?");
        fault("PY_TO_PA_OUT", worker, msg, args, nargs);
    }
    unbox(res, ret_tag, ret_elem, out, worker, args, nargs);
    Py_DECREF(res); Py_DECREF(tup); Py_DECREF(f); Py_DECREF(m);
    PyGILState_Release(g);
}
