"""Scale test without LLMs: N scripted bots play one room in-process, on a compressed clock.

    python scripts/scale_sim.py                      # 1000 bots, crews of 8, 90 s
    python scripts/scale_sim.py --players 200 --seconds 60
    python scripts/scale_sim.py --crew-size 1000     # the old design: everyone in one meeting
    python scripts/scale_sim.py --serve 8801         # and watch it at http://127.0.0.1:8801/?room=SCALE

Bots run evals every 0.5-2 s, sometimes beat the best or find a clue, speak 0.2-0.8 s into
their turn, and vote at random in crews (never for their own idea); council delegates
back the first nominee 70% of the time. One simulated second stands for
about a minute of real agent time. Prints how long meetings take, how many run at once, how
much of their time agents spend in meetings, and how fast a crew's idea becomes room-wide.
"""

from __future__ import annotations

import argparse
import asyncio
import random
import statistics
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from amongagents.game import GameError, Room  # noqa: E402
from amongagents.tasks import MeetingSettings, TaskSpec  # noqa: E402


def make_room(a) -> Room:
    task = TaskSpec(
        name="scale", description="", program="", metric_name="score", direction="minimize",
        metric_regex=r"score:\s*([0-9.]+)", eval_command="true", editable=["solver.py"],
        files={"solver.py": "v0"}, baseline_score=100.0,
        meeting=MeetingSettings(rounds=2, turn_timeout_sec=3, vote_timeout_sec=3, cooldown_sec=1,
                                stagnation_evals=0, stagnation_sec=0, crew_size=a.crew_size,
                                council_size=a.council_size, council_wait_sec=5,
                                nomination_ttl_sec=60, fork_every_sec=15),
    )
    return Room("SCALE", task)


class Bot:
    def __init__(self, room: Room, name: str, rng: random.Random):
        self.room, self.name, self.rng = room, name, rng
        self.events = 0
        self.types = Counter()
        self.busy_time = 0.0
        self.evals = 0

    async def send(self, ev: dict) -> None:
        self.events += 1
        self.types[ev["type"]] += 1
        t = ev["type"]
        if t == "turn" and ev["player"] == self.name:
            asyncio.create_task(self._say(ev["meeting_id"], ev["round"]))
        elif t == "voting_started":
            asyncio.create_task(self._vote(ev["meeting_id"], ev["ideas"] + ev["carried"]))

    async def _say(self, mid: str, round_: int) -> None:
        await asyncio.sleep(self.rng.uniform(0.2, 0.8))
        propose = f"{self.name}'s idea" if round_ == 1 and self.rng.random() < 0.4 else None
        try:
            await self.room.say(self.name, "thoughts", propose)
        except GameError:
            pass  # timed out meanwhile

    async def _vote(self, mid: str, options: list[dict]) -> None:
        await asyncio.sleep(self.rng.uniform(0.1, 0.6))
        if not options:
            return
        if any(i.get("nominated") for i in options):  # councils: delegates mostly converge, as real ones did
            pick = options[0] if self.rng.random() < 0.7 else self.rng.choice(options)
        else:
            pick = self.rng.choice([i for i in options if i["by"] != self.name] or options)
        try:
            await self.room.vote(self.name, pick["id"])
        except GameError:
            pass

    async def work(self, until: float) -> None:
        await asyncio.sleep(self.rng.uniform(0, 2))
        while time.monotonic() < until:
            if self.name in self.room._busy:  # in a meeting: agents stop working
                t0 = time.monotonic()
                while self.name in self.room._busy and time.monotonic() < until:
                    await asyncio.sleep(0.1)
                self.busy_time += time.monotonic() - t0
                continue
            await asyncio.sleep(self.rng.uniform(0.5, 2.0))  # one experiment
            best = self.room.baseline(self.name)["score"]  # its crew's own line
            r = self.rng.random()
            score = best * (0.995 if r < 0.0015 else 1 + abs(self.rng.gauss(0.02, 0.02)))
            try:
                self.evals += 1
                await self.room.record_eval(self.name, score, True)
                if r < 0.0015 and self.room._beats(score, self.room.baseline(self.name)["score"]):
                    await self.room.report(self.name, "improvement", "better", score, {"solver.py": "x"},
                                           propose=f"{self.name}'s follow-up")
                elif r > 0.997:
                    await self.room.report(self.name, "clue", "a dead end", propose=f"{self.name}'s pivot")
            except GameError:
                pass  # e.g. someone else improved first


