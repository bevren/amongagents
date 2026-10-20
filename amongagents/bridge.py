"""MCP bridge: lets any MCP-capable agent harness (Claude Code, Codex, ...) play.

Each player runs one bridge, started by their harness over stdio:

    python -m amongagents.bridge --server ws://HOST:8000 --room ABC123 --name alice --trust-task
    python -m amongagents.bridge ... --trust-task --sandbox docker     # run every eval in a container

The bridge keeps the WebSocket to the game server, writes task files into the
workspace, runs the task's eval command locally, and exposes game actions as
MCP tools. Every tool result carries a meeting notice when one is in progress,
so the agent finds out even if the harness has no hooks.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import itertools
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import quote

import websockets
from mcp.server.fastmcp import FastMCP

from .install import load_join
from .sandbox import Sandbox
from .tasks import TaskSpec, parse_metric

log = logging.getLogger("amongagents.bridge")
STATE_DIR = ".amongagents"
TOOL_WAIT_CAP = 50  # stay under typical MCP tool-call timeouts; agents re-call to keep waiting
# a `wait` this soon after one that found no meeting blocks until something happens: agents use
# it as a timer (one called it 230 times in 40 minutes, every 2 s, each call a paid model turn)
IDLE_REPEAT_SEC = 30


def _fmt_transcript(transcript: list[dict]) -> str:
    lines = []
    for t in transcript:
        tag = f"REPORT {t.get('kind', '')}".strip() if t["round"] == 0 else f"R{t['round']}"
        text = "(skipped: no response in time)" if t.get("skipped") else t["text"]
        idea = f"\n    💡 proposed idea {t['idea']}" if t.get("idea") else ""
        lines.append(f"[{tag}] {t['player']}: {text}{idea}")
    return "\n\n".join(lines)


def _fmt_ideas(ideas: list[dict]) -> str:
    return "\n".join(f"  {i['id']} ({i['by']}): {i['text']}" for i in ideas) or "  (no ideas proposed yet)"


def _fmt_plan(directions: list[dict], me: str) -> str:
    if not directions:
        return "  (no directions yet: explore freely until the first meeting assigns them)"
    return "\n".join(
        f"  {'👉 ' if me in d['agents'] else '   '}{d['id']}: {d['text']}  "
        f"[proposed by {d['by']}, {d['votes']} vote{'s' if d['votes'] != 1 else ''}; "
        f"explorers: {', '.join(d['agents'])}]" + _fmt_bet(d)
        for d in directions
    )


def _fmt_bet(d: dict) -> str:
    lb = d.get("long_bet")
    left = (lb["until"] - time.time()) / 60 if lb else 0
    return f"  🎯 LONG BET of {lb['by']}, protected for {left:.0f} more min" if left > 0 else ""


def _fmt_decisions(decisions: list[dict]) -> str:
    return "\n".join(
        f"  {d['idea']}: {d['text']}  [{'room-wide' if d['scope'] == 'room' else 'council ' + d['council']}, "
        f"from {d.get('from') or '-'}, {d['votes']}/{d['voters']} delegate votes, "
        f"{len(d['moved'])} new explorer{'s' if len(d['moved']) != 1 else ''}]"
        for d in decisions[-8:])


def _merge_idea(ideas: list[dict], idea: dict) -> None:
    for i, old in enumerate(ideas):
        if old["id"] == idea["id"]:
            ideas[i] = idea
            return
    ideas.append(idea)


class Session:
    def __init__(self, server: str, room: str, name: str, workspace: Path, trust: bool,
                 sandbox: Sandbox | None = None, sandbox_overrides: dict | None = None) -> None:
        self.server = server.rstrip("/")
        self.room = room.upper()
        self.name = name
        self.ws_dir = workspace
        self.trust = trust
        self.sandbox = sandbox or Sandbox()
        self.sandbox_overrides = sandbox_overrides or {}  # CLI choices win over the task's
        self.sandbox_ok: bool | None = None

        self.ws = None
        self.reader: asyncio.Task | None = None
        self.pending: dict[int, asyncio.Future] = {}
        self.ids = itertools.count(1)
        self.wake = asyncio.Event()
        self.last_idle_wait = float("-inf")  # monotonic time a `wait` last found no meeting

        self.task: TaskSpec | None = None
        self.rules = ""
        self.state = "disconnected"
        self.meeting: dict | None = None
        self.my_turn = False
        self.voted = False
        self.last_result: dict | None = None
        self.result_unseen = False
        self.best_score: float | None = None
        self.best_by: str | None = None
        self.baseline: dict | None = None
        self.synced_version = 0
        self.notes: list[dict] = []
        self.directions: list[dict] = []  # our crew's
        self.assignments: dict[str, str] = {}
        self.crew: str | None = None
        self.crews = 1
        self.decisions: list[dict] = []  # what councils picked
        self.room_best: dict = {}  # the room's record (our own best is our crew's line)
        self.lineage = "room"
        self.margin = 0.0  # how much an improvement must beat the best by
        self.inbox: list[str] = []

        self.job: asyncio.Task | None = None
        self.job_started = 0.0
        self.last_eval: dict | None = None
        self.setup_done = False

    # ----------------------------------------------------------- connection

    async def ensure(self) -> None:
        if self.ws is not None and self.reader and not self.reader.done():
            return
        url = f"{self.server}/ws/rooms/{quote(self.room)}/play?name={quote(self.name)}"
        self.ws = await websockets.connect(url, max_size=50_000_000, ping_interval=20)
        await self.ws.send(json.dumps({"type": "hello", "token": self._load_token()}))
        while True:  # broadcasts (e.g. our own player_joined) can arrive before the welcome
            first = json.loads(await asyncio.wait_for(self.ws.recv(), 30))
            if first.get("type") in ("welcome", "fatal"):
                break
        if first.get("type") == "fatal":
            await self.ws.close()
            self.ws = None
            raise RuntimeError(first["error"])
        if first.get("token"):
            self._save_token(first["token"])
        self._apply_welcome(first)
        self.reader = asyncio.create_task(self._read())
        self._write_session_file()

    def _token_file(self) -> Path:
        return self.ws_dir / STATE_DIR / "tokens.json"

    def _load_token(self) -> str:
        """The secret the server gave this workspace's player on first join, if any."""
        try:
            return json.loads(self._token_file().read_text(encoding="utf-8")).get(f"{self.room}/{self.name}", "")
        except (OSError, ValueError):
            return ""

    def _save_token(self, token: str) -> None:
        f = self._token_file()
        try:
            tokens = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            tokens = {}
        tokens[f"{self.room}/{self.name}"] = token
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps(tokens), encoding="utf-8")

    def _apply_welcome(self, w: dict) -> None:
        self.task = TaskSpec.from_dict(w["task"])
        self.sandbox.image = self.sandbox_overrides.get("image") or self.task.sandbox_image
        self.sandbox.gpus = self.sandbox_overrides.get("gpus") or self.task.sandbox_gpus
        self.rules = w["rules"]
        snap = w["snapshot"]
        self.state = snap["state"]
        self.meeting = snap["meeting"]
        self.my_turn = bool(self.meeting and self.meeting["turn"] == self.name)
        self.voted = bool(self.meeting and self.name in self.meeting["voted"])
        self.notes = snap["notes"]
        self.directions, self.assignments = snap["directions"], snap["assignments"]
        self.crew, self.crews = snap.get("crew"), snap.get("crews", 1)
        self.decisions = snap.get("decisions", [])
        self._apply_board(snap)
        self.baseline = w["baseline"]
        self.best_score, self.best_by = self.baseline["score"], self.baseline["by"]
        self._prepare_workspace()

    def _apply_board(self, snap: dict) -> None:
        self.room_best = snap.get("room_best") or {}
        self.lineage, self.margin = snap.get("lineage", "room"), snap.get("margin") or 0.0

    def own_line(self) -> bool:
        """Does our crew build on its own baseline (several crews, crew lineages)?"""
        return self.lineage == "crew" and self.crews > 1

    def best_label(self) -> str:
        return "your crew's best" if self.own_line() else "shared best"

    def room_record(self) -> str:
        r = self.room_best
        if not self.own_line() or r.get("score") is None:
            return ""
        return f" (room record {_fmt_score(r['score'])} by {r['by']}, crew {r['crew']})"

    def _write_session_file(self) -> None:
        d = self.ws_dir / STATE_DIR
        d.mkdir(exist_ok=True)
        http = self.server.replace("wss://", "https://").replace("ws://", "http://")
        (d / "session.json").write_text(json.dumps({"http": http, "room": self.room, "name": self.name}))

    async def _read(self) -> None:
        try:
            async for raw in self.ws:
                msg = json.loads(raw)
                if msg.get("type") == "reply":
                    fut = self.pending.pop(msg.get("req_id"), None)
                    if fut and not fut.done():
                        fut.set_result(msg)
                else:
                    self._on_event(msg)
                    self.wake.set()
        except websockets.ConnectionClosed:
            pass
        finally:
            self.state = "disconnected"
            for fut in self.pending.values():
                if not fut.done():
                    fut.set_exception(RuntimeError("connection to game server lost"))
            self.pending.clear()
            self.wake.set()

    async def request(self, op: str, **data) -> dict:
        await self.ensure()
        req_id = next(self.ids)
        fut = asyncio.get_running_loop().create_future()
        self.pending[req_id] = fut
        await self.ws.send(json.dumps({"type": op, "req_id": req_id, **data}))
        reply = await asyncio.wait_for(fut, 30)
        if not reply["ok"]:
            raise RuntimeError(reply["error"])
        return reply["data"]

    # --------------------------------------------------------------- events

    def _on_event(self, ev: dict) -> None:
        t = ev["type"]
        if t == "meeting_started":
            self.state = "meeting"
            self.meeting = ev["meeting"]
            self.my_turn = self.voted = False
            fs = self.meeting.get("findings") or [self.meeting["finding"]]
            if self.meeting.get("level"):
                self.inbox.append(f"COUNCIL MEETING {self.meeting['group']}: you're a delegate. Call `wait`.")
            elif fs[0]["kind"] == "stagnation":
                self.inbox.append("STAGNATION MEETING called by the room: progress has stalled.")
            else:
                self.inbox.append("EMERGENCY MEETING about " + ", ".join(f"{f['reporter']}'s {f['kind']}" for f in fs) + ".")
        elif t == "turn" and self.meeting:
            self.meeting.update(turn=ev["player"], round=ev["round"], turn_deadline=ev["deadline"],
                                transcript=ev["transcript"], participants=ev["participants"], ideas=ev["ideas"])
            self.my_turn = ev["player"] == self.name
        elif t in ("said", "turn_skipped") and self.meeting:
            entry = {"round": ev["round"], "player": ev["player"], "text": ev.get("text")}
            if t == "turn_skipped":
                entry["skipped"] = True
            if ev.get("idea"):
                entry["idea"] = ev["idea"]["id"]
                _merge_idea(self.meeting["ideas"], ev["idea"])
            self.meeting["transcript"].append(entry)
            if ev["player"] == self.name:
                self.my_turn = False
        elif t == "voting_started" and self.meeting:
            self.state = "voting"
            self.my_turn = False
            self.meeting.update(transcript=ev["transcript"], ideas=ev["ideas"], carried=ev.get("carried", []))
        elif t == "meeting_ended":
            self.state = "working"
            self.last_result = ev["meeting"]
            self.result_unseen = True
            self.meeting = None
            self.my_turn = False
            res = self.last_result["result"]
            chosen = [d["id"] for d in res["directions"]]
            if res.get("level"):  # a council: our crew's plan changes through assignments_changed
                self.inbox.append(f"Council meeting ended: it picked {chosen[0]}." if chosen
                                  else "Council meeting ended without a pick.")
            else:
                if res["changed"]:  # apply now: `wait` may report the result before assignments_changed arrives
                    self.directions, self.assignments = res["directions"], res["assignments"]
                self.inbox.append("Meeting ended: " + (f"the crew will explore {', '.join(chosen)} in parallel."
                                                       if chosen else "no votes, everyone keeps their direction."))
        elif t == "decision":
            d = ev["decision"]
            self.decisions = [*self.decisions, d][-20:]
            self.inbox.append(f"🏛 Council {d['council']} picked {d['idea']} ("
                              + ("room-wide" if d["scope"] == "room" else f"for its {d['crews']} crews")
                              + f"): {d['text'][:200]}"
                              + (" You are one of its new explorers." if self.name in d["moved"] else ""))
        elif t == "assignments_changed":
            self.directions, self.assignments = ev["directions"], ev["assignments"]
            mine = self.my_direction()
            if mine:
                self.inbox.append(f"YOUR ASSIGNED DIRECTION is now {mine['id']}: {mine['text']}")
        elif t == "baseline_updated":
            self.baseline = ev["baseline"]
            self.best_score, self.best_by = self.baseline["score"], self.baseline["by"]
            self.inbox.append(
                f"{'Your crew' if self.own_line() else 'Shared'} baseline is now v{self.baseline['version']} "
                f"(score {self.best_score} by {self.best_by}). Call `sync_baseline`."
            )
        elif t == "crew_forked":
            f = ev["fork"]
            self.baseline = {**ev["baseline"], "files": None}
            self.best_score, self.best_by = self.baseline["score"], self.baseline["by"]
            old = f["replaced"]
            self.inbox.append(
                f"🍴 FORK: your crew was the weakest, so it now builds on crew {f['from']}'s best code "
                f"(score {_fmt_score(f['score'])} by {f['by']}), as your crew's v{f['version']}. Your old line "
                f"(v{old['version']}, {_fmt_score(old['score'])}) stays in `history`. Call `sync_baseline`, then "
                "keep exploring your own direction on top of it: fresh ideas on stronger code."
            )
        elif t == "room_record":
            self.room_best = {"score": ev["score"], "by": ev["by"], "crew": ev["crew"]}
            self.inbox.append(f"Room record: crew {ev['crew']} reached {_fmt_score(ev['score'])} ({ev['by']}). "
                              "Your crew keeps building on its own line; the weakest crews fork from the best ones "
                              "on a schedule.")
        elif t == "eval":
            if ev["beats_best"] and ev["player"] != self.name:
                self.inbox.append(f"{ev['player']} got {ev['score']}, which beats the shared best; a report may be coming.")
        elif t == "finding_queued":
            f = ev["finding"]
            if f["reporter"] != self.name:
                self.inbox.append(f"{f['reporter']} queued a {f['kind']} for the next meeting.")
        elif t == "player_joined":
            self.inbox.append(f"{ev['player']} joined.")
        elif t == "player_left":
            self.inbox.append(f"{ev['player']} left.")
        elif t == "room_closed":
            self.inbox.append("The room was closed by its admin.")

    def my_direction(self) -> dict | None:
        mine = self.assignments.get(self.name)
        return next((d for d in self.directions if d["id"] == mine), None)

    def in_meeting(self) -> bool:
        return bool(self.meeting and self.name in self.meeting["participants"])

    def notice(self) -> str:
        parts = []
        if self.inbox:
            parts.append("Events: " + " | ".join(self.inbox[-10:]))
            self.inbox.clear()
        if self.state in ("meeting", "voting") and self.meeting and not self.in_meeting():
            parts.append("A meeting you are not part of is in progress (you joined during voting). Keep working.")
        elif self.state in ("meeting", "voting") and self.meeting:
            f = self.meeting["finding"]
            what = "council meeting" if self.meeting.get("level") else "emergency meeting"
            if self.state == "voting" and not self.voted:
                parts.append("🚨 VOTING IS OPEN. Call `vote` now.")
            elif self.my_turn:
                parts.append(f"🚨 IT'S YOUR TURN TO SPEAK in the {what}. Call `wait` to read the transcript, then `say`.")
            elif self.meeting.get("level"):
                parts.append(f"🏛 COUNCIL MEETING {self.meeting['group']} in progress: you're a delegate. "
                             "Stop working and call `wait`.")
            else:
                parts.append(f"🚨 EMERGENCY MEETING in progress (reported by {f['reporter']}). Stop working and call `wait`.")
        elif self.baseline and self.baseline["version"] > self.synced_version:
            parts.append(f"Baseline v{self.baseline['version']} isn't synced yet. Call `sync_baseline` before your next experiment.")
        return ("\n\n---\n" + "\n".join(parts)) if parts else ""

    # ------------------------------------------------------------ workspace

    def _path(self, rel: str) -> Path:
        p = (self.ws_dir / rel).resolve()
        if self.ws_dir.resolve() not in p.parents and p != self.ws_dir.resolve():
            raise RuntimeError(f"path escapes workspace: {rel}")
        return p

    def _prepare_workspace(self) -> None:
        assert self.task
        (self.ws_dir / STATE_DIR).mkdir(parents=True, exist_ok=True)
        self.setup_done = (self.ws_dir / STATE_DIR / "setup_done").exists()
        for rel, content in self.task.files.items():
            p = self._path(rel)
            if rel in self.task.protected_files or not p.exists():
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(content, encoding="utf-8")
        marker = self.ws_dir / STATE_DIR / "synced_version"
        self.synced_version = int(marker.read_text()) if marker.exists() else 0
        if self.baseline["version"] > 0 and self.synced_version == 0 and not any(
            self._path(r).exists() and self._path(r).read_text(encoding="utf-8") != self.task.files.get(r)
            for r in self.task.editable
        ):
            self.sync_files()  # fresh workspace joining a room that already moved on

    def restore_protected(self) -> None:
        for rel in self.task.protected_files:
            p = self._path(rel)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(self.task.files[rel], encoding="utf-8")

    def editable_hash(self) -> str:
        h = hashlib.sha256()
        for rel in sorted(self.task.editable):
            p = self._path(rel)
            h.update(rel.encode() + b"\0" + (p.read_bytes() if p.exists() else b"<missing>") + b"\0")
        return h.hexdigest()

    def read_editable(self) -> dict[str, str]:
        out = {}
        for rel in self.task.editable:
            p = self._path(rel)
            if p.exists():
                out[rel] = p.read_text(encoding="utf-8")
        return out

    def write_files(self, files: dict[str, str]) -> str:
        """Overwrite editable files in the workspace, backing up the current ones first."""
        backup = self.ws_dir / STATE_DIR / "backup" / time.strftime("%Y%m%d-%H%M%S")
        for rel, content in files.items():
            if rel not in self.task.editable:
                continue
            p = self._path(rel)
            if p.exists():
                (backup / rel).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(p, backup / rel)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8")
        return str(backup) if backup.exists() else ""

    def sync_files(self) -> str:
        backup = self.write_files(self.baseline["files"])
        self.mark_synced(self.baseline["version"])
        return backup

    def mark_synced(self, version: int) -> None:
        self.synced_version = version
        (self.ws_dir / STATE_DIR / "synced_version").write_text(str(version))

    # ------------------------------------------------------------- commands

    async def _run(self, cmd: str, timeout: float, cwd: Path | None = None,
                   network: bool = False) -> tuple[int | None, str]:
        """Run a task command in the workspace: directly, or inside the sandbox container
        (where only setup gets network access)."""
        cwd = cwd or self.ws_dir
        session = {} if os.name == "nt" else {"start_new_session": True}  # so _kill_tree only hits this
        pipes = dict(stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, stdin=asyncio.subprocess.DEVNULL)
        name = None
        if self.sandbox.enabled:
            name = self.sandbox.new_name()
            proc = await asyncio.create_subprocess_exec(*self.sandbox.wrap(cmd, cwd, network, name), **pipes, **session)
        else:
            proc = await asyncio.create_subprocess_shell(cmd, cwd=cwd, **pipes, **session)
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout)
            return proc.returncode, out.decode("utf-8", "replace")
        except asyncio.TimeoutError:
            if name:  # killing the docker CLI doesn't stop the container itself
                subprocess.run(self.sandbox.kill(name), capture_output=True)
            _kill_tree(proc.pid)
            await proc.wait()
            return None, f"(timed out after {timeout:.0f}s)"

    async def check_sandbox(self) -> str | None:
        """None if commands can run; otherwise why not (checked once)."""
        if not self.sandbox.enabled or self.sandbox_ok:
            return None
        try:
            out = subprocess.run(self.sandbox.check(), capture_output=True, text=True, timeout=30)
            self.sandbox_ok = out.returncode == 0
            detail = (out.stderr or out.stdout).strip()
        except (OSError, subprocess.TimeoutExpired) as e:
            self.sandbox_ok, detail = False, str(e)
        return None if self.sandbox_ok else (
            f"--sandbox docker is on, but Docker isn't usable here ({detail[:200]}). Install and start Docker "
            "(plus the NVIDIA container toolkit for GPU tasks), or ask your human to restart without --sandbox.")

    async def holdout_check(self, files: dict[str, str]) -> dict:
        """Run the task's holdout eval with a one-time secret seed from the server, on our candidate
        `files` and on the shared baseline (each in a temporary copy of the workspace), so both are
        compared on the same fresh instances."""
        assert self.task
        problem = await self.check_sandbox()
        if problem:
            raise RuntimeError(problem)
        challenge = await self.request("challenge")
        baseline = await self.request("baseline")
        cmd = self.task.holdout_command.replace("{seed}", str(challenge["seed"]))
        timeout = self.task.holdout_timeout_sec
        candidate, out = await self._run_copy(cmd, timeout, files)
        base, out_b = await self._run_copy(cmd, timeout, baseline["files"])
        return {"seed": challenge["seed"], "baseline_version": challenge["baseline_version"],
                "candidate": candidate, "baseline": base,
                "candidate_output": out[-800:], "baseline_output": out_b[-800:]}

    async def _run_copy(self, cmd: str, timeout: float, files: dict[str, str]) -> tuple[float | None, str]:
        """Run cmd in a temporary copy of the workspace with these editable files and pristine
        protected ones; returns (metric or None, output)."""
        tmp = Path(tempfile.mkdtemp(prefix="amongagents-holdout-"))
        try:
            copy = tmp / "ws"
            shutil.copytree(self.ws_dir, copy, ignore=shutil.ignore_patterns(STATE_DIR, ".git", "__pycache__"))
            for rel, content in {**self.task.files, **files}.items():
                if rel in self.task.protected_files or rel in files:
                    (copy / rel).parent.mkdir(parents=True, exist_ok=True)
                    (copy / rel).write_text(content, encoding="utf-8")
            code, out = await self._run(cmd, timeout, cwd=copy)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        return (parse_metric(self.task.metric_regex, out) if code == 0 else None), out

    async def eval_job(self, description: str = "") -> dict:
        assert self.task
        output = ""
        problem = await self.check_sandbox()
        if problem:
            return {"ok": False, "score": None, "output": problem, "error": "sandbox unavailable"}
        if self.task.setup_command and not self.setup_done:
            code, out = await self._run(self.task.setup_command, 3600, network=True)
            output += f"$ {self.task.setup_command}\n{out[-3000:]}\n"
            if code != 0:
                return {"ok": False, "score": None, "output": output, "error": f"setup failed (exit {code})"}
            (self.ws_dir / STATE_DIR / "setup_done").write_text("ok")
            self.setup_done = True
        self.restore_protected()
        files_hash, files = self.editable_hash(), self.read_editable()  # what this run scores
        started = time.time()
        code, out = await self._run(self.task.eval_command, self.task.eval_timeout_sec)
        output += out
        score = parse_metric(self.task.metric_regex, out)
        ok = code == 0 and score is not None
        error = None if ok else ("timeout" if code is None else f"exit {code}" if code else "metric not found in output")
        result = {"ok": ok, "score": score, "error": error, "output": output,
                  "files_hash": files_hash, "files": files, "seconds": round(time.time() - started, 1)}
        try:
            # the files go along so the server can commit this run on our experiment branch
            entry = await self.request("eval", score=score, ok=ok, note=error or "", description=description,
                                       files=files, based_on=self.synced_version)
            result["beats_best"] = entry["beats_best"]
            result["commit"], result["branch"] = entry.get("commit"), entry.get("branch")
        except Exception as e:
            result["beats_best"] = None
            result["server_error"] = str(e)
        self.last_eval = result
        return result


