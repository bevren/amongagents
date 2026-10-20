"""Bilinear matrix multiplication algorithms. Edit me.

`decomposition()` returns a list of algorithms. Each is a dict with the shape <n,m,p>
(A is n x m, B is m x p) and r terms:

    M_t     = (sum_ij U[t][i][j] * A[i][j]) * (sum_jk V[t][j][k] * B[j][k])
    C[i][k] = sum_t W[t][i][k] * M_t

Coefficients must be exact: int, fractions.Fraction, a string like "-1/2", or a pair
(re, im) of those for a complex coefficient. The score is the total rank over the eval's
ladder of shapes; a shape you don't submit counts as schoolbook.

This starter submits the schoolbook 2 x 2 algorithm: one product per A[i][j] * B[j][k].
"""


def schoolbook(n, m, p):
    U, V, W = [], [], []
    for i in range(n):
        for j in range(m):
            for k in range(p):
                U.append([[int((a, b) == (i, j)) for b in range(m)] for a in range(n)])
                V.append([[int((a, b) == (j, k)) for b in range(p)] for a in range(m)])
                W.append([[int((a, b) == (i, k)) for b in range(p)] for a in range(n)])
    return {"n": n, "m": m, "p": p, "U": U, "V": V, "W": W}


def decomposition():
    return [schoolbook(2, 2, 2)]
