// Reference C++ Ray Tracing in One Weekend (vec3 + virtual-free/flat-comparable scene) for g++ -O3 benchmark.
#include <chrono>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <fstream>
#include <vector>

struct Vec3 {
    double x, y, z;
    Vec3 operator+(const Vec3& b) const { return {x + b.x, y + b.y, z + b.z}; }
    Vec3 operator-(const Vec3& b) const { return {x - b.x, y - b.y, z - b.z}; }
    Vec3 operator*(double s) const { return {x * s, y * s, z * s}; }
    Vec3 operator*(const Vec3& b) const { return {x * b.x, y * b.y, z * b.z}; }
};

static inline double dot(const Vec3& a, const Vec3& b) { return a.x * b.x + a.y * b.y + a.z * b.z; }
static inline double len2(const Vec3& a) { return dot(a, a); }
static inline Vec3 unit(const Vec3& a) { double l = std::sqrt(len2(a)); return a * (1.0 / l); }
static inline Vec3 cross(const Vec3& a, const Vec3& b) {
    return {a.y * b.z - a.z * b.y, a.z * b.x - a.x * b.z, a.x * b.y - a.y * b.x};
}
static inline Vec3 reflect(const Vec3& v, const Vec3& n) { return v - n * (2.0 * dot(v, n)); }
static inline Vec3 refract(const Vec3& uv, const Vec3& n, double etai_over_etat) {
    double cos_t = std::fmin(dot(uv * -1.0, n), 1.0);
    Vec3 r_perp = (uv + n * cos_t) * etai_over_etat;
    double k = std::fabs(1.0 - len2(r_perp));
    Vec3 r_par = n * (-std::sqrt(k));
    return r_perp + r_par;
}
static inline double schlick(double cosine, double ref_idx) {
    double r0 = (1.0 - ref_idx) / (1.0 + ref_idx);
    r0 = r0 * r0;
    double m = 1.0 - cosine;
    return r0 + (1.0 - r0) * m * m * m * m * m;
}

struct Rng {
    uint64_t s;
    double next01() {
        uint64_t x = s;
        x ^= x >> 12;
        x ^= x << 25;
        x ^= x >> 27;
        s = x;
        uint64_t v = x * 2685821657736338717ULL;
        return static_cast<double>(v >> 11) * (1.0 / 9007199254740992.0);
    }
    Vec3 rand_in_unit_sphere() {
        while (true) {
            Vec3 p{next01() * 2.0 - 1.0, next01() * 2.0 - 1.0, next01() * 2.0 - 1.0};
            if (len2(p) < 1.0) return p;
        }
    }
};

struct Sphere {
    Vec3 center;
    double radius;
    int mat;       // 0=lambertian, 1=metal, 2=dielectric
    Vec3 albedo;
    double param;  // fuzz or ior
};

static Vec3 trace_ray(Vec3 ro, Vec3 rd, const std::vector<Sphere>& world, Rng& rng, int max_depth) {
    Vec3 atten{1.0, 1.0, 1.0};
    for (int depth = 0; depth < max_depth; ++depth) {
        double closest = 1e30;
        int hit_idx = -1;
        for (size_t i = 0; i < world.size(); ++i) {
            Vec3 oc = ro - world[i].center;
            double a = len2(rd);
            double hb = dot(oc, rd);
            double c = len2(oc) - world[i].radius * world[i].radius;
            double disc = hb * hb - a * c;
            if (disc > 0.0) {
                double sq = std::sqrt(disc);
                double root = (-hb - sq) / a;
                if (root < 0.001 || root >= closest) root = (-hb + sq) / a;
                if (root >= 0.001 && root < closest) {
                    closest = root;
                    hit_idx = static_cast<int>(i);
                }
            }
        }
        if (hit_idx < 0) {
            Vec3 ud = unit(rd);
            double t = 0.5 * (ud.y + 1.0);
            Vec3 sky = Vec3{1.0, 1.0, 1.0} * (1.0 - t) + Vec3{0.45, 0.70, 1.0} * t;
            return atten * sky;
        }
        const Sphere& sp = world[hit_idx];
        Vec3 p = ro + rd * closest;
        Vec3 out_n = (p - sp.center) * (1.0 / sp.radius);
        bool front = dot(rd, out_n) < 0.0;
        Vec3 n = front ? out_n : out_n * -1.0;

        if (sp.mat == 0) {
            Vec3 target = n + unit(rng.rand_in_unit_sphere());
            if (len2(target) < 1e-12) target = n;
            ro = p;
            rd = target;
            atten = atten * sp.albedo;
        } else if (sp.mat == 1) {
            Vec3 refl = reflect(unit(rd), n) + rng.rand_in_unit_sphere() * sp.param;
            if (dot(refl, n) <= 0.0) return {0.0, 0.0, 0.0};
            ro = p;
            rd = refl;
            atten = atten * sp.albedo;
        } else {
            double ratio = front ? (1.0 / sp.param) : sp.param;
            Vec3 ud = unit(rd);
            double cos_t = std::fmin(dot(ud * -1.0, n), 1.0);
            double sin_t = std::sqrt(std::fmax(0.0, 1.0 - cos_t * cos_t));
            bool cannot = ratio * sin_t > 1.0;
            Vec3 dir = (cannot || schlick(cos_t, ratio) > rng.next01())
                           ? reflect(ud, n)
                           : refract(ud, n, ratio);
            ro = p;
            rd = dir;
        }
    }
    return {0.0, 0.0, 0.0};
}

