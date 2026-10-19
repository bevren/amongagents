"""FastAPI server: room HTTP API, player WebSocket, spectator WebSocket, web UI."""

from __future__ import annotations

import asyncio
import logging
import os
import secrets
import string
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, PlainTextResponse
from pydantic import BaseModel

from .game import GameError, Room
from .history import RoomStore
from .tasks import TaskSpec

STATIC = Path(__file__).parent / "static"
log = logging.getLogger("amongagents.server")
rooms: dict[str, Room] = {}
admin_tokens: dict[str, str] = {}


def data_dir() -> Path | None:
    """Where rooms are stored (git repo + database). AMONG_DATA=none keeps rooms in memory."""
    raw = os.environ.get("AMONG_DATA", "data")
    return None if raw.lower() in ("", "none", "memory") else Path(raw)


def load_rooms(root: Path) -> None:
    for d in sorted(root.iterdir()) if root.exists() else []:
        if not d.is_dir() or d.name.upper() in rooms:
            continue
        store = RoomStore(root, d.name)
        try:
            if not store.has_state():
                continue
            rooms[d.name] = Room.from_state(store.load_state(), store)
            admin_tokens[d.name] = store.load_meta().get("admin_token", "")
        except Exception as e:  # one broken room must not keep the server down
            log.error("could not restore room %s: %r", d.name, e)


@asynccontextmanager
async def lifespan(_: FastAPI):
    root = data_dir()
    if root:
        load_rooms(root)
        log.warning("loaded %d room(s) from %s", len(rooms), root.resolve())
    yield


app = FastAPI(title="AmongAgents", lifespan=lifespan)


def _room(room_id: str) -> Room:
    room = rooms.get(room_id.upper())
    if room is None:
        raise HTTPException(404, f"room {room_id} not found")
    return room


def host_key() -> str:
    """Set (by `amongagents serve`, or AMONG_HOST_KEY): creating a room needs it. Empty: anyone can."""
    return os.environ.get("AMONG_HOST_KEY", "")


def _check_admin(room_id: str, request: Request) -> None:
    token = request.headers.get("x-admin-token", "")
    if not secrets.compare_digest(token, admin_tokens.get(room_id.upper(), "")):
        raise HTTPException(403, "admin token required")


class CreateRoom(BaseModel):
    task: dict
    meeting: dict | None = None  # overrides task.toml [meeting]


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


@app.get("/api/rooms")
async def list_rooms() -> list[dict]:
    return [
        {"room": r.id, "task": r.task.name, "state": r.state, "crews": len(r._crews()),
         "players": sum(p.connected for p in r.players.values()), "best_score": r.best_score,
         "created": r.created}
        for r in rooms.values()
    ]


@app.post("/api/rooms")
async def create_room(body: CreateRoom, request: Request) -> dict:
    key = host_key()
    if key and not secrets.compare_digest(request.headers.get("x-host-key", ""), key):
        raise HTTPException(403, "creating a room needs the server's host key (see `amongagents serve`)")
    task_dict = dict(body.task)
    if body.meeting:
        task_dict["meeting"] = {**task_dict.get("meeting", {}), **body.meeting}
    try:
        task = TaskSpec.from_dict(task_dict)
    except (TypeError, ValueError) as e:
        raise HTTPException(400, f"invalid task: {e}") from e
    room_id = "".join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(6))
    token = secrets.token_urlsafe(16)
    store = None
    if root := data_dir():
        store = RoomStore(root, room_id)
        baseline = {p: task.files[p] for p in task.editable if p in task.files}
        try:
            await asyncio.to_thread(store.init, room_id, task, baseline, {"admin_token": token})
        except Exception as e:
            raise HTTPException(500, f"could not create the room's git repo: {e}") from e
    room = Room(room_id, task, store)
    if store:
        room.version_commits[0] = (await asyncio.to_thread(store.log))[-1]["commit"]
        await asyncio.to_thread(store.save, room.live_state())
    rooms[room_id] = room
    admin_tokens[room_id] = token
    return {"room": room_id, "admin_token": token, "state": room.state, "history": bool(store)}


@app.get("/api/rooms/{room_id}")
async def get_room(room_id: str, crew: str | None = None) -> dict:
    """The room as spectators see it, centred on one crew or council (default: the main storyline)."""
    return _room(room_id).snapshot(focus=crew)


@app.get("/api/rooms/{room_id}/players/{name}")
async def get_player(room_id: str, name: str) -> dict:
    """A player's meeting status: what the meeting hook polls after every tool call."""
    room = _room(room_id)
    if name not in room.players:
        raise HTTPException(404, f"no player {name!r} in room {room_id}")
    return room.player_status(name)


def _store(room_id: str) -> RoomStore:
    room = _room(room_id)
    if not room.store:
        raise HTTPException(404, "this room has no history (server runs with AMONG_DATA=none)")
    return room.store


@app.get("/api/rooms/{room_id}/history")
async def get_history(room_id: str) -> list[dict]:
    return await asyncio.to_thread(_store(room_id).log)


def _ref(ref: str) -> str:
    return f"v{ref}" if ref.isdigit() else ref


@app.get("/api/rooms/{room_id}/diff/{ref:path}", response_class=PlainTextResponse)
async def get_diff(room_id: str, ref: str) -> str:
    """What one version (`3` or `v3`), experiment commit, or exp/ branch tip changed."""
    try:
        return await asyncio.to_thread(_store(room_id).diff_ref, _ref(ref))
    except (ValueError, RuntimeError) as e:
        raise HTTPException(404, str(e)) from e


