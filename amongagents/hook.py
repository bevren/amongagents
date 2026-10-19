"""Claude Code hook: interrupts a working agent when an emergency meeting starts.

Configured as a PostToolUse hook (see README). After every tool call it asks
the game server whether a meeting is running and, if so, injects a notice
into the agent's context, even while the agent is busy with Bash or edits.
"""

from __future__ import annotations

import json
import sys
import time
import urllib.request
from pathlib import Path
from urllib.parse import quote


def main() -> None:
    try:
        hook_input = json.load(sys.stdin)
    except Exception:
        hook_input = {}
    if "amongagents" in str(hook_input.get("tool_name", "")):
        return  # bridge tools already carry the notice (Claude Code and Codex name them differently)
    cwd = Path(hook_input.get("cwd") or ".")
    session_file = cwd / ".amongagents" / "session.json"
    if not session_file.exists():
        return
    log = session_file.parent / "hook.log"
    try:
        s = json.loads(session_file.read_text())
        url = f"{s['http']}/api/rooms/{quote(s['room'])}/players/{quote(s['name'])}"
        with urllib.request.urlopen(url, timeout=3) as r:
            st = json.load(r)  # just this player's meeting, not the whole room
    except Exception as e:
        _log(log, f"error {e!r}")
        return
    m = st.get("meeting")
    if st.get("state") not in ("meeting", "voting") or not m:
        _log(log, f"checked after {hook_input.get('tool_name')}: state={st.get('state')}, nothing to inject")
        return
    if st["state"] == "voting":
        if m.get("voted"):
            return
        msg = "🚨 AmongAgents: VOTING IS OPEN. Stop what you're doing and call the amongagents `wait` tool, then `vote`."
    elif m.get("turn") == s["name"]:
        msg = "🚨 AmongAgents: IT'S YOUR TURN in the meeting. Call the amongagents `wait` tool now, then `say`."
    else:
        what = (f"COUNCIL MEETING {m['group']}: you're a delegate" if m.get("level") else
                "STAGNATION MEETING called by the room" if m["kind"] == "stagnation" else
                f"EMERGENCY MEETING called by {m['reporter']} ({m['kind']})")
        msg = f"🚨 AmongAgents: {what}. Pause your current work and call the amongagents `wait` tool."
    _log(log, f"INJECTED after {hook_input.get('tool_name')}: {msg[:80]}")
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": msg}}))


def _log(path: Path, line: str) -> None:
    try:
        with path.open("a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%H:%M:%S')} {line}\n")
    except Exception:
        pass


if __name__ == "__main__":
    main()
