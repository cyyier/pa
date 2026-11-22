/* Pa core runtime (no Python): fault raising, guarded entry points for shared libraries, print helpers.
 *
 * Executables : a fault writes a JSON report, prints a summary and exit(70).
 * Shared libs : exported functions are wrapped in a guard (setjmp); a fault records the error in
 *               thread-local storage and longjmps back, so the host process survives. The host
 *               reads pa_error_code() / pa_error_message() / pa_error_json() after each call.
 */
#define _CRT_SECURE_NO_WARNINGS
#include <math.h>
#include <setjmp.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#ifndef PA_MODE
#if defined(PA_MODE_PROD) && PA_MODE_PROD
#define PA_MODE "prod"
#else
#define PA_MODE "dev"
#endif
#endif

#ifdef _MSC_VER
#define PA_TLS __declspec(thread)
#define PA_STRDUP _strdup
#else
#define PA_TLS __thread
#define PA_STRDUP strdup
#endif

#ifdef _WIN32
#define PA_EXPORT __declspec(dllexport)
#else
#define PA_EXPORT __attribute__((visibility("default")))
#endif

static PA_TLS jmp_buf *g_guard = NULL;
static PA_TLS int g_err_code = 0;
static PA_TLS char g_err_msg[512];
static PA_TLS char *g_err_json = NULL;
static void (*g_fault_hook)(void) = NULL;   /* e.g. release the GIL before unwinding */

void pa_set_fault_hook(void (*hook)(void)) { g_fault_hook = hook; }

void *pa_guard_enter(void *jb) {
    void *prev = g_guard;
    g_guard = (jmp_buf *)jb;
    g_err_code = 0;
    return prev;
}
void pa_guard_leave(void *prev) { g_guard = (jmp_buf *)prev; }

PA_EXPORT int pa_error_code(void) { return g_err_code; }
PA_EXPORT const char *pa_error_message(void) { return g_err_msg; }
PA_EXPORT const char *pa_error_json(void) { return g_err_json ? g_err_json : ""; }
PA_EXPORT void pa_error_clear(void) { g_err_code = 0; g_err_msg[0] = 0; free(g_err_json); g_err_json = NULL; }

void pa_fault_raise(const char *file_default, const char *summary, const char *json) {
    if (g_fault_hook) g_fault_hook();
    if (g_guard) {
        g_err_code = 70;
        snprintf(g_err_msg, sizeof g_err_msg, "%s", summary);
        free(g_err_json);
        g_err_json = PA_STRDUP(json);
        jmp_buf *g = g_guard;
        g_guard = NULL;
        longjmp(*g, 1);
    }
    const char *path = getenv("PA_FAULT_PATH");
    FILE *f = fopen(path ? path : file_default, "w");
    if (f) { fputs(json, f); fclose(f); }
    fprintf(stderr, "[pa fault] %s (see %s)\n", summary, path ? path : file_default);
    fflush(NULL);
    exit(70);
}

static void json_str(char *dst, size_t cap, const char *s) {
    size_t n = 0;
    for (; *s && n + 8 < cap; s++) {
        unsigned char c = (unsigned char)*s;
        if (c == '"' || c == '\\') { dst[n++] = '\\'; dst[n++] = c; }
        else if (c < 0x20) n += snprintf(dst + n, cap - n, "\\u%04x", c);
        else dst[n++] = c;
    }
    dst[n] = 0;
}

/* Runtime faults raised by generated code: ZeroDivisionError, IndexError, ValueError, OverflowError. */
void pa_rt_fault(const char *kind, const char *detail, int64_t line, const char *func) {
    char d[400], f[200], json[900], summary[600];
    json_str(d, sizeof d, detail);
    json_str(f, sizeof f, func);
    snprintf(json, sizeof json,
             "{\n  \"mode\": \"%s\",\n  \"kind\": \"%s\",\n  \"detail\": \"%s\",\n  \"function\": \"%s\",\n  \"line\": %lld\n}\n",
             PA_MODE, kind, d, f, (long long)line);
    snprintf(summary, sizeof summary, "%s: %s (in %s, line %lld)", kind, detail, func, (long long)line);
    pa_fault_raise("pa_runtime_fault.json", summary, json);
}

void pa_rt_index_fault(int64_t idx, int64_t len, int64_t line, const char *func) {
    char d[160];
    snprintf(d, sizeof d, "index %lld out of range for length %lld", (long long)idx, (long long)len);
    pa_rt_fault("IndexError", d, line, func);
}

/* ---- print helpers: Python-compatible formatting ------------------------------------------ */
void pa_print_i64(int64_t v) { printf("%lld", (long long)v); }
void pa_print_u64(uint64_t v) { printf("%llu", (unsigned long long)v); }
void pa_print_bool(int v) { fputs(v ? "True" : "False", stdout); }
void pa_print_str(const char *p, int64_t n) { if (n > 0) fwrite(p, 1, (size_t)n, stdout); }
void pa_print_sep(void) { putchar(' '); }
void pa_print_nl(void) { putchar('\n'); }

/* repr(float): shortest round-trip digits, Python's fixed/exponent switch, always a '.0' or exponent. */
void pa_format_f64(double v, char *out, size_t cap) {
    if (isnan(v)) { snprintf(out, cap, "nan"); return; }
    if (isinf(v)) { snprintf(out, cap, v > 0 ? "inf" : "-inf"); return; }
    char buf[48];
    int prec;
    for (prec = 1; prec <= 17; prec++) {
        snprintf(buf, sizeof buf, "%.*e", prec - 1, v);
        if (strtod(buf, NULL) == v) break;
    }
    const char *p = buf;
    int neg = 0;
    if (*p == '-') { neg = 1; p++; }
    char digits[24];
    int nd = 0;
    for (; *p && *p != 'e'; p++) if (*p != '.') digits[nd++] = *p;
    digits[nd] = 0;
    int exp10 = atoi(p + 1);
    while (nd > 1 && digits[nd - 1] == '0') digits[--nd] = 0;
    int decpt = exp10 + 1;
    char tmp[64];
    int n = 0;
    if (neg) tmp[n++] = '-';
    if (decpt <= -4 || decpt > 16) {
        tmp[n++] = digits[0];
        if (nd > 1) { tmp[n++] = '.'; memcpy(tmp + n, digits + 1, nd - 1); n += nd - 1; }
        n += snprintf(tmp + n, sizeof tmp - n, "e%c%02d", exp10 < 0 ? '-' : '+', abs(exp10));
    } else if (decpt <= 0) {
        n += snprintf(tmp + n, sizeof tmp - n, "0.");
        for (int i = 0; i < -decpt; i++) tmp[n++] = '0';
        memcpy(tmp + n, digits, nd); n += nd;
    } else if (decpt >= nd) {
        memcpy(tmp + n, digits, nd); n += nd;
        for (int i = 0; i < decpt - nd; i++) tmp[n++] = '0';
        tmp[n++] = '.'; tmp[n++] = '0';
    } else {
        memcpy(tmp + n, digits, decpt); n += decpt;
        tmp[n++] = '.';
        memcpy(tmp + n, digits + decpt, nd - decpt); n += nd - decpt;
    }
    tmp[n] = 0;
    snprintf(out, cap, "%s", tmp);
}
void pa_print_f64(double v) { char b[64]; pa_format_f64(v, b, sizeof b); fputs(b, stdout); }
