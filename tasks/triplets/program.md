# Hard bin packing research

Goal: minimize the mean number of bins `eval.py` reports over its 5 fixed
instances. Each instance has 360 items (sizes 250-500) and bin capacity 1000.

Greedy heuristics do badly here: first fit scores 134.4, and "smarter" sorted
variants like first-fit decreasing do worse (140.0). Find out why, and how far
this can be pushed.

- Edit `solver.py` only. `pack(items, capacity)` returns a list of bins, each a
  list of item indices. Every index must appear exactly once, and no bin may go
  over capacity.
- Each instance has a **1.0 s** time limit. A full eval takes a few seconds.
- Standard library only. `eval.py` is protected and restored before every run.

Things worth exploring: exact-fill search (3 items summing to exactly 1000),
largest-first with complementary pairs, bin-completion / branch and bound,
local search on the bin with the most slack, randomized restarts, and
combinations of these.
