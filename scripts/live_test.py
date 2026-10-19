"""Live test with real LLM agents: a local server plus N headless Claude Code players.

    python scripts/live_test.py                          # 3 Haiku agents on tasks/tsp for 15 minutes
    python scripts/live_test.py --minutes 5 --agents 2 --model claude-haiku-5-5 --task tasks/tsp

Each agent gets an empty workspace under runs/<timestamp>/ws/<name>, the MCP bridge,
the meeting hook, and permissions for the game tools, file read/edit, and `python` commands
(their own code on their own machine, for side experiments like other instance sizes; other
shell commands except read-only ones are denied). Only `run_eval` results are scored and shared.
This spends real usage. Defaults to Haiku. An agent whose session ends is restarted
with --continue until the deadline (agents often end a turn on their own). Crashed sessions
(nonzero exit) count against --max-sessions, and nothing restarts after a usage-limit or
rate-limit message.

With --flush reassign (and/or --flush-every-evals N), an agent's session is restarted fresh,
without --continue, when a meeting or council moves it to a new direction (or every N of its
evals): the chat context is dropped and the workspace files are kept, as in fork-and-flush
(arXiv 2610.07447), where carried-over context kept runs stuck in one "idea basin". The fresh
session rejoins and gets its brief from `join_room` (direction, decisions, notes, what the crew
tried). Flushes wait until the agent is out of meetings, and don't count against --max-sessions.

Watch live at the printed URL. Afterwards, runs/<timestamp>/ holds the per-agent logs,
hook logs, and final_snapshot.json.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from amongagents.tasks import TaskSpec  # noqa: E402

NAMES = ["alice", "bob", "carol", "dave", "erin", "frank"]
# only the CLI's own quota messages; an agent's summary may say "time limit" and must not stop it
LIMIT_MARKERS = re.compile(r"hit your (session|usage|weekly) limit|rate limit|overloaded_error|"
                           r"credit balance is too low|quota exceeded", re.I)

PROMPT = """You are {name}, a player in a live AmongAgents research room with other AI agents.
Start by calling the amongagents `join_room` tool and follow the rules it returns exactly.
Work only on the files in the current directory. Run experiments with the `run_eval` tool.
Besides `run_eval` you may run `python ...` commands in this directory for side experiments (other instance sizes,
timing, a quick script); only `run_eval` results are scored and shared, so confirm anything promising with it. An
experiment you can't run is never a reason to stop: pick the next most promising idea.
This is a long-running game: never finish or stop on your own: there is no finish line, and a plateau means
try something fundamentally different. Keep looping
(work -> run_eval -> report_finding when you find something -> wait/say/vote in meetings -> sync_baseline -> back to work)."""

FRESH = """You are {name}, a player in a live AmongAgents research room with other AI agents. You were already
playing: this session starts fresh on purpose (your earlier conversation was cleared, your workspace files are kept),
so judge your direction with fresh eyes instead of continuing old habits. Call the amongagents `join_room` tool: it
shows the rules, your assigned direction, room-wide decisions, shared notes and what your crew has tried. Then call
`wait` in case a meeting is running, and work on your direction. Work only on the files in the current directory and
run experiments with `run_eval`.
Besides `run_eval` you may run `python ...` commands in this directory for side experiments (other instance sizes,
timing, a quick script); only `run_eval` results are scored and shared, so confirm anything promising with it. An
experiment you can't run is never a reason to stop: pick the next most promising idea. There is no finish line: never stop on your own, and a plateau means try something
fundamentally different. Keep looping
(work -> run_eval -> report_finding when you find something -> wait/say/vote in meetings -> sync_baseline -> back to work)."""

CONTINUE = """Keep playing the AmongAgents game. Call the amongagents `wait` tool first (in case a meeting is running),
then `status`, then continue working on your assigned direction. Never stop on your own, and don't sit waiting for an
assignment: if your direction looks done or blocked, run the most promising untested variant of it (or of the notes'
open questions) and report what you find. You can run `python ...` in this directory for side experiments."""


def http(method: str, url: str, body: dict | None = None) -> dict:
    req = urllib.request.Request(url, method=method, data=json.dumps(body).encode() if body else None,
                                 headers={"content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.load(r)


def _results(log_path: Path):
    for line in log_path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if ev.get("type") == "result":
            yield ev


def last_result(log_path: Path) -> str | None:
    """Final message of the latest session, e.g. "You've hit your session limit"."""
    text = None
    for ev in _results(log_path):
        text = str(ev.get("result") or ev.get("subtype"))
    return text


# relative token prices (input = 1); the same ratios hold across Claude models
TOKEN_WEIGHTS = {"input_tokens": 1.0, "cache_creation_input_tokens": 1.25,
                 "cache_read_input_tokens": 0.1, "output_tokens": 5.0}


def _units(usage: dict[str, dict]) -> float:
    return sum(w * (u.get(k) or 0) for u in usage.values() for k, w in TOKEN_WEIGHTS.items())


def sessions(log_path: Path) -> dict[str, dict]:
    """Per session id: the reported cost (None if it never reported one), the token usage it
    covers, and the usage after its last report (a --continue run killed at the deadline), all
    in input-token units.

    A --continue session reports the running total of its conversation, so take the max per
    session id instead of summing every result."""
    out: dict[str, dict] = {}
    calls: dict[str, dict[str, dict]] = {}  # usage since the session's last result, by message id
    for line in log_path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        sid = ev.get("session_id")
        if not sid:
            continue
        s = out.setdefault(sid, {"cost": None, "units": 0.0, "tail": 0.0})
        if ev.get("type") == "assistant":  # one event per content block; dedupe by message id
            msg = ev.get("message") or {}
            calls.setdefault(sid, {})[msg.get("id", "?")] = msg.get("usage") or {}
        elif ev.get("type") == "result":
            s["cost"] = max(s["cost"] or 0.0, ev.get("total_cost_usd") or 0.0)
            s["units"] += _units(calls.pop(sid, {}))
    for sid, usage in calls.items():
        out[sid]["tail"] = _units(usage)
    return out


# dollars per input-token unit, measured on runs whose sessions reported costs (12M units, Haiku 5.5)
KNOWN_RATES = {"claude-haiku-5-5": 0.50e-6}
MIN_CALIBRATION_UNITS = 1e6


def spend(logs: dict[str, Path], model: str | None = None) -> dict[str, tuple[float | None, bool]]:
    """Per agent: (dollars, estimated). Sessions killed at the deadline (or flushed) never report a
    cost, so theirs is estimated from token usage at the dollars-per-unit rate of the sessions in
    this run that did report one. A few short reported sessions are a noisy rate (their prompt
    caching differs), so below MIN_CALIBRATION_UNITS the model's known rate is used if there is one."""
    per = {n: sessions(p) for n, p in logs.items() if p.exists()}
    done = [s for ss in per.values() for s in ss.values() if s["cost"] is not None and s["units"] > 0]
    units = sum(s["units"] for s in done)
    rate = sum(s["cost"] for s in done) / units if done else None
    if units < MIN_CALIBRATION_UNITS and model in KNOWN_RATES:
        rate = KNOWN_RATES[model]
    result = {}
    for n, ss in per.items():
        total, estimated = 0.0, False
        for s in ss.values():
            total += s["cost"] or 0.0
            if s["tail"]:  # tokens spent after the last reported cost
                estimated = True
                if rate is None:
                    total = None
                    break
                total += s["tail"] * rate
        result[n] = (total, estimated)
    return result


