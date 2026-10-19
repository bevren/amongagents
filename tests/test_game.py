import asyncio

import pytest

from amongagents.game import GameError, Room, assign
from amongagents.tasks import MeetingSettings, TaskSpec


def make_task(**meeting) -> TaskSpec:
    return TaskSpec(
        name="t", description="", program="", metric_name="score", direction="minimize",
        metric_regex=r"score:\s*([0-9.]+)", eval_command="true", editable=["solver.py"],
        files={"solver.py": "v0", "eval.py": "protected"},
        meeting=MeetingSettings(**{"rounds": 2, "turn_timeout_sec": 2, "vote_timeout_sec": 2, "cooldown_sec": 0, **meeting}),
    )


class Inbox:
    def __init__(self):
        self.events = []

    async def send(self, ev):
        self.events.append(ev)


async def setup(names, **meeting):
    room = Room("R", make_task(**meeting))
    boxes = {}
    for n in names:
        boxes[n] = Inbox()
        await room.join(n, boxes[n].send)
    return room, boxes


def ended_meeting(room):
    return [e for e in room.log if e["type"] == "meeting_ended"][-1]["meeting"]


async def play_meeting(room, speak=True, ideas=None, votes=None):
    """Each player speaks when it's their turn (proposing ideas[player] in round 1), then votes."""
    seen = 0
    while room.state == "meeting":
        m = room.meeting
        if speak and m.turn and len(m.transcript) == seen + 1:
            seen += 1
            propose = (ideas or {}).get(m.turn) if m.round == 1 else None
            await room.say(m.turn, f"{m.turn} in round {m.round}", propose)
        await asyncio.sleep(0.01)
    for n, v in (votes or {}).items():
        await room.vote(n, v)
    while room.state != "working":
        await asyncio.sleep(0.01)


IDEAS = [{"id": f"I{n}", "by": b, "text": t} for n, (b, t) in enumerate([("a", "x"), ("b", "y"), ("c", "z")], 1)]


def test_assign_spreads_agents_and_follows_votes():
    directions, out = assign(IDEAS, {"a": "I2", "b": "I3", "c": "I2", "d": "I2"}, ["a", "b", "c", "d"])
    assert [d["id"] for d in directions] == ["I2", "I3"]  # I1 got no votes -> dropped
    assert out == {"a": "I2", "b": "I3", "c": "I2", "d": "I2"}
    assert directions[0]["agents"] == ["a", "c", "d"] and directions[0]["votes"] == 3


def test_assign_staffs_every_voted_direction():
    # everyone votes I1 except b; I3 still gets someone even though nobody believes in it more than I1
    directions, out = assign(IDEAS, {"a": "I3", "b": "I1", "c": "I1"}, ["a", "b", "c"])
    assert [d["id"] for d in directions] == ["I1", "I3"]
    assert out == {"b": "I1", "a": "I3", "c": "I1"}
    # more voted ideas than agents: only the top ones survive
    directions, out = assign(IDEAS, {"a": "I2", "b": "I3"}, ["a", "b"])
    assert len(directions) == 2 and set(out.values()) == {"I2", "I3"}
    # non-voters fill the least-staffed direction
    directions, out = assign(IDEAS, {"a": "I2", "b": "I2"}, ["a", "b", "c"])
    assert out["c"] == "I2" and [d["id"] for d in directions] == ["I2"]
    assert assign(IDEAS, {}, ["a"]) == ([], {})


def test_improvement_becomes_baseline_without_a_vote():
    async def main():
        room, boxes = await setup(["a", "b", "c"])
        await room.record_eval("b", 10.0, True)
        await room.report("b", "improvement", "found it", 10.0, {"solver.py": "v1"})
        assert room.state == "meeting"
        assert room.best_score == 10.0 and room.baseline_version == 1 and room.baseline_files == {"solver.py": "v1"}
        assert any(e["type"] == "baseline_updated" for e in boxes["c"].events)

    asyncio.run(main())


