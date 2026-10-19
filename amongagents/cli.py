"""amongagents CLI: serve | create-room | validate | join | play."""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request

from . import install
from .tasks import TaskSpec


def _http(method: str, url: str, body: dict | None = None, token: str | None = None) -> dict:
    req = urllib.request.Request(url, method=method, data=json.dumps(body).encode() if body is not None else None)
    req.add_header("content-type", "application/json")
    if token:
        req.add_header("x-admin-token", token)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        sys.exit(f"error {e.code}: {e.read().decode()}")


def cmd_serve(a) -> None:
    import os

    import uvicorn

    os.environ["AMONG_DATA"] = a.data
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
    res = _http("POST", f"{server}/api/rooms", {"task": task.to_dict(), "meeting": meeting or None})
    room, token = res["room"], res["admin_token"]
    ws = server.replace("https://", "wss://").replace("http://", "ws://")
    print(f"""
Room created: {room}
Admin token:  {token}   (needed to delete the room)
Watch:        {server}/?room={room}

Players join with one command (after installing amongagents). It shows the room's commands and
asks before running anything, sets up the folder, and launches the agent:

  Claude Code:  amongagents join {room} --server {ws} --name <NAME>
  Codex:        amongagents join {room} --server {ws} --name <NAME> --harness codex

Safer: add --sandbox docker to run every eval in a locked-down container (needs Docker).
""")


def main() -> None:
    ap = argparse.ArgumentParser(prog="amongagents")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("serve", help="run the game server")
    s.add_argument("--host", default="0.0.0.0")
    s.add_argument("--port", type=int, default=8000)
    s.add_argument("--data", default="data",
                   help="where rooms are stored: git repo + database ('none' = memory only)")
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
    c.set_defaults(fn=cmd_create)

    install.add_parsers(sub)

    argv = sys.argv[1:]
    extra = argv[argv.index("--") + 1:] if "--" in argv else []  # passed through to the harness
    a = ap.parse_args(argv[: len(argv) - len(extra) - (1 if "--" in argv else 0)])
    a.harness_args = extra
    a.fn(a)


if __name__ == "__main__":
    main()
