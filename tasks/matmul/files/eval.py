"""Protected evaluator: prints `score: <total rank>` on success.

decomposition.py describes bilinear algorithms. One multiplies an n x m matrix A by an m x p
matrix B with r multiplications:

    M_t    = (sum_ij U[t][i][j] * A[i][j]) * (sum_jk V[t][j][k] * B[j][k])     t = 0 .. r-1
    C[i][k] = sum_t W[t][i][k] * M_t

Such an algorithm (rank r for the shape <n,m,p>) proves omega <= 3 ln r / ln(n m p).

The score is the total rank over a fixed LADDER of shapes: each shape counts with the lowest
rank submitted for it (or any permutation of it: the rank is the same), and a shape with no
algorithm counts as the schoolbook n m p. One multiplication saved anywhere is one point.

Correctness is checked exactly as a polynomial identity: the sum of the r rank-one tensors
must equal the matrix multiplication tensor. The check runs modulo several primes the eval
draws at random each run (with i = sqrt(-1) mod p, so complex coefficients work), and
coefficients are bounded, so a wrong decomposition can't pass by luck or by design.
"""

import importlib
import math
import os
import secrets
import sys
import time
from fractions import Fraction

import numpy as np

LADDER = [(2, 2, 2), (2, 2, 3), (2, 3, 3), (2, 2, 4), (2, 3, 4), (3, 3, 3), (2, 4, 4),
          (3, 3, 4), (3, 4, 4), (4, 4, 4), (3, 3, 6), (4, 4, 5), (4, 5, 5), (5, 5, 5)]
MAX_VOLUME = 512          # n * m * p, so a check stays fast
MAX_COEFF = 2 ** 20       # numerators and denominators of every coefficient
PRIMES = 4                # random primes per check
CPU_LIMIT = 60.0          # seconds for decomposition() to build its answer


class Bad(Exception):
    pass


def parse(x):
    """An exact coefficient: int, Fraction, a string like "-1/2", or (re, im) of those."""
    if isinstance(x, tuple) and len(x) == 2:
        return parse(x[0])[0], parse(x[1])[0]
    if isinstance(x, bool) or isinstance(x, float) or isinstance(x, complex):
        raise Bad(f"coefficient {x!r}: use exact values (int, Fraction, \"a/b\", or (re, im)), not floats")
    try:
        q = Fraction(x)
    except (TypeError, ValueError):
        raise Bad(f"coefficient {x!r} isn't a number")
    if abs(q.numerator) > MAX_COEFF or q.denominator > MAX_COEFF:
        raise Bad(f"coefficient {x!r} is larger than {MAX_COEFF} in numerator or denominator")
    return q, Fraction(0)


def random_prime():
    """A random prime in [2^20, 2^21) with p = 1 mod 4, so -1 has a square root mod p."""
    while True:
        p = (1 << 20) + secrets.randbelow(1 << 20)
        p += (1 - p) % 4
        if p < (1 << 21) and all(p % d for d in range(3, int(p ** 0.5) + 1, 2)):
            return p


