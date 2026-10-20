import asyncio
import time
from types import SimpleNamespace

import pytest

from amongagents.game import GameError

from test_game import play_meeting, setup


async def meeting(room, reporter, ideas, votes):
    """One crew meeting: `reporter` reports a clue, ideas[player] are proposed, then votes[player] =
    dict(idea=..., work_on=..., long_bet=...) are cast."""
    await room.report(reporter, "clue", "x", propose=ideas.pop(reporter, None))
    task = asyncio.create_task(play_meeting(room, ideas=ideas))
    while room.state != "voting":
        await asyncio.sleep(0.01)
    for n, v in votes.items():
        await room.vote(n, v["idea"], work_on=v.get("work_on"), long_bet=v.get("long_bet"))
    await task


def test_a_long_bet_survives_later_crew_votes():
    async def main():
        room, _ = await setup(["a", "b", "c"], rounds=1)
        await meeting(room, "a", {"a": "slow exact solver", "b": "tune kicks"}, {  # I1, I2
            "a": dict(idea="I2", work_on="I1", long_bet=True),
            "b": dict(idea="I1", work_on="I2"),
            "c": dict(idea="I1", work_on="I2"),
        })
        assert room.assignments == {"a": "I1", "b": "I2", "c": "I2"}
        bet = next(d for d in room.directions if d["id"] == "I1")
        assert bet["agents"] == ["a"] and bet["long_bet"]["by"] == "a"

        # everyone, a included, backs c's new idea: a still keeps the bet, and nobody joins it
        await meeting(room, "c", {"c": "new hot idea"}, {  # I3
            "a": dict(idea="I3", work_on="I3"),
            "b": dict(idea="I3", work_on="I1"),
            "c": dict(idea="I1", work_on="I3"),
        })
        assert room.assignments["a"] == "I1" and room.assignments["c"] == "I3"
        assert next(d for d in room.directions if d["id"] == "I1")["agents"] == ["a"]

        # one bet per crew, and a holder can't start a second one
        await room.report("b", "clue", "y", propose="another long shot")  # I4
        task = asyncio.create_task(play_meeting(room))
        while room.state != "voting":
            await asyncio.sleep(0.01)
        with pytest.raises(GameError, match="slots are taken"):
            await room.vote("b", "I1", work_on="I4", long_bet=True)
        with pytest.raises(GameError, match="already hold a long bet on I1"):
            await room.vote("a", "I4", work_on="I4", long_bet=True)
        await room.vote("a", "I4", work_on="I4", long_bet=False)  # a ends it early...
        await room.vote("b", "I1", work_on="I4")
        await room.vote("c", "I4", work_on="I3")
        await task
        assert room.assignments["a"] == "I4"  # ... and follows the vote again
        assert not room._long_bets(room.groups["C1"])

    asyncio.run(main())


def test_long_bets_need_three_agents_and_expire():
    async def main():
        room, _ = await setup(["a", "b"], rounds=1)
        await room.report("a", "clue", "x", propose="slow idea")
        task = asyncio.create_task(play_meeting(room, ideas={"b": "other"}))
        while room.state != "voting":
            await asyncio.sleep(0.01)
        with pytest.raises(GameError, match="3\\+ online"):
            await room.vote("a", "I2", work_on="I1", long_bet=True)
        await room.vote("a", "I2", work_on="I1")
        await room.vote("b", "I1", work_on="I2")
        await task

        room, _ = await setup(["a", "b", "c"], rounds=1)
        c1 = room.groups["C1"]
        c1.directions = [{"id": "I1", "by": "a", "text": "bet", "votes": 1, "agents": ["a"],
                          "long_bet": {"by": "a", "until": time.time() - 1}},  # expired
                         {"id": "I2", "by": "b", "text": "y", "votes": 2, "agents": ["b", "c"]}]
        room.assignments = {"a": "I1", "b": "I2", "c": "I2"}
        room._idea_seq = 2
        assert not room._long_bets(c1)
        await meeting(room, "c", {"c": "z"}, {"a": dict(idea="I3", work_on="I3"),
                                              "b": dict(idea="I3", work_on="I2"),
                                              "c": dict(idea="I2", work_on="I3")})
        assert room.assignments["a"] == "I3"  # an expired bet is an ordinary direction again

    asyncio.run(main())


def test_councils_never_move_a_long_bet_holder():
    async def main():
        room, _ = await setup(["a", "b", "c"], rounds=1)
        c1 = room.groups["C1"]
        until = time.time() + 600
        # everyone leads their own direction, and the bet is the crew's last-ranked one: without the
        # protection, the council's pick would replace it
        c1.directions = [{"id": "I2", "by": "b", "text": "y", "votes": 2, "agents": ["b"]},
                         {"id": "I3", "by": "c", "text": "z", "votes": 1, "agents": ["c"]},
                         {"id": "I1", "by": "a", "text": "bet", "votes": 0, "agents": ["a"],
                          "long_bet": {"by": "a", "until": until}}]
        room.assignments = {"a": "I1", "b": "I2", "c": "I3"}
        moved, _ = room._spread({"id": "I9", "by": "x", "text": "council pick"}, [c1],
                                SimpleNamespace(id="K1", level=1))
        assert moved == {"c": "I9"} and room.assignments["a"] == "I1"
        assert [d["id"] for d in c1.directions] == ["I2", "I1", "I9"]

        # with a crowded direction, the explorer comes from there, never the holder
        c1.directions = [{"id": "I2", "by": "b", "text": "y", "votes": 2, "agents": ["b", "c"]},
                         {"id": "I1", "by": "a", "text": "bet", "votes": 0, "agents": ["a"],
                          "long_bet": {"by": "a", "until": until}}]
        room.assignments = {"a": "I1", "b": "I2", "c": "I2"}
        moved, _ = room._spread({"id": "I8", "by": "x", "text": "another pick"}, [c1],
                                SimpleNamespace(id="K1", level=1))
        assert "a" not in moved and len(moved) == 1

    asyncio.run(main())
