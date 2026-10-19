import asyncio

import pytest

from amongagents.game import GameError, Room, council_of

from test_game import Inbox, make_task


async def setup(names, **meeting):
    room = Room("R", make_task(**{"crew_size": 2, "council_size": 2, **meeting}))
    boxes = {}
    for n in names:
        boxes[n] = Inbox()
        await room.join(n, boxes[n].send)
    return room, boxes


async def autoplay(room, ideas, choose, skip=lambda g, name: False):
    """Bots for every running meeting: speak on their turn (proposing ideas[name] in round 1), then
    vote with choose(name, options) -> idea id. skip(group, name) leaves a player to the test."""
    spoken = set()
    while True:
        for g in list(room.groups.values()):
            m = g.meeting
            if not m:
                continue
            if g.state == "meeting" and m.turn and (m.id, m.round, m.turn) not in spoken:
                if skip(g, m.turn):
                    continue
                spoken.add((m.id, m.round, m.turn))
                await room.say(m.turn, f"{m.turn} speaks", ideas.get(m.turn) if m.round == 1 else None)
            elif g.state == "voting":
                for n in m.participants:
                    if n not in m.votes and not skip(g, n):
                        await room.vote(n, choose(n, room._options(g, m)))
        await asyncio.sleep(0.01)


async def until(cond, timeout=5.0):
    for _ in range(int(timeout / 0.01)):
        if cond():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("timed out")


def agree_on_first(n, options):
    if any(i.get("nominated") for i in options):
        return options[0]["id"]
    return next((i["id"] for i in options if i["by"] != n), options[0]["id"])


def test_council_tree():
    assert council_of(0, 0, 1, 8) is None  # one crew: no council
    assert council_of(0, 3, 8, 8) == (1, 0)
    assert council_of(1, 0, 8, 8) is None  # K1.1 is the top for up to 8 crews
    assert council_of(0, 8, 9, 8) == (2, 0)  # a lone 9th crew skips its one-child council
    assert council_of(1, 0, 9, 8) == (2, 0)
    assert council_of(0, 124, 125, 8) == (1, 15) and council_of(1, 15, 125, 8) == (2, 1)
    assert council_of(2, 1, 125, 8) == (3, 0) and council_of(3, 0, 125, 8) is None  # 3 levels for 1000 agents


def test_players_fill_crews_and_meetings_stay_in_the_crew():
    async def main():
        room, boxes = await setup(["a", "b", "c", "d", "e"])
        assert [(g.id, g.members) for g in room._crews()] == [("C1", ["a", "b"]), ("C2", ["c", "d"]), ("C3", ["e"])]
        await room.report("a", "clue", "a's clue")
        await room.report("c", "clue", "c's clue")  # C2 meets at the same time
        assert room.groups["C1"].meeting.participants == ["a", "b"]
        assert room.groups["C2"].meeting.participants == ["c", "d"]
        assert room.groups["C3"].state == "working"
        assert not any(e["type"] in ("meeting_started", "turn") for e in boxes["e"].events)
        assert not any(e["type"] == "turn" and e["group"] == "C2" for e in boxes["a"].events)
        with pytest.raises(GameError, match="no discussion"):
            await room.say("e", "not in any meeting")
        assert room.snapshot(viewer="e")["meeting"] is None
        assert room.snapshot(viewer="d")["meeting"]["group"] == "C2"
        assert room.player_status("b") == {"state": "meeting", "crew": "C1", "idea": None, "evals": 0, "council_moved": None, "crew_forked": None, "meeting": {
            "group": "C1", "level": 0, "kind": "clue", "reporter": "a", "turn": "a", "voted": False}}

        # a new best becomes its own crew's baseline; the other crews only hear of the record
        await room.record_eval("e", 5.0, True)
        await room.report("e", "improvement", "faster", 5.0, {"solver.py": "v1"})
        crew = room.groups[room.crew_of["e"]].members
        for n, b in boxes.items():
            types = {ev["type"] for ev in b.events}
            assert ("baseline_updated" in types) == (n in crew) and ("room_record" in types) == (n not in crew)
        assert room.baseline("e")["score"] == 5.0 and room.baseline("a")["score"] is None
        assert room.best_score == 5.0 and room.best_crew == room.crew_of["e"]

        # a leaver's slot is refilled before a new crew is opened
        await room.leave("d")
        await room.join("f", Inbox().send)
        assert room.crew_of["f"] == "C2"

    asyncio.run(main())


