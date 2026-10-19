"""When the live-test launcher restarts an agent with a fresh context (fork-and-flush)."""

import importlib.util
from pathlib import Path
from types import SimpleNamespace

spec = importlib.util.spec_from_file_location("live_test", Path(__file__).parents[1] / "scripts" / "live_test.py")
live_test = importlib.util.module_from_spec(spec)
spec.loader.exec_module(live_test)
flush_reason = live_test.flush_reason


def args(flush="reassign", every=0, min_age=120):
    return SimpleNamespace(flush=flush, flush_every_evals=every, flush_min_age=min_age)


def test_flush_on_reassignment_only_between_meetings():
    start = {"idea": "I1", "evals": 3}
    moved = {"idea": "I4", "evals": 5, "meeting": None}
    assert flush_reason(moved, start, args(), 300) == "reassigned I1 -> I4"
    assert flush_reason({**moved, "meeting": {"group": "K1.1"}}, start, args(), 300) is None  # still voting
    assert flush_reason(moved, start, args(), 60) is None  # too young
    assert flush_reason(moved, start, args(min_age=480), 300) is None
    assert flush_reason({**moved, "idea": "I1"}, start, args(), 300) is None
    assert flush_reason(moved, {"idea": None, "evals": 0}, args(), 300) is None  # first direction isn't a move
    assert flush_reason(moved, start, args("off"), 300) is None


def test_flush_on_council_moves_only():
    start = {"idea": "I1", "evals": 3}
    crew_move = {"idea": "I4", "evals": 5, "meeting": None, "council_moved": None}
    assert flush_reason(crew_move, start, args("council"), 600, started=1000.0) is None
    # the move that caused this session (before it started) doesn't count again
    assert flush_reason({**crew_move, "council_moved": 900.0}, start, args("council"), 600, started=1000.0) is None
    assert flush_reason({**crew_move, "council_moved": 1200.0}, start, args("council"), 600,
                        started=1000.0) == "a council moved it to I4"


def test_flush_when_the_crew_forks():
    start = {"idea": "I1", "evals": 3}
    st = {"idea": "I1", "evals": 4, "meeting": None, "crew_forked": 1100.0}
    assert flush_reason(st, start, args("council"), 30, started=1000.0) == "its crew forked"
    assert flush_reason(st, start, args("off"), 30, started=1000.0) is None
    assert flush_reason({**st, "crew_forked": 900.0}, start, args("council"), 30, started=1000.0) is None


def test_flush_every_n_evals():
    start = {"idea": "I1", "evals": 3}
    assert flush_reason({"idea": "I1", "evals": 8, "meeting": None}, start, args("off", 5), 300) == "5 evals this session"
    assert flush_reason({"idea": "I1", "evals": 7, "meeting": None}, start, args("off", 5), 300) is None
