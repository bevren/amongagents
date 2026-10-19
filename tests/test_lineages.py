"""Crew lineages, the measured noise margin, and scheduled forks of the weakest crews."""

import asyncio
import subprocess
import time

import pytest

from amongagents.game import GameError, Room
from amongagents.history import RoomStore
from amongagents.tasks import MeetingSettings, TaskSpec

from test_game import Inbox


def make_task(**meeting) -> TaskSpec:
    return TaskSpec(
        name="t", description="", program="", metric_name="score", direction="minimize",
        metric_regex=r"score:\s*([0-9.]+)", eval_command="true", editable=["solver.py"],
        files={"solver.py": "v0\n", "eval.py": "protected"}, baseline_score=20.0,
        meeting=MeetingSettings(**{"rounds": 1, "turn_timeout_sec": 0.05, "vote_timeout_sec": 0.05,
                                   "cooldown_sec": 0, "crew_size": 2, "fork_every_sec": 600, **meeting}),
    )


async def setup(tmp_path, names, **meeting):
    store = RoomStore(tmp_path, "R")
    task = make_task(**meeting)
    store.init("R", task, {"solver.py": "v0\n"}, {})
    room = Room("R", task, store)
    room.version_commits[0] = store.log()[-1]["commit"]
    boxes = {}
    for n in names:
        boxes[n] = Inbox()
        await room.join(n, boxes[n].send)
    return room, store, boxes


async def improve(room, name, score, code):
    await room.record_eval(name, score, True, files={"solver.py": code})
    return await room.report(name, "improvement", f"{name}: {score}", score, {"solver.py": code})


def git(store, *args):
    return subprocess.run(["git", "-C", str(store.repo), *args], capture_output=True, text=True).stdout.strip()


def types(box):
    return {ev["type"] for ev in box.events}


def test_each_crew_builds_on_its_own_line(tmp_path):
    async def main():
        room, store, boxes = await setup(tmp_path, ["a", "b", "c", "d"])
        assert room.crew_of == {"a": "C1", "b": "C1", "c": "C2", "d": "C2"}
        f1 = await improve(room, "a", 15.0, "a15\n")
        assert room.baseline("b")["score"] == 15.0 and room.baseline("c")["score"] == 20.0
        assert "baseline_updated" not in types(boxes["c"]) and "room_record" in types(boxes["c"])

        # c only has to beat its own crew's best, not the room's
        f2 = await improve(room, "c", 18.0, "c18\n")
        assert room.baseline("d")["files"] == {"solver.py": "c18\n"} and room.baseline("d")["version"] == 2
        assert room.best_score == 15.0 and room.best_crew == "C1" and room.baseline()["score"] == 15.0
        with pytest.raises(GameError, match="your crew's best 18"):
            await room.record_eval("d", 18.0, True, files={"solver.py": "d18\n"})
            await room.report("d", "improvement", "same", 18.0, {"solver.py": "d18\n"})

        # history: one tag per version, each on its own lineage
        await asyncio.sleep(0.05)
        log = {c["version"]: c for c in store.log()}
        assert sorted(log) == [0, 1, 2] and log[2]["title"] == "v2: 18 by c [crew C2]"
        assert git(store, "rev-parse", "main") == f1.commit and git(store, "rev-parse", "crew/C2") == f2.commit
        diff = store.diff(2)  # against C2's own previous version, v0, not v1
        assert "-v0" in diff and "+c18" in diff and "a15" not in diff
        # an eval is "based on" the player's own line
        e = await room.record_eval("d", 19.0, True, files={"solver.py": "d19\n"})
        assert e["baseline_version"] == 2 and not e["beats_best"]

        # restart keeps every line
        again = Room.from_state(room.to_state())
        assert again.baseline("c")["files"] == {"solver.py": "c18\n"} and again.baseline("a")["score"] == 15.0
        await room.close()

    asyncio.run(main())


def test_a_shared_lineage_is_the_old_game(tmp_path):
    async def main():
        room, _, boxes = await setup(tmp_path, ["a", "b", "c", "d"], lineage="room")
        await improve(room, "a", 15.0, "a15\n")
        assert room.baseline("c")["score"] == 15.0
        assert all("baseline_updated" in types(b) for b in boxes.values())
        with pytest.raises(GameError, match="the shared best 15"):
            await improve(room, "c", 18.0, "c18\n")
        await room.close()

    asyncio.run(main())


