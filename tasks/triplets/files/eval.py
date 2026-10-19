"""Protected evaluator: prints `score: <mean bins used>` on success (lower is better)."""

import importlib
import random
import sys
import time

SEEDS = [7, 21, 42, 77, 99]
BINS = 120
CAPACITY = 1000
TIME_LIMIT = 1.0


def instance(seed):
    rng = random.Random(seed)
    items = []
    while len(items) < 3 * BINS:
        a = rng.randint(380, 490)
        b = rng.randint(250, min(500, CAPACITY - a - 250))
        c = CAPACITY - a - b
        if 250 <= c <= 500:
            items += [a, b, c]
    rng.shuffle(items)
    return items


def seeds():
    """The public instances, or 5 hidden ones with `--seed-base N` (the room's holdout check)."""
    if "--seed-base" in sys.argv:
        base = int(sys.argv[sys.argv.index("--seed-base") + 1])
        return [base + 7919 * i for i in range(5)]
    return SEEDS


def load_solver():
    """Import the solver under the time limit too, so work can't hide in import-time code."""
    sys.path.insert(0, ".")
    t0 = time.perf_counter()
    solver = importlib.import_module("solver")
    dt = time.perf_counter() - t0
    if dt > TIME_LIMIT:
        sys.exit(f"importing solver.py took {dt:.2f}s, over the {TIME_LIMIT}s limit")
    return solver


def main():
    solver = load_solver()
    used = []
    for seed in seeds():
        items = instance(seed)
        t0 = time.perf_counter()
        bins = solver.pack(list(items), CAPACITY)
        dt = time.perf_counter() - t0
        flat = sorted(i for b in bins for i in b)
        if flat != list(range(len(items))):
            sys.exit(f"seed {seed}: every item index must be packed exactly once")
        if any(sum(items[i] for i in b) > CAPACITY for b in bins):
            sys.exit(f"seed {seed}: a bin is over capacity")
        if dt > TIME_LIMIT:
            sys.exit(f"seed {seed}: took {dt:.2f}s, over the {TIME_LIMIT}s limit")
        used.append(len(bins))
        print(f"seed {seed}: {len(bins)} bins in {dt:.3f}s")
    print(f"score: {sum(used) / len(used):.2f}")


if __name__ == "__main__":
    main()
