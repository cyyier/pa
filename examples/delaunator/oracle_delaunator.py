"""Pure-Python reference implementation of Mapbox Delaunator (2D Delaunay triangulation)."""
import math

EPS = 1e-12


def _dist2(ax, ay, bx, by):
    dx = ax - bx
    dy = ay - by
    return dx * dx + dy * dy


def _orient2d(px, py, qx, qy, rx, ry):
    return (py - ry) * (qx - rx) - (px - rx) * (qy - ry)


def _circumradius2(ax, ay, bx, by, cx, cy):
    dx, dy = bx - ax, by - ay
    ex, ey = cx - ax, cy - ay
    bl = dx * dx + dy * dy
    cl = ex * ex + ey * ey
    d = 0.5 / (dx * ey - dy * ex)
    x = (ey * bl - dy * cl) * d
    y = (dx * cl - ex * bl) * d
    return x * x + y * y


def _circumcenter(ax, ay, bx, by, cx, cy):
    dx, dy = bx - ax, by - ay
    ex, ey = cx - ax, cy - ay
    bl = dx * dx + dy * dy
    cl = ex * ex + ey * ey
    d = 0.5 / (dx * ey - dy * ex)
    return ax + (ey * bl - dy * cl) * d, ay + (dx * cl - ex * bl) * d


def _in_circle(ax, ay, bx, by, cx, cy, px, py):
    dx, dy = ax - px, ay - py
    ex, ey = bx - px, by - py
    fx, fy = cx - px, cy - py
    ap = dx * dx + dy * dy
    bp = ex * ex + ey * ey
    cp = fx * fx + fy * fy
    return (dx * (ey * cp - bp * fy) - dy * (ex * cp - bp * fx) + ap * (ex * fy - ey * fx)) < 0.0


def _pseudo_angle(dx, dy):
    p = dx / (abs(dx) + abs(dy))
    return (3.0 - p) / 4.0 if dy > 0.0 else (1.0 + p) / 4.0