def _kill_tree(pid: int) -> None:
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)], capture_output=True)
        else:
            os.killpg(os.getpgid(pid), 9)
    except Exception:
        pass


# ---------------------------------------------------------------- MCP tools

mcp = FastMCP(
    "amongagents",
    instructions=(
        "Multiplayer research game. Start by calling `join_room`, follow the rules it returns, "
        "and never stop looping: work -> run_eval -> report_finding / wait -> say -> vote -> sync_baseline."
    ),
    log_level="WARNING",
)
S: Session  # configured in main()


def _with_notice(text: str) -> str:
    return text + S.notice()


def _fmt_score(x) -> str:
    return "none yet" if x is None else f"{x:g}"


@mcp.tool()
async def join_room() -> str:
    """Join the configured room (safe to call again). Returns the rules, the research program,
    your assigned direction, and the current shared state. Call this first."""
    await S.ensure()
    try:  # the room may have grown into several crews, or councils decided things, since we joined
        snap = await S.request("snapshot")
        S.crew, S.crews = snap.get("crew"), snap.get("crews", 1)
        S.decisions, S.notes = snap.get("decisions", S.decisions), snap["notes"]
        S.directions, S.assignments = snap["directions"], snap["assignments"]
        S._apply_board(snap)
    except Exception:
        pass
    t = S.task
    parts = [
        S.rules,
        f"ROOM {S.room} | you are '{S.name}' | your state: {S.state}",
    ]
    if S.crews > 1:
        parts.append(f"YOUR CREW: {S.crew} (one of {S.crews} crews). Your meetings, votes and directions are "
                     "your crew's; councils of delegates pick ideas across crews.")
    parts += [
        f"TASK: {t.name}: {t.description}",
        f"METRIC: {t.metric_name} ({t.direction}). {S.best_label().capitalize()}: {_fmt_score(S.best_score)}"
        + (f" by {S.best_by}" if S.best_by else "") + S.room_record()
        + f". An improvement must beat it by {S.margin:.4g}.",
        f"EDITABLE FILES: {', '.join(t.editable) or '(none listed)'}",
        f"PROTECTED FILES (restored before every eval, don't edit): {', '.join(t.protected_files) or '-'}",
        f"WORKSPACE: {S.ws_dir}",
        "SANDBOX: " + (f"docker ({S.sandbox.image}{', GPUs' if S.sandbox.gpus else ''}): evals run in a container "
                       "with no network and only this workspace mounted" if S.sandbox.enabled
                       else "none (task commands run directly on this machine)"),
        "PROGRAM:\n" + (t.program or "(no program.md)"),
        "DIRECTIONS BEING EXPLORED IN PARALLEL (👉 = yours; stay on it, the others are covered):\n"
        + _fmt_plan(S.directions, S.name),
    ]
    if S.decisions:
        parts.append("ROOM-WIDE DECISIONS (council picks, newest last):\n" + _fmt_decisions(S.decisions))
    if S.notes:
        parts.append("SHARED NOTES (your crew's findings and the room's improvements, newest last):\n"
                     + "\n".join(f"- [{n['by']}] {n['text'][:1500]}" for n in S.notes))
    tried = await _tried_digest()  # a fresh session rejoining mid-game starts from the evidence
    if tried:
        parts.append(tried)
    return _with_notice("\n\n".join(parts))


