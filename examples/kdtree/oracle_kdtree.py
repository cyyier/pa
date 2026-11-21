"""Pure-Python 2D KD-Tree reference implementation (same algorithm as main.pa)."""


def _partition(nodes, lo, hi, pivot_idx, axis):
    pval = nodes[pivot_idx][axis]
    nodes[pivot_idx], nodes[hi] = nodes[hi], nodes[pivot_idx]
    store = lo
    for i in range(lo, hi):
        if nodes[i][axis] < pval:
            nodes[store], nodes[i] = nodes[i], nodes[store]
            store += 1
    nodes[store], nodes[hi] = nodes[hi], nodes[store]
    return store


def _quickselect(nodes, lo, hi, k, axis):
    while lo < hi:
        mid = (lo + hi) >> 1
        p = _partition(nodes, lo, hi, mid, axis)
        if p == k:
            return
        if k < p:
            hi = p - 1
        else:
            lo = p + 1


def build_kd_py(px, py):
    n = len(px)
    # [x, y, orig_idx, left, right, axis]
    nodes = [[px[i], py[i], i, -1, -1, 0] for i in range(n)]

    def rec(lo, hi, depth):
        if lo > hi:
            return -1
        axis = depth & 1
        mid = (lo + hi) >> 1
        _quickselect(nodes, lo, hi, mid, axis)
        lc = rec(lo, mid - 1, depth + 1)
        rc = rec(mid + 1, hi, depth + 1)
        nd = nodes[mid]
        nd[3], nd[4], nd[5] = lc, rc, axis
        return mid

    root = rec(0, n - 1, 0)
    return nodes, root


def batch_query_py(nodes, root, qx, qy):
    nq = len(qx)
    out_idx = [0] * nq
    out_d2 = [0.0] * nq
    stack = [0] * 128
    for i in range(nq):
        x, y = qx[i], qy[i]
        best_idx = -1
        best_d2 = 1e308
        stack[0] = root
        sp = 1
        while sp > 0:
            sp -= 1
            nd = nodes[stack[sp]]
            dx = nd[0] - x
            dy = nd[1] - y
            d2 = dx * dx + dy * dy
            if d2 < best_d2:
                best_d2 = d2
                best_idx = nd[2]
            diff = x - nd[0] if nd[5] == 0 else y - nd[1]
            near = nd[3] if diff < 0.0 else nd[4]
            far = nd[4] if diff < 0.0 else nd[3]
            if far != -1 and diff * diff < best_d2:
                stack[sp] = far
                sp += 1
            if near != -1:
                stack[sp] = near
                sp += 1
        out_idx[i] = best_idx
        out_d2[i] = best_d2
    return out_idx, out_d2
