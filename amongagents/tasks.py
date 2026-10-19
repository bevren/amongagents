"""Task plugins.

A task is a folder anyone can write, with no Python required:

    my-task/
      task.toml      # metric, eval command, editable files, meeting settings
      program.md     # instructions for the agents (what to research, constraints)
      files/         # starter files shipped to every player's workspace (optional)

The room creator packages the folder (see `TaskSpec.from_dir`) and uploads it
when creating a room, so the server never needs the task on its own disk.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import asdict, dataclass, field
from pathlib import Path

MAX_TASK_FILE_BYTES = 1_000_000
MAX_TASK_TOTAL_BYTES = 5_000_000


@dataclass
class MeetingSettings:
    rounds: int = 2
    turn_timeout_sec: float = 180
    vote_timeout_sec: float = 180
    cooldown_sec: float = 30
    # no new shared best for this many runs or seconds -> the room calls a stagnation meeting (0 = off)
    stagnation_evals: int = 20
    stagnation_sec: float = 1200
    # big rooms split into crews that meet on their own; councils of delegates join them up
    crew_size: int = 8
    council_size: int = 8  # delegates per council meeting, and crews (or councils) per council
    council_wait_sec: float = 300  # a council meets once half its children nominated, or after this
    nomination_ttl_sec: float = 1800  # a nominated idea nobody took up by then is dropped
    # "crew": every crew builds on its own best (separate lineages), "room": one shared baseline
    lineage: str = "crew"
    # every this often, the weakest crews take the strongest crews' best code (0 = never)
    fork_every_sec: float = 900
    # an improvement must beat the best by max(min_delta, noise_k x the measured run-to-run noise)
    noise_k: float = 2.0


@dataclass
class TaskSpec:
    name: str
    description: str
    program: str
    metric_name: str
    direction: str  # "minimize" | "maximize"
    metric_regex: str
    eval_command: str
    eval_timeout_sec: float = 600
    min_delta: float = 0.0
    baseline_score: float | None = None
    editable: list[str] = field(default_factory=list)
    setup_command: str = ""
    # optional hidden-instance check for improvements: run with a secret {seed} chosen by the server
    holdout_command: str = ""
    holdout_timeout_sec: float = 600
    # container players use with `--sandbox docker`
    sandbox_image: str = "python:3.12-slim"
    sandbox_gpus: bool = False
    files: dict[str, str] = field(default_factory=dict)
    meeting: MeetingSettings = field(default_factory=MeetingSettings)

    def __post_init__(self) -> None:
        if self.direction not in ("minimize", "maximize"):
            raise ValueError(f"metric.direction must be minimize or maximize, got {self.direction!r}")
        re.compile(self.metric_regex)
        for rel in [*self.files, *self.editable]:
            _check_relpath(rel)

    def is_better(self, score: float, best: float | None) -> bool:
        if best is None:
            return True
        if self.direction == "minimize":
            return score < best - self.min_delta
        return score > best + self.min_delta

    @property
    def protected_files(self) -> list[str]:
        """Shipped files the agents must not edit (e.g. the evaluator)."""
        return [p for p in self.files if p not in self.editable]

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "TaskSpec":
        d = dict(d)
        d["meeting"] = MeetingSettings(**d.get("meeting", {}))
        return cls(**d)

    @classmethod
    def from_dir(cls, path: str | Path) -> "TaskSpec":
        root = Path(path)
        cfg = tomllib.loads((root / "task.toml").read_text(encoding="utf-8"))
        program_path = root / cfg.get("program", "program.md")
        program = program_path.read_text(encoding="utf-8") if program_path.exists() else ""

        files: dict[str, str] = {}
        total = 0
        files_dir = root / "files"
        if files_dir.is_dir():
            for f in sorted(files_dir.rglob("*")):
                if not f.is_file() or "__pycache__" in f.parts:
                    continue
                size = f.stat().st_size
                if size > MAX_TASK_FILE_BYTES:
                    raise ValueError(f"{f} is too large to ship ({size} bytes)")
                total += size
                files[f.relative_to(files_dir).as_posix()] = f.read_text(encoding="utf-8")
        if total > MAX_TASK_TOTAL_BYTES:
            raise ValueError("task files too large; use setup.command to fetch big assets")

        metric = cfg.get("metric", {})
        ev = cfg.get("eval", {})
        return cls(
            name=cfg.get("name", root.name),
            description=cfg.get("description", ""),
            program=program,
            metric_name=metric.get("name", "score"),
            direction=metric.get("direction", "minimize"),
            metric_regex=metric.get("regex", r"score:\s*([-+0-9.eE]+)"),
            min_delta=float(metric.get("min_delta", 0.0)),
            baseline_score=metric.get("baseline"),
            eval_command=ev["command"],
            eval_timeout_sec=float(ev.get("timeout_sec", 600)),
            editable=list(cfg.get("files", {}).get("editable", [])),
            setup_command=cfg.get("setup", {}).get("command", ""),
            holdout_command=cfg.get("holdout", {}).get("command", ""),
            sandbox_image=cfg.get("sandbox", {}).get("image", "python:3.12-slim"),
            sandbox_gpus=bool(cfg.get("sandbox", {}).get("gpus", False)),
            holdout_timeout_sec=float(cfg.get("holdout", {}).get("timeout_sec", ev.get("timeout_sec", 600))),
            files=files,
            meeting=MeetingSettings(**cfg.get("meeting", {})),
        )


def parse_metric(regex: str, output: str) -> float | None:
    """Return the last match of the metric regex in the eval output."""
    matches = re.findall(regex, output)
    if not matches:
        return None
    last = matches[-1]
    if isinstance(last, tuple):
        last = last[0]
    try:
        return float(last)
    except ValueError:
        return None


def _check_relpath(rel: str) -> None:
    p = Path(rel)
    if p.is_absolute() or ".." in p.parts or rel.startswith(("/", "\\")):
        raise ValueError(f"task paths must be relative and inside the workspace: {rel!r}")
