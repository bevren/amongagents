"""A bilinear matrix multiplication algorithm. Edit me.

`decomposition()` returns the shape <n,m,p> (A is n x m, B is m x p) and r terms:

    M_t     = (sum_ij U[t][i][j] * A[i][j]) * (sum_jk V[t][j][k] * B[j][k])
    C[i][k] = sum_t W[t][i][k] * M_t

Coefficients must be exact: int, fractions.Fraction, a string like "-1/2", or a pair
(re, im) of those for a complex coefficient. The score is 3 ln r / ln(n m p).

This starter is the schoolbook algorithm for 2 x 2 times 2 x 2: one product per A[i][j] * B[j][k].
"""


def decomposition():
    n = m = p = 2
    U, V, W = [], [], []
    for i in range(n):
        for j in range(m):
            for k in range(p):
                U.append([[int((a, b) == (i, j)) for b in range(m)] for a in range(n)])
                V.append([[int((a, b) == (j, k)) for b in range(p)] for a in range(m)])
                W.append([[int((a, b) == (i, k)) for b in range(p)] for a in range(n)])
    return {"n": n, "m": m, "p": p, "U": U, "V": V, "W": W}