@app.get("/api/rooms/{room_id}/files/{ref:path}")
async def get_files(room_id: str, ref: str) -> dict:
    room = _room(room_id)
    try:
        return await asyncio.to_thread(_store(room_id).files_at, _ref(ref), room.task.editable)
    except (ValueError, RuntimeError) as e:
        raise HTTPException(404, str(e)) from e


@app.get("/api/rooms/{room_id}/experiments")
async def get_experiments(room_id: str, player: str | None = None, direction: str | None = None,
                          limit: int = 50) -> list[dict]:
    return await asyncio.to_thread(_room(room_id).experiments, player, direction, limit)


@app.get("/api/rooms/{room_id}/results.tsv", response_class=PlainTextResponse)
async def get_results(room_id: str) -> str:
    return await asyncio.to_thread(_store(room_id).results_text)


@app.delete("/api/rooms/{room_id}")
async def delete_room(room_id: str, request: Request) -> dict:
    _check_admin(room_id, request)
    room = rooms.pop(room_id.upper(), None)
    if room:
        await room.close()
        if room.store and room.store.dir.exists():  # keep the history, but don't restore the room again
            await asyncio.to_thread(room.store.mark_deleted)
    return {"deleted": room_id}


def _sender(ws: WebSocket):
    lock = asyncio.Lock()

    async def send(msg: dict) -> None:
        async with lock:
            await ws.send_json(msg)

    return send


@app.post("/api/rooms/{room_id}/players/{name}/reset")
async def reset_player(room_id: str, name: str, request: Request) -> dict:
    """Admin: let a player who lost their workspace (and so their token) rejoin under their name."""
    _check_admin(room_id, request)
    try:
        _room(room_id).reset_token(name)
    except GameError as e:
        raise HTTPException(400, str(e)) from e
    return {"reset": name}


@app.websocket("/ws/rooms/{room_id}/play")
async def play(ws: WebSocket, room_id: str, name: str) -> None:
    await ws.accept()
    room = rooms.get(room_id.upper())
    send = _sender(ws)
    if room is None:
        await send({"type": "fatal", "error": f"room {room_id} not found"})
        await ws.close()
        return
    try:
        # the token comes in a first message, not the URL, so it never shows up in access logs
        hello = await asyncio.wait_for(ws.receive_json(), 15)
        if not isinstance(hello, dict) or hello.get("type") != "hello":
            raise GameError("this server needs a newer client: run `pip install -U` for amongagents")
        welcome = await room.join(name, send, str(hello.get("token") or ""))
    except WebSocketDisconnect:
        return
    except (GameError, asyncio.TimeoutError) as e:
        if isinstance(e, asyncio.TimeoutError):
            e = GameError("no hello from the client")
        await send({"type": "fatal", "error": str(e)})
        await ws.close()
        return
    name = welcome["player"]
    await send(welcome)

    handlers = {
        "eval": lambda m: room.record_eval(name, m.get("score"), bool(m.get("ok")), m.get("note", ""),
                                           m.get("description", ""), m.get("files"), m.get("based_on")),
        "report": lambda m: room.report(name, m.get("kind", ""), m.get("summary", ""), m.get("score"),
                                        m.get("files"), m.get("propose"), m.get("holdout")),
        "say": lambda m: room.say(name, m.get("text", ""), m.get("propose")),
        "vote": lambda m: room.vote(name, m.get("idea", ""), m.get("reason", ""), m.get("work_on")),
    }
    try:
        while True:
            msg = await ws.receive_json()
            op, req_id = msg.get("type"), msg.get("req_id")
            try:
                if op == "snapshot":
                    data = room.snapshot(viewer=name)
                elif op == "baseline":
                    data = room.baseline(name)
                elif op == "challenge":
                    data = room.challenge(name)
                elif op == "experiments":
                    data = await asyncio.to_thread(room.experiments, msg.get("player"), msg.get("direction"),
                                                   int(msg.get("limit", 20)))
                elif op in ("history", "diff", "files"):
                    if not room.store:
                        raise GameError("this room has no history")
                    ref = _ref(str(msg.get("ref", "")))
                    try:
                        if op == "history":
                            data = await asyncio.to_thread(room.store.log)
                        elif op == "diff":
                            data = await asyncio.to_thread(room.store.diff_ref, ref)
                        else:
                            data = await asyncio.to_thread(room.store.files_at, ref, room.task.editable)
                    except (ValueError, RuntimeError) as e:
                        raise GameError(str(e)) from e
                elif op in handlers:
                    data = await handlers[op](msg)
                    if hasattr(data, "public"):
                        data = data.public()
                else:
                    raise GameError(f"unknown op {op!r}")
                await send({"type": "reply", "req_id": req_id, "ok": True, "data": data})
            except GameError as e:
                await send({"type": "reply", "req_id": req_id, "ok": False, "error": str(e)})
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        await room.leave(name)


@app.websocket("/ws/rooms/{room_id}/watch")
async def watch(ws: WebSocket, room_id: str) -> None:
    await ws.accept()
    room = rooms.get(room_id.upper())
    if room is None:
        await ws.send_json({"type": "fatal", "error": f"room {room_id} not found"})
        await ws.close()
        return
    send = _sender(ws)
    await send({"type": "snapshot", "snapshot": room.snapshot()})
    room.spectators.add(send)
    try:
        while True:
            await ws.receive_text()  # spectators are read-only; just keep the socket alive
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        room.spectators.discard(send)
