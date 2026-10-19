"""Make this faster. It must return exactly what reference.py returns, for every input."""


def count_inversions(xs):
    """How many pairs i < j have xs[i] > xs[j]."""
    n = len(xs)
    count = 0
    for i in range(n):
        for j in range(i + 1, n):
            if xs[i] > xs[j]:
                count += 1
    return count
