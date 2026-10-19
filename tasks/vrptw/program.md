# Vehicle routing with time windows

Goal: minimize the mean cost that `eval.py` reports over its fixed set of random
instances. Cost = total distance driven + 100 per vehicle used.

- Edit `solver.py` only. `solve(inst)` gets a dict and must return a list of
  routes, each a list of customer indices (1..200) in visiting order. Every
  customer must be visited exactly once.
- `inst` holds: `xy` (coordinates in a 100 x 100 square, index 0 is the depot),
  `demand`, `ready` and `due` (per index), `service` (10, the same for every
  customer), `capacity` (200 per vehicle), `horizon` (1000) and `vehicle_cost`.
- Rules of a route: it starts at the depot at time 0; driving takes as long as
  the Euclidean distance. Arriving before `ready[c]` means waiting until then.
  Service must start by `due[c]` and takes `service` time. The route must be back
  at the depot by `horizon`, and its demands must sum to at most `capacity`.
  Any violation fails the whole run, with the reason.
- About half the customers sit in a few clusters and half are spread out; about
  three in four have a narrow time window. Both distance and vehicle count matter.
- **Scores are deterministic: identical code gives the identical score**, on any
  machine and under any load. So a better score is a better solver, never luck,
  and one run is enough to judge a change.
  - `solver.py` gets **no clock**: `time`, `datetime` and `os.times` are
    unavailable. Budget the search by a work count instead (iterations, moves
    tried, ruin-and-recreate rounds).
  - `random` is seeded per instance before `solve` is called, and string hashing
    is fixed (`PYTHONHASHSEED=0`).
  - Each instance still has a **3.0 s CPU-time cap**, enforced by the eval. Over
    it, the run fails. Keep your work budget to about 2 s on your machine, so
    the same code also passes on a slower one.
- Standard library only (no numpy, no external solvers), in one process:
  `multiprocessing` and `subprocess` are blocked.
- `eval.py` is protected and restored before every run. Its `cost()` function is
  the feasibility checker, so read it if a run fails.

Starting point: one vehicle at a time, always driving to the nearest customer it
can still serve. Directions people take on problems like this: savings
(Clarke-Wright) or insertion construction (Solomon I1), relocate / exchange /
2-opt* / Or-opt moves between and within routes, route elimination, ejection
chains, large neighbourhood search (ruin and recreate), simulated annealing,
time-window slack bookkeeping to check moves in O(1).

In meetings, be specific: what changed, how much it helped (distance and
vehicles separately), how close it ran to the time limit, and whether it looks
robust or lucky.
