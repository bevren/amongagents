"""`amongagents join`: set up a workspace for a harness and launch it, with no config editing.

    amongagents join ROOM --server ws://HOST:8000 --name alice              # Claude Code
    amongagents join ROOM --server ws://HOST:8000 --name bob --harness codex

Everything is written inside the workspace folder, never into the harness's global config:

    .amongagents/join.json        server, room, name, and sandbox settings (read by the bridge)
    Claude Code: .mcp.json        the amongagents MCP server
                 .claude/settings.local.json
                                  enables that server, allows the game tools without prompts,
                                  and adds the meeting hook (PostToolUse)
    Codex:       nothing; the MCP server and the hook are passed as `-c` overrides at launch

`amongagents play` relaunches the harness in an existing workspace.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

PKG_ROOT = Path(__file__).resolve().parent.parent  # on PYTHONPATH, so source checkouts work too
JOIN_FILE = Path(".amongagents") / "join.json"
HARNESSES = ("claude", "codex")
# one line, no quotes or shell metacharacters: it goes through cmd.exe for Codex's npm shim on Windows
PLAY_PROMPT = ("Call the amongagents join_room tool and follow the rules it returns. "
               "This is a long-running game: never stop on your own; there is no finish line.")


def http_base(server: str) -> str:
    return re.sub(r"^ws", "http", server.rstrip("/"))


def bridge_command(ws: Path) -> tuple[str, list[str], dict[str, str]]:
    return sys.executable, ["-m", "amongagents.bridge", "--workspace", str(ws)], {"PYTHONPATH": str(PKG_ROOT)}


def hook_command(harness: str = "claude") -> str:
    # by path, so it runs even where the package isn't installed; the hook only needs the stdlib
    cmd = f'"{sys.executable}" "{PKG_ROOT / "amongagents" / "hook.py"}"'
    # Claude Code runs hooks with Git Bash on Windows; Codex runs them with PowerShell, where a
    # quoted program path needs the call operator
    return f"& {cmd}" if harness == "codex" and os.name == "nt" else cmd


def codex_exe() -> list[str] | None:
    """The Codex launcher. On Windows npm installs a codex.cmd shim, and cmd.exe mangles quotes
    in arguments (the -c overrides have plenty), so run its codex.js with node directly."""
    exe = shutil.which("codex")
    if exe and os.name == "nt" and exe.lower().endswith((".cmd", ".bat")):
        js = Path(exe).parent / "node_modules" / "@openai" / "codex" / "bin" / "codex.js"
        node = shutil.which("node")
        if js.exists() and node:
            return [node, str(js)]
    return [exe] if exe else None


def _load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8") or "{}")
    except ValueError:
        sys.exit(f"{path} isn't valid JSON; fix or remove it and run join again")
    return data if isinstance(data, dict) else {}


def _save_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def configure_claude(ws: Path) -> list[Path]:
    """Project-scoped config, merged into whatever the folder already has."""
    command, args, env = bridge_command(ws)
    mcp_path = ws / ".mcp.json"
    mcp = _load_json(mcp_path)
    mcp.setdefault("mcpServers", {})["amongagents"] = {"command": command, "args": args, "env": env}
    _save_json(mcp_path, mcp)

    settings_path = ws / ".claude" / "settings.local.json"
    s = _load_json(settings_path)
    enabled = s.setdefault("enabledMcpjsonServers", [])
    if "amongagents" not in enabled:
        enabled.append("amongagents")
    allow = s.setdefault("permissions", {}).setdefault("allow", [])
    if "mcp__amongagents" not in allow:
        allow.append("mcp__amongagents")
    post = s.setdefault("hooks", {}).setdefault("PostToolUse", [])
    post[:] = [e for e in post if not any("amongagents" in h.get("command", "") for h in e.get("hooks", []))]
    post.append({"matcher": "*", "hooks": [{"type": "command", "command": hook_command(), "timeout": 10}]})
    _save_json(settings_path, s)
    return [mcp_path, settings_path]


def _toml(v) -> str:
    """Inline TOML for -c overrides (JSON strings are valid TOML basic strings)."""
    if isinstance(v, dict):
        return "{" + ", ".join(f"{k} = {_toml(x)}" for k, x in v.items()) + "}"
    if isinstance(v, list):
        return "[" + ", ".join(_toml(x) for x in v) + "]"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    return json.dumps(str(v))


def codex_overrides(ws: Path) -> list[str]:
    command, args, env = bridge_command(ws)
    server = {"command": command, "args": args, "env": env,
              "startup_timeout_sec": 30, "tool_timeout_sec": 900,  # holdout checks can take a while
              "default_tools_approval_mode": "approve"}  # the game tools, like mcp__amongagents for Claude
    hook = [{"matcher": "*", "hooks": [{"type": "command", "command": hook_command("codex"), "timeout": 10}]}]
    return ["-c", f"mcp_servers.amongagents={_toml(server)}", "-c", f"hooks.PostToolUse={_toml(hook)}"]


def launch_argv(harness: str, ws: Path, extra: list[str]) -> list[str]:
    exe = codex_exe() if harness == "codex" else ([p] if (p := shutil.which(harness)) else None)
    if not exe:
        sys.exit(f"`{harness}` isn't on PATH. Install it, or run with --no-launch and start it yourself.")
    if harness == "codex":
        return [*exe, *codex_overrides(ws), *extra, PLAY_PROMPT]
    return [*exe, *extra, PLAY_PROMPT]


def _room_info(server: str, room: str) -> dict:
    url = f"{http_base(server)}/api/rooms/{room}"
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        sys.exit(f"room {room} not found on {server} ({e.code})" if e.code == 404 else f"{url}: HTTP {e.code}")
    except OSError as e:
        sys.exit(f"can't reach the game server at {http_base(server)}: {e}")


def _consent(snap: dict, sandbox: str, yes: bool) -> None:
    """Joining runs the room's commands, and through them teammates' code, on this machine."""
    t = snap["task"]
    cmds = t.get("commands") or {}
    print(f"Room {snap['room']}: {t['name']}: {t['description']}")
    print("Playing runs these commands in your workspace, and through them code your teammates wrote:")
    for k in ("setup", "eval", "holdout"):
        if cmds.get(k):
            print(f"  {k:8} {cmds[k]}")
    print("  inside a locked-down Docker container (no network for evals, only the workspace mounted)"
          if sandbox == "docker" else
          "  directly on this machine, with your user's permissions. Add --sandbox docker to isolate them.")
    if yes:
        return
    if not sys.stdin.isatty():
        sys.exit("pass --yes to agree to running these commands (no terminal to ask on)")
    try:
        answer = input("Continue? [y/N] ")
    except EOFError:  # Windows reports NUL as a terminal
        sys.exit("\npass --yes to agree to running these commands (no terminal to ask on)")
    if answer.strip().lower() not in ("y", "yes"):
        sys.exit("not joined")


def join(a) -> None:
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", a.name):
        sys.exit("--name: letters, digits, - and _ only (max 32)")
    ws = Path(a.dir or f"amongagents-{a.room}-{a.name}").resolve()
    snap = _room_info(a.server, a.room)
    _consent(snap, a.sandbox, a.yes)
    ws.mkdir(parents=True, exist_ok=True)
    cfg = {"server": a.server, "room": a.room, "name": a.name, "harness": a.harness, "trust_task": True,
           "sandbox": {"kind": a.sandbox, "image": a.sandbox_image, "gpus": a.sandbox_gpus,
                       "cpus": a.sandbox_cpus, "memory": a.sandbox_memory}}
    _save_json(ws / JOIN_FILE, cfg)
    written = [ws / JOIN_FILE]
    if a.harness == "claude":
        written += configure_claude(ws)
    print(f"\nWorkspace: {ws}")
    for p in written:
        print(f"  wrote {p.relative_to(ws)}")
    _launch(a.harness, ws, a.harness_args, a.no_launch)


def play(a) -> None:
    ws = Path(a.dir).resolve()
    cfg = _load_json(ws / JOIN_FILE)
    if not cfg:
        sys.exit(f"{ws} isn't an AmongAgents workspace; run `amongagents join` first")
    harness = a.harness or cfg.get("harness", "claude")
    if harness == "claude":
        configure_claude(ws)  # refresh paths in case Python or the package moved
    _launch(harness, ws, a.harness_args, a.no_launch)


def _launch(harness: str, ws: Path, extra: list[str], no_launch: bool) -> None:
    argv = launch_argv(harness, ws, extra) if not no_launch else None
    if no_launch:
        how = (f'cd "{ws}" && claude "{PLAY_PROMPT}"' if harness == "claude"
               else f'amongagents play --dir "{ws}" --harness codex')
        print(f"\nStart it with:\n  {how}")
        return
    print(f"\nLaunching {harness} in {ws} ...\n", flush=True)
    sys.exit(subprocess.call(argv, cwd=ws))


def load_join(ws: Path) -> dict:
    return _load_json(ws / JOIN_FILE)


def add_parsers(sub) -> None:
    j = sub.add_parser("join", help="set up a workspace for Claude Code or Codex and start playing")
    j.add_argument("room")
    j.add_argument("--server", default=os.environ.get("AMONG_SERVER", "ws://localhost:8000"))
    j.add_argument("--name", required=True)
    j.add_argument("--dir", help="workspace folder (default: ./amongagents-ROOM-NAME)")
    _common(j)
    j.add_argument("--yes", "-y", action="store_true", help="agree to run the room's commands without asking")
    j.add_argument("--sandbox", choices=["none", "docker"], default="none",
                   help="run setup/eval/holdout (and so teammates' code) in a locked-down container")
    j.add_argument("--sandbox-image", help="container image (default: the task's)")
    j.add_argument("--sandbox-gpus", action="store_true")
    j.add_argument("--sandbox-cpus")
    j.add_argument("--sandbox-memory", default="8g")
    j.set_defaults(fn=join)

    p = sub.add_parser("play", help="relaunch the harness in an existing workspace")
    p.add_argument("--dir", default=".")
    _common(p, default=None)
    p.set_defaults(fn=play)


def _common(p, default: str | None = "claude") -> None:
    p.add_argument("--harness", choices=HARNESSES, default=default)
    p.add_argument("--no-launch", action="store_true", help="only write the config")
    p.epilog = "Arguments after -- go to the harness, e.g. -- --model claude-haiku-5-5"