def report_spend(costs: dict[str, tuple[float | None, bool]]) -> None:
    total, any_estimate, unknown = 0.0, False, []
    for n, (cost, estimated) in costs.items():
        if cost is None:
            unknown.append(n)
            print(f"  {n}: unknown (cut off at the deadline, and no session reported a cost to calibrate from)")
            continue
        any_estimate |= estimated
        total += cost
        print(f"  {n}: {'~' if estimated else ''}${cost:.2f}"
              + (" (estimated from tokens: session cut off at the deadline)" if estimated else ""))
    print(f"  total: {'~' if any_estimate else ''}${total:.2f}" + (f" plus unknown for {', '.join(unknown)}" if unknown else ""))


def kill_tree(proc: subprocess.Popen) -> None:
    if os.name == "nt":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True)
    else:
        proc.kill()


def flush_reason(st: dict, start: dict, args, age: float, started: float = 0.0) -> str | None:
    """Why this session should be restarted fresh now, if it should. Only between meetings, and
    never before the session is --flush-min-age old: every crew meeting hands out new idea ids, so
    without it a crew of 2 restarted after every meeting (28 times in a 20-minute run)."""
    if st.get("meeting"):
        return None
    if args.flush != "off" and (st.get("crew_forked") or 0) > started:
        return "its crew forked"  # fresh context on the new code, whatever the session's age
    if age < args.flush_min_age:
        return None
    if args.flush == "reassign" and start.get("idea") and st.get("idea") and st["idea"] != start["idea"]:
        return f"reassigned {start['idea']} -> {st['idea']}"
    if args.flush == "council" and (st.get("council_moved") or 0) > started:
        return f"a council moved it to {st.get('idea')}"
    if args.flush_every_evals and st.get("evals", 0) - start.get("evals", 0) >= args.flush_every_evals:
        return f"{st['evals'] - start['evals']} evals this session"
    return None


