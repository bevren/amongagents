"""Protected evaluator: prints `score: <mean cost>` on success.

Vehicle routing with capacities and time windows. Cost of a solution =
total distance + VEHICLE_COST per route.

Scores are deterministic: identical code gives identical scores on any machine,
under any load. The solver gets no clock (it budgets its search by its own work
count), `random` is seeded per instance, hash seeding is fixed, and worker
processes are blocked. The eval still enforces a CPU-time cap per instance.
"""

import importlib
import math
import os
import random
import sys
import time
import types

SEEDS = [101, 202, 303, 404, 505]
N = 200              # customers; index 0 is the depot
CAPACITY = 200
SERVICE = 10.0
HORIZON = 1000.0
VEHICLE_COST = 100.0
TIME_LIMIT = 3.0     # CPU seconds per instance
WALL_LIMIT = 9.0     # wall seconds per instance, only against hangs


def instance(seed):
    """Half the customers in a few clusters, half spread out; most have a narrow time window."""
    rng = random.Random(seed)
    depot = (50.0, 50.0)
    centers = [(rng.uniform(10, 90), rng.uniform(10, 90)) for _ in range(rng.randint(5, 8))]
    xy = [depot]
    for i in range(N):
        if i % 2 == 0:
            cx, cy = rng.choice(centers)
            p = (cx + rng.gauss(0, 6), cy + rng.gauss(0, 6))
        else:
            p = (rng.uniform(0, 100), rng.uniform(0, 100))
        xy.append((min(100.0, max(0.0, p[0])), min(100.0, max(0.0, p[1]))))
    demand, ready, due = [0], [0.0], [HORIZON]
    for i in range(1, N + 1):
        demand.append(rng.randint(5, 30))
        d0 = math.dist(depot, xy[i])
        latest = HORIZON - d0 - SERVICE  # last start of service that still gets back in time
        if rng.random() < 0.75:
            c, w = rng.uniform(d0, latest), rng.uniform(20, 120)
            ready.append(max(0.0, c - w / 2))
            due.append(min(latest, c + w / 2))
        else:
            ready.append(0.0)
            due.append(latest)
    return {"xy": xy, "demand": demand, "ready": ready, "due": due, "service": SERVICE,
            "capacity": CAPACITY, "horizon": HORIZON, "vehicle_cost": VEHICLE_COST}


def seeds():
    """The public instances, or 8 hidden ones with `--seed-base N` (the room's holdout check)."""
    if "--seed-base" in sys.argv:
        base = int(sys.argv[sys.argv.index("--seed-base") + 1])
        return [base + 7919 * i for i in range(8)]
    return SEEDS


def fix_hash_seed():
    """String hashing (so set and dict order of strings) varies per process unless PYTHONHASHSEED is
    fixed, and it can only be fixed at startup: rerun this eval in a child process with it set."""
    if os.environ.get("PYTHONHASHSEED") == "0":
        return
    import subprocess
    r = subprocess.run([sys.executable, os.path.abspath(__file__), *sys.argv[1:]],
                       env={**os.environ, "PYTHONHASHSEED": "0"})
    sys.exit(r.returncode)


def no_clock(*_, **__):
    raise RuntimeError("solver.py gets no clock in this task: budget the search by a work count "
                       "(iterations, moves tried), so identical code scores the same on any machine")


def hide_clocks():
    """A time budget makes the score depend on machine speed and load (the noise that froze the first
    long run), so the solver can't read any clock. The real `time` module stays bound in this file."""
    stub = types.ModuleType("time")
    stub.__getattr__ = lambda name: no_clock
    sys.modules["time"] = stub
    sys.modules["datetime"] = None
    os.times = no_clock


def block_workers():
    """CPU time of child processes isn't the solver's, so the solver gets no child processes."""
    for mod in ("multiprocessing", "subprocess", "concurrent.futures.process"):
        sys.modules[mod] = None


def load_solver():
    """Import the solver under the time limit too, so work can't hide in import-time code."""
    sys.path.insert(0, ".")
    block_workers()
    hide_clocks()
    t0 = time.process_time()
    solver = importlib.import_module("solver")
    dt = time.process_time() - t0
    if dt > TIME_LIMIT:
        sys.exit(f"importing solver.py took {dt:.2f}s of CPU, over the {TIME_LIMIT}s limit")
    return solver


def cost(inst, routes):
    """Check feasibility and return (distance, vehicles); raise ValueError on any violation."""
    xy, demand, ready, due = inst["xy"], inst["demand"], inst["ready"], inst["due"]
    seen = []
    distance = 0.0
    used = 0
    for r, route in enumerate(routes):
        route = list(route)
        if not route:
            continue
        used += 1
        if sum(demand[c] for c in route) > CAPACITY:
            raise ValueError(f"route {r} carries more than the capacity {CAPACITY}")
        t, prev = 0.0, 0
        for c in route:
            if not isinstance(c, int) or not 1 <= c <= N:
                raise ValueError(f"route {r} visits {c!r}, not a customer index 1..{N}")
            d = math.dist(xy[prev], xy[c])
            distance += d
            t = max(t + d, ready[c])
            if t > due[c] + 1e-9:
                raise ValueError(f"route {r} reaches customer {c} at {t:.3f}, after its window closes at {due[c]:.3f}")
            t += SERVICE
            prev = c
            seen.append(c)
        d = math.dist(xy[prev], xy[0])
        distance += d
        if t + d > HORIZON + 1e-9:
            raise ValueError(f"route {r} gets back to the depot at {t + d:.3f}, after {HORIZON:g}")
    if sorted(seen) != list(range(1, N + 1)):
        raise ValueError(f"every customer 1..{N} must be visited exactly once")
    return distance, used


def main():
    fix_hash_seed()
    solver = load_solver()
    costs = []
    for seed in seeds():
        inst = instance(seed)
        random.seed(seed)  # the solver's randomness is reproducible too
        t0, w0 = time.process_time(), time.perf_counter()
        routes = solver.solve({k: (list(v) if isinstance(v, list) else v) for k, v in inst.items()})
        dt, wall = time.process_time() - t0, time.perf_counter() - w0
        if dt > TIME_LIMIT:
            sys.exit(f"seed {seed}: took {dt:.2f}s of CPU, over the {TIME_LIMIT}s limit")
        if wall > WALL_LIMIT:
            sys.exit(f"seed {seed}: took {wall:.2f}s of wall time, over the {WALL_LIMIT}s limit")
        try:
            distance, vehicles = cost(inst, routes)
        except (TypeError, ValueError) as e:
            sys.exit(f"seed {seed}: invalid solution: {e}")
        total = distance + VEHICLE_COST * vehicles
        costs.append(total)
        print(f"seed {seed}: cost {total:.3f} = distance {distance:.3f} + {vehicles} vehicles in {dt:.2f}s CPU ({wall:.2f}s wall)")
    print(f"score: {sum(costs) / len(costs):.6f}")


if __name__ == "__main__":
    main()