def test_sequential_meeting_splits_crew_across_directions():
    async def main():
        room, boxes = await setup(["a", "b", "c", "d"])
        await room.report("b", "clue", "warmup doesn't matter", propose="bigger learning rate")
        with pytest.raises(GameError):
            await room.say("c", "out of turn")  # it's a's turn first
        await play_meeting(room, ideas={"a": "weight tying", "c": "deeper model"},
                           votes={"a": "I3", "b": "I3", "c": "I1", "d": "I1"})

        m = ended_meeting(room)
        order = [(t["round"], t["player"]) for t in m["transcript"]]
        assert order == [(0, "b"), *[(r, p) for r in (1, 2) for p in "abcd"]]
        assert [(i["id"], i["by"]) for i in m["ideas"]] == [("I1", "b"), ("I2", "a"), ("I3", "c")]
        assert [d["id"] for d in room.directions] == ["I1", "I3"]  # 2-2 tie -> earliest first; I2 dropped
        assert room.assignments == {"c": "I1", "a": "I3", "b": "I3", "d": "I1"}
        assert room.findings[0].chosen_ideas == ["I1", "I3"]
        assert any(e["type"] == "assignments_changed" for e in boxes["a"].events)
        assert room.notes[0]["text"] == "warmup doesn't matter"

        # idea ids keep counting across meetings, and evals are tagged with the explorer's idea
        await room.record_eval("a", 9.0, True)
        assert room.evals[-1]["idea"] == "I3"
        await room.report("a", "clue", "second", propose="another idea")
        assert room.meeting.ideas[0]["id"] == "I4"

    asyncio.run(main())


def test_vote_rules():
    async def main():
        room, _ = await setup(["a", "b"], rounds=1)
        await room.report("a", "clue", "x", propose="idea A")
        task = asyncio.create_task(play_meeting(room, ideas={"b": "idea B"}))
        while room.state != "voting":
            await asyncio.sleep(0.01)
        with pytest.raises(GameError, match="own idea"):
            await room.vote("a", "I1")
        with pytest.raises(GameError, match="unknown idea"):
            await room.vote("a", "I7")
        await room.vote("a", "i2")  # case-insensitive
        await room.vote("b", "I1")
        await task
        assert {d["id"]: d["agents"] for d in room.directions} == {"I1": ["b"], "I2": ["a"]}

    asyncio.run(main())


def test_no_votes_keeps_previous_directions():
    async def main():
        room, _ = await setup(["a", "b"], rounds=1, turn_timeout_sec=0.1, vote_timeout_sec=0.2)
        room.groups["C1"].directions = [{"id": "I9", "by": "z", "text": "old", "votes": 1, "agents": ["a", "b"]}]
        room.assignments = {"a": "I9", "b": "I9"}
        await room.report("a", "clue", "lr warmup is useless")
        await play_meeting(room, speak=False)
        ended = ended_meeting(room)
        assert [t.get("skipped") for t in ended["transcript"][1:]] == [True, True]
        assert ended["result"]["changed"] is False
        assert room.assignments == {"a": "I9", "b": "I9"}

    asyncio.run(main())


def test_report_validation():
    async def main():
        room, _ = await setup(["a", "b"])
        with pytest.raises(GameError, match="most recent"):
            await room.report("a", "improvement", "x", 5.0, {"solver.py": "x"})
        await room.record_eval("a", 5.0, True)
        with pytest.raises(GameError, match="editable"):
            await room.report("a", "improvement", "x", 5.0, {"eval.py": "hacked"})
        room._line(room._crew("a"))["score"] = 4.0
        with pytest.raises(GameError, match="does not beat"):
            await room.report("a", "improvement", "x", 5.0, {"solver.py": "x"})
        await room.report("a", "clue", "x")
        f = await room.report("b", "clue", "y")  # a meeting is running: queued, not rejected
        assert f.queued and room.queue[0][0] is f

    asyncio.run(main())


