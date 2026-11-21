N = 50000000
MOD = 2147483648


def lcg_sum(n):
    x = 12345
    s = 0
    for i in range(n):
        x = (x * 1103515245 + 12345) % MOD
        s += x % 1000
    return s


def leibniz(n):
    s = 0.0
    sign = 1.0
    for i in range(n):
        s = s + sign / (2 * i + 1)
        sign = -sign
    return 4.0 * s


if __name__ == "__main__":
    print(lcg_sum(N))
    print("%.10g" % leibniz(N))
