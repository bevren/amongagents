"""Protected evaluator: prints `score: <mean tour length>` on success.

The time limit is CPU time (time.process_time), not wall clock, so a run scores the same whether
or not other agents' evals share the machine, and `random` is seeded per instance, so identical
code gives (nearly) identical scores. Worker processes are blocked: their CPU time wouldn't count.
"""

import importlib
import math
import random
import sys
import time

SEEDS = [11, 23, 37, 41, 59]
N = 400
TIME_LIMIT = 2.0  # CPU seconds per instance
WALL_LIMIT = 6.0  # wall seconds per instance, only against hangs


def instance(seed):
    rng = random.Random(seed)
    return [(rng.random(), rng.random()) for _ in range(N)]


def seeds():
    """The public instances, or 10 hidden ones with `--seed-base N` (the room's holdout check)."""
    if "--seed-base" in sys.argv:
        base = int(sys.argv[sys.argv.index("--seed-base") + 1])
        return [base + 7919 * i for i in range(10)]
    return SEEDS


def block_workers():
    """CPU time of child processes isn't the solver's, so the solver gets no child processes."""
    for mod in ("multiprocessing", "subprocess", "concurrent.futures.process"):
        sys.modules[mod] = None


def load_solver():
    """Import the solver under the time limit too, so work can't hide in import-time code."""
    sys.path.insert(0, ".")
    block_workers()
    t0 = time.process_time()
    solver = importlib.import_module("solver")
    dt = time.process_time() - t0
    if dt > TIME_LIMIT:
        sys.exit(f"importing solver.py took {dt:.2f}s of CPU, over the {TIME_LIMIT}s limit")
    return solver


def tour_length(points, tour):
    return sum(math.dist(points[tour[i - 1]], points[tour[i]]) for i in range(len(tour)))


def main():
    solver = load_solver()
    lengths = []
    for seed in seeds():
        pts = instance(seed)
        random.seed(seed)  # the solver's randomness is reproducible too
        t0, w0 = time.process_time(), time.perf_counter()
        tour = list(solver.solve(list(pts)))
        dt, wall = time.process_time() - t0, time.perf_counter() - w0
        if sorted(tour) != list(range(N)):
            sys.exit(f"seed {seed}: invalid tour (not a permutation of 0..{N - 1})")
        if dt > TIME_LIMIT:
            sys.exit(f"seed {seed}: took {dt:.2f}s of CPU, over the {TIME_LIMIT}s limit")
        if wall > WALL_LIMIT:
            sys.exit(f"seed {seed}: took {wall:.2f}s of wall time, over the {WALL_LIMIT}s limit")
        length = tour_length(pts, tour)
        lengths.append(length)
        print(f"seed {seed}: length {length:.4f} in {dt:.2f}s CPU ({wall:.2f}s wall)")
    print(f"score: {sum(lengths) / len(lengths):.6f}")


if __name__ == "__main__":
    main()
