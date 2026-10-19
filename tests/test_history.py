import asyncio
import json

from fastapi.testclient import TestClient

from amongagents.game import Room
from amongagents.history import RoomStore
from amongagents.tasks import MeetingSettings, TaskSpec


def make_task() -> TaskSpec:
    return TaskSpec(
        name="t", description="demo", program="research!", metric_name="score", direction="minimize",
        metric_regex=r"score:\s*([0-9.]+)", eval_command="true", editable=["solver.py"],
        files={"solver.py": "v0\n", "eval.py": "protected"}, baseline_score=20.0,
        meeting=MeetingSettings(rounds=1, turn_timeout_sec=0.05, vote_timeout_sec=0.05, cooldown_sec=0),
    )


async def noop(_):
    pass


def new_room(tmp_path, room_id="ROOM01"):
    store = RoomStore(tmp_path, room_id)
    task = make_task()
    store.init(room_id, task, {"solver.py": "v0\n"}, {"admin_token": "tok"})
    room = Room(room_id, task, store)
    room.version_commits[0] = store.log()[-1]["commit"]
    return room, store


async def settle(room):
    while room.state != "working":
        await asyncio.sleep(0.01)
    await asyncio.sleep(0.3)  # let the debounced state save land


def test_improvements_become_git_commits_and_results_are_logged(tmp_path):
    async def main():
        room, store = new_room(tmp_path)
        await room.join("alice", noop)
        await room.join("bob", noop)
        await room.record_eval("bob", 25.0, True)            # worse
        await room.record_eval("alice", None, False, "exit 1")  # crash
        await room.record_eval("alice", 18.0, True)
        f = await room.report("alice", "improvement", "added 2-opt", 18.0, {"solver.py": "v1\n"})
        await settle(room)

        log = store.log()
        assert [c["version"] for c in log] == [1, 0]
        assert log[0]["author"] == "alice" and log[0]["title"] == "v1: 18 by alice"
        assert log[0]["body"] == "added 2-opt" and f.commit == log[0]["commit"]
        assert room.baseline()["commit"] == f.commit
        diff = store.diff(1)
        assert "-v0" in diff and "+v1" in diff
        assert store.files_at("v1", ["solver.py"]) == {"solver.py": "v1\n"}
        assert git(store, "rev-parse", "main").strip() == f.commit  # one crew: its line is main

        rows = store.results_text().splitlines()
        assert rows[0].startswith("time\tplayer")
        assert [r.split("\t")[1:9] for r in rows[1:]] == [  # no files sent -> no experiment commits
            ["bob", "", "v0", "", "25.000000", "ok", "no", ""],
            ["alice", "", "v0", "", "", "crash", "no", ""],
            ["alice", "", "v0", "", "18.000000", "ok", "yes", ""],
        ]

    asyncio.run(main())


def git(store, *args):
    return store._git(*args).strip()


