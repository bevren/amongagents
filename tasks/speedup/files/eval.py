"""Protected evaluator: prints `score: <speedup>` (higher is better) when target.py is correct.

1. Correctness: target and reference must give the same result on edge cases and random inputs.
2. Speed: both run on the same fresh benchmark inputs, interleaved, in this process. The score
   is the median of reference_time / target_time over the rounds. A ratio measured on one
   machine carries over to another far better than raw seconds, so players with different
   computers can compare scores. Each round uses a new input, so caching results doesn't help.

`--seed-base N` draws different inputs (the room's hidden-instance check), so code tuned to
the public inputs, or that hard-codes their answers, fails there.
"""

import copy
import gc
import importlib
import random
import statistics
import sys
import time

FUNCTION = "count_inversions"
BENCH_SIZE = 3000   # the reference takes a few tenths of a second on this
ROUNDS = 5
SEED = 1


def check_inputs(rng):
    """Edge cases first, then random inputs of mixed sizes."""
    cases = [[], [1], [2, 1], [1, 1, 1], list(range(50)), list(range(50, 0, -1)), [-3, 5, -3, 0, 5, -10]]
    for size in (10, 100, 400):
        cases.append([rng.randint(-1000, 1000) for _ in range(size)])
        cases.append([rng.randint(0, 5) for _ in range(size)])  # many duplicates
    return cases


def bench_input(rng):
    return [rng.randint(-10**6, 10**6) for _ in range(BENCH_SIZE)]


def timed(fn, arg):
    gc.collect()
    gc.disable()
    try:
        t0 = time.perf_counter()
        out = fn(arg)
        return out, time.perf_counter() - t0
    finally:
        gc.enable()


def main():
    seed = int(sys.argv[sys.argv.index("--seed-base") + 1]) if "--seed-base" in sys.argv else SEED
    rng = random.Random(seed)
    sys.path.insert(0, ".")
    reference = getattr(importlib.import_module("reference"), FUNCTION)
    target = getattr(importlib.import_module("target"), FUNCTION)

    for i, xs in enumerate(check_inputs(rng)):
        want, got = reference(copy.deepcopy(xs)), target(copy.deepcopy(xs))
        if got != want:
            sys.exit(f"wrong result on check input {i} (length {len(xs)}): got {got!r}, expected {want!r}")

    ratios = []
    for r in range(ROUNDS):
        xs = bench_input(rng)
        want, t_ref = timed(reference, copy.deepcopy(xs))
        got, t_new = timed(target, copy.deepcopy(xs))
        if got != want:
            sys.exit(f"wrong result on benchmark round {r}: got {got!r}, expected {want!r}")
        ratios.append(t_ref / max(t_new, 1e-9))
        print(f"round {r}: reference {t_ref * 1000:.2f} ms, target {t_new * 1000:.3f} ms, speedup {ratios[-1]:.2f}x")
    print(f"score: {statistics.median(ratios):.4f}")


if __name__ == "__main__":
    main()