@mcp.tool()
async def status() -> str:
    """Current room state, who explores which direction, leaderboard, and shared notes."""
    snap = await S.request("snapshot")
    best_per: dict[str, float] = {}
    better = min if snap["task"]["direction"] == "minimize" else max
    for e in snap["evals"]:
        if e["ok"] and e["score"] is not None:
            best_per[e["player"]] = better(best_per.get(e["player"], e["score"]), e["score"])
    board = "\n".join(
        f"  {p['name']}: best {_fmt_score(best_per.get(p['name']))}, {p['evals']} evals"
        + (f", exploring {p['idea']}" if p["idea"] else "") + ("" if p["connected"] else " (offline)")
        for p in snap["players"]
    )
    notes = "\n".join(f"  - [{n['by']}] {n['text'][:300]}" for n in snap["notes"]) or "  (none)"
    S.directions, S.assignments = snap["directions"], snap["assignments"]
    S.decisions, S.crews = snap.get("decisions", S.decisions), snap.get("crews", S.crews)
    S._apply_board(snap)
    crew = (f" | crew {snap['crew']} of {snap['crews']}, {snap['players_online']} players online"
            if snap.get("crews", 1) > 1 else "")
    noise = snap.get("noise") or {}
    margin = (f"Improvements must beat it by {S.margin:.4g}"
              + (f" (run-to-run noise {noise['sd']:.3g} over {noise['repeats']} repeats of identical code)"
                 if noise.get("sd") is not None else "") + ".\n")
    decisions = f"Room-wide decisions:\n{_fmt_decisions(S.decisions)}\n" if S.decisions else ""
    return _with_notice(
        f"State: {snap['state']} | {S.best_label()} {_fmt_score(snap['best_score'])} by {snap['best_by'] or '-'}"
        f"{S.room_record()} | baseline v{snap['baseline_version']} (you have v{S.synced_version}){crew}\n{margin}"
        f"Directions (👉 = yours):\n{_fmt_plan(S.directions, S.name)}\n{decisions}"
        f"{'Your crew' if crew else 'Players'}:\n{board}\nShared notes:\n{notes}"
    )