def test_every_eval_is_committed_on_its_experiment_branch(tmp_path):
    async def main():
        room, store = new_room(tmp_path)
        await room.join("alice", noop)
        await room.join("bob", noop)
        room.groups["C1"].directions = [{"id": "I2", "by": "bob", "text": "neighbor lists", "votes": 1, "agents": ["alice"]}]
        room.assignments = {"alice": "I2"}
        e1 = await room.record_eval("alice", 19.0, True, description="K=8 lists",
                                    files={"solver.py": "a1\nline2\n", "eval.py": "ignored: not editable"})
        e2 = await room.record_eval("alice", None, False, "exit 1", description="broke it",
                                    files={"solver.py": "a2\n"})
        e3 = await room.record_eval("bob", 21.0, True, files={"solver.py": "b1\n"})  # no direction -> free

        assert (e1["branch"], e2["branch"], e3["branch"]) == ("exp/I2/alice", "exp/I2/alice", "exp/free/bob")
        assert git(store, "rev-parse", "exp/I2/alice") == e2["commit"]
        assert git(store, "rev-parse", f"{e2['commit']}^") == e1["commit"]   # failures stay in the history
        assert git(store, "rev-parse", f"{e1['commit']}^") == room.version_commits[0]  # branched from v0
        assert git(store, "log", "-1", "--format=%an|%s|%b", e1["commit"]) == \
            "alice|alice: 19|K=8 lists\ndirection I2\nbased on v0"
        assert "failed (exit 1)" in git(store, "log", "-1", "--format=%s", e2["commit"])
        assert store.files_at(e1["commit"], ["solver.py", "eval.py"]) == {"solver.py": "a1\nline2\n"}
        assert store.files_at("v0", ["solver.py"]) == {"solver.py": "v0\n"}
        assert "-a1" in store.diff_ref(e2["commit"]) and "+a2" in store.diff_ref(e2["commit"])
        assert git(store, "status", "--porcelain") == ""  # main's working tree untouched
        assert git(store, "rev-parse", "--abbrev-ref", "HEAD") == "main"

        rows = [r.split("\t") for r in store.results_text().splitlines()[1:]]
        assert [(r[2], r[4], r[8]) for r in rows] == [("I2", e1["commit"][:8], "K=8 lists"),
                                                      ("I2", e2["commit"][:8], "broke it"),
                                                      ("", e3["commit"][:8], "")]
        assert [e["commit"] for e in room.experiments()] == [e1["commit"], e3["commit"], e2["commit"]]
        assert [e["player"] for e in room.experiments(idea="i2")] == ["alice", "alice"]

        # an improvement on main points at the experiment it came from
        await room.record_eval("alice", 17.0, True, files={"solver.py": "a3\n"})
        await room.report("alice", "improvement", "best so far", 17.0, {"solver.py": "a3\n"})
        body = store.log()[0]["body"]
        assert body.startswith("best so far") and f"exp/I2/alice@{room.players['alice'].last_eval['commit'][:8]}" in body
        for bad in ("../etc", "HEAD~1", "main; rm"):
            try:
                store.files_at(bad, ["solver.py"])
                raise AssertionError(f"accepted {bad}")
            except ValueError:
                pass

    asyncio.run(main())


def test_room_survives_a_restart(tmp_path):
    async def main():
        room, store = new_room(tmp_path)
        await room.join("alice", noop)
        await room.join("bob", noop)
        await room.record_eval("alice", 18.0, True)
        await room.report("alice", "improvement", "2-opt", 18.0, {"solver.py": "v1\n"}, propose="try Or-opt")
        await room.report("bob", "clue", "queued while the meeting runs")
        while room.state != "voting":
            await asyncio.sleep(0.01)
        await room.vote("bob", "I1", work_on="I1")
        await room.vote("alice", "I1")
        await settle(room)
        before = room.snapshot()

        restored = Room.from_state(RoomStore(tmp_path, "ROOM01").load_state(), RoomStore(tmp_path, "ROOM01"))
        after = restored.snapshot()
        for key in ("best_score", "best_by", "baseline_version", "version_commits", "directions",
                    "assignments", "notes", "evals", "findings"):
            assert after[key] == before[key], key
        assert restored.baseline_files == {"solver.py": "v1\n"}
        assert [p["name"] for p in after["players"]] == ["alice", "bob"]
        assert not any(p["connected"] for p in after["players"])
        assert restored._idea_seq == room._idea_seq
        # the restored room keeps going: the next improvement is v2 on top of the same repo
        await restored.join("alice", noop)
        await restored.record_eval("alice", 17.0, True)
        await restored.report("alice", "improvement", "Or-opt", 17.0, {"solver.py": "v2\n"})
        assert [c["version"] for c in restored.store.log()] == [2, 1, 0]

    asyncio.run(main())