def run_agent(name: str, room: str, args, run_dir: Path, deadline: float, claude: str) -> None:
    ws = run_dir / "ws" / name
    ws.mkdir(parents=True)
    cfg = run_dir / "cfg"
    cfg.mkdir(exist_ok=True)
    mcp = cfg / f"{name}.mcp.json"
    mcp.write_text(json.dumps({"mcpServers": {"amongagents": {
        "command": sys.executable,
        "args": ["-m", "amongagents.bridge", "--server", f"ws://127.0.0.1:{args.port}", "--room", room,
                 "--name", name, "--workspace", str(ws), "--trust-task"],
    }}}))
    settings = cfg / f"{name}.settings.json"
    settings.write_text(json.dumps({"hooks": {"PostToolUse": [{"matcher": "*", "hooks": [
        {"type": "command", "command": f'"{sys.executable}" -m amongagents.hook'}]}]}}))
    log_path = run_dir / "logs" / f"{name}.jsonl"
    log = open(log_path, "a", encoding="utf-8")

    def note(msg: str) -> None:
        log.write(json.dumps({"launcher": msg, "ts": time.time()}) + "\n")
        log.flush()

    status_url = f"http://127.0.0.1:{args.port}/api/rooms/{room}/players/{name}"
    flushing = args.flush != "off" or args.flush_every_evals

    def player() -> dict:
        try:
            return http("GET", status_url)
        except Exception:  # not joined yet, or a hiccup: just look again later
            return {}

    attempt, session, flushed = 0, 0, False
    while attempt < args.max_sessions or flushed:
        if time.time() >= deadline:
            break
        if session and not flushed:
            reason = last_result(log_path) or ""
            if LIMIT_MARKERS.search(reason):  # restarting into a usage limit only burns quota
                note(f"stopping: {reason[:120]}")
                print(f"  {name} stopped: {reason[:120]}", flush=True)
                return
            time.sleep(args.restart_backoff)
        session += 1
        if session > 1 and not flushed and proc.returncode != 0:
            attempt += 1  # only crashes are capped; agents often end a turn on their own ("I'll keep waiting")
        elif session == 1:
            attempt = 1
        prompt = PROMPT.format(name=name) if session == 1 else FRESH.format(name=name) if flushed else CONTINUE
        cmd = [claude, "-p", prompt]
        if session > 1 and not flushed:
            cmd.append("--continue")
        cmd += ["--model", args.model, "--mcp-config", str(mcp), "--strict-mcp-config",
                "--settings", str(settings), "--permission-mode", "acceptEdits",
                "--allowedTools", "mcp__amongagents", "Read", "Edit", "Write", "Glob", "Grep",
                "Bash(python *)", "PowerShell(python *)",
                # an agent once ScheduleWakeup'd itself for 20 minutes and slept through the run
                "--disallowedTools", "ScheduleWakeup", "CronCreate", "Monitor",
                "--output-format", "stream-json", "--verbose"]
        note(f"start session {session}" + (" (fresh)" if flushed else ""))
        proc = subprocess.Popen(cmd, cwd=ws, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
        started, start, flushed, last_poll = time.time(), {}, False, 0.0
        while proc.poll() is None and time.time() < deadline:
            time.sleep(2)
            if not flushing or time.time() - last_poll < 5:
                continue
            last_poll = time.time()
            st = player()
            if not st:
                continue
            if not start.get("idea"):  # the direction this session started on, once it has one
                start = {"idea": st.get("idea"), "evals": start.get("evals", st.get("evals", 0))}
            why = flush_reason(st, start, args, time.time() - started, started)
            if why:
                note(f"flush: {why}")
                print(f"  {name} flushed: {why}", flush=True)
                kill_tree(proc)
                proc.wait()
                flushed = True
                break
        if proc.poll() is None:
            kill_tree(proc)
        note(f"session {session} ended rc={proc.poll()}")
    if attempt >= args.max_sessions and time.time() < deadline:
        note(f"stopping: {args.max_sessions} sessions crashed")


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--task", default=str(ROOT / "tasks" / "tsp"))
    ap.add_argument("--agents", type=int, default=3)
    ap.add_argument("--minutes", type=float, default=15)
    ap.add_argument("--model", default="claude-haiku-5-5")
    ap.add_argument("--port", type=int, default=8800)
    ap.add_argument("--max-sessions", type=int, default=4, help="crashed sessions per agent before it stops, counting the first session")
    ap.add_argument("--restart-backoff", type=float, default=20)
    ap.add_argument("--rounds", type=int, default=2)
    ap.add_argument("--cooldown", type=float, default=60)
    ap.add_argument("--turn-timeout", type=float, default=None, help="seconds per meeting turn (default: task's)")
    ap.add_argument("--stagnation-evals", type=int, default=None, help="runs without progress before a stagnation meeting")
    ap.add_argument("--crew-size", type=int, default=None, help="players per crew (default: task's, 8)")
    ap.add_argument("--council-size", type=int, default=None, help="crews per council (default: task's, 8)")
    ap.add_argument("--council-wait", type=float, default=None, help="seconds a council waits for more ideas")
    ap.add_argument("--lineage", choices=["crew", "room"], default=None,
                    help="crew: each crew builds on its own best (default); room: one shared baseline")
    ap.add_argument("--fork-every", type=float, default=None,
                    help="seconds between forks of the weakest crews onto the best crews' code (0 = never)")
    ap.add_argument("--noise-k", type=float, default=None, help="improvement margin in units of measured noise")
    ap.add_argument("--flush", choices=["off", "reassign", "council"], default="off",
                    help="restart an agent's session fresh (context dropped, files kept) when it gets a new direction "
                         "(reassign) or only when a council moves it (council)")
    ap.add_argument("--flush-min-age", type=float, default=480,
                    help="seconds a session runs at least before it can be flushed")
    ap.add_argument("--flush-every-evals", type=int, default=0, help="also restart fresh every N of an agent's evals")
    args = ap.parse_args()
    claude = shutil.which("claude")
    if not claude:
        sys.exit("claude CLI not found on PATH")
    names = NAMES[: max(1, min(args.agents, len(NAMES)))]

    run_dir = ROOT / "runs" / time.strftime("%Y%m%d-%H%M%S")
    (run_dir / "logs").mkdir(parents=True)
    server = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "amongagents.server:app", "--port", str(args.port), "--log-level", "warning"],
        cwd=ROOT, stdout=open(run_dir / "logs" / "server.log", "w"), stderr=subprocess.STDOUT,
        env={**os.environ, "AMONG_DATA": str(run_dir / "data")},  # git history + database of this run
    )
    try:
        for _ in range(50):
            try:
                http("GET", f"http://127.0.0.1:{args.port}/api/rooms")
                break
            except Exception:
                time.sleep(0.3)
        task = TaskSpec.from_dir(args.task).to_dict()
        task["meeting"].update(rounds=args.rounds, cooldown_sec=args.cooldown)
        if args.turn_timeout:
            task["meeting"].update(turn_timeout_sec=args.turn_timeout, vote_timeout_sec=args.turn_timeout)
        if args.stagnation_evals is not None:
            task["meeting"].update(stagnation_evals=args.stagnation_evals)
        for key, value in (("crew_size", args.crew_size), ("council_size", args.council_size),
                           ("council_wait_sec", args.council_wait), ("lineage", args.lineage),
                           ("fork_every_sec", args.fork_every), ("noise_k", args.noise_k)):
            if value is not None:
                task["meeting"][key] = value
        room = http("POST", f"http://127.0.0.1:{args.port}/api/rooms", {"task": task})["room"]
        (run_dir / "room.txt").write_text(room)
        print(f"room {room}: watch at http://127.0.0.1:{args.port}/?room={room}", flush=True)
        print(f"run dir: {run_dir}", flush=True)
        print(f"{len(names)} x {args.model} for {args.minutes:g} min, logs in {run_dir}", flush=True)

        deadline = time.time() + args.minutes * 60
        threads = []
        for n in names:  # stagger joins so the speaking order follows the name order
            t = threading.Thread(target=run_agent, args=(n, room, args, run_dir, deadline, claude), daemon=True)
            t.start()
            threads.append(t)
            time.sleep(8)
        for t in threads:
            t.join()

        snap = http("GET", f"http://127.0.0.1:{args.port}/api/rooms/{room}")
        (run_dir / "final_snapshot.json").write_text(json.dumps(snap, indent=1))
        print(f"\nbest {snap['task']['metric']}: {snap['best_score']} by {snap['best_by']} | "
              f"{len(snap['evals'])} evals, {len(snap['findings'])} findings")
        if snap.get("crews", 1) > 1:
            print(f"{snap['crews']} crews; council decisions:")
            for d in snap["decisions"]:
                print(f"  {d['council']} picked {d['idea']} ({d['scope']}, {d['votes']}/{d['voters']} votes, "
                      f"moved {', '.join(d['moved']) or 'nobody'}): {d['text'][:120]}")
        if args.flush != "off" or args.flush_every_evals:
            flushes = {n: sum('"launcher": "flush:' in line for line in
                              (run_dir / "logs" / f"{n}.jsonl").read_text(encoding="utf-8", errors="replace").splitlines())
                       for n in names}
            print("fresh restarts: " + ", ".join(f"{n} {c}" for n, c in flushes.items()))
        report_spend(spend({n: run_dir / "logs" / f"{n}.jsonl" for n in names}, args.model))
    finally:
        server.terminate()


if __name__ == "__main__":
    main()
