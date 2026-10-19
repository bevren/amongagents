# autoresearch, multiplayer edition

This is karpathy/autoresearch (https://github.com/karpathy/autoresearch). Your
workspace is a checkout of the repo; the first `run_eval` sets it up
(`uv sync`, `uv run prepare.py`).

- **Only edit `train.py`.** Architecture, optimizer, hyperparameters, batch
  size, and model size are all fair game.
- Don't modify `prepare.py` (it holds the ground-truth `evaluate_bpb`), and
  don't add dependencies.
- Training always stops after a fixed 5-minute budget. The metric is
  `val_bpb` (lower is better). `run_eval` runs `uv run train.py` and reads the
  `val_bpb:` line.
- Simplicity matters: a tiny gain that adds a lot of hacky code isn't worth
  much. Deleting code for an equal or better result is a great finding.

Multiplayer notes:
- Players may have different GPUs, so scores can drift between machines. When
  you discuss a finding, consider whether the gain could come from hardware
  rather than the idea. Rerunning someone's change on your own machine is
  strong evidence.
- Coordinate: if the shared notes say someone is exploring optimizers, try
  architecture or data-efficiency ideas instead.