def test_server_persists_and_serves_history(tmp_path, monkeypatch):
    monkeypatch.setenv("AMONG_DATA", str(tmp_path))
    from amongagents import server

    server.rooms.clear()
    with TestClient(server.app) as c:
        res = c.post("/api/rooms", json={"task": make_task().to_dict()}).json()
        room_id = res["room"]
        assert res["history"] is True
        hist = c.get(f"/api/rooms/{room_id}/history").json()
        assert [h["version"] for h in hist] == [0]
        assert c.get(f"/api/rooms/{room_id}/results.tsv").text.startswith("time\t")
        assert c.get(f"/api/rooms/{room_id}/diff/0").status_code == 404

    server.rooms.clear()
    server.admin_tokens.clear()
    with TestClient(server.app) as c:  # a fresh server process loads the room from disk
        assert c.get(f"/api/rooms/{room_id}").json()["room"] == room_id
        state = RoomStore(tmp_path, room_id).load_state()
        assert state["version_commits"]["0"] == hist[0]["commit"]
        r = c.delete(f"/api/rooms/{room_id}", headers={"x-admin-token": res["admin_token"]})
        assert r.status_code == 200
    assert (tmp_path / room_id / "repo" / ".git").exists()  # deleting keeps the history on disk
    assert (tmp_path / room_id / "room.db").exists() and not RoomStore(tmp_path, room_id).has_state()
    server.rooms.clear()
    with TestClient(server.app) as c:  # ... and a deleted room isn't restored
        assert c.get(f"/api/rooms/{room_id}").status_code == 404
    server.rooms.clear()


def test_records_are_rows_and_experiments_cover_the_whole_history(tmp_path):
    """Evals past the in-memory window stay queryable, and a save writes rows, not the whole room."""
    async def main():
        from amongagents import game
        room, store = new_room(tmp_path)
        await room.join("alice", noop)
        await room.join("bob", noop)
        old = game.KEEP["evals"]
        game.KEEP["evals"] = 5  # a small window, so most evals live only in the database
        try:
            for i in range(12):
                await room.record_eval("alice", 30.0 - i, True, description=f"run {i}", files={"solver.py": f"a{i}\n"})
        finally:
            game.KEEP["evals"] = old
        await room.report("bob", "clue", "a dead end")
        await settle(room)
        assert len(room.evals) == 5
        best = room.experiments(limit=3)
        assert [e["description"] for e in best] == ["run 11", "run 10", "run 9"]
        assert len(room.experiments(limit=50)) == 12  # older than the window, still there
        assert len(store.results_text().splitlines()) == 13
        live = store.load_state()
        assert len(live["evals"]) == 12 and live["notes"][-1]["text"] == "a dead end"
        assert live["findings"][-1]["files"] == []  # findings keep file names; the code is in git
        await room.close()

    asyncio.run(main())


def test_a_room_saved_as_state_json_moves_into_the_database(tmp_path):
    async def main():
        room, store = new_room(tmp_path)
        await room.join("alice", noop)
        await room.record_eval("alice", 18.0, True, files={"solver.py": "v1\n"})
        await room.report("alice", "improvement", "2-opt", 18.0, {"solver.py": "v1\n"})
        await settle(room)
        legacy = room.to_state()  # the old format: everything in one JSON file
        await room.close()
        store.db_file.unlink()
        for extra in ("room.db-wal", "room.db-shm"):
            (store.dir / extra).unlink(missing_ok=True)
        store.state_file.write_text(json.dumps(legacy), encoding="utf-8")

        fresh = RoomStore(tmp_path, "ROOM01")
        assert fresh.has_state()
        restored = Room.from_state(fresh.load_state(), fresh)
        assert restored.best_score == 18.0 and restored.baseline_files == {"solver.py": "v1\n"}
        assert not fresh.state_file.exists() and (fresh.dir / "state.migrated.json").exists()
        again = RoomStore(tmp_path, "ROOM01").load_state()  # now it loads from the database
        assert [e["score"] for e in again["evals"]] == [18.0] and again["notes"][0]["text"] == "2-opt"
        await restored.close()

    asyncio.run(main())


def test_a_relative_data_folder_works(tmp_path, monkeypatch):
    """`amongagents serve` stores rooms in "data", a relative path; git runs inside the repo."""
    monkeypatch.chdir(tmp_path)
    store = RoomStore("data", "R1")
    store.init("R1", make_task(), {"solver.py": "v0\n"}, {})
    sha = store.commit_experiment("exp/free/a", "v0", {"solver.py": "x\n"}, "a", "try", "")
    assert store.files_at(sha, ["solver.py"]) == {"solver.py": "x\n"}
    store.commit_improvement(1, {"solver.py": "x\n"}, "a", 1.0, "better", None)
    assert [c["version"] for c in store.log()] == [1, 0]