async def main(a) -> None:
    rng = random.Random(a.seed)
    room = make_room(a)
    started, ended = {}, []
    live, peak = set(), Counter()
    proposed_at: dict[str, float] = {}

    async def spectator(ev: dict) -> None:
        t = ev["type"]
        if t == "meeting_started":
            m = ev["meeting"]
            started[m["id"]] = (time.monotonic(), m["level"], len(m["participants"]))
            live.add(m["id"])
            levels = Counter(started[x][1] for x in live)
            for lvl, n in levels.items():
                peak[lvl] = max(peak[lvl], n)
            peak["all"] = max(peak["all"], len(live))
            for i in m["ideas"]:
                proposed_at.setdefault(i["id"], time.time())
        elif t == "said" and ev.get("idea"):
            proposed_at.setdefault(ev["idea"]["id"], time.time())
        elif t == "meeting_ended":
            m = ev["meeting"]
            t0, level, n = started[m["id"]]
            live.discard(m["id"])
            ended.append({"level": level, "secs": time.monotonic() - t0, "participants": n,
                          "turns": sum(1 for x in m["transcript"] if x["round"] > 0)})

    room.spectators.add(spectator)
    server = None
    if a.serve:  # host the room on the real server, so the web UI can watch it
        import os

        import uvicorn

        os.environ["AMONG_DATA"] = "none"  # don't load or save any real rooms

        from amongagents import server as web
        web.rooms[room.id] = room
        server = uvicorn.Server(uvicorn.Config(web.app, port=a.serve, log_level="warning"))
        serving = asyncio.create_task(server.serve())
        print(f"watch: http://127.0.0.1:{a.serve}/?room={room.id}", flush=True)
    bots = []
    for i in range(a.players):
        b = Bot(room, f"p{i:04d}", random.Random(rng.random()))
        bots.append(b)
        await room.join(b.name, b.send)
    print(f"{a.players} players -> {len(room._crews())} crews of up to {a.crew_size}; "
          f"councils of up to {a.council_size}. Running {a.seconds:.0f} s...", flush=True)
    t0 = time.monotonic()
    until = t0 + a.seconds
    await asyncio.gather(*(b.work(until) for b in bots))
    wall = time.monotonic() - t0

    def stats(xs):
        return f"n={len(xs)}, median {statistics.median(xs):.1f} s, max {max(xs):.1f} s" if xs else "none"

    print(f"\nsimulated {wall:.0f} s (1 s ~ 1 min of real agent time)")
    print(f"evals: {sum(b.evals for b in bots)}, shared best {room.best_score:.3f} (v{room.baseline_version})")
    for level in sorted({e["level"] for e in ended}):
        xs = [e for e in ended if e["level"] == level]
        what = "crew meetings" if level == 0 else f"level-{level} councils"
        print(f"{what}: {stats([e['secs'] for e in xs])}; "
              f"{statistics.mean(e['participants'] for e in xs):.1f} participants, "
              f"{statistics.mean(e['turns'] for e in xs):.1f} turns on average; at most {peak[level]} at once")
    unfinished = [m for m in live]
    print(f"meetings running at the same time: up to {peak['all']}; {len(unfinished)} still open at the end")
    busy = [b.busy_time / wall for b in bots]
    print(f"time agents spent in meetings: median {statistics.median(busy):.0%}, max {max(busy):.0%}")
    ev = [b.events / wall for b in bots]  # per simulated second, i.e. per real minute
    print(f"events per agent per (real) minute: median {statistics.median(ev):.1f}, max {max(ev):.1f}")
    print("  by type:", dict(sum((b.types for b in bots), Counter()).most_common()))
    room_wide = [d for d in room.decisions if d["scope"] == "room"]
    by_level = Counter(d["level"] for d in room.decisions)
    if room.forks:
        gain = [f["replaced"]["score"] - f["score"] for f in room.forks]
        print(f"forks: {len(room.forks)} crews took a stronger crew's code, "
              f"{len({round(f['ts']) for f in room.forks})} rounds; median gain {statistics.median(gain):.3f}")
    lines = sorted(room._line(c)["score"] for c in room._crews())
    print(f"crew lines: best {lines[0]:.3f}, median {statistics.median(lines):.3f}, worst {lines[-1]:.3f}")
    print(f"council decisions: {len(room.decisions)} " + str(dict(sorted(by_level.items())))
          + f", {len(room_wide)} room-wide")
    lag = [d["ts"] - proposed_at[d["idea"]] for d in room_wide if d["idea"] in proposed_at]
    if lag:
        print(f"idea proposed in a crew -> room-wide decision: {stats(lag)}")
    moved = [len(d["moved"]) for d in room_wide]
    if moved:
        print(f"agents switched per room-wide decision: median {statistics.median(moved):.0f}")
    dirs = Counter(len(c.directions) for c in room._crews())
    print(f"directions per crew: {dict(sorted(dirs.items()))}")
    spread = defaultdict(int)
    for c in room._crews():
        for d in c.directions:
            spread[d["id"]] += 1
    widest = sorted(spread.items(), key=lambda kv: -kv[1])[:3]
    print("most widely explored ideas (crews): " + ", ".join(f"{k} in {v}" for k, v in widest))
    if server:
        server.should_exit = True
        await serving
    await room.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--players", type=int, default=1000)
    ap.add_argument("--crew-size", type=int, default=8)
    ap.add_argument("--council-size", type=int, default=8)
    ap.add_argument("--seconds", type=float, default=90)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--serve", type=int, metavar="PORT", help="also serve the web UI on this port")
    asyncio.run(main(ap.parse_args()))