async def _await_job(wait_sec: float) -> str:
    done, _ = await asyncio.wait({S.job}, timeout=max(1, min(wait_sec, TOOL_WAIT_CAP)))
    if not done:
        return f"Eval still running ({time.time() - S.job_started:.0f}s so far). Call `eval_status` to keep waiting."
    try:
        r = S.job.result()
    except Exception as e:
        return f"Eval crashed: {e!r}"
    head = (f"OK: {S.task.metric_name} = {r['score']:g}" if r["ok"] else f"FAILED: {r['error']}") + f" in {r['seconds']}s"
    if r["ok"]:
        head += f" | {S.best_label()}: {_fmt_score(S.best_score)} (to beat by {S.margin:.4g})"
        if r.get("beats_best"):
            head += (f"\n🔎 This BEATS {'your crew' if S.own_line() else 'the shared'} best. Call `report_finding(kind='improvement', summary=...)` "
                     "and explain what you changed and why it works.")
    if r.get("commit"):
        head += f"\nCommitted as {r['commit'][:8]} on {r['branch']}."
    if r.get("server_error"):
        head += f"\n(server didn't record it: {r['server_error']})"
    return f"{head}\n\nOutput (tail):\n{r['output'][-2500:]}"


@mcp.tool()
async def run_eval(description: str = "", wait_sec: float = 45) -> str:
    """Run the task's evaluation on your current workspace and report the score to the room.
    description: one line on what this experiment tries (it becomes the git commit message and the
    results.tsv entry, so the crew can follow your work). Every run is committed on your branch
    exp/<direction>/<you>, including failures. Waits up to wait_sec (max 50); if it's still running,
    call `eval_status`."""
    await S.ensure()
    if not S.trust:
        return _with_notice(
            "This bridge was started without --trust-task, so it won't run task commands.\n"
            f"Setup: {S.task.setup_command or '-'}\nEval: {S.task.eval_command}\n"
            "Ask your human to review these commands and restart the bridge with --trust-task."
        )
    if S.job and not S.job.done():
        return _with_notice(await _await_job(wait_sec))
    S.job_started = time.time()
    S.job = asyncio.create_task(S.eval_job(description))
    return _with_notice(await _await_job(wait_sec))


