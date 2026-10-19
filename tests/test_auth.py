"""Player tokens (nobody takes over a disconnected name) and the host key (who can create rooms)."""

import asyncio

import pytest
from fastapi.testclient import TestClient

from amongagents.game import GameError, Room

from test_history import make_task, noop


def test_a_name_belongs_to_whoever_holds_its_token():
    async def main():
        room = Room("R", make_task())
        w = await room.join("alice", noop, "")
        token = w["token"]
        assert token and room.players["alice"].token_hash != token  # only a hash is kept
        await room.leave("alice")

        with pytest.raises(GameError, match="taken"):
            await room.join("alice", noop, "")
        with pytest.raises(GameError, match="taken"):
            await room.join("alice", noop, "guess")
        again = await room.join("alice", noop, token)
        assert "token" not in again  # the same token keeps working; no new one is issued

        # the token survives a restart
        await room.leave("alice")
        restored = Room.from_state(room.to_state())
        with pytest.raises(GameError, match="taken"):
            await restored.join("alice", noop, "")
        await restored.join("alice", noop, token)

        # the host resets a player who lost their workspace; their next join claims the name
        await restored.leave("alice")
        restored.reset_token("alice")
        fresh = await restored.join("alice", noop, "")
        assert fresh["token"] and fresh["token"] != token

    asyncio.run(main())


def test_players_saved_before_tokens_claim_their_name_on_rejoin():
    async def main():
        room = Room("R", make_task())
        await room.join("bob", noop)  # in-process: no token check
        state = room.to_state()
        for p in state["players"]:
            p.pop("token_hash")
        old = Room.from_state(state)
        w = await old.join("bob", noop, "")
        assert w["token"]
        await old.leave("bob")
        with pytest.raises(GameError, match="taken"):
            await old.join("bob", noop, "")

    asyncio.run(main())


def test_server_needs_the_host_key_and_a_hello(monkeypatch):
    monkeypatch.setenv("AMONG_DATA", "none")
    monkeypatch.setenv("AMONG_HOST_KEY", "sesame")
    from amongagents import server

    server.rooms.clear()
    body = {"task": make_task().to_dict()}
    with TestClient(server.app) as c:
        assert c.post("/api/rooms", json=body).status_code == 403
        assert c.post("/api/rooms", json=body, headers={"x-host-key": "wrong"}).status_code == 403
        res = c.post("/api/rooms", json=body, headers={"x-host-key": "sesame"}).json()
        room = res["room"]

        def join(token):
            with c.websocket_connect(f"/ws/rooms/{room}/play?name=alice") as ws:
                ws.send_json({"type": "hello", "token": token})
                while True:
                    msg = ws.receive_json()
                    if msg["type"] in ("welcome", "fatal"):
                        return msg

        first = join("")
        assert first["type"] == "welcome" and first["token"]
        assert join("")["type"] == "fatal"
        assert join(first["token"])["type"] == "welcome"

        # the host resets alice with the room's admin token
        assert c.post(f"/api/rooms/{room}/players/alice/reset").status_code == 403
        r = c.post(f"/api/rooms/{room}/players/alice/reset", headers={"x-admin-token": res["admin_token"]})
        assert r.status_code == 200
        assert join("")["type"] == "welcome"

        # an old client that doesn't say hello gets a clear error, not a hang
        with c.websocket_connect(f"/ws/rooms/{room}/play?name=carol") as ws:
            ws.send_json({"type": "snapshot"})
            msg = ws.receive_json()
            assert msg["type"] == "fatal" and "newer client" in msg["error"]
    server.rooms.clear()


def test_without_a_host_key_anyone_can_create_rooms(monkeypatch):
    monkeypatch.setenv("AMONG_DATA", "none")
    monkeypatch.delenv("AMONG_HOST_KEY", raising=False)
    from amongagents import server

    server.rooms.clear()
    with TestClient(server.app) as c:
        assert c.post("/api/rooms", json={"task": make_task().to_dict()}).status_code == 200
    server.rooms.clear()