def delaunator_py(xs, ys):
    n = len(xs)
    min_x, max_x = min(xs), max(xs)
    min_y, max_y = min(ys), max(ys)
    cx, cy = (min_x + max_x) * 0.5, (min_y + max_y) * 0.5

    i0, min_d = 0, float("inf")
    for i in range(n):
        d = _dist2(xs[i], ys[i], cx, cy)
        if d < min_d:
            i0, min_d = i, d

    i1, min_d = 0, float("inf")
    x0, y0 = xs[i0], ys[i0]
    for i in range(n):
        if i == i0:
            continue
        d = _dist2(x0, y0, xs[i], ys[i])
        if d < min_d and d > 0.0:
            i1, min_d = i, d

    i2, min_r = 0, float("inf")
    x1, y1 = xs[i1], ys[i1]
    for i in range(n):
        if i == i0 or i == i1:
            continue
        r = _circumradius2(x0, y0, x1, y1, xs[i], ys[i])
        if r < min_r:
            i2, min_r = i, r

    if _orient2d(xs[i0], ys[i0], xs[i1], ys[i1], xs[i2], ys[i2]) < 0.0:
        i1, i2 = i2, i1

    cx, cy = _circumcenter(xs[i0], ys[i0], xs[i1], ys[i1], xs[i2], ys[i2])
    dists = [_dist2(xs[i], ys[i], cx, cy) for i in range(n)]
    ids = sorted(range(n), key=dists.__getitem__)

    hash_size = math.ceil(math.sqrt(n))
    hull_prev = [0] * n
    hull_next = [0] * n
    hull_tri = [0] * n
    hull_hash = [-1] * hash_size

    def hash_key(x, y):
        return math.floor(_pseudo_angle(x - cx, y - cy) * hash_size) % hash_size

    hull_next[i0] = hull_prev[i2] = i1
    hull_next[i1] = hull_prev[i0] = i2
    hull_next[i2] = hull_prev[i1] = i0
    hull_tri[i0] = 0
    hull_tri[i1] = 1
    hull_tri[i2] = 2
    hull_hash[hash_key(xs[i0], ys[i0])] = i0
    hull_hash[hash_key(xs[i1], ys[i1])] = i1
    hull_hash[hash_key(xs[i2], ys[i2])] = i2

    triangles = []
    halfedges = []

    def link(a, b):
        halfedges[a] = b
        if b != -1:
            halfedges[b] = a

    def add_triangle(a0, a1, a2, e0, e1, e2):
        t = len(triangles)
        triangles.extend((a0, a1, a2))
        halfedges.extend((-1, -1, -1))
        link(t, e0)
        link(t + 1, e1)
        link(t + 2, e2)
        return t

    edge_stack = [0] * 512

    def legalize(a):
        sp = 0
        ar = 0
        while True:
            b = halfedges[a]
            a0 = a - a % 3
            ar = a0 + (a + 2) % 3
            if b == -1:
                if sp == 0:
                    break
                sp -= 1
                a = edge_stack[sp]
                continue
            b0 = b - b % 3
            al = a0 + (a + 1) % 3
            bl = b0 + (b + 2) % 3
            p0 = triangles[ar]
            pr = triangles[a]
            pl = triangles[al]
            p1 = triangles[bl]
            if _in_circle(xs[p0], ys[p0], xs[pr], ys[pr], xs[pl], ys[pl], xs[p1], ys[p1]):
                triangles[a] = p1
                triangles[b] = p0
                hbl = halfedges[bl]
                if hbl == -1:
                    hull_tri[p1] = a
                link(a, hbl)
                link(b, halfedges[ar])
                link(ar, bl)
                br = b0 + (b + 1) % 3
                if sp < 512:
                    edge_stack[sp] = br
                    sp += 1
                continue
            if sp == 0:
                break
            sp -= 1
            a = edge_stack[sp]
        return ar

    add_triangle(i0, i1, i2, -1, -1, -1)
    xp = yp = 0.0
    for k in range(n):
        i = ids[k]
        x, y = xs[i], ys[i]
        if k > 0 and abs(x - xp) <= EPS and abs(y - yp) <= EPS:
            continue
        xp, yp = x, y
        if i == i0 or i == i1 or i == i2:
            continue

        key = hash_key(x, y)
        start = 0
        for j in range(hash_size):
            start = hull_hash[(key + j) % hash_size]
            if start != -1 and start != hull_next[start]:
                break

        start = hull_prev[start]
        e = start
        q = hull_next[e]
        while _orient2d(x, y, xs[e], ys[e], xs[q], ys[q]) >= 0.0:
            e = q
            if e == start:
                e = -1
                break
            q = hull_next[e]
        if e == -1:
            continue

        t = add_triangle(e, i, hull_next[e], -1, -1, hull_tri[e])
        hull_tri[i] = legalize(t + 2)
        hull_tri[e] = t

        q = hull_next[e]
        n_idx = hull_next[q]
        while _orient2d(x, y, xs[q], ys[q], xs[n_idx], ys[n_idx]) < 0.0:
            t = add_triangle(q, i, n_idx, hull_tri[i], -1, hull_tri[q])
            hull_tri[i] = legalize(t + 2)
            hull_next[q] = q
            q = n_idx
            n_idx = hull_next[q]

        if e == start:
            q2 = hull_prev[e]
            while _orient2d(x, y, xs[q2], ys[q2], xs[e], ys[e]) < 0.0:
                t = add_triangle(q2, i, e, -1, hull_tri[e], hull_tri[q2])
                legalize(t + 2)
                hull_tri[q2] = t
                hull_next[e] = e
                e = q2
                q2 = hull_prev[e]

        hull_prev[i] = e
        hull_next[e] = i
        hull_prev[q] = i
        hull_next[i] = q
        hull_hash[key] = i
        hull_hash[hash_key(xs[e], ys[e])] = e

    return triangles, halfedges