def test_crew_picks_go_to_the_council_and_spread():
    async def main():
        room, boxes = await setup(["a", "b", "c", "d"], rounds=1)
        bots = asyncio.create_task(autoplay(room, {"a": "idea A", "c": "idea C"}, agree_on_first))
        await room.report("a", "clue", "a's clue")
        await room.report("c", "clue", "c's clue")
        council = None
        await until(lambda: "K1.1" in room.groups and room.groups["K1.1"].meeting)
        council = room.groups["K1.1"].meeting
        # one delegate per nominated idea (its proposer), presenting it in round 0
        assert sorted(council.participants) == ["a", "c"] and council.level == 1
        assert [t["kind"] for t in council.transcript] == ["nomination", "nomination"]
        assert {i["id"]: i["from"] for i in council.ideas} == {"I1": "C1", "I2": "C2"}
        assert room.player_status("a")["meeting"]["level"] == 1
        await until(lambda: room.decisions)
        bots.cancel()

        d = room.decisions[0]
        assert d["scope"] == "room" and d["council"] == "K1.1" and d["crews"] == 2 and d["votes"] == 2
        # the other crew gets one explorer for it, and that's the member who wasn't at the council
        winner, other_crew = (d["idea"], "C2") if d["idea"] == "I1" else (d["idea"], "C1")
        loser = "I2" if winner == "I1" else "I1"
        moved = next(iter(d["moved"]))
        assert d["moved"] == {moved: winner} and room.crew_of[moved] == other_crew
        assert moved not in council.participants
        dirs = {x["id"]: x["agents"] for x in room.groups[other_crew].directions}
        assert dirs[winner] == [moved] and moved not in dirs[loser]
        assert room.assignments[moved] == winner
        assert any(e["type"] == "decision" for e in boxes["b"].events)
        assert not room._busy  # delegates are back with their crews

    asyncio.run(main())


def test_a_lone_nomination_waits_and_delegates_are_never_double_booked():
    async def main():
        room, _ = await setup(["a", "b", "c", "d"], rounds=1, nomination_ttl_sec=0.5)
        bots = asyncio.create_task(autoplay(room, {"a": "idea A"}, lambda n, o: o[0]["id"]))
        await room.report("a", "clue", "a's clue")
        await until(lambda: room.groups.get("K1.1") and room.groups["K1.1"].queue)
        await asyncio.sleep(0.3)
        assert room.groups["K1.1"].state == "working"  # one idea isn't a debate
        await until(lambda: not room.groups["K1.1"].queue)  # dropped after the ttl
        assert not room.decisions

        # while a is a council delegate, a's crew can still meet, without a
        room.groups["K1.1"].queue.append({"idea": {"id": "I1", "by": "a", "text": "x"}, "from": "C1",
                                          "delegates": ["a"], "votes": 1, "voters": 2, "ts": 1e18})
        room.groups["K1.1"].queue.append({"idea": {"id": "I9", "by": "c", "text": "y"}, "from": "C2",
                                          "delegates": ["c"], "votes": 1, "voters": 2, "ts": 1e18})
        room.task.meeting.nomination_ttl_sec = 1e9
        room._schedule_drain(room.groups["K1.1"])
        await until(lambda: room.groups["K1.1"].meeting)
        bots.cancel()
        await room.report("b", "clue", "while a is away")
        assert room.groups["C1"].meeting.participants == ["b"]
        assert room._busy == {"a": "K1.1", "c": "K1.1", "b": "C1"}

    asyncio.run(main())


