"""The original function: the correctness oracle and the timing baseline. Protected.

To use this task for your own function, paste it here unchanged, and paste the same code
into target.py as the starting point.
"""


def count_inversions(xs):
    """How many pairs i < j have xs[i] > xs[j]."""
    n = len(xs)
    count = 0
    for i in range(n):
        for j in range(i + 1, n):
            if xs[i] > xs[j]:
                count += 1
    return count