def test_legacy_state_gives_every_crew_the_shared_baseline(tmp_path):
    async def main():
        room, _, _ = await setup(tmp_path, ["a", "b", "c", "d"])
        await improve(room, "a", 15.0, "a15\n")
        state = room.to_state()
        del state["lines"]
        state.update(baseline_files={"solver.py": "a15\n"}, best_score=15.0, best_by="a", baseline_version=1)
        again = Room.from_state(state)
        assert again.baseline("c") == again.baseline("a") and again.baseline("c")["score"] == 15.0
        await room.close()

    asyncio.run(main())


def test_repeats_of_identical_code_set_the_margin(tmp_path):
    async def main():
        room, _, _ = await setup(tmp_path, ["a", "b"])
        assert room.margin() == 0.0
        for s in (19.6, 20.0, 19.8, 20.2):  # the same code, four runs: sd ~0.26
            await room.record_eval("a", s, True, files={"solver.py": "same\n"})
        noise = room.noise()
        assert noise["repeats"] == 3 and abs(noise["sd"] - 0.2582) < 1e-3
        assert abs(room.margin() - 2 * noise["sd"]) < 1e-9
        e = await room.record_eval("a", 19.6, True, files={"solver.py": "lucky\n"})
        assert not e["beats_best"]  # 0.4 better than 20, inside the noise
        with pytest.raises(GameError, match="margin 0.516"):
            await room.report("a", "improvement", "lucky", 19.6, {"solver.py": "lucky\n"})
        e = await room.record_eval("a", 19.0, True, files={"solver.py": "real\n"})
        assert e["beats_best"]
        await room.report("a", "improvement", "real", 19.0, {"solver.py": "real\n"})
        # the noise survives a restart (rebuilt from the evals)
        assert Room.from_state(room.to_state()).noise() == room.noise()
        await room.close()

    asyncio.run(main())


def test_the_weakest_crew_forks_onto_the_strongest(tmp_path):
    async def main():
        room, store, boxes = await setup(tmp_path, ["a", "b", "c", "d", "e", "f"])
        await improve(room, "a", 15.0, "a15\n")
        await improve(room, "c", 18.0, "c18\n")
        await improve(room, "e", 15.0, "e15\n")
        await room.record_eval("d", 19.0, True, files={"solver.py": "d19\n"})
        assert not room.forks  # not due yet

        room.last_fork = time.time() - 601
        await room.record_eval("d", 19.0, True, files={"solver.py": "d19\n"})  # any eval checks the schedule
        assert len(room.forks) == 1
        f = room.forks[0]
        assert (f["crew"], f["from"], f["version"], f["from_version"], f["score"]) == ("C2", "C1", 4, 1, 15.0)
        assert f["replaced"] == {"version": 2, "score": 18.0, "by": "c"}
        line = room._line(room.groups["C2"])
        assert line["files"] == {"solver.py": "a15\n"} and line["version"] == 4
        assert room.baseline("a")["version"] == 1  # the donor keeps its line
        assert all(("crew_forked" in types(boxes[n])) == (n in ("c", "d")) for n in boxes)
        assert room.player_status("c")["crew_forked"] == f["ts"] and room.player_status("a")["crew_forked"] is None
        await asyncio.sleep(0.05)
        assert store.files_at("v4", ["solver.py"]) == {"solver.py": "a15\n"}
        assert any(c["version"] == 4 and "fork" in c["title"] for c in store.log())

        # C2 now builds on the forked code: an improvement has to beat 15
        with pytest.raises(GameError, match="your crew's best 15"):
            await improve(room, "d", 16.0, "d16\n")
        # equal lines (within the margin): nothing to take, so the next due fork does nothing
        room.last_fork = time.time() - 601
        await room.record_eval("d", 16.0, True, files={"solver.py": "d16\n"})
        assert len(room.forks) == 1
        await room.close()

    asyncio.run(main())


def test_two_crews_never_fork(tmp_path):
    """A fork with two crews would leave the room a single lineage."""
    async def main():
        room, _, _ = await setup(tmp_path, ["a", "b", "c", "d"])
        await improve(room, "a", 15.0, "a15\n")
        room.last_fork = time.time() - 601
        await room.record_eval("d", 19.0, True, files={"solver.py": "d19\n"})
        assert not room.forks and room.baseline("c")["score"] == 20.0
        await room.close()

    asyncio.run(main())
