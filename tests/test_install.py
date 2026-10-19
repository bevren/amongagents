import json
import sys
import tomllib
from types import SimpleNamespace

from amongagents import bridge, install


def test_claude_config_merges_into_existing_files(tmp_path):
    (tmp_path / ".mcp.json").write_text(json.dumps({"mcpServers": {"other": {"command": "x"}}}))
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "settings.local.json").write_text(json.dumps({
        "permissions": {"allow": ["Bash(ls)"]},
        "hooks": {"PostToolUse": [{"matcher": "Edit", "hooks": [{"type": "command", "command": "fmt"}]}]}}))
    for _ in range(2):  # idempotent: joining again doesn't duplicate anything
        install.configure_claude(tmp_path)

    mcp = json.loads((tmp_path / ".mcp.json").read_text())
    assert set(mcp["mcpServers"]) == {"other", "amongagents"}
    server = mcp["mcpServers"]["amongagents"]
    assert server["command"] == sys.executable
    assert server["args"] == ["-m", "amongagents.bridge", "--workspace", str(tmp_path)]

    s = json.loads((tmp_path / ".claude" / "settings.local.json").read_text())
    assert s["enabledMcpjsonServers"] == ["amongagents"]
    assert s["permissions"]["allow"] == ["Bash(ls)", "mcp__amongagents"]
    post = s["hooks"]["PostToolUse"]
    assert len(post) == 2 and post[0]["matcher"] == "Edit"  # the user's own hook is kept
    assert post[1]["hooks"][0]["command"].endswith('hook.py"')


def test_codex_overrides_are_valid_toml(tmp_path):
    ws = tmp_path / "with space"
    o = install.codex_overrides(ws)
    assert o[0::2] == ["-c", "-c"]
    for kv in o[1::2]:
        key, value = kv.split("=", 1)
        parsed = tomllib.loads(f"v = {value}")["v"]
        if key == "mcp_servers.amongagents":
            assert parsed["command"] == sys.executable and parsed["args"][-1] == str(ws)
            assert parsed["default_tools_approval_mode"] == "approve"
        else:
            assert key == "hooks.PostToolUse"
            assert "hook.py" in parsed[0]["hooks"][0]["command"]


def test_bridge_reads_saved_join_settings(tmp_path, monkeypatch):
    install._save_json(tmp_path / install.JOIN_FILE, {
        "server": "ws://example:9", "room": "ABC123", "name": "alice", "trust_task": True,
        "sandbox": {"kind": "docker", "memory": "4g", "gpus": True}})
    seen = {}
    monkeypatch.setattr(bridge.mcp, "run", lambda: seen.update(s=bridge.S))
    monkeypatch.setattr(sys, "argv", ["bridge", "--workspace", str(tmp_path), "--name", "bob"])
    bridge.main()
    s = seen["s"]
    assert (s.server, s.room, s.name, s.trust) == ("ws://example:9", "ABC123", "bob", True)  # flags win
    assert s.sandbox.kind == "docker" and s.sandbox.memory == "4g"


def test_join_needs_consent_without_a_terminal(tmp_path, monkeypatch):
    snap = {"room": "R", "task": {"name": "t", "description": "d", "commands": {"eval": "python eval.py"}}}
    monkeypatch.setattr(install, "_room_info", lambda server, room: snap)
    monkeypatch.setattr("builtins.input", lambda _: (_ for _ in ()).throw(EOFError()))
    a = SimpleNamespace(name="alice", room="R", server="ws://x", dir=str(tmp_path / "ws"), yes=False,
                        sandbox="none", harness="claude", harness_args=[], no_launch=True, sandbox_image=None,
                        sandbox_gpus=False, sandbox_cpus=None, sandbox_memory="8g")
    try:
        install.join(a)
        raise AssertionError("joined without consent")
    except SystemExit as e:
        assert "--yes" in str(e)
    assert not (tmp_path / "ws").exists()
    a.yes = True
    install.join(a)
    assert json.loads((tmp_path / "ws" / install.JOIN_FILE).read_text())["room"] == "R"
    assert (tmp_path / "ws" / ".mcp.json").exists()


def test_play_can_switch_to_a_new_server_address(tmp_path):
    """A host's quick tunnel gets a new address on every restart; players move with `play --server`."""
    install._save_json(tmp_path / install.JOIN_FILE, {"server": "wss://old.trycloudflare.com", "room": "R",
                                                      "name": "alice", "harness": "claude"})
    a = SimpleNamespace(dir=str(tmp_path), server="wss://new.trycloudflare.com", harness=None,
                        harness_args=[], no_launch=True)
    install.play(a)
    cfg = json.loads((tmp_path / install.JOIN_FILE).read_text())
    assert cfg["server"] == "wss://new.trycloudflare.com" and cfg["room"] == "R"


def test_join_without_a_name_picks_one_nobody_has(tmp_path, monkeypatch):
    snap = {"room": "R", "players": [{"name": "swift-otter"}],
            "task": {"name": "t", "description": "d", "commands": {"eval": "python eval.py"}}}
    monkeypatch.setattr(install, "_room_info", lambda server, room: snap)
    monkeypatch.chdir(tmp_path)
    a = SimpleNamespace(name=None, room="R", server="ws://x", dir=None, yes=True, sandbox="none",
                        harness="claude", harness_args=[], no_launch=True, sandbox_image=None,
                        sandbox_gpus=False, sandbox_cpus=None, sandbox_memory="8g")
    install.join(a)
    cfg = json.loads((tmp_path / f"amongagents-R-{a.name}" / install.JOIN_FILE).read_text())
    assert cfg["name"] == a.name and a.name != "swift-otter"
    adj, animal = a.name.split("-")
    assert adj in install.ADJECTIVES and animal in install.ANIMALS
    assert install.random_name({f"{x}-{y}" for x in install.ADJECTIVES for y in install.ANIMALS}).startswith("player-")
