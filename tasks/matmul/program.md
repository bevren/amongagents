# Matrix multiplication exponent

This task comes from the `MatrixMultiplication` Lean challenge in
[openai/math](https://github.com/openai/math/tree/main/lean/ComparatorChallenges).
That challenge defines matrix multiplication algorithms as straight-line programs of
`+`, `-`, `*` gates over ℂ, the cost of an algorithm as its number of gates, and `omega ℂ`
as the smallest τ such that n x n matrices can be multiplied with C·n^(τ+ε) gates.

Here you bound `omega` from above with finite constructions.

## The score

Edit `decomposition.py`. It returns a **bilinear algorithm** for multiplying an n x m
matrix A by an m x p matrix B with r multiplications:

    M_t     = (sum_ij U[t][i][j] * A[i][j]) * (sum_jk V[t][j][k] * B[j][k])     t = 0 .. r-1
    C[i][k] = sum_t W[t][i][k] * M_t

Equivalently: r rank-one tensors that sum to the matrix multiplication tensor <n,m,p>.
Such an algorithm proves

    omega <= 3 ln r / ln(n m p)

(apply it recursively to blocks, after symmetrizing the shape: <n,m,p>, <m,p,n> and <p,n,m>
together give an <nmp,nmp,nmp> algorithm of rank r^3). That number is your score, and lower
is better. The schoolbook 2 x 2 algorithm (r = 8) scores 3.

Rules of the eval (`eval.py`, protected):

- Coefficients are exact: `int`, `fractions.Fraction`, a string like `"-1/2"`, or a pair
  `(re, im)` of those for a complex number. Floats are rejected. Numerators and denominators
  are at most 2^20.
- n·m·p ≤ 512, and the rank is at most n·m·p.
- Correctness is checked as an exact polynomial identity, modulo 4 primes the eval draws at
  random every run. A decomposition that's wrong anywhere fails.
- `decomposition()` gets 60 s of CPU. It can build its answer (for example as a product of
  smaller algorithms), but long searches belong in your own scripts: save what they find
  into `decomposition.py` as data or as short code.

## Ways in

- Any shape counts, square or not: compare 3 ln r / ln(nmp) across shapes before investing
  in one.
- Products: algorithms for <n1,m1,p1> of rank r1 and <n2,m2,p2> of rank r2 give
  <n1n2, m1m2, p1p2> of rank r1·r2 (Kronecker products of U, V, W). This never beats its
  factors' best score, but it's a way to reach a shape for further search.
- Numerical search: alternating least squares or gradient descent on U, V, W for a target
  rank, then rounding to exact values (small integers, halves) and repairing. Regularize
  toward sparse, discrete solutions.
- Discrete search: random walks over decompositions of one rank (flip moves that keep the
  sum exact), with reductions that lower the rank when two terms can merge.
- Symmetry: decompositions invariant under a group (cyclic shifts of <n,n,n>, transposes)
  have far fewer unknowns.
- Over ℂ, complex coefficients are allowed, and some ranks only exist with them.

Report the shapes and ranks you've tried, including searches that found nothing at a given
rank: a known-hard rank is a useful clue for the crew.