def test_queued_findings_open_the_next_meeting_together():
    async def main():
        room, _ = await setup(["a", "b"], rounds=1, cooldown_sec=0.3)
        await room.report("a", "clue", "first")
        await room.record_eval("b", 10.0, True)
        f2 = await room.report("b", "improvement", "during meeting", 10.0, {"solver.py": "v1"}, propose="B idea")
        assert f2.queued and room.baseline_version == 1  # the baseline merges at once, even when queued
        await play_meeting(room, votes={})
        f3 = await room.report("a", "clue", "during cooldown")
        assert f3.queued and room.state == "working"
        for _ in range(100):  # the drain opens one meeting for both queued findings after the cooldown
            if room.state == "meeting":
                break
            await asyncio.sleep(0.02)
        m = room.meeting
        assert [f.id for f in m.findings] == [f2.id, f3.id] and not room.queue
        assert [(t["round"], t["player"]) for t in m.transcript] == [(0, "b"), (0, "a")]
        assert m.ideas[0]["text"] == "B idea" and m.transcript[0]["idea"] == m.ideas[0]["id"]

    asyncio.run(main())


def test_assign_honors_work_on_claims():
    # meeting 1 of the live test: the crew agreed alice->I1, bob->I2, carol->I3 but voted I2, I3, I2
    votes = {"a": "I2", "b": "I3", "c": "I2"}
    claims = {"a": "I1", "b": "I2", "c": "I3"}
    directions, out = assign(IDEAS, votes, ["a", "b", "c"], claims)
    assert out == {"a": "I1", "b": "I2", "c": "I3"}
    assert [d["id"] for d in directions] == ["I2", "I3", "I1"]  # ranked by votes, claimed-only idea last
    # everyone claims I1, but I2 got votes: someone is moved from the crowd to cover it
    directions, out = assign(IDEAS, {"a": "I2", "b": "I2", "c": "I1"}, ["a", "b", "c"], {"a": "I1", "b": "I1", "c": "I1"})
    assert sorted(out.values()) == ["I1", "I1", "I2"] and out["c"] == "I2"


def test_work_on_can_keep_a_current_direction():
    async def main():
        room, boxes = await setup(["a", "b", "c"], rounds=1)
        room.groups["C1"].directions = [{"id": "I5", "by": "c", "text": "old lead", "votes": 2, "agents": ["a", "b", "c"]},
                           {"id": "I6", "by": "a", "text": "stale", "votes": 1, "agents": []}]
        room.assignments = {"a": "I5", "b": "I5", "c": "I5"}
        room._idea_seq = 6
        await room.report("a", "clue", "x", propose="new idea A")  # I7
        task = asyncio.create_task(play_meeting(room, ideas={"b": "new idea B"}))  # I8
        while room.state != "voting":
            await asyncio.sleep(0.01)
        started = [e for e in room.log if e["type"] == "voting_started"][-1]
        assert [i["id"] for i in started["carried"]] == ["I5", "I6"]
        with pytest.raises(GameError, match="own idea"):
            await room.vote("a", "I6")             # a proposed I6 earlier: still their own
        await room.vote("a", "I8", work_on="I5")  # keep the current direction
        await room.vote("b", "I5", work_on="I8")  # an old direction can also get votes
        await room.vote("c", "I7", work_on="I7")
        await task
        assert room.assignments == {"a": "I5", "b": "I8", "c": "I7"}
        # 1 vote + 1 claim each, so ties go to order: new ideas, then carried ones. I6 had neither: dropped.
        assert [d["id"] for d in room.directions] == ["I7", "I8", "I5"]
        assert next(d for d in room.directions if d["id"] == "I5")["text"] == "old lead"

    asyncio.run(main())