int main() {
    const int W = 400, H = 225, SPP = 24, MAX_DEPTH = 8;
    std::vector<Sphere> world = {
        {{0.0, -1000.0, 0.0}, 1000.0, 0, {0.42, 0.48, 0.44}, 0.0},
        {{0.0, 1.0, 0.0}, 1.0, 2, {1.0, 1.0, 1.0}, 1.5},
        {{-2.3, 1.0, -0.4}, 1.0, 0, {0.82, 0.28, 0.24}, 0.0},
        {{2.3, 1.0, -0.2}, 1.0, 1, {0.85, 0.72, 0.42}, 0.06},
        {{-0.9, 0.35, 1.7}, 0.35, 1, {0.80, 0.85, 0.92}, 0.02},
        {{0.95, 0.35, 1.65}, 0.35, 0, {0.22, 0.56, 0.82}, 0.0},
        {{0.0, 0.28, 2.25}, 0.28, 0, {0.92, 0.72, 0.22}, 0.0},
        {{-1.7, 0.30, 1.3}, 0.30, 2, {1.0, 1.0, 1.0}, 1.5},
        {{1.8, 0.30, 1.25}, 0.30, 0, {0.32, 0.72, 0.48}, 0.0},
        {{-3.3, 0.45, 0.9}, 0.45, 1, {0.75, 0.45, 0.65}, 0.18},
        {{3.4, 0.45, 0.8}, 0.45, 0, {0.25, 0.35, 0.68}, 0.0},
        {{-1.1, 0.40, -1.9}, 0.40, 1, {0.90, 0.90, 0.90}, 0.0},
        {{1.2, 0.40, -1.8}, 0.40, 0, {0.88, 0.45, 0.18}, 0.0},
    };

    Vec3 lookfrom{0.0, 1.85, 6.2}, lookat{0.0, 0.85, 0.0}, vup{0.0, 1.0, 0.0};
    double fov_rad = 34.0 * 3.141592653589793 / 180.0;
    double vh = 2.0 * std::tan(fov_rad * 0.5);
    double vw = vh * (static_cast<double>(W) / H);
    Vec3 cw = unit(lookfrom - lookat);
    Vec3 cu = unit(cross(vup, cw));
    Vec3 cv = cross(cw, cu);
    Vec3 horiz = cu * vw;
    Vec3 vert = cv * vh;
    Vec3 lower_left = lookfrom - horiz * 0.5 - vert * 0.5 - cw;

    std::vector<uint8_t> rgb(W * H * 3);
    Rng rng{12345678901234567ULL};

    auto t0 = std::chrono::high_resolution_clock::now();
    for (int j = 0; j < H; ++j) {
        for (int i = 0; i < W; ++i) {
            Vec3 col{0.0, 0.0, 0.0};
            for (int s = 0; s < SPP; ++s) {
                double u = (i + rng.next01()) / (W - 1);
                double v = ((H - 1 - j) + rng.next01()) / (H - 1);
                Vec3 rd = lower_left + horiz * u + vert * v - lookfrom;
                col = col + trace_ray(lookfrom, rd, world, rng, MAX_DEPTH);
            }
            double inv = 1.0 / SPP;
            double r = std::sqrt(col.x * inv);
            double g = std::sqrt(col.y * inv);
            double b = std::sqrt(col.z * inv);
            auto to_u8 = [](double x) -> uint8_t {
                int v = static_cast<int>(255.999 * std::fmax(0.0, std::fmin(0.9999, x)));
                return static_cast<uint8_t>(v);
            };
            int off = (j * W + i) * 3;
            rgb[off] = to_u8(r);
            rgb[off + 1] = to_u8(g);
            rgb[off + 2] = to_u8(b);
        }
    }
    auto t1 = std::chrono::high_resolution_clock::now();
    double ms = std::chrono::duration<double, std::milli>(t1 - t0).count();

    std::ofstream f("/tmp/ref_rt.rgb", std::ios::binary);
    f.write(reinterpret_cast<const char*>(rgb.data()), rgb.size());
    std::printf("%.4f\n", ms);
    return 0;
}