def sqrt_minus_one(p):
    for g in range(2, p):
        i = pow(g, (p - 1) // 4, p)
        if i * i % p == p - 1:
            return i


def to_mod(coeffs, p, i):
    out = np.empty(len(coeffs), dtype=np.int64)
    for k, (re, im) in enumerate(coeffs):
        if re.denominator % p == 0 or im.denominator % p == 0:
            raise ZeroDivisionError
        v = re.numerator * pow(re.denominator, -1, p) + i * im.numerator * pow(im.denominator, -1, p)
        out[k] = v % p
    return out


def check(n, m, p_, U, V, W):
    """U: r x (n*m), V: r x (m*p), W: r x (n*p), lists of parsed coefficients."""
    r = len(U)
    # target T[(i,j), (j2,k), (i2,k2)] = [i == i2][j == j2][k == k2]
    target = np.zeros((n * m, m * p_, n * p_), dtype=np.int64)
    for i in range(n):
        for j in range(m):
            for k in range(p_):
                target[i * m + j, j * p_ + k, i * p_ + k] = 1
    done = 0
    while done < PRIMES:
        q = random_prime()
        root = sqrt_minus_one(q)
        try:
            u = np.stack([to_mod(row, q, root) for row in U])
            v = np.stack([to_mod(row, q, root) for row in V])
            w = np.stack([to_mod(row, q, root) for row in W])
        except ZeroDivisionError:
            continue  # a denominator divisible by this prime: draw another
        uv = (u[:, :, None] * v[:, None, :]) % q                  # r x nm x mp, entries < 2^42
        total = (uv.reshape(r, -1).T @ w) % q                      # sums of r < 2^10 terms < 2^52
        diff = (total.reshape(target.shape) - target) % q
        if diff.any():
            a, b, c = (int(x) for x in np.argwhere(diff)[0])
            raise Bad(f"not a correct algorithm: the coefficient of A[{a // m}][{a % m}] * "
                      f"B[{b // p_}][{b % p_}] in C[{c // p_}][{c % p_}] is wrong")
        done += 1


def flatten(mats, rows, cols, name, t):
    if len(mats) != rows or any(len(row) != cols for row in mats):
        raise Bad(f"{name}[{t}] must be a {rows} x {cols} matrix")
    return [parse(x) for row in mats for x in row]


def load():
    """decomposition() may build its answer (products of smaller algorithms, a short search), but
    the search itself belongs in your own scripts: the eval only gives it CPU_LIMIT seconds."""
    sys.path.insert(0, os.getcwd())
    t0 = time.process_time()
    d = importlib.import_module("decomposition").decomposition()
    dt = time.process_time() - t0
    if dt > CPU_LIMIT:
        raise Bad(f"decomposition() took {dt:.1f}s of CPU, over the {CPU_LIMIT:.0f}s limit")
    return d


def verify(d):
    """Check one algorithm; returns (shape, rank)."""
    n, m, p_ = int(d["n"]), int(d["m"]), int(d["p"])
    U, V, W = d["U"], d["V"], d["W"]
    if min(n, m, p_) < 1 or n * m * p_ < 2:
        raise Bad("need n, m, p >= 1 and n * m * p >= 2")
    if n * m * p_ > MAX_VOLUME:
        raise Bad(f"n * m * p = {n * m * p_} is over the cap of {MAX_VOLUME}")
    r = len(U)
    if not (len(V) == len(W) == r) or r < 1:
        raise Bad("U, V and W need the same number r >= 1 of terms")
    if r > n * m * p_:
        raise Bad(f"rank {r} is above the schoolbook {n * m * p_}")
    U = [flatten(U[t], n, m, "U", t) for t in range(r)]
    V = [flatten(V[t], m, p_, "V", t) for t in range(r)]
    W = [flatten(W[t], n, p_, "W", t) for t in range(r)]
    check(n, m, p_, U, V, W)
    return (n, m, p_), r


def main():
    t0 = time.time()
    best = {shape: math.prod(shape) for shape in LADDER}
    found = {}
    try:
        algos = load()
        if isinstance(algos, dict):
            algos = [algos]
        for k, d in enumerate(algos):
            try:
                shape, r = verify(d)
            except Bad as e:
                raise Bad(f"algorithm #{k}: {e}")
            slot = tuple(sorted(shape))
            if slot not in best:
                print(f"note: algorithm #{k} is <{shape[0]},{shape[1]},{shape[2]}>, not a ladder shape "
                      "(it still counts for the omega bound below)")
            elif r < best[slot]:
                best[slot] = r
            if r < found.get(shape, (math.inf,))[0]:
                found[shape] = (r,)
    except Bad as e:
        print(f"invalid: {e}")
        sys.exit(1)
    for shape in LADDER:
        tag = "" if best[shape] < math.prod(shape) else "  (schoolbook)"
        print(f"  <{shape[0]},{shape[1]},{shape[2]}>  rank {best[shape]:4d}{tag}")
    omega = min([3 * math.log(r) / math.log(math.prod(sh)) for sh, (r,) in found.items()] + [3.0])
    print(f"total rank {sum(best.values())} (schoolbook {sum(math.prod(sh) for sh in LADDER)}), "
          f"best omega bound {omega:.6f}, {len(found)} shape(s) checked modulo {PRIMES} random primes "
          f"in {time.time() - t0:.1f}s")
    print(f"score: {sum(best.values())}")


if __name__ == "__main__":
    main()