@mcp.tool()
async def eval_status(wait_sec: float = 45) -> str:
    """Wait for (or fetch) the result of the most recent run_eval."""
    if S.job is None:
        return _with_notice("No eval has been started.")
    return _with_notice(await _await_job(wait_sec))


@mcp.tool()
async def report_finding(kind: str, summary: str, propose: str | None = None) -> str:
    """Call an EMERGENCY MEETING about a finding.
    kind='improvement': your last eval beat the shared best. The files that eval scored become the shared
    baseline immediately, since the score proves it (edits you made after it stay in your workspace). If the task has a holdout check, this first runs
    your solver and the baseline on fresh hidden instances, and only accepts a win there too.
    kind='clue': a useful insight without a better score (a dead end, a bug, a surprising result).
    The summary is your opening presentation: what you changed, the evidence, and what it tells us.
    propose: optional one-line idea worth exploring next (others can vote for it)."""
    await S.ensure()
    score, files, edited = None, None, False
    if kind == "improvement":
        r = S.last_eval
        if not r or not r["ok"]:
            raise RuntimeError("no successful eval to report; call run_eval first")
        if S.job and not S.job.done():
            raise RuntimeError("an eval is still running; wait for it to finish")
        # the files that eval scored, even if you've edited since: a win isn't lost to a later edit
        score, files = r["score"], r["files"]
        edited = S.editable_hash() != r["files_hash"]
        if S.task.holdout_command and not S.trust:
            raise RuntimeError("this task checks improvements on hidden instances, which needs --trust-task")
    for attempt in range(3):
        holdout = None
        if kind == "improvement" and S.task.holdout_command:
            h = await S.holdout_check(files)
            holdout = {k: h[k] for k in ("seed", "baseline_version", "candidate", "baseline")}
            if h["candidate"] is None:
                raise RuntimeError("your solver failed on the hidden instances:\n" + h["candidate_output"])
        try:
            f = await S.request("report", kind=kind, summary=summary, score=score, files=files, propose=propose,
                                holdout=holdout)
            break
        except Exception as e:  # another win landed mid-check: check again against the new baseline
            if "baseline changed during your holdout check" not in str(e) or attempt == 2:
                raise
    if kind == "improvement" and not edited:  # our files *are* the new baseline (its event arrives first)
        S.mark_synced(S.baseline["version"])
    note = (" You had edited your files since that eval: the eval's files were reported, and your edits are "
            "untouched (`sync_baseline` would replace them, after a backup)." if edited else "")
    if f.get("queued"):
        return _with_notice(
            "Finding recorded and QUEUED: a meeting is running or the room is in cooldown. It will open the "
            "next meeting automatically, so don't report it again. "
            + (f"Your eval's files are already the new shared baseline.{note} " if kind == "improvement" else "")
            + "Keep working (and join any running meeting via `wait`)."
        )
    checked = (f" Hidden-instance check passed: {holdout['candidate']:g} vs baseline {holdout['baseline']:g}."
               if holdout else "")
    return _with_notice(f"Emergency meeting {f['id']} called.{checked}{note} Now call `wait` and follow the discussion.")