def test_stagnation_meeting_forces_new_directions():
    async def main():
        room, boxes = await setup(["a", "b"], rounds=1, stagnation_evals=3)
        room.best_score = 10.0
        room.groups["C1"].directions = [{"id": "I1", "by": "a", "text": "tweak kicks", "votes": 2, "agents": ["a", "b"]}]
        room.assignments = {"a": "I1", "b": "I1"}
        room._idea_seq = 1
        await room.record_eval("a", 11.0, True)
        await room.record_eval("b", 12.0, True)
        assert room.state == "working"
        await room.record_eval("a", 11.5, True)  # 3rd run without a new best -> the room calls a meeting
        assert room.state == "meeting"
        m = room.meeting
        assert m.finding.kind == "stagnation" and m.finding.reporter == "room"
        assert "no new best in 3 runs" in m.finding.summary and "I1 (a, b): tweak kicks -> best 11" in m.finding.summary
        assert m.participants == ["a", "b"]
        task = asyncio.create_task(play_meeting(room, ideas={"a": "new representation", "b": "new algorithm"}))
        while room.state != "voting":
            await asyncio.sleep(0.01)
        started = [e for e in room.log if e["type"] == "voting_started"][-1]
        assert started["carried"] == []
        with pytest.raises(GameError, match="unknown idea"):
            await room.vote("a", "I3", work_on="I1")  # keeping the stalled direction isn't allowed
        await room.vote("a", "I3", work_on="I2")
        await room.vote("b", "I2", work_on="I3")
        await task
        assert room.assignments == {"a": "I2", "b": "I3"} and "I1" not in [d["id"] for d in room.directions]
        assert room.groups["C1"].evals_since_progress == 0  # a fresh window for the new directions
        await room.record_eval("a", 11.0, True)
        assert room.state == "working"

    asyncio.run(main())


def test_progress_resets_the_stagnation_counter():
    async def main():
        room, _ = await setup(["a", "b"], rounds=1, stagnation_evals=3)
        await room.record_eval("a", 5.0, True)
        await room.record_eval("a", 4.0, True)
        await room.report("a", "improvement", "better", 4.0, {"solver.py": "v1"})
        assert room.evals_since_progress == 0
        await play_meeting(room)
        await room.record_eval("b", 6.0, True)
        await room.record_eval("b", 6.0, True)
        assert room.state == "working"  # only 2 runs since the last new best

    asyncio.run(main())


def test_improvements_must_win_on_hidden_instances():
    async def main():
        room, _ = await setup(["a", "b"], rounds=1)
        room.task.holdout_command = "python eval.py --seed-base {seed}"
        await room.record_eval("a", 9.0, True)
        files = {"solver.py": "v1"}
        with pytest.raises(GameError, match="fresh seed"):  # no challenge requested
            await room.report("a", "improvement", "x", 9.0, files, holdout={"seed": 1, "candidate": 1, "baseline": 2})
        ch = room.challenge("a")
        with pytest.raises(GameError, match="fresh seed"):  # wrong seed (and the challenge is now spent)
            await room.report("a", "improvement", "x", 9.0, files,
                              holdout={"seed": ch["seed"] + 1, "baseline_version": 0, "candidate": 8, "baseline": 10})
        ch = room.challenge("a")
        with pytest.raises(GameError, match="likely overfit"):  # wins publicly, loses on hidden instances
            await room.report("a", "improvement", "x", 9.0, files,
                              holdout={"seed": ch["seed"], "baseline_version": 0, "candidate": 10.5, "baseline": 10.0})
        assert room.baseline_version == 0 and room.state == "working"
        ch = room.challenge("a")
        with pytest.raises(GameError, match="baseline changed"):
            await room.report("a", "improvement", "x", 9.0, files,
                              holdout={"seed": ch["seed"], "baseline_version": 7, "candidate": 8, "baseline": 10})
        ch = room.challenge("a")
        f = await room.report("a", "improvement", "generalizes", 9.0, files,
                              holdout={"seed": ch["seed"], "baseline_version": 0, "candidate": 8.0, "baseline": 10.0})
        # pooled with the earlier failed check of the same code: (10.5 + 8) / 2 vs (10 + 10) / 2
        assert room.baseline_version == 1 and f.holdout == {
            "seed": ch["seed"], "candidate": 8.0, "baseline": 10.0, "baseline_version": 0,
            "samples": 2, "mean_candidate": 9.25, "mean_baseline": 10.0}
        await room.record_eval("a", 8.0, True)
        with pytest.raises(GameError, match="fresh seed"):  # the used seed can't be replayed
            await room.report("a", "improvement", "again", 8.0, {"solver.py": "v2"},
                              holdout={"seed": ch["seed"], "baseline_version": 1, "candidate": 7, "baseline": 8})
        await room.report("b", "clue", "clues need no holdout check")  # queued behind the running meeting
        assert room.findings[-1].kind == "clue"

    asyncio.run(main())


