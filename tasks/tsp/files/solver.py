"""TSP solver. Edit me. `solve(points)` returns a permutation of point indices."""

import math


def solve(points):
    n = len(points)
    unvisited = set(range(1, n))
    tour = [0]
    while unvisited:
        x, y = points[tour[-1]]
        nxt = min(unvisited, key=lambda j: math.hypot(points[j][0] - x, points[j][1] - y))
        unvisited.remove(nxt)
        tour.append(nxt)
    return tour