def _fmt_run(e: dict) -> str:
    score = _fmt_score(e["score"]) if e["ok"] else "FAILED"
    desc = (e.get("description") or e.get("note") or "").replace("\n", " ")[:80]
    return f"{e['commit'][:8]}  {score:>8}  {e['player']:<8} {e.get('idea') or 'free':<5} {desc}"


async def _tried_digest() -> str:
    """What the crew has actually run, from the committed experiments, so discussion and votes are
    grounded in evidence (agents rarely call `experiments` on their own)."""
    try:
        rows = await S.request("experiments", limit=200)
    except Exception:
        return ""
    if not rows:
        return ""
    best_per: dict[str, dict] = {}
    for e in rows:  # rows come best first, failures last
        if e["ok"]:
            best_per.setdefault(e.get("idea") or "free", e)
    failures = [e for e in rows if not e["ok"]][:3]
    parts = ["WHAT THE CREW HAS TRIED (committed runs; `checkout(<commit>)` loads one, "
             "`history(ref=<commit>)` shows its diff, `experiments(...)` lists more):",
             "  best per direction:", *(f"    {_fmt_run(e)}" for e in best_per.values()),
             "  top runs:", *(f"    {_fmt_run(e)}" for e in rows[:5] if e["ok"])]
    if failures:
        parts += ["  recent failures:", *(f"    {_fmt_run(e)}" for e in failures)]
    return "\n".join(parts)


async def _best_on(idea: str) -> str:
    try:
        rows = await S.request("experiments", direction=idea, limit=1)
    except Exception:
        return ""
    if not rows or not rows[0]["ok"]:
        return ""
    e = rows[0]
    return (f"Best run so far on {idea}: {_fmt_score(e['score'])} by {e['player']} ({e['commit'][:8]}). "
            f"`checkout(\"{e['commit'][:8]}\")` to start from it, or `history(ref=\"{e['commit'][:8]}\")` to see it.\n")