def test_holdout_retries_are_pooled():
    """Re-reporting the same code until one seed passes doesn't work: every check of it counts."""
    async def main():
        room, _ = await setup(["a", "b"], rounds=1)
        room.task.holdout_command = "python eval.py --seed-base {seed}"
        await room.record_eval("a", 9.0, True)

        async def check(name, files, cand, base):
            ch = room.challenge(name)
            return await room.report(name, "improvement", "x", 9.0, files,
                                     holdout={"seed": ch["seed"], "baseline_version": 0,
                                              "candidate": cand, "baseline": base})

        code = {"solver.py": "def solve():\n    return 1\n"}
        for _ in range(2):  # two clear losses on hidden instances
            with pytest.raises(GameError, match="likely overfit"):
                await check("a", code, 11.0, 10.0)
        await room.record_eval("b", 9.0, True)
        # a lucky win, from another player, with the same code saved with CRLF and trailing spaces
        same = {"solver.py": "def solve():  \r\n\r\n    return 1\r\n"}
        with pytest.raises(GameError, match=r"Pooled over all 3 .*10\.6667 vs 10"):
            await check("b", same, 10.0, 10.0)
        assert room.baseline_version == 0
        # a real edit is new code with its own pool
        f = await check("a", {"solver.py": "def solve():\n    return 2\n"}, 9.0, 10.0)
        assert room.baseline_version == 1 and f.holdout["samples"] == 1

        restored = Room.from_state(room.to_state())  # only the current baseline's pools are kept
        assert all(k.startswith("v1:") for k in restored._holdout_samples)

    asyncio.run(main())


def test_vote_work_on_validation():
    async def main():
        room, _ = await setup(["a", "b"], rounds=1)
        await room.report("a", "clue", "x", propose="idea A")
        task = asyncio.create_task(play_meeting(room, ideas={"b": "idea B"}))
        while room.state != "voting":
            await asyncio.sleep(0.01)
        with pytest.raises(GameError, match="work_on"):
            await room.vote("a", "I2", work_on="I9")
        await room.vote("a", "I2", work_on="I1")  # working on your own idea is fine
        await room.vote("b", "I1", work_on="i2")
        await task
        assert room.assignments == {"a": "I1", "b": "I2"}

    asyncio.run(main())


def test_latecomers_speak_last_and_reinforce_thinnest_direction():
    async def main():
        room, boxes = await setup(["a", "b"], rounds=1)
        await room.report("a", "clue", "x")
        boxes["late"] = Inbox()
        welcome = await room.join("late", boxes["late"].send)
        assert welcome["snapshot"]["meeting"]["participants"] == ["a", "b", "late"]
        await play_meeting(room, ideas={"a": "A", "late": "L"}, votes={"a": "I2", "b": "I2", "late": "I1"})
        assert [t["player"] for t in ended_meeting(room)["transcript"]] == ["a", "a", "b", "late"]
        assert {d["id"]: d["agents"] for d in room.directions} == {"I2": ["a", "b"], "I1": ["late"]}

        boxes["later"] = Inbox()
        await room.join("later", boxes["later"].send)  # joins while working
        assert room.assignments["later"] == "I1"
        assert ended_meeting(room)["result"]["directions"][1]["agents"] == ["late"]  # history untouched

    asyncio.run(main())
