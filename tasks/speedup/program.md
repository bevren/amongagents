# Speed up a function

Goal: make `count_inversions` in `target.py` as fast as possible, while it keeps
returning exactly what the original returns for every input.

- Edit `target.py` only. `reference.py` holds the original function, and it is
  both the correctness check and the timing baseline.
- `eval.py` first checks `target.py` against the reference on edge cases (empty
  input, one element, duplicates, negative numbers, sorted and reversed input)
  and on random inputs. Any mismatch fails the run.
- Then it times both on fresh random inputs of 3000 numbers, alternating between
  them for 5 rounds. **Score = the median of reference time / your time**, so
  1.0 means as fast as the original and 10.0 means ten times faster.
- The score is a ratio measured on your own machine, so players with different
  computers can still compare scores. Every round uses a new input, so caching
  answers between calls doesn't help.
- When you report an improvement, the room reruns the check on fresh hidden
  inputs. Code tuned to the public inputs, or code that guesses answers, fails
  there.
- Standard library only, unless the room's setup installs more.

Timing has some noise, typically a few percent. The room measures it from
repeated runs of identical code and asks improvements to beat it.

In meetings, be specific: what changed, the speedup before and after, the
complexity of the new approach, and which inputs it might get wrong.
