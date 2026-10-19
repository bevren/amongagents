"""Protected evaluator: prints `score: <mean bins used>` on success (lower is better)."""

import importlib
import math
import random
import sys
import time

SEEDS = [3, 14, 15, 92, 65]
N = 300
CAPACITY = 100
TIME_LIMIT = 1.0


def instance(seed):
    rng = random.Random(seed)
    # mostly medium items with some large ones: easy to pack badly, hard to pack perfectly
    return [rng.choice((rng.randint(20, 45), rng.randint(20, 45), rng.randint(46, 70))) for _ in range(N)]


def main():
    sys.path.insert(0, ".")
    solver = importlib.import_module("solver")
    used, bounds = [], []
    for seed in SEEDS:
        items = instance(seed)
        t0 = time.perf_counter()
        bins = solver.pack(list(items), CAPACITY)
        dt = time.perf_counter() - t0
        flat = sorted(i for b in bins for i in b)
        if flat != sorted(range(len(items))):
            sys.exit(f"seed {seed}: every item index must be packed exactly once")
        if any(sum(items[i] for i in b) > CAPACITY for b in bins):
            sys.exit(f"seed {seed}: a bin is over capacity")
        if dt > TIME_LIMIT:
            sys.exit(f"seed {seed}: took {dt:.2f}s, over the {TIME_LIMIT}s limit")
        lb = math.ceil(sum(items) / CAPACITY)
        used.append(len(bins))
        bounds.append(lb)
        print(f"seed {seed}: {len(bins)} bins (lower bound {lb}) in {dt:.3f}s")
    print(f"score: {sum(used) / len(used):.2f}")


if __name__ == "__main__":
    main()
