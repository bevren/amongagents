import asyncio
import time

from amongagents.game import Room

from test_game import Inbox, make_task


def test_polling_wait_blocks_until_something_happens(tmp_path, monkeypatch):
    """Agents use `wait` as a timer: a repeat call blocks instead of costing a model turn every 2 s."""
    from amongagents import bridge

    async def main():
        room = Room("R", make_task(rounds=1, cooldown_sec=0))
        sess = bridge.Session("ws://x", "R", "a", tmp_path, trust=True)

        async def ensure():
            pass

        sess.ensure = ensure
        monkeypatch.setattr(bridge, "S", sess, raising=False)

        async def to_bridge(ev):
            sess._on_event(ev)
            sess.wake.set()

        sess._apply_welcome(await room.join("a", to_bridge))
        await room.join("b", Inbox().send)

        t = time.monotonic()
        assert (await bridge.wait(1)).startswith("No meeting in progress. Continue")  # the first call answers at once
        assert time.monotonic() - t < 0.5
        t = time.monotonic()
        out = await bridge.wait(1)  # right after: it blocks for the whole timeout
        assert time.monotonic() - t >= 0.9 and "No meeting in progress (waited 1s" in out

        t = time.monotonic()
        waiting = asyncio.create_task(bridge.wait(5))
        await asyncio.sleep(0.2)
        await room.report("b", "clue", "a dead end")  # a meeting starts: the blocked wait answers
        out = await waiting
        assert time.monotonic() - t < 3 and "YOUR TURN" in out

        sess.last_idle_wait = float("-inf")  # after real work, a wait answers at once again
        await room.close()

    asyncio.run(main())
