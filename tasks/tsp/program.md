# TSP heuristics research

Goal: minimize the mean tour length that `eval.py` reports over its fixed set of
random Euclidean TSP instances (in the unit square).

- Edit `solver.py` only. `solve(points)` gets a list of `(x, y)` tuples and must
  return a permutation of `range(len(points))`.
- Each instance has a **2.0 s CPU-time** limit, measured with `time.process_time()`
  (budget your search with that, not wall-clock time). If you go over, or
  return an invalid tour, the run fails. CPU time doesn't depend on what else
  runs on the machine, so scores are comparable between players and runs.
- `random` is seeded per instance before `solve` is called, so identical code
  gives the same tour. To test robustness, vary the seed yourself
  (`random.seed(...)` inside `solve`), but the score uses the default seeding.
- Standard library only (no numpy, no external solvers), in one process:
  `multiprocessing` and `subprocess` are blocked.
- `eval.py` is protected and restored before every run, so don't bother editing it.

Starting point: nearest neighbor. Ideas others might be exploring: 2-opt,
Or-opt, neighbor lists, don't-look bits, greedy-edge or space-filling-curve
construction, time-aware local search, random restarts.

In meetings, be specific: what changed, how much it helped, how close it ran to
the time limit, and whether it looks robust or lucky.
