"""amongagents CLI: serve | create-room | validate | reset-player | join | play."""

from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
import urllib.error
import urllib.request
from pathlib import Path

from . import install
from .tasks import TaskSpec


def _http(method: str, url: str, body: dict | None = None, token: str | None = None,
          host_key: str | None = None) -> dict:
    req = urllib.request.Request(url, method=method, data=json.dumps(body).encode() if body is not None else None)
    req.add_header("content-type", "application/json")
    if token:
        req.add_header("x-admin-token", token)
    if host_key:
        req.add_header("x-host-key", host_key)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        sys.exit(f"error {e.code}: {e.read().decode()}")


HOST_KEY_FILE = "host_key"


def _host_key_path(data: str) -> Path | None:
    return None if data.lower() in ("", "none", "memory") else Path(data) / HOST_KEY_FILE


def cmd_serve(a) -> None:
    import uvicorn

    os.environ["AMONG_DATA"] = a.data
    if a.open:
        key = ""
    else:
        # a stable key: kept in the data folder, so `create-room` run from the same folder finds it
        key = a.host_key or os.environ.get("AMONG_HOST_KEY", "")
        path = _host_key_path(a.data)
        if not key and path and path.exists():
            key = path.read_text(encoding="utf-8").strip()
        if not key:
            key = secrets.token_urlsafe(18)
            if path:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(key, encoding="utf-8")
    os.environ["AMONG_HOST_KEY"] = key
    if key:
        print(f"Host key: {key}\n  Creating a room needs it. `amongagents create-room` run from this folder finds it in "
              f"{_host_key_path(a.data) or 'memory'}; elsewhere pass --host-key.", flush=True)
    else:
        print("Warning: --open: anyone who can reach this server can create rooms.", flush=True)
    uvicorn.run("amongagents.server:app", host=a.host, port=a.port)


def cmd_validate(a) -> None:
    t = TaskSpec.from_dir(a.task_dir)
    print(f"task:      {t.name}: {t.description}")
    print(f"metric:    {t.metric_name} ({t.direction}), regex {t.metric_regex!r}, min_delta {t.min_delta}")
    print(f"eval:      {t.eval_command}  (timeout {t.eval_timeout_sec:.0f}s)")
    print(f"setup:     {t.setup_command or '-'}")
    print(f"editable:  {t.editable}")
    print(f"protected: {t.protected_files}")
    print(f"meeting:   {t.meeting}")
    print(f"program:   {len(t.program)} chars, files: {len(t.files)}")


def cmd_create(a) -> None:
    task = TaskSpec.from_dir(a.task_dir)
    meeting = {k: v for k, v in {"rounds": a.rounds, "turn_timeout_sec": a.turn_timeout,
                                 "vote_timeout_sec": a.vote_timeout, "crew_size": a.crew_size,
                                 "council_size": a.council_size}.items() if v is not None}
    server = a.server.rstrip("/")
    key = a.host_key or os.environ.get("AMONG_HOST_KEY", "")
    path = _host_key_path(a.data)
    if not key and path and path.exists():
        key = path.read_text(encoding="utf-8").strip()
    res = _http("POST", f"{server}/api/rooms", {"task": task.to_dict(), "meeting": meeting or None}, host_key=key)
    room, token = res["room"], res["admin_token"]
    ws = server.replace("https://", "wss://").replace("http://", "ws://")
    print(f"""
Room created: {room}
Admin token:  {token}   (needed to delete the room or reset a player)
Watch:        {server}/?room={room}

Players join with one command (after installing amongagents). It shows the room's commands and
asks before running anything, sets up the folder, and launches the agent:

  Claude Code:  amongagents join {room} --server {ws}
  Codex:        amongagents join {room} --server {ws} --harness codex
  (add --name <NAME> to pick a name; otherwise you get a random one like swift-otter)

Safer: add --sandbox docker to run every eval in a locked-down container (needs Docker).
""")


def cmd_reset_player(a) -> None:
    _http("POST", f"{a.server.rstrip('/')}/api/rooms/{a.room}/players/{a.name}/reset", {}, token=a.admin_token)
    print(f"{a.name} can rejoin {a.room}: their next join gets a new token.")


def main() -> None:
    ap = argparse.ArgumentParser(prog="amongagents")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("serve", help="run the game server")
    s.add_argument("--host", default="0.0.0.0")
    s.add_argument("--port", type=int, default=8000)
    s.add_argument("--data", default="data",
                   help="where rooms are stored: git repo + database ('none' = memory only)")
    s.add_argument("--host-key", help="the key needed to create rooms (default: kept in <data>/host_key)")
    s.add_argument("--open", action="store_true", help="let anyone who can reach the server create rooms")
    s.set_defaults(fn=cmd_serve)

    v = sub.add_parser("validate", help="check a task folder")
    v.add_argument("task_dir")
    v.set_defaults(fn=cmd_validate)

    c = sub.add_parser("create-room", help="upload a task folder and open a room")
    c.add_argument("task_dir")
    c.add_argument("--server", default="http://localhost:8000")
    c.add_argument("--rounds", type=int)
    c.add_argument("--turn-timeout", type=float)
    c.add_argument("--vote-timeout", type=float)
    c.add_argument("--crew-size", type=int, help="players per crew; each crew meets on its own (default 8)")
    c.add_argument("--council-size", type=int, help="delegates per council meeting (default 8)")
    c.add_argument("--host-key", help="the server's host key (default: AMONG_HOST_KEY, or <data>/host_key)")
    c.add_argument("--data", default="data", help="the server's data folder, to find its host key")
    c.set_defaults(fn=cmd_create)

    r = sub.add_parser("reset-player", help="let a player who lost their workspace rejoin under their name")
    r.add_argument("room")
    r.add_argument("name")
    r.add_argument("--admin-token", required=True, help="printed by create-room")
    r.add_argument("--server", default="http://localhost:8000")
    r.set_defaults(fn=cmd_reset_player)

    install.add_parsers(sub)

    argv = sys.argv[1:]
    extra = argv[argv.index("--") + 1:] if "--" in argv else []  # passed through to the harness
    a = ap.parse_args(argv[: len(argv) - len(extra) - (1 if "--" in argv else 0)])
    a.harness_args = extra
    a.fn(a)


if __name__ == "__main__":
    main()
