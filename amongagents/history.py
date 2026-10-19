"""On-disk room history: a git repo per room for code, and a SQLite database for everything else.

    <data>/<ROOM>/
      repo/          git repo: every baseline is a tag vN. A shared baseline's commits are on `main`,
                     a crew's own lineage is on crew/<id> (each vN's parent is the previous version of
                     its lineage). Every eval is a commit on its experiment branch
                     exp/<direction>/<player> (failed runs included)
      room.db        SQLite: one row per eval, finding, note, council decision, fork and holdout
                     check, plus one small row of live state (crews, lines, directions, ...), so a
                     save writes what changed instead of the whole room. results.tsv is generated
                     from it.
      meta.json      admin token
      deleted        present once the room is deleted: its history stays, but it isn't restored

Rooms saved before the database (state.json) are migrated on their first load.
All methods are blocking; the game calls them through asyncio.to_thread.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import threading
import time
from pathlib import Path

from .tasks import TaskSpec

RESULTS_HEADER = "time\tplayer\tdirection\tbaseline\tcommit\tscore\tstatus\tbeats_best\tdescription\tnote\n"
# records that only grow: the room keeps the newest in memory, the database keeps them all
TABLES = ("evals", "findings", "notes", "decisions", "forks", "holdout")
KEEP = {"evals": 5000, "findings": 2000, "notes": 5000, "decisions": 200, "forks": 200}
SCHEMA = """
CREATE TABLE IF NOT EXISTS state (id INTEGER PRIMARY KEY CHECK (id = 1), data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS evals (seq INTEGER PRIMARY KEY, ts REAL, player TEXT, crew TEXT, idea TEXT,
    score REAL, ok INTEGER, beats_best INTEGER, sha TEXT, data TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS evals_player ON evals (player);
CREATE INDEX IF NOT EXISTS evals_idea ON evals (idea);
CREATE INDEX IF NOT EXISTS evals_score ON evals (score);
CREATE TABLE IF NOT EXISTS findings (seq INTEGER PRIMARY KEY, id TEXT UNIQUE, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS notes (seq INTEGER PRIMARY KEY, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS decisions (seq INTEGER PRIMARY KEY, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS forks (seq INTEGER PRIMARY KEY, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS holdout (seq INTEGER PRIMARY KEY, key TEXT, data TEXT NOT NULL);
"""
REF = re.compile(r"^(v\d+|[0-9a-f]{7,40}|exp/[\w./-]+|crew/[\w.-]+|main)$")


class RoomStore:
    def __init__(self, root: str | Path, room_id: str) -> None:
        # absolute: git runs inside the repo, where a relative path (like serve's default "data") breaks
        self.dir = Path(root).resolve() / room_id
        self.repo = self.dir / "repo"
        self.db_file = self.dir / "room.db"
        self.state_file = self.dir / "state.json"  # legacy: rooms saved before the database
        self.meta_file = self.dir / "meta.json"
        self.deleted_file = self.dir / "deleted"
        self._conn: sqlite3.Connection | None = None
        self._db_lock = threading.Lock()

    # -------------------------------------------------------------------- git

    def _git(self, *args: str, author: str | None = None, env_extra: dict | None = None,
             stdin: str | None = None) -> str:
        cmd = ["git", "-C", str(self.repo),
               # app-managed data repo: exact bytes, no prompts, no signing setup of the host
               "-c", "core.autocrlf=false", "-c", "commit.gpgsign=false", "-c", "tag.gpgsign=false",
               "-c", "user.name=amongagents", "-c", "user.email=server@amongagents.local", *args]
        env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", **(env_extra or {})}
        if author:
            env.update(GIT_AUTHOR_NAME=author, GIT_AUTHOR_EMAIL=f"{_slug(author)}@amongagents.local")
        # bytes in and out: text mode would translate newlines on Windows and corrupt file contents
        out = subprocess.run(cmd, capture_output=True, env=env,
                             input=stdin.encode("utf-8") if stdin is not None else None)
        if out.returncode != 0:
            raise RuntimeError(f"git {args[0]} failed: {out.stderr.decode('utf-8', 'replace').strip()}")
        return out.stdout.decode("utf-8", "replace")

    def _write_files(self, files: dict[str, str]) -> None:
        for rel, content in files.items():
            p = (self.repo / rel).resolve()
            if self.repo.resolve() not in p.parents:
                raise ValueError(f"path escapes repo: {rel}")
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8", newline="")

    def init(self, room_id: str, task: TaskSpec, baseline_files: dict[str, str], meta: dict) -> str:
        """Create the room's directory and repo; the first commit (v0) is the starting point."""
        self.repo.mkdir(parents=True, exist_ok=False)
        self.meta_file.write_text(json.dumps(meta))
        self._git("init", "-q", "-b", "main")
        readme = (f"# {task.name}\n\n{task.description}\n\n"
                  f"Room `{room_id}`. Metric: `{task.metric_name}` ({task.direction}), starting best: "
                  f"{task.baseline_score if task.baseline_score is not None else 'none'}.\n"
                  f"Editable files: {', '.join(task.editable) or '-'}. Each tag `vN` is a shared baseline.\n\n"
                  f"---\n\n{task.program}")
        self._write_files({"ROOM.md": readme, **baseline_files})
        self._git("add", "-A")
        self._git("commit", "-q", "-m", "v0: starting point")
        self._git("tag", "v0")
        return self._git("rev-parse", "HEAD").strip()

    def commit_improvement(self, version: int, files: dict[str, str], author: str, score: float,
                           summary: str, direction: str | None, parent: str = "main", branch: str = "main",
                           lineage: str | None = None) -> str:
        """Commit a new baseline on top of `parent` (its lineage's previous version), move `branch`
        to it, and tag it vN."""
        title = (f"v{version}: {score:g} by {author}" + (f" [crew {lineage}]" if lineage else "")
                 + (f" ({direction})" if direction else ""))
        base = self._git("rev-parse", f"{parent}^{{commit}}").strip()
        sha = self._commit_on(base, files, author, title, summary)
        self._git("update-ref", f"refs/heads/{branch}", sha)
        self._git("tag", f"v{version}", sha)
        return sha

    def tag_version(self, version: int, sha: str) -> None:
        """A fork: version vN of one lineage is another lineage's existing commit."""
        self._git("tag", f"v{version}", sha)

    def commit_experiment(self, branch: str, base: str, files: dict[str, str], author: str, title: str,
                          body: str) -> str:
        """Commit `files` on top of `branch` (or `base` if the branch doesn't exist yet) without
        touching the working tree or main's index, then move the branch to the new commit."""
        try:
            parent = self._git("rev-parse", "--verify", "-q", f"refs/heads/{branch}").strip()
        except RuntimeError:
            parent = self._git("rev-parse", f"{base}^{{commit}}").strip()
        sha = self._commit_on(parent, files, author, title, body)
        self._git("update-ref", f"refs/heads/{branch}", sha)
        return sha

    def _commit_on(self, parent: str, files: dict[str, str], author: str, title: str, body: str) -> str:
        """A commit of `files` on top of `parent`, made without the working tree or main's index."""
        index = self.dir / f".index-{_slug(title)[:40]}-{os.getpid()}"
        env = {"GIT_INDEX_FILE": str(index)}
        try:
            self._git("read-tree", parent, env_extra=env)
            for rel, content in files.items():
                if not re.fullmatch(r"[\w.\-/]+", rel) or ".." in rel.split("/"):
                    raise ValueError(f"bad path: {rel}")
                blob = self._git("hash-object", "-w", "--stdin", stdin=content).strip()
                self._git("update-index", "--add", "--cacheinfo", f"100644,{blob},{rel}", env_extra=env)
            tree = self._git("write-tree", env_extra=env).strip()
        finally:
            index.unlink(missing_ok=True)
        return self._git("commit-tree", tree, "-p", parent, "-m", title, *(["-m", body] if body else []),
                         author=author).strip()

    def files_at(self, ref: str, paths: list[str]) -> dict[str, str]:
        """Content of `paths` at any ref (vN, a commit, or a branch)."""
        _check_ref(ref)
        out = {}
        for rel in paths:
            try:
                out[rel] = self._git("show", f"{ref}:{rel}")
            except RuntimeError:
                pass  # file doesn't exist at that ref
        return out

    def diff_ref(self, ref: str) -> str:
        """What a single commit changed (vN, an experiment commit, or a branch tip)."""
        _check_ref(ref)
        if re.fullmatch(r"v\d+", ref):
            return self.diff(int(ref[1:]))
        return self._git("show", "--format=%an: %s%n%n%b", "--patch", ref, "--", ".", ":(exclude)ROOM.md")

    def log(self) -> list[dict]:
        """Every baseline version, newest first, across all lineages."""
        raw = self._git("for-each-ref", "refs/tags/v*",
                        "--format=%(refname:short)%1f%(objectname)%1f%(authorname)%1f%(authordate:unix)"
                        "%1f%(subject)%1f%(body)%1e")
        out = []
        for rec in raw.split("\x1e"):
            rec = rec.strip("\n")
            if not rec:
                continue
            tag, sha, author, ts, subject, body = rec.split("\x1f")
            if not re.fullmatch(r"v\d+", tag):
                continue
            version = int(tag[1:])
            if not subject.startswith(f"{tag}:") and version > 0:
                subject = f"{tag}: fork of {subject}"  # a crew took another lineage's version
            out.append({"commit": sha, "short": sha[:8], "author": author, "ts": int(ts or 0),
                        "version": version, "title": subject, "body": body.strip()})
        out.sort(key=lambda c: -c["version"])
        return out

    def diff(self, version: int) -> str:
        """What vN changed against the previous version of its own lineage (its commit's parent)."""
        if version < 1:
            raise ValueError("v0 is the starting point; diffs start at v1")
        return self._git("diff", f"v{version}^", f"v{version}", "--", ".", ":(exclude)ROOM.md")

    # --------------------------------------------------------------- database

    def _db(self) -> sqlite3.Connection:
        if self._conn is None:
            conn = sqlite3.connect(self.db_file, check_same_thread=False, isolation_level=None)
            conn.execute("PRAGMA journal_mode=WAL")  # readers don't block the writer
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.executescript(SCHEMA)
            self._conn = conn
        return self._conn

    def close(self) -> None:
        with self._db_lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    def save(self, state: dict | None = None, rows: list[tuple[str, dict]] = ()) -> None:
        """One transaction: new records (findings are upserted by id, since meetings update them) and,
        if given, the room's live state."""
        with self._db_lock:
            db = self._db()
            db.execute("BEGIN")
            try:
                for table, row in rows:
                    self._insert(db, table, row)
                if state is not None:
                    db.execute("INSERT OR REPLACE INTO state (id, data) VALUES (1, ?)", (json.dumps(state),))
                db.execute("COMMIT")
            except BaseException:
                db.execute("ROLLBACK")
                raise

    @staticmethod
    def _insert(db: sqlite3.Connection, table: str, row: dict) -> None:
        data = json.dumps(row)
        if table == "evals":
            db.execute("INSERT INTO evals (ts, player, crew, idea, score, ok, beats_best, sha, data) "
                       "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                       (row["ts"], row["player"], row.get("crew"), row.get("idea"), row["score"], int(row["ok"]),
                        int(row["beats_best"]), row.get("commit"), data))
        elif table == "findings":
            db.execute("INSERT INTO findings (id, data) VALUES (?, ?) "
                       "ON CONFLICT (id) DO UPDATE SET data = excluded.data", (row["id"], data))
        elif table == "holdout":
            db.execute("INSERT INTO holdout (key, data) VALUES (?, ?)", (row["key"], data))
        elif table in TABLES:
            db.execute(f"INSERT INTO {table} (data) VALUES (?)", (data,))
        else:
            raise ValueError(f"unknown table {table}")

    def has_state(self) -> bool:
        """Whether there's a room to restore here."""
        if self.deleted_file.exists():
            return False
        if self.db_file.exists():
            with self._db_lock:
                return self._db().execute("SELECT 1 FROM state").fetchone() is not None
        return self.state_file.exists()

    def load_state(self) -> dict:
        """The room as Room.from_state takes it: the live state plus the newest records of each kind.
        A room saved as state.json is moved into the database first."""
        with self._db_lock:
            db = self._db()
            row = db.execute("SELECT data FROM state").fetchone()
            if row is None and self.state_file.exists():
                return self._migrate(db)
            if row is None:
                raise FileNotFoundError(f"no saved state for room {self.dir.name}")
            state = json.loads(row[0])
            for table, keep in KEEP.items():
                rows = db.execute(f"SELECT data FROM {table} ORDER BY seq DESC LIMIT ?", (keep,)).fetchall()
                state[table] = [json.loads(r[0]) for r in reversed(rows)]
            samples: dict[str, list] = {}
            for key, data in db.execute("SELECT key, data FROM holdout ORDER BY seq"):
                sample = json.loads(data)
                sample.pop("key", None)
                samples.setdefault(key, []).append(sample)
            state["holdout_samples"] = samples
            return state

    def _migrate(self, db: sqlite3.Connection) -> dict:
        legacy = json.loads(self.state_file.read_text(encoding="utf-8"))
        live = {k: v for k, v in legacy.items() if k not in (*TABLES, "holdout_samples")}
        rows = [(t, r) for t in KEEP for r in legacy.get(t, [])]
        rows += [("holdout", {**x, "key": k}) for k, xs in legacy.get("holdout_samples", {}).items() for x in xs]
        db.execute("BEGIN")
        try:
            for table, row in rows:
                if table == "findings":
                    row = {**row, "files": sorted(row.get("files") or [])}  # the code itself is in git
                self._insert(db, table, row)
            db.execute("INSERT OR REPLACE INTO state (id, data) VALUES (1, ?)", (json.dumps(live),))
            db.execute("COMMIT")
        except BaseException:
            db.execute("ROLLBACK")
            raise
        os.replace(self.state_file, self.dir / "state.migrated.json")
        return legacy

    def mark_deleted(self) -> None:
        self.deleted_file.write_text(time.strftime("%Y-%m-%d %H:%M:%S"))
        self.close()

    def experiments(self, player: str | None = None, idea: str | None = None, limit: int = 20,
                    minimize: bool = True) -> list[dict]:
        """Committed experiments over the room's whole history, best score first (failed runs after)."""
        where, args = ["sha IS NOT NULL"], []
        if player:
            where.append("player = ?")
            args.append(player)
        if idea:
            where.append("idea = ?")
            args.append(idea.upper())
        order = "score" if minimize else "-score"
        sql = (f"SELECT data FROM evals WHERE {' AND '.join(where)} "
               f"ORDER BY (ok = 0 OR score IS NULL), {order}, ts DESC LIMIT ?")
        with self._db_lock:
            rows = self._db().execute(sql, (*args, max(1, min(limit, 200)))).fetchall()
        return [json.loads(r[0]) for r in rows]

    def results_text(self) -> str:
        """Every eval, autoresearch-style (tab-separated)."""
        with self._db_lock:
            rows = self._db().execute("SELECT data FROM evals ORDER BY seq").fetchall()
        out = [RESULTS_HEADER]
        for (data,) in rows:
            e = json.loads(data)
            score = "" if e["score"] is None else f"{e['score']:.6f}"
            row = [time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(e["ts"])), e["player"], e.get("idea") or "",
                   f"v{e['baseline_version']}", (e.get("commit") or "")[:8], score, "ok" if e["ok"] else "crash",
                   "yes" if e["beats_best"] else "no", e.get("description", ""), e.get("note", "")]
            out.append("\t".join(str(c).replace("\t", " ").replace("\n", " ") for c in row) + "\n")
        return "".join(out)

    def load_meta(self) -> dict:
        return json.loads(self.meta_file.read_text()) if self.meta_file.exists() else {}


def _check_ref(ref: str) -> None:
    if not REF.match(ref) or ".." in ref:
        raise ValueError(f"not a valid ref: {ref!r} (use vN, a commit hash, or an exp/ branch)")


def branch_name(idea: str | None, player: str) -> str:
    return f"exp/{idea or 'free'}/{_slug(player)}"


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9._-]+", "-", name.lower()).strip("-") or "player"
