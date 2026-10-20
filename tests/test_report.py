import asyncio
import sys

from amongagents.game import Room
from amongagents.tasks import MeetingSettings, TaskSpec

from test_game import Inbox


def make_task(holdout: bool) -> TaskSpec:
    score = f'"{sys.executable}" -c "print(\'score:\', open(\'solver.py\').read())"'
    return TaskSpec(
        name="t", description="", program="", metric_name="score", direction="minimize",
        metric_regex=r"score:\s*([0-9.]+)", eval_command=score, editable=["solver.py"],
        holdout_command=score + " {seed}" if holdout else "", holdout_timeout_sec=30,
        files={"solver.py": "20", "eval.py": "protected"},
        meeting=MeetingSettings(rounds=1, turn_timeout_sec=1, vote_timeout_sec=1, cooldown_sec=0),
    )


async def bridge_for(room, tmp_path, monkeypatch, on_request=None):
    """A bridge for player "a" talking to an in-process room."""
    from amongagents import bridge

    sess = bridge.Session("ws://x", "R", "a", tmp_path, trust=True)

    async def ensure():
        pass

    async def request(op, **kw):
        if on_request:
            await on_request(op)
        if op == "eval":
            return await room.record_eval("a", kw["score"], kw["ok"], kw.get("note", ""), kw.get("description", ""),
                                          kw.get("files"))
        if op == "report":
            f = await room.report("a", kw["kind"], kw["summary"], kw.get("score"), kw.get("files"),
                                  kw.get("propose"), kw.get("holdout"))
            return f.public()
        if op == "baseline":
            return room.baseline("a")
        if op == "challenge":
            return room.challenge("a")
        return room.snapshot(viewer="a")

    async def to_bridge(ev):
        sess._on_event(ev)
        sess.wake.set()

    sess.ensure, sess.request = ensure, request
    monkeypatch.setattr(bridge, "S", sess, raising=False)
    sess._apply_welcome(await room.join("a", to_bridge))
    sess.restore_protected()
    for rel, content in room.task.files.items():
        if rel in room.task.editable:
            (tmp_path / rel).write_text(content)
    return bridge, sess


def test_a_win_is_reported_with_the_files_its_eval_scored(tmp_path, monkeypatch):
    """Editing after a winning eval used to block the report ("re-run run_eval"); the win was often lost."""
    async def main():
        room = Room("R", make_task(holdout=False))
        bridge, sess = await bridge_for(room, tmp_path, monkeypatch)
        await room.join("b", Inbox().send)
        (tmp_path / "solver.py").write_text("17")
        assert (await sess.eval_job())["score"] == 17.0
        (tmp_path / "solver.py").write_text("my next experiment")  # keep working before reporting
        out = await bridge.report_finding("improvement", "17 beats 20")
        assert "the eval's files were reported" in out
        assert room.baseline("a")["files"] == {"solver.py": "17"} and room.best_score == 17.0
        assert (tmp_path / "solver.py").read_text() == "my next experiment"  # edits untouched
        assert sess.synced_version == 0  # ... and not marked as the synced baseline
        await room.close()

    asyncio.run(main())


def test_a_win_that_lands_during_the_holdout_check_triggers_a_new_check(tmp_path, monkeypatch):
    async def main():
        room = Room("R", make_task(holdout=True))
        await room.join("b", Inbox().send)
        challenges = []

        async def on_request(op):
            if op == "challenge":
                challenges.append(op)
            if op == "baseline" and len(challenges) == 1:  # b's 18 lands while a's check runs
                    await room.record_eval("b", 18.0, True)
                    room.challenge("b")
                    await room.report("b", "improvement", "18", 18.0, {"solver.py": "18"},
                                      holdout={"seed": room._challenges["b"], "baseline_version": 0,
                                               "candidate": 18.0, "baseline": 20.0})

        bridge, sess = await bridge_for(room, tmp_path, monkeypatch, on_request)
        (tmp_path / "solver.py").write_text("17")
        await sess.eval_job()
        out = await bridge.report_finding("improvement", "17 beats 20")
        assert len(challenges) == 2 and "QUEUED" in out  # b's meeting is running
        assert (room.findings[-1].holdout["candidate"], room.findings[-1].holdout["baseline"]) == (17.0, 18.0)
        assert room.best_score == 17.0 and room.baseline("a")["files"] == {"solver.py": "17"}
        await room.close()

    asyncio.run(main())