def test_crews_survive_a_restart():
    async def main():
        room, _ = await setup(["a", "b", "c"])
        room.groups["C2"].directions = [{"id": "I3", "by": "c", "text": "t", "votes": 1, "agents": ["c"]}]
        room.decisions.append({"idea": "I3", "text": "t"})
        restored = Room.from_state(room.to_state())
        assert restored.crew_of == {"a": "C1", "b": "C1", "c": "C2"}
        assert restored.groups["C2"].directions[0]["id"] == "I3" and restored.decisions[0]["idea"] == "I3"

        legacy = room.to_state()  # a room saved before crews existed is one crew
        for k in ("groups", "crew_of", "decisions"):
            del legacy[k]
        legacy.update(directions=[], last_meeting=None, last_meeting_end=0, queue=[])
        old = Room.from_state(legacy)
        assert old.crew_of == {"a": "C1", "b": "C1", "c": "C1"} and old.groups["C1"].members == ["a", "b", "c"]

    asyncio.run(main())


def test_bridge_prompts_for_a_council_delegate(tmp_path, monkeypatch):
    from amongagents import bridge

    async def main():
        room = Room("R", make_task(crew_size=2, council_size=2, rounds=1))
        sess = bridge.Session("ws://x", "R", "a", tmp_path, trust=True)

        async def ensure():
            pass

        async def request(op, **kw):
            return room.snapshot(viewer="a") if op == "snapshot" else []

        sess.ensure, sess.request = ensure, request
        monkeypatch.setattr(bridge, "S", sess, raising=False)

        async def to_bridge(ev):
            sess._on_event(ev)
            sess.wake.set()

        sess._apply_welcome(await room.join("a", to_bridge))
        for n in "bcd":
            await room.join(n, Inbox().send)
        assert "YOUR CREW: C1 (one of 2 crews)" in await bridge.join_room()
        bots = asyncio.create_task(autoplay(room, {"a": "idea A", "c": "idea C"}, agree_on_first,
                                            skip=lambda g, n: g.level > 0 and n == "a"))
        await room.report("a", "clue", "a's clue", propose="idea A")
        await room.report("c", "clue", "c's clue", propose="idea C")
        await until(lambda: room.groups.get("K1.1") and room.groups["K1.1"].meeting
                    and room.groups["K1.1"].meeting.turn == "a")
        out = await bridge.wait(2)
        assert "YOUR TURN" in out and "This is COUNCIL K1.1" in out
        assert "C1 picked I1" in out and "YOUR CREW’S DIRECTIONS" in out
        await room.say("a", "I1 is backed by evidence")
        sess.my_turn = False
        await until(lambda: room.groups["K1.1"].state == "voting")
        out = await bridge.wait(2)
        assert "COUNCIL VOTE" in out and "whichever crew it came from" in out and "work_on" in out
        await room.vote("a", "I1")  # no own-crew ban in councils
        sess.voted = True
        await until(lambda: room.decisions)
        out = await bridge.wait(2)
        assert "Council meeting over. It picked I1: idea A" in out and "Back to your crew's work" in out
        assert "🏛 Council K1.1 picked I1 (room-wide)" in out
        status_decisions = bridge._fmt_decisions(sess.decisions)
        assert "I1: idea A  [room-wide, from C1, 2/2 delegate votes, 1 new explorer]" in status_decisions
        bots.cancel()

    asyncio.run(main())


