"""End-to-end demo without LLMs: a real server plus scripted bots that drive real
MCP bridges over stdio, exactly like Claude Code or Codex would.

    python scripts/simulate.py            # run once, verify, exit
    python scripts/simulate.py --keep     # leave the server up so you can look at the UI

The bot "alice" writes a 2-opt solver and beats the baseline (which becomes the shared
baseline at once), then calls a meeting. Everyone speaks in order for two rounds,
proposes ideas, and votes; the voted ideas become directions, and the crew splits up
to explore them in parallel.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from amongagents.tasks import TaskSpec  # noqa: E402

TWO_OPT = '''"""Nearest neighbor + time-boxed 2-opt."""
import math, time

def solve(points):
    t0 = time.perf_counter()
    n = len(points)
    d = lambda a, b: math.dist(points[a], points[b])
    unvisited = set(range(1, n)); tour = [0]
    while unvisited:
        last = tour[-1]
        nxt = min(unvisited, key=lambda j: d(last, j)); unvisited.remove(nxt); tour.append(nxt)
    improved = True
    while improved and time.perf_counter() - t0 < 1.2:
        improved = False
        for i in range(1, n - 1):
            a, b = tour[i - 1], tour[i]
            for j in range(i + 1, n):
                c, e = tour[j], tour[(j + 1) % n]
                if d(a, c) + d(b, e) < d(a, b) + d(c, e) - 1e-12:
                    tour[i:j + 1] = reversed(tour[i:j + 1]); improved = True
                    a, b = tour[i - 1], tour[i]
            if time.perf_counter() - t0 > 1.2:
                break
    return tour
'''


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def http(method, url, body=None):
    req = urllib.request.Request(url, method=method, data=json.dumps(body).encode() if body else None,
                                 headers={"content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.load(r)


async def bot(name: str, room: str, port: int, ws_dir: Path, transcript_log: list, plan: dict) -> dict:
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "amongagents.bridge", "--server", f"ws://127.0.0.1:{port}", "--room", room,
              "--name", name, "--workspace", str(ws_dir), "--trust-task"],
        cwd=str(ROOT),
    )
    async with stdio_client(params) as (r, w), ClientSession(r, w) as s:
        await s.initialize()

        async def call(tool, **args) -> str:
            print(f"  {name} -> {tool}", flush=True)
            res = await s.call_tool(tool, args)
            text = "".join(c.text for c in res.content if hasattr(c, "text"))
            if res.isError:
                raise RuntimeError(f"{name}.{tool}: {text}")
            return text

        info = await call("join_room")
        assert "EDITABLE FILES: solver.py" in info, info
        out = await call("run_eval")
        assert "OK: mean_tour_length" in out, out
        await plan["ready"].wait()  # everyone has joined

        if plan.get("improve"):
            (ws_dir / "solver.py").write_text(TWO_OPT)
            (ws_dir / "eval.py").write_text("print('score: 0.0001')  # cheating attempt")
            out = await call("run_eval", description="time-boxed 2-opt after nearest neighbor")
            assert "on exp/free/alice" in out, out
            assert "BEATS the shared best" in out, out  # eval.py was restored, so the score is honest
            await call("report_finding", kind="improvement",
                       summary="Added time-boxed 2-opt on top of nearest neighbor; ~16% shorter tours, under 1.3s.",
                       propose=plan["idea"])

        spoke, voted = 0, False
        prompts = {}
        while not voted or "Meeting over" not in out:
            out = await call("wait", timeout_sec=20)
            if "No meeting in progress" in out:
                await asyncio.sleep(0.3)
            if "YOUR TURN" in out:
                prompts.setdefault("turn", out)
                spoke += 1
                transcript_log.append((name, out.split("round ")[1].split("/")[0]))
                propose = plan["idea"] if spoke == 1 and not plan.get("improve") else None
                await call("say", text=f"{name} (turn {spoke}): 2-opt helps; the next bottleneck is speed.",
                           propose=propose)
            elif "VOTING" in out:
                prompts.setdefault("vote", out)
                ideas = dict((who, iid) for iid, who in re.findall(r"(I\d+) \((\w+)\)", out))
                await call("vote", idea=ideas[plan["vote_for"]], reason=f"{plan['vote_for']}'s idea is the most promising")
                voted = True
        meeting_over = out
        synced = await call("sync_baseline")
        hist = await call("history")
        diff = await call("history", version=1)
        status = await call("status")
        extra = {}
        if name == "carol":
            exps = await call("experiments")
            best = re.search(r"([0-9a-f]{8})\s+15\.8591\s+alice", exps)
            assert best, exps
            await call("checkout", ref="v0")
            extra["v0_solver"] = (ws_dir / "solver.py").read_text()
            await call("checkout", ref=best.group(1))
            extra["best_solver"] = (ws_dir / "solver.py").read_text()
            extra["exps"] = exps
        return {"name": name, "spoke": spoke, "synced": synced, "status": status, "meeting_over": meeting_over, "prompts": prompts, "history": hist, "diff": diff, **extra,
                "solver": (ws_dir / "solver.py").read_text()}


async def main(keep: bool) -> None:
    port = free_port()
    tmp = Path(tempfile.mkdtemp(prefix="amongagents-sim-"))
    server = subprocess.Popen([sys.executable, "-m", "uvicorn", "amongagents.server:app", "--port", str(port),
                               "--log-level", "warning"], cwd=ROOT,
                              env={**os.environ, "AMONG_DATA": str(tmp / "data")})
    try:
        for _ in range(50):
            try:
                http("GET", f"http://127.0.0.1:{port}/api/rooms")
                break
            except Exception:
                time.sleep(0.2)
        task = TaskSpec.from_dir(ROOT / "tasks" / "tsp").to_dict()
        task["meeting"].update(rounds=2, turn_timeout_sec=30, vote_timeout_sec=30, cooldown_sec=0)
        room = http("POST", f"http://127.0.0.1:{port}/api/rooms", {"task": task})["room"]
        print(f"room {room}; watch at http://127.0.0.1:{port}/?room={room}")

        order: list = []
        names = ["alice", "bob", "carol"]
        results = []
        tasks = []
        ready = asyncio.Barrier(len(names))
        plans = {
            "alice": {"improve": True, "idea": "Add Or-opt moves after 2-opt", "vote_for": "bob"},
            "bob": {"idea": "Use neighbor lists so 2-opt converges within the time limit", "vote_for": "carol"},
            "carol": {"idea": "Random restarts with the leftover time", "vote_for": "bob"},
        }
        for n in names:  # join in a fixed order: that's the speaking order
            tasks.append(asyncio.create_task(bot(n, room, port, tmp / n, order, {**plans[n], "ready": ready})))
            await asyncio.sleep(1.5)
        results = await asyncio.gather(*tasks)

        snap = http("GET", f"http://127.0.0.1:{port}/api/rooms/{room}")
        print("speaking order:", order)
        assert order == [("alice", "1"), ("bob", "1"), ("carol", "1"), ("alice", "2"), ("bob", "2"), ("carol", "2")], order
        assert snap["baseline_version"] == 1 and snap["best_score"] < 18.0, snap["best_score"]
        assert all(r["solver"] == TWO_OPT for r in results), "bob/carol didn't receive the new baseline"
        # ideas: I1 alice, I2 bob, I3 carol; votes I2, I3, I2 -> I2 (alice, carol) and I3 (bob); I1 dropped
        assert [(d["id"], d["agents"]) for d in snap["directions"]] == [("I2", ["alice", "carol"]), ("I3", ["bob"])], snap["directions"]
        for r, mine in zip(results, ["I2", "I3", "I2"]):
            assert f"YOUR DIRECTION: {mine}" in r["meeting_over"], r["meeting_over"]
        print(f"✅ new shared best {snap['best_score']:.4f} (was 18.8454), all workspaces synced")
        for r in results:  # meeting prompts carry the evidence, so agents don't have to ask for it
            for kind in ("turn", "vote"):
                assert "WHAT THE CREW HAS TRIED" in r["prompts"][kind] and "15.8591" in r["prompts"][kind], kind
        print("✅ every turn and vote prompt showed the crew's committed runs, e.g.:")
        digest = results[1]["prompts"]["vote"].split("WHAT THE CREW HAS TRIED")[1].split("\n\n")[0]
        print("WHAT THE CREW HAS TRIED" + digest[:700])
        assert "v1: 15.8591 by alice" in results[1]["history"] and "v0: starting point" in results[1]["history"]
        assert "+def solve" in results[1]["diff"] or "2-opt" in results[1]["diff"], results[1]["diff"][:500]
        log = http("GET", f"http://127.0.0.1:{port}/api/rooms/{room}/history")
        tsv = urllib.request.urlopen(f"http://127.0.0.1:{port}/api/rooms/{room}/results.tsv").read().decode()
        print(f"✅ git history: {[c['title'] for c in log]}; results.tsv has {len(tsv.splitlines()) - 1} evals")
        hold = snap["findings"][0]["holdout"]
        assert hold and hold["candidate"] < hold["baseline"] and "Holdout check (seed" in log[0]["body"], (hold, log[0])
        print(f"✅ improvement passed the hidden-instance check: {hold['candidate']:.4f} vs baseline "
              f"{hold['baseline']:.4f} on secret seed {hold['seed']}")
        carol = results[2]
        starter = TaskSpec.from_dir(ROOT / "tasks" / "tsp").files["solver.py"]
        assert carol["v0_solver"] == starter and carol["best_solver"] == TWO_OPT
        table = carol["exps"].split("\n\n---")[0].splitlines()
        assert len(table) == 5, carol["exps"]  # header + one committed run per eval (4 evals)
        assert "2-opt after nearest neighbor" in tsv
        print("✅ every eval committed on its experiment branch; carol checked out v0 and alice's best run:")
        print("\n".join(table))
        for d in snap["directions"]:
            print(f"✅ direction {d['id']} ({d['votes']} votes): {d['text']!r} -> explored by {', '.join(d['agents'])}")
        print("\n--- what alice sees after the meeting ---\n" + results[0]["meeting_over"])
        print("\n--- bob's status ---\n" + results[1]["status"])
        if keep:
            print(f"\nServer still running: http://127.0.0.1:{port}/?room={room}  (Ctrl+C to stop)")
            while True:
                await asyncio.sleep(3600)
    finally:
        server.terminate()
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # Windows consoles may not be UTF-8
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", action="store_true")
    try:
        asyncio.run(main(ap.parse_args().keep))
    except KeyboardInterrupt:
        pass
