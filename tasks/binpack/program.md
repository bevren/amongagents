# Bin packing research

Goal: minimize the mean number of bins `eval.py` reports over its 5 fixed
instances (300 items each, bin capacity 100).

- Edit `solver.py` only. `pack(items, capacity)` returns a list of bins, each a
  list of item indices. Every index must appear exactly once, and no bin may go
  over capacity.
- Each instance has a **1.0 s** time limit, and the whole eval runs in about a
  second, so experiment freely.
- Standard library only. `eval.py` is protected and restored before every run.
- The eval prints each instance's lower bound (ceil(total size / capacity)),
  so you can see how far from optimal you are.

Starting point: next fit (bad on purpose). Some directions: first/best fit
decreasing, pairing large items with complements, bin-completion search,
local search that empties the least-full bin, time-boxed randomized restarts.