@mcp.tool()
async def wait(timeout_sec: float = 45) -> str:
    """Block until something needs your action: your turn to speak, voting, or the end of a meeting.
    Returns the meeting transcript when relevant. Call again if it times out. With no meeting
    running it returns at once; called again right after, it blocks until something happens."""
    await S.ensure()
    start = time.monotonic()
    deadline = start + max(1, min(timeout_sec, TOOL_WAIT_CAP))
    idle = start - S.last_idle_wait < IDLE_REPEAT_SEC  # polling: block instead of answering at once
    while True:
        if S.state == "meeting" and S.my_turn:
            m = S.meeting
            left = max(0, m["turn_deadline"] - time.time())
            stagnation = m["finding"]["kind"] == "stagnation"
            council = bool(m.get("level"))
            msg = (
                f"🎤 YOUR TURN: round {m['round']}/{m['rounds']}, about {left:.0f}s left. Call `say` once.\n"
                + (f"🏛 This is COUNCIL {m['group']}: each delegate brought the idea their crew voted for. Make the "
                   "case for yours with evidence (scores, runs), take the others seriously, and help the council "
                   "find the idea most worth spreading to every crew. A better idea that comes up in the "
                   "discussion can be added with `say(text, propose=...)`.\n" if council else
                   "🧨 This is a STAGNATION MEETING: the current directions have stalled and can't be voted for. "
                   "Propose something fundamentally different (new representation, algorithm family, or "
                   "framing), and challenge assumptions everyone has been making. Variations don't count. "
                   "Add it as `say(text, propose=\"one-line idea\")`.\n" if stagnation else
                   "Discuss the finding with the crew. If you have a concrete idea worth exploring next, "
                   "add it as `say(text, propose=\"one-line idea\")`.\n")
                + f"Speaking order: {' -> '.join(m['participants'])}\n\nTRANSCRIPT:\n{_fmt_transcript(m['transcript'])}"
                f"\n\nIDEAS ON THE TABLE:\n{_fmt_ideas(m['ideas'])}"
                f"\n\n{'YOUR CREW’S DIRECTIONS' if council else 'DIRECTIONS CURRENTLY BEING EXPLORED'}"
                f" (👉 = yours):\n{_fmt_plan(S.directions, S.name)}"
                f"\n\n{await _tried_digest()}"
            )
        elif S.state in ("meeting", "voting") and not S.in_meeting():
            return _with_notice("Voting on a meeting you weren't part of is in progress. Continue your research.")
        elif S.state == "voting" and not S.voted and S.meeting["ideas"] and S.meeting.get("level"):
            m = S.meeting
            msg = (f"🗳️ COUNCIL VOTE: call `vote(idea=<one of {', '.join(i['id'] for i in m['ideas'])}>, reason=...)` "
                   "for the idea most worth exploring in every crew, whichever crew it came from: if the "
                   "discussion agreed on one, vote for it. Only an idea a majority of delegates vote for is a decision; "
                   "a split vote, or one where delegates don't vote, changes nothing. The pick gets an explorer in each crew this council covers. (No work_on: you go back "
                   "to your crew's direction.)"
                   + "\n\nIDEAS:\n" + "\n".join(f"  {i['id']} (from {i.get('from') or i['by']}): {i['text']}"
                                                  for i in m["ideas"])
                   + f"\n\n{await _tried_digest()}"
                   + f"\n\nTRANSCRIPT:\n{_fmt_transcript(m['transcript'])}")
        elif S.state == "voting" and not S.voted and S.meeting["ideas"]:
            m = S.meeting
            carried = [] if m["finding"]["kind"] == "stagnation" else m.get("carried")
            if carried is None:  # e.g. reconnected mid-vote: the current directions are the carried options
                new = {i["id"] for i in m["ideas"]}
                carried = [d for d in S.directions if d["id"] not in new]
            choices = m["ideas"] + carried
            options = [i["id"] for i in choices]
            own = [i["id"] for i in choices if i["by"] == S.name]
            mine = S.my_direction()
            msg = (
                "🗳️ VOTING: call `vote(idea=<most promising, one of " + ", ".join(options) + ">, "
                "work_on=<the idea YOU will explore next, may be your own>, reason=...)`."
                + (f" `idea` can't be your own ({', '.join(own)})." if own and len(options) > 1 else "")
                + " Use work_on to lock in the split the crew agreed on in the discussion"
                + (f", or work_on=\"{mine['id']}\" to keep your current direction" if mine and carried else "")
                + (" (stagnation meeting: only the new ideas below count)" if m["finding"]["kind"] == "stagnation" else "")
                + ". Ideas with a vote or a claim become parallel directions; a voted idea nobody claimed"
                " still gets someone. A real long shot that needs time? Add long_bet=true to keep work_on"
                " protected from re-splits (one agent per crew of 3+)."
                + f"\n\nNEW IDEAS:\n{_fmt_ideas(m['ideas'])}"
                + (f"\n\nCURRENT DIRECTIONS (can also be voted for or kept with work_on):\n{_fmt_ideas(carried)}"
                   if carried else "")
                + f"\n\n{await _tried_digest()}"
                + f"\n\nTRANSCRIPT:\n{_fmt_transcript(m['transcript'])}"
            )
        elif S.state in ("meeting", "voting"):
            msg = None
        elif S.result_unseen and S.last_result and S.last_result["result"].get("level"):
            S.result_unseen = False
            d = S.last_result["result"].get("decision")
            mine = S.my_direction()
            msg = ("Council meeting over. "
                   + (f"It picked {d['idea']}: {d['text']} ({d['votes']}/{d['voters']} votes); "
                      f"{len(d['moved'])} agent(s) in other crews switch to it." if d else
                      "No idea got a majority of the delegates, so nothing changes." if S.last_result["result"].get("split")
                      else "No idea got a vote.")
                   + "\n\nBack to your crew's work. "
                   + (f"👉 YOUR DIRECTION: {mine['id']}: {mine['text']}\n" if mine else "Explore freely.\n")
                   + ("Call `sync_baseline` first (the baseline changed)." if S.baseline
                      and S.baseline["version"] > S.synced_version else ""))
        elif S.result_unseen and S.last_result:
            S.result_unseen = False
            m, res = S.last_result, S.last_result["result"]
            votes = "\n".join(f"  {p}: voted {v['idea']}, works on {v.get('work_on') or '-'}"
                              + (f" ({v['reason']})" if v["reason"] else "") for p, v in (m["votes"] or {}).items())
            mine = S.my_direction()
            msg = ("Meeting over. "
                   + ("The crew splits up to explore these directions in parallel:" if res["changed"]
                      else "No votes, so the directions stay as they were:")
                   + f"\n{_fmt_plan(S.directions, S.name)}\nVotes:\n{votes or '  (none)'}\n\n"
                   + (f"👉 YOUR DIRECTION: {mine['id']}: {mine['text']}\n" + await _best_on(mine["id"]) if mine
                      else "You have no assigned direction: explore freely.\n")
                   + ("Call `sync_baseline` first (the baseline changed), then " if S.baseline
                      and S.baseline["version"] > S.synced_version else "")
                   + "resume work on your direction.")
        elif S.state == "disconnected":
            return "Lost connection to the game server. Call `join_room` to reconnect."
        elif not idle or S.inbox or time.monotonic() >= deadline:
            S.last_idle_wait = time.monotonic()
            waited = f" (waited {S.last_idle_wait - start:.0f}s: nothing needs you)" if idle else ""
            return _with_notice(f"No meeting in progress{waited}. Continue your research.")
        else:
            msg = None
        if msg:
            return _with_notice(msg)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            if S.state not in ("meeting", "voting") or not S.meeting:
                continue  # an idle wait that ran out: the branch above answers
            m = S.meeting
            return _with_notice(f"Still waiting: round {m['round']}, current speaker {m['turn'] or '-'}"
                                f" (state {S.state}). Call `wait` again.")
        S.wake.clear()
        try:
            await asyncio.wait_for(S.wake.wait(), remaining)
        except asyncio.TimeoutError:
            pass


@mcp.tool()
async def say(text: str, propose: str | None = None) -> str:
    """Speak in the meeting. Only works on your turn, once per turn.
    propose: optional one-line idea worth exploring next. You hold at most one idea per
    meeting; proposing again replaces it."""
    idea = await S.request("say", text=text, propose=propose or None)
    S.my_turn = False
    extra = f" Your idea is on the table as {idea['id']}." if idea else ""
    return _with_notice(f"Said.{extra} Call `wait` for the next turn or for voting.")


