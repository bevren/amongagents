"""Vehicle routing solver. Edit me. `solve(inst)` returns a list of routes."""

import math


def solve(inst):
    xy, demand, ready, due = inst["xy"], inst["demand"], inst["ready"], inst["due"]
    service, capacity, horizon = inst["service"], inst["capacity"], inst["horizon"]
    left = set(range(1, len(xy)))
    routes = []
    while left:
        # one vehicle at a time: keep driving to the nearest customer it can still serve in time
        route, t, load, prev = [], 0.0, 0, 0
        while True:
            best, best_d = None, math.inf
            for c in left:
                d = math.dist(xy[prev], xy[c])
                arrive = max(t + d, ready[c])
                back = arrive + service + math.dist(xy[c], xy[0])
                if load + demand[c] <= capacity and arrive <= due[c] and back <= horizon and d < best_d:
                    best, best_d = c, d
            if best is None:
                break
            t = max(t + best_d, ready[best]) + service
            load += demand[best]
            route.append(best)
            left.remove(best)
            prev = best
        routes.append(route)
    return routes