def test_a_council_can_agree_on_a_synthesis_and_small_crews_still_follow_it():
    """From the live run: two delegates agreed on an idea, but an own-idea rule split the vote 1-1,
    and in crews of 2 (one direction each) nobody moved."""
    async def main():
        room, _ = await setup(["a", "b", "c", "d"], rounds=1)
        for crew, (x, y) in (("C1", "ab"), ("C2", "cd")):
            room.groups[crew].directions = [{"id": f"D{x}", "by": x, "text": x, "votes": 1, "agents": [x]},
                                            {"id": f"D{y}", "by": y, "text": y, "votes": 1, "agents": [y]}]
            room.assignments.update({x: f"D{x}", y: f"D{y}"})
        k = room._parent(room.groups["C1"])
        k.queue = [{"idea": {"id": "I1", "by": "a", "text": "a's"}, "from": "C1", "delegates": ["a"], "votes": 1,
                    "voters": 2, "ts": 1e18},
                   {"idea": {"id": "I2", "by": "c", "text": "c's"}, "from": "C2", "delegates": ["c"], "votes": 1,
                    "voters": 2, "ts": 1e18}]
        room._idea_seq = 2
        room._schedule_drain(k)
        await until(lambda: k.meeting and k.meeting.turn == "a")
        await room.say("a", "let's do something else", propose="synthesis")  # I3
        await until(lambda: k.meeting.turn == "c")
        await room.say("c", "agreed, the synthesis")
        await until(lambda: k.state == "voting")
        await room.vote("a", "I3")  # a's own idea
        await room.vote("c", "I3")
        await until(lambda: room.decisions)
        d = room.decisions[0]
        assert d["idea"] == "I3" and d["votes"] == 2
        # each 2-agent crew gives its lowest-ranked direction's explorer to the pick
        assert d["moved"] == {"b": "I3", "d": "I3"}
        assert [x["id"] for x in room.groups["C1"].directions] == ["Da", "I3"]
        assert [x["id"] for x in room.groups["C2"].directions] == ["Dc", "I3"]

    asyncio.run(main())


def test_a_split_council_decides_nothing_and_each_crew_has_one_nomination():
    """From the live run: a 1-1 tie went to the first idea listed, a dead end that two delegates of
    the same crew had both argued against."""
    async def main():
        room, _ = await setup(["a", "b", "c", "d"], rounds=1)
        k = room._parent(room.groups["C1"])
        src = {g: room.groups[g] for g in ("C1", "C2")}
        m = type("M", (), {"findings": [], "participants": ["x", "y"]})()
        room._nominate(k, {"id": "I1", "by": "a", "text": "old pick", "votes": 1}, src["C1"], m, ["a"])
        room._nominate(k, {"id": "I2", "by": "b", "text": "newer pick", "votes": 2}, src["C1"], m, ["b"])
        assert [(n["from"], n["idea"]["id"]) for n in k.queue] == [("C1", "I2")]  # replaced, not added
        room._nominate(k, {"id": "I3", "by": "c", "text": "c's", "votes": 1}, src["C2"], m, ["c"])
        await until(lambda: k.meeting)
        assert sorted(k.meeting.participants) == ["b", "c"]
        bots = asyncio.create_task(autoplay(room, {}, lambda n, o: "I3" if n == "b" else "I2"))
        await until(lambda: k.last_meeting)
        bots.cancel()
        r = k.last_meeting["result"]
        assert r["split"] and r["decision"] is None and not room.decisions
        assert room._cooldown_left(k) > 200  # councils rest for council_wait_sec

    asyncio.run(main())


def test_a_council_pick_needs_a_majority_of_its_delegates():
    """From a live run: one delegate of two didn't vote, and the other's lone vote moved the room."""
    async def main():
        room, _ = await setup(["a", "b", "c", "d"], rounds=1, vote_timeout_sec=0.3)
        k = room._parent(room.groups["C1"])
        m = type("M", (), {"findings": [], "participants": ["x", "y"]})()
        room._nominate(k, {"id": "I1", "by": "a", "text": "a's", "votes": 1}, room.groups["C1"], m, ["a"])
        room._nominate(k, {"id": "I2", "by": "c", "text": "c's", "votes": 1}, room.groups["C2"], m, ["c"])
        await until(lambda: k.meeting)
        bots = asyncio.create_task(autoplay(room, {}, lambda n, o: "I1",
                                            skip=lambda g, n: g.state == "voting" and n == "c"))
        await until(lambda: k.last_meeting)
        bots.cancel()
        r = k.last_meeting["result"]
        assert r["tally"] == {"I1": 1} and r["abstain"] == 1
        assert r["split"] and r["decision"] is None and not room.decisions

    asyncio.run(main())