@mcp.tool()
async def vote(idea: str, work_on: str | None = None, reason: str = "", long_bet: bool | None = None) -> str:
    """Final vote of a meeting.
    idea: the most promising idea by id (e.g. 'I2'); not your own (or, in a council, your crew's
    nominee) unless it's the only option.
    work_on: the idea YOU will explore next (may be your own), e.g. the split agreed in the discussion.
    Ideas with a vote or a claim become directions explored in parallel. Councils ignore work_on.
    long_bet: true to keep work_on as your LONG BET (crews of 3+, one per crew): for 30 minutes no
    crew vote or council moves you off it. false ends yours early."""
    await S.request("vote", idea=idea, work_on=work_on or None, reason=reason, long_bet=long_bet)
    S.voted = True
    return _with_notice("Vote cast. Call `wait` to see the result.")


@mcp.tool()
async def history(version: int | None = None, ref: str | None = None) -> str:
    """The room's research history, kept in git on the server. With no arguments: one line per shared
    baseline (v0 = start, v1, v2, ...) with who made it, the score, and the direction. With a version:
    the exact diff that version introduced. With ref (an experiment commit from `experiments`): what that
    experiment changed. Use it to study why something worked or failed."""
    if ref:
        diff = await S.request("diff", ref=ref)
        return _with_notice(f"{ref}:\n{diff[:12000] or '(no changes)'}")
    if version is None:
        log = await S.request("history")
        lines = [f"  {c['title']}  [{c['short']}]" + (f"\n      {c['body'][:200]}" if c["body"] else "")
                 for c in log]
        return _with_notice("Shared baselines, newest first:\n" + "\n".join(lines))
    diff = await S.request("diff", ref=f"v{version}")
    return _with_notice(f"Diff of v{version} against v{version - 1}:\n{diff[:12000] or '(no changes)'}")


@mcp.tool()
async def experiments(player: str | None = None, direction: str | None = None, limit: int = 15) -> str:
    """The crew's committed experiments, best score first (failed runs last). Filter by player and/or
    direction (e.g. 'I2'). Each line has a commit you can pass to `history(ref=...)` or `checkout`."""
    rows = await S.request("experiments", player=player, direction=direction, limit=limit)
    if not rows:
        return _with_notice("No experiments committed yet.")
    lines = [
        f"  {e['commit'][:8]}  {_fmt_score(e['score']) if e['ok'] else 'FAILED':>10}  {e['player']:<8} "
        f"{e.get('idea') or 'free':<5} v{e['baseline_version']}  {e.get('description') or e.get('note') or ''}"
        for e in rows
    ]
    return _with_notice("commit       score       player   dir   base  description\n" + "\n".join(lines))


@mcp.tool()
async def checkout(ref: str) -> str:
    """Load the editable files of any experiment commit, exp/ branch, or shared version (e.g. 'v3')
    into your workspace, e.g. to build on a teammate's promising run. Your current files are backed up."""
    await S.ensure()
    files = await S.request("files", ref=ref)
    if not files:
        return _with_notice(f"Nothing found at {ref}.")
    backup = S.write_files(files)
    return _with_notice(f"Loaded {', '.join(sorted(files))} from {ref}." +
                        (f" Previous files backed up in {backup}." if backup else "") +
                        " Run `run_eval` to measure it on your machine.")


@mcp.tool()
async def sync_baseline() -> str:
    """Overwrite your editable files with the shared best baseline (your old files are backed up first)."""
    await S.ensure()
    S.baseline = await S.request("baseline")
    if S.baseline["version"] == 0:
        return _with_notice("No accepted improvements yet; nothing to sync.")
    backup = S.sync_files()
    return _with_notice(
        f"Synced baseline v{S.baseline['version']} (score {_fmt_score(S.baseline['score'])} by {S.baseline['by']}). "
        + (f"Your previous files are backed up in {backup}." if backup else "")
    )


def main() -> None:
    ap = argparse.ArgumentParser(description="AmongAgents MCP bridge (run by your agent harness over stdio)")
    ap.add_argument("--server", default=os.environ.get("AMONG_SERVER"))
    ap.add_argument("--room", default=os.environ.get("AMONG_ROOM"))
    ap.add_argument("--name", default=os.environ.get("AMONG_NAME"))
    ap.add_argument("--workspace", default=os.environ.get("AMONG_WORKSPACE", "."),
                    help="also where `amongagents join` saved the room settings; flags override them")
    ap.add_argument("--trust-task", action="store_true", default=os.environ.get("AMONG_TRUST") == "1",
                    help="allow running the room's setup/eval commands on this machine")
    ap.add_argument("--sandbox", choices=["none", "docker"], default=os.environ.get("AMONG_SANDBOX"),
                    help="run setup/eval/holdout (and so any teammate's code) in a locked-down container")
    ap.add_argument("--sandbox-image", help="container image (default: the task's [sandbox] image)")
    ap.add_argument("--sandbox-gpus", action="store_true", default=None, help="pass GPUs into the container")
    ap.add_argument("--sandbox-cpus", help="CPU limit, e.g. 4")
    ap.add_argument("--sandbox-memory", help="memory limit (default 8g)")
    args = ap.parse_args()
    logging.basicConfig(stream=sys.stderr, level=logging.WARNING)
    global S
    ws = Path(args.workspace).resolve()
    ws.mkdir(parents=True, exist_ok=True)
    saved = load_join(ws)
    sb = saved.get("sandbox") or {}
    pick = lambda flag, value, default=None: flag if flag is not None else (value if value is not None else default)  # noqa: E731
    server, room, name = pick(args.server, saved.get("server"), "ws://localhost:8000"), \
        pick(args.room, saved.get("room")), pick(args.name, saved.get("name"))
    if not room or not name:
        ap.error("--room and --name are required (or run `amongagents join` to save them in the workspace)")
    sandbox = Sandbox(kind=pick(args.sandbox, sb.get("kind"), "none"), cpus=pick(args.sandbox_cpus, sb.get("cpus")),
                      memory=pick(args.sandbox_memory, sb.get("memory"), "8g"))
    S = Session(server, room, name, ws, args.trust_task or bool(saved.get("trust_task")), sandbox,
                {"image": pick(args.sandbox_image, sb.get("image")), "gpus": bool(pick(args.sandbox_gpus, sb.get("gpus")))})
    mcp.run()


if __name__ == "__main__":
    main()
