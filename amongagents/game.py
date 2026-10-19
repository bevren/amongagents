"""Room state machine. Transport-agnostic: players are just async `send` callables.

A room is split into CREWS of up to `crew_size` players, and every crew runs its own meetings:
    working  -> everyone runs experiments on their assigned direction
    meeting  -> sequential discussion of a finding: agent 1 -> agent 2 -> ... for N rounds;
                on their turn agents may propose an idea for what to explore next
    voting   -> every participant votes for the most promising idea; every idea that gets
                votes becomes a direction of the crew, and its agents are spread across them
So a meeting never grows with the room (crew_size x rounds turns), and many crews meet at once.
A measured improvement becomes its crew's baseline immediately, without a meeting.

LINEAGES: each crew builds on its own best code, not the room's, so crews search separate basins
instead of all syncing to one (with lineage = "room" there is one shared baseline). On a schedule
the weakest crews FORK: they take the strongest crews' best code and carry on with their own
ideas from there (fork-and-flush, arXiv 2610.07447, one level up). Versions vN are numbered
room-wide; each one's git parent is the previous version of the same lineage.

NOISE: identical code evaluated twice rarely scores the same. The room pools every repeat of the
same code into a run-to-run noise estimate, and an improvement must beat the best by
max(min_delta, noise_k x that noise), so a lucky run doesn't become a baseline.

COUNCILS join the crews up: a tree with up to `council_size` children per node (crews, then
councils of councils, so log(rooms) levels). When a crew's vote picks a new idea, the idea goes
up to its council with a delegate (whoever proposed it, or one of its voters). A council meets
like a crew, with one delegate per nominated idea; its pick gets an explorer in every crew it
covers and moves up to the next council. The top council's picks are room-wide decisions.
A room of up to crew_size players is a single crew with no council: the plain game.
"""

from __future__ import annotations

import asyncio
import hashlib
import math
import secrets
import time
import uuid
from collections import Counter
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Iterable

from .history import KEEP, RoomStore, branch_name
from .tasks import TaskSpec

Send = Callable[[dict], Awaitable[None]]

MAX_SAY_CHARS = 4000
MAX_SUMMARY_CHARS = 6000
MAX_IDEA_CHARS = 500
MAX_LOG = 1000
MAX_DECISIONS = 200
NOISE_MIN_REPEATS = 3  # repeats of identical code before the measured noise counts

RULES = """\
You are a player in an AmongAgents research room. Several AI agents (possibly
different harnesses: Claude Code, Codex, ...) work on the SAME research task in
their own workspaces, share discoveries in emergency meetings, and split up to
explore the most promising directions in parallel.

CREWS: a big room is split into crews of a few agents. Meetings, votes and
directions belong to your crew: a finding calls a meeting in YOUR crew only,
while the other crews keep working.

GAME LOOP
1. WORK: explore YOUR ASSIGNED DIRECTION (others are covering the other
   directions). Change the editable files, then call `run_eval` to measure.
   Every eval result is on the shared leaderboard.
2. FIND SOMETHING: if `run_eval` says you beat the best, call
   `report_finding(kind="improvement", summary=...)`. Your files become the
   shared baseline right away, because the score already proves it. If you
   learn something useful without a better score (a dead end, a bug, a
   surprising result), call `report_finding(kind="clue", summary=...)`.
   Either one calls an EMERGENCY MEETING of your crew. If one is running or
   your crew is in its short cooldown, your finding is queued and opens the
   next meeting automatically, so report once and keep working.
3. MEETING: when a meeting starts, finish your current step and call `wait`.
   Discussion is SEQUENTIAL: agent 1 -> agent 2 -> ... for a fixed number of
   rounds. On your turn, call `say` once. Discuss the finding together: what
   it tells us, why it worked or failed, what it implies for each direction
   being explored. Build on what the others said. If you have a concrete idea
   for what to explore next, pass it as `say(text, propose="one-line idea")`.
   You hold at most one idea per meeting; proposing again replaces yours.
   If you're too slow, your turn is skipped.
4. VOTE: after the last round, call
   `vote(idea="I2", work_on="I3", reason=...)`.
   - `idea` is the most promising idea. You can't pick your own unless it's the only one.
   - `work_on` is the idea YOU will explore next, and it may be your own. Use
     it to lock in the split the crew agreed on in the discussion.
   Both can also name a direction that's already being explored (shown as
   CURRENT DIRECTIONS), e.g. `work_on="I5"` to keep going on your current one.
   Every idea that gets a vote or a work_on claim becomes a direction, and the
   crew explores them in parallel. You get your work_on idea, and a voted idea
   that nobody claimed is still given someone. Ideas with neither are dropped.
   Before you speak or vote, check what has actually been tried: the meeting
   prompt lists the crew's best runs, and `checkout(<commit>)` loads any of them.
COUNCIL (only in rooms with several crews): when your crew's vote picks a new
   idea, it goes up to a council, and its proposer (or one of its voters) is
   called as its DELEGATE. A council meeting runs like a crew meeting, between
   delegates of different crews: present your crew's idea and its evidence,
   weigh it honestly against the others, then `vote(idea=...)` for the idea
   most worth exploring everywhere, whichever crew it came from, or one raised
   in the council. A council only decides when a majority of its delegates
   vote for the same idea: if delegates just back their own crews, or don't
   vote, nothing changes. The council's
   pick gets an explorer in every crew it covers and goes up to the next
   council; the top council's picks are
   ROOM-WIDE DECISIONS. Afterwards, go back to your crew's work.
LINEAGES (rooms with several crews): your crew builds on ITS OWN best code,
   not the room's. An improvement only has to beat your crew's best, and only
   your crew syncs to it. Other crews' results reach you as notes and council
   ideas, never as code you must adopt, so crews explore different approaches.
   Every so often the weakest crews FORK (in rooms of 3+ crews, and only if they
   trail by more than the noise margin): their baseline is replaced by the
   strongest crew's best code. Then sync and keep exploring your own direction
   on top of it.
NOISE: the room measures run-to-run noise from repeated evals of identical
   code. An improvement must beat the best by a margin (shown in `status`):
   the task's min_delta, or twice the measured noise if that's bigger. So don't
   burn meetings on measurement protocol: the room already handles it.
HISTORY: every baseline is a git commit on the server (v0, v1, ...).
   `history()` lists them; `history(version=N)` shows exactly what vN changed.
   HIDDEN INSTANCES: if the task has a holdout check, reporting an improvement
   also runs your solver and the shared baseline on fresh hidden instances (a
   secret seed per report). It's only accepted if it wins there too, so don't
   tune to the public instances: make it generalize. Re-reporting the same code
   doesn't re-roll the dice: every check of it is pooled, failed ones included,
   and the verdict is on the average.
   Every `run_eval` is committed too, on your branch exp/<direction>/<player>,
   failed runs included. Pass `run_eval(description="what you tried")` so the
   log is readable. `experiments()` lists the room's runs, best first, and
   `checkout(ref)` loads any of them (or a vN) into your workspace.
STAGNATION: if your crew goes too long without a new best, the room
   calls a STAGNATION MEETING of your crew. Only ideas proposed in that meeting
   can be voted for or claimed (the current directions aren't options), so bring
   something fundamentally different, not a variation of what's been tried.
5. RESUME: call `sync_baseline` if the baseline changed, then work on your
   (possibly new) assigned direction.

THERE IS NO FINISH LINE. The goal is breakthroughs, not reaching a number: nobody
knows how good a solution can get. A plateau, or a "bound" you think you've hit,
is a signal to question your assumptions and try something fundamentally
different (new representation, new algorithm family, new problem framing), not
to stop. Dead ends are worth reporting as clues: they save the crew time.
Never stop on your own. Keep looping work -> eval -> report/wait until the room closes.
"""


class GameError(Exception):
    pass


@dataclass
class Player:
    name: str
    send: Send | None
    seq: int
    connected: bool = True
    last_eval: dict | None = None
    token_hash: str | None = None  # sha256 of the secret that proves this name is yours


@dataclass
class Finding:
    id: str
    reporter: str
    kind: str  # "improvement" | "clue" | "stagnation" | "council"
    summary: str
    score: float | None
    files: dict[str, str]
    created: float
    chosen_ideas: list[str] = field(default_factory=list)
    queued: bool = False
    commit: str | None = None  # git commit of the baseline this improvement created
    holdout: dict | None = None  # paired check on hidden instances: seed, candidate, baseline
    crew: str | None = None
    paths: list[str] = field(default_factory=list)  # file names of a finding restored from disk

    def public(self) -> dict:
        return {
            "id": self.id,
            "reporter": self.reporter,
            "kind": self.kind,
            "summary": self.summary,
            "score": self.score,
            "files": sorted(self.files) or self.paths,
            "created": self.created,
            "chosen_ideas": self.chosen_ideas,
            "queued": self.queued,
            "commit": self.commit,
            "holdout": self.holdout,
            "crew": self.crew,
        }


@dataclass
class Meeting:
    id: str
    finding: Finding
    participants: list[str]
    rounds: int
    group: str = "C1"
    level: int = 0  # 0 = crew meeting, 1+ = council
    started: float = field(default_factory=time.time)
    findings: list[Finding] = field(default_factory=list)  # [finding, *queued findings]
    transcript: list[dict] = field(default_factory=list)
    ideas: list[dict] = field(default_factory=list)
    round: int = 0
    turn: str | None = None
    turn_deadline: float | None = None
    votes: dict[str, dict] = field(default_factory=dict)
    vote_deadline: float | None = None
    result: dict | None = None

    def public(self, reveal_votes: bool = False) -> dict:
        return {
            "id": self.id,
            "group": self.group,
            "level": self.level,
            "started": self.started,
            "finding": self.finding.public(),
            "findings": [f.public() for f in self.findings],
            "participants": self.participants,
            "rounds": self.rounds,
            "round": self.round,
            "turn": self.turn,
            "turn_deadline": self.turn_deadline,
            "transcript": self.transcript,
            "ideas": self.ideas,
            "voted": sorted(self.votes),
            "votes": self.votes if reveal_votes else None,
            "vote_deadline": self.vote_deadline,
            "result": self.result,
        }


@dataclass
class Group:
    """A crew of players, or a council of delegates. Each one holds its own meetings."""
    id: str  # crews C1, C2, ...; councils K<level>.<n>
    level: int  # 0 = crew; 1 = council over crews; 2 = council over councils; ...
    index: int  # position in its level: its parent is index // council_size one level up
    members: list[str] = field(default_factory=list)  # crews only, in join order
    state: str = "working"
    meeting: Meeting | None = None
    queue: list = field(default_factory=list)  # crews: (Finding, propose); councils: nominations
    directions: list[dict] = field(default_factory=list)  # crews only, each with its "agents"
    last_meeting: dict | None = None
    last_meeting_end: float = 0.0
    last_progress: float = field(default_factory=time.time)  # crews: last new best from a member
    evals_since_progress: int = 0
    turn_done: asyncio.Event = field(default_factory=asyncio.Event)
    votes_done: asyncio.Event = field(default_factory=asyncio.Event)
    meeting_task: asyncio.Task | None = None
    drain_task: asyncio.Task | None = None

    def to_state(self) -> dict:
        return {"id": self.id, "level": self.level, "index": self.index, "members": self.members,
                "directions": self.directions, "last_meeting": self.last_meeting,
                "last_meeting_end": self.last_meeting_end, "last_progress": self.last_progress,
                "evals_since_progress": self.evals_since_progress,
                "queue": [[f.id, p] for f, p in self.queue] if self.level == 0 else self.queue}


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def code_fingerprint(files: dict[str, str]) -> str:
    """Identity of a submission for pooling holdout checks. Line endings, trailing whitespace and
    blank lines don't count, so the same code from two players (or re-saved) gets the same id. Any
    real edit gives a new id; that's a nudge against re-rolling seeds, not a defense against a
    player set on gaming the check (scores are self-reported anyway)."""
    h = hashlib.sha256()
    for path in sorted(files):
        lines = [l.rstrip() for l in files[path].replace("\r\n", "\n").split("\n")]
        h.update(path.encode() + b"\0" + "\n".join(l for l in lines if l).encode() + b"\0")
    return h.hexdigest()[:16]


def council_of(level: int, index: int, crews: int, fan: int) -> tuple[int, int] | None:
    """(level, index) of the council above a group, or None at the top. A council with a single
    child is skipped, so a lone crew at the end of the list reports to the level above."""
    fan = max(2, fan)
    width = lambda lvl: -(-crews // fan ** lvl)  # noqa: E731  (groups at a level)
    while width(level) > 1:
        level, index = level + 1, index // fan
        if min(fan, width(level - 1) - index * fan) > 1:
            return level, index
    return None


def assign(
    ideas: list[dict], votes: dict[str, str | None], agents: list[str], claims: dict[str, str | None] | None = None
) -> tuple[list[dict], dict[str, str]]:
    """Turn a meeting's votes and work claims into parallel directions.

    `votes` say which idea each agent finds most promising; `claims` say which idea each
    agent will work on (its own idea is allowed). An idea becomes a direction if it got a
    vote or a claim, ranked by votes, then claims, then proposal order, up to one per agent.

    Agents first get the idea they claimed. A voted idea nobody claimed still gets someone:
    a free agent (preferably one of its voters), or else one taken from the most crowded
    direction. Leftover agents follow their vote, or fill the least-staffed direction.
    Returns (directions, {agent: idea_id}).
    """
    claims = claims or {}
    tally = Counter(v for v in votes.values() if v)
    claimed = Counter(c for a, c in claims.items() if c and a in agents)
    order = {i["id"]: n for n, i in enumerate(ideas)}
    ranked = sorted(
        (i for i in ideas if tally[i["id"]] or claimed[i["id"]]),
        key=lambda i: (-tally[i["id"]], -claimed[i["id"]], order[i["id"]]),
    )[: len(agents)]
    if not ranked:
        return [], {}
    chosen = {i["id"] for i in ranked}
    out = {a: claims[a] for a in agents if claims.get(a) in chosen}
    for idea in ranked:  # a voted idea must not go unexplored
        if not tally[idea["id"]] or idea["id"] in out.values():
            continue
        free = [a for a in agents if a not in out]
        free.sort(key=lambda a: votes.get(a) != idea["id"])
        if not free:
            staffed = Counter(out.values())
            crowded = max(staffed, key=lambda i: (staffed[i], -order[i]))
            if staffed[crowded] < 2:
                continue
            free = [a for a in reversed(agents) if out.get(a) == crowded]
        out[free[0]] = idea["id"]
    for a in agents:
        if a in out:
            continue
        if votes.get(a) in chosen:
            out[a] = votes[a]
        else:
            staffed = Counter(out.values())
            out[a] = min(ranked, key=lambda i: (staffed[i["id"]], order[i["id"]]))["id"]
    directions = [
        {**i, "votes": tally[i["id"]], "agents": [a for a in agents if out[a] == i["id"]]} for i in ranked
    ]
    return [d for d in directions if d["agents"]], out


class Room:
    def __init__(self, room_id: str, task: TaskSpec, store: RoomStore | None = None) -> None:
        self.id = room_id
        self.task = task
        self.store = store  # git history + persistence; None keeps the room in memory only
        self.created = time.time()
        self.version_commits: dict[int, str] = {}
        self._io = asyncio.Lock()
        self._save_task: asyncio.Task | None = None
        self._rows: list[tuple[str, dict]] = []  # new records for the database, written with the next save

        self.players: dict[str, Player] = {}
        self.spectators: set[Send] = set()
        self._seq = 0
        self.groups: dict[str, Group] = {"C1": Group("C1", 0, 0)}
        self.crew_of: dict[str, str] = {}
        self._busy: dict[str, str] = {}  # player -> the group whose meeting they're in
        self.decisions: list[dict] = []  # what councils picked, newest last

        self.best_score: float | None = task.baseline_score  # the room's record, over every lineage
        self.best_by: str | None = None
        self.best_crew: str | None = None
        self.baseline_version = 0  # the newest vN in the room: versions are numbered room-wide
        # every crew's own line of baselines ("room" alone with lineage = "room")
        self.lines: dict[str, dict] = {}
        self._start = {"score": task.baseline_score, "by": None, "version": 0,
                       "files": {p: task.files[p] for p in task.editable if p in task.files}}
        self.forks: list[dict] = []
        self.last_fork = time.time()
        # run-to-run noise: per code fingerprint (n, mean, M2) over its successful evals
        self._repeats: dict[str, list[float]] = {}
        self._noise_ss, self._noise_df = 0.0, 0
        self.evals: list[dict] = []
        self.findings: list[Finding] = []
        self.notes: list[dict] = []
        self.assignments: dict[str, str] = {}  # player -> idea id
        self.last_progress = time.time()  # last new shared best anywhere in the room
        self.evals_since_progress = 0
        self._challenges: dict[str, int] = {}  # one-time secret holdout seed per player
        self._holdout_samples: dict[str, list[dict]] = {}  # "v<baseline>:<code hash>" -> every check of it
        self.log: list[dict] = []
        self._event_seq = 0
        self._idea_seq = 0
        self._lock = asyncio.Lock()

    # ------------------------------------------------- whole-room views

    @property
    def state(self) -> str:
        states = {g.state for g in self.groups.values()}
        return "voting" if "voting" in states else "meeting" if "meeting" in states else "working"

    @property
    def meeting(self) -> Meeting | None:
        """The most prominent meeting running (the only one, in a single-crew room)."""
        active = [g for g in self.groups.values() if g.meeting]
        return max(active, key=lambda g: (g.level, -g.meeting.started)).meeting if active else None

    @property
    def queue(self) -> list[tuple[Finding, str | None]]:
        return [item for c in self._crews() for item in c.queue]

    @property
    def directions(self) -> list[dict]:
        return [d for c in self._crews() for d in c.directions]

    # ---------------------------------------------------------- groups

    def _crews(self) -> list[Group]:
        return sorted((g for g in self.groups.values() if g.level == 0), key=lambda g: g.index)

    def _crew(self, name: str) -> Group:
        return self.groups[self.crew_of[name]]

    def _online(self, crew: Group) -> list[str]:
        return [n for n in crew.members if self.players[n].connected]

    def _fan(self) -> int:
        return max(2, self.task.meeting.council_size)

    def _parent_id(self, g: Group) -> str | None:
        up = council_of(g.level, g.index, len(self._crews()), self._fan())
        return f"K{up[0]}.{up[1] + 1}" if up else None

    def _parent(self, g: Group) -> Group | None:
        gid = self._parent_id(g)
        if gid and gid not in self.groups:  # councils come into being when first needed
            level, n = gid[1:].split(".")
            self.groups[gid] = Group(gid, int(level), int(n) - 1)
        return self.groups.get(gid) if gid else None

    def _covered(self, g: Group) -> list[Group]:
        """The crews under a group."""
        span = self._fan() ** g.level
        return [c for c in self._crews() if c.index // span == g.index]

    def _place(self, name: str) -> Group:
        """New players fill the emptiest crew that has room, or start a new one."""
        size = max(1, self.task.meeting.crew_size)
        crews = self._crews()
        open_ = [c for c in crews if len(self._online(c)) < size]
        if open_:
            crew = min(open_, key=lambda c: (len(self._online(c)), c.index))
        else:
            crew = Group(f"C{len(crews) + 1}", 0, len(crews))
            self.groups[crew.id] = crew
        crew.members.append(name)
        self.crew_of[name] = crew.id
        return crew

    def _crew_assignments(self, crew: Group) -> dict[str, str]:
        return {n: self.assignments[n] for n in crew.members if n in self.assignments}

    # ---------------------------------------------------------------- events

    async def _emit(self, type_: str, to: Iterable[str] | None = None, **data) -> None:
        """Send an event to the given players (everyone if None) and to all spectators."""
        self._event_seq += 1
        event = {"type": type_, "seq": self._event_seq, "ts": time.time(), **data}
        self.log.append(event)
        del self.log[:-MAX_LOG]
        players = self.players.values() if to is None else (self.players[n] for n in set(to) if n in self.players)
        targets = [p.send for p in players if p.connected and p.send] + list(self.spectators)
        await asyncio.gather(*(self._safe_send(s, event) for s in targets))

    @staticmethod
    async def _safe_send(send: Send, event: dict) -> None:
        try:
            await send(event)
        except Exception:
            pass  # a dead socket is handled by its own disconnect path

    async def _emit_assignments(self, crew: Group) -> None:
        await self._emit("assignments_changed", to=crew.members, crew=crew.id, directions=crew.directions,
                         assignments=self._crew_assignments(crew))

    # --------------------------------------------------------------- players

    def _player(self, name: str) -> Player:
        p = self.players.get(name)
        if p is None:
            raise GameError(f"unknown player {name!r}; join first")
        return p

    async def join(self, name: str, send: Send, token: str | None = None) -> dict:
        """Join or rejoin. A new name gets a secret token (in the welcome, once); rejoining that name
        needs it, so nobody can take over a disconnected player. The server always passes the
        client's token (or ""); token=None is for trusted in-process callers like tests."""
        name = name.strip()[:40]
        if not name:
            raise GameError("name required")
        assigned = False
        issued = None
        async with self._lock:
            p = self.players.get(name)
            if p is not None:
                if p.connected:
                    raise GameError(f"name {name!r} is already connected")
                if token is not None and p.token_hash and not secrets.compare_digest(_hash(token), p.token_hash):
                    raise GameError(
                        f"name {name!r} is taken in this room. To rejoin as {name}, use the workspace you "
                        "joined from (it keeps the player's token); otherwise pick another name, or ask "
                        "the host to reset this player")
                if token is not None and not p.token_hash:  # saved before tokens: the next join claims it
                    issued = token or secrets.token_urlsafe(18)
                    p.token_hash = _hash(issued)
                p.send, p.connected = send, True
                rejoined = True
            else:
                self._seq += 1
                issued = secrets.token_urlsafe(18)
                p = Player(name=name, send=send, seq=self._seq, token_hash=_hash(issued))
                self.players[name] = p
                rejoined = False
            crew = self._crew(name) if name in self.crew_of else self._place(name)
            m = crew.meeting
            if crew.state == "meeting" and m and name not in m.participants and name not in self._busy:
                m.participants.append(name)  # latecomers speak at the end of the order
                self._busy[name] = crew.id
            if crew.directions and name not in self.assignments:
                # latecomers reinforce the least-staffed direction
                d = min(crew.directions, key=lambda d: len(d["agents"]))
                d["agents"].append(name)
                self.assignments[name] = d["id"]
                assigned = True
        await self._emit("player_joined", to=crew.members, player=name, rejoined=rejoined, crew=crew.id)
        if assigned:
            await self._emit_assignments(crew)
        for g in self.groups.values():  # e.g. findings queued before a server restart
            if g.queue:
                self._schedule_drain(g)
        self._persist()
        return {**self.welcome(name), **({"token": issued} if issued else {})}

    def reset_token(self, name: str) -> None:
        """The host lets a player who lost their workspace rejoin: their next join gets a new token."""
        p = self._player(name)
        if p.connected:
            raise GameError(f"{name} is connected; they can only be reset while disconnected")
        p.token_hash = None
        self._persist()

    async def leave(self, name: str) -> None:
        p = self.players.get(name)
        if not p or not p.connected:
            return
        p.connected, p.send = False, None
        await self._emit("player_left", to=self._crew(name).members, player=name)
        g = self.groups.get(self._busy.get(name, ""))
        if g and g.meeting and g.meeting.turn == name:
            g.turn_done.set()
        if g:
            self._check_votes_done(g)

    def welcome(self, name: str) -> dict:
        return {
            "type": "welcome",
            "room": self.id,
            "player": name,
            "rules": RULES,
            "task": self.task.to_dict(),
            "baseline": self.baseline(name),
            "snapshot": self.snapshot(viewer=name),
        }

    # ------------------------------------------------------------- lineages

    def _line_key(self, crew: Group) -> str:
        return crew.id if self.task.meeting.lineage == "crew" else "room"

    def _line(self, crew: Group) -> dict:
        """A crew's line of baselines. A crew that forms mid-game starts from the room's best."""
        key = self._line_key(crew)
        if key not in self.lines:
            src = self._best_line() or self._start
            self.lines[key] = {**src, "files": dict(src["files"])}
        return self.lines[key]

    def _line_members(self, crew: Group) -> list[str] | None:
        """Who builds on this crew's line (None: everyone)."""
        return crew.members if self.task.meeting.lineage == "crew" else None

    def _rank_key(self, score: float | None) -> float:
        if score is None:
            return math.inf
        return score if self.task.direction == "minimize" else -score

    def _best_line(self) -> dict | None:
        return min(self.lines.values(), key=lambda l: self._rank_key(l["score"]), default=None)

    @property
    def baseline_files(self) -> dict[str, str]:
        """The room's best code (each crew builds on its own line's)."""
        return (self._best_line() or self._start)["files"]

    def _line_view(self, line: dict) -> dict:
        return {"version": line["version"], "score": line["score"], "by": line["by"],
                "commit": self.version_commits.get(line["version"]), "files": line["files"]}

    def baseline(self, name: str | None = None) -> dict:
        """The baseline a player builds on (their crew's line), or the room's best."""
        if name in self.crew_of:
            return self._line_view(self._line(self._crew(name)))
        return self._line_view(self._best_line() or self._start)

    def _best_label(self, crew: Group) -> str:
        return ("your crew's best" if self.task.meeting.lineage == "crew" and len(self._crews()) > 1
                else "the shared best")

    # ----------------------------------------------------------------- noise

    def _noise_add(self, code: str, score: float) -> None:
        """Welford update of one code's repeats; pooled sums kept so the noise is O(1) to read."""
        n, mean, m2 = self._repeats.get(code, (0, 0.0, 0.0))
        n += 1
        delta = score - mean
        mean += delta / n
        new_m2 = m2 + delta * (score - mean)
        self._noise_ss += new_m2 - m2
        self._noise_df += 1 if n > 1 else 0
        self._repeats[code] = [n, mean, new_m2]

    def noise(self) -> dict:
        """Pooled run-to-run standard deviation of identical code, and how many repeats back it."""
        df = self._noise_df
        return {"sd": math.sqrt(max(self._noise_ss, 0.0) / df) if df else None, "repeats": df}

    def margin(self) -> float:
        """How much an improvement must beat the best by."""
        n, k = self.noise(), self.task.meeting.noise_k
        measured = k * n["sd"] if k and n["sd"] is not None and n["repeats"] >= NOISE_MIN_REPEATS else 0.0
        return max(self.task.min_delta, measured)

    def _beats(self, score: float, best: float | None) -> bool:
        if best is None:
            return True
        m = self.margin()
        return score < best - m if self.task.direction == "minimize" else score > best + m

    def _margin_text(self) -> str:
        n = self.noise()
        if n["sd"] is None or n["repeats"] < NOISE_MIN_REPEATS:
            return f"min_delta {self.task.min_delta:g}"
        return (f"max(min_delta {self.task.min_delta:g}, {self.task.meeting.noise_k:g} x run-to-run noise "
                f"{n['sd']:.3g} over {n['repeats']} repeats)")

    # ----------------------------------------------------------------- forks

    async def _maybe_fork(self) -> None:
        """Fork-and-flush across crews: on a schedule, the weakest crews drop their own line and take
        the strongest crews' best code, then carry on with their own ideas from there. (The paper
        found that independent lineages plus forking from the leader beat a single lineage, and a
        launcher can also restart the forked crews' agents with fresh contexts.)"""
        s = self.task.meeting
        if s.lineage != "crew" or not s.fork_every_sec or time.time() - self.last_fork < s.fork_every_sec:
            return
        crews = [c for c in self._crews() if c.members]
        if len(crews) < 3:
            return  # with two crews a fork would leave the room a single lineage
        self.last_fork = time.time()
        ranked = sorted(crews, key=lambda c: (self._rank_key(self._line(c)["score"]), c.index))
        k = max(1, len(ranked) // 4)
        donors, takers = ranked[:k], [c for c in ranked[::-1][:k] if c not in ranked[:k]]
        done = []
        for i, taker in enumerate(takers):
            donor = donors[i % k]
            d, t = self._line(donor), self._line(taker)
            if d["score"] is None or t["score"] is not None and not self._beats(d["score"], t["score"]):
                continue  # within the noise margin: nothing worth giving up its own line for
            self.baseline_version += 1
            fork = {"ts": time.time(), "crew": taker.id, "from": donor.id, "version": self.baseline_version,
                    "from_version": d["version"], "score": d["score"], "by": d["by"],
                    "replaced": {"version": t["version"], "score": t["score"], "by": t["by"]}}
            t.update(score=d["score"], by=d["by"], files=dict(d["files"]), version=self.baseline_version)
            sha = self.version_commits.get(d["version"])
            if sha:
                self.version_commits[self.baseline_version] = sha
            taker.last_progress, taker.evals_since_progress = time.time(), 0
            self.forks.append(fork)
            self._record("forks", fork)
            done.append((taker, fork, sha))
        del self.forks[:-200]
        for taker, fork, sha in done:
            if self.store and sha:
                try:
                    async with self._io:
                        await asyncio.to_thread(self.store.tag_version, fork["version"], sha)
                except Exception as e:
                    await self._emit("error", to=[], message=f"fork tag failed: {e!r}")
            await self._emit("crew_forked", to=taker.members, fork=fork,
                             baseline={**self._line_view(self._line(taker)), "files": None})
        if done:
            self._persist()

    # ------------------------------------------------------------- research

    async def record_eval(
        self,
        name: str,
        score: float | None,
        ok: bool,
        note: str = "",
        description: str = "",
        files: dict[str, str] | None = None,
        based_on: int | None = None,
    ) -> dict:
        """Record one experiment. With a store and the agent's editable files, it's also committed on
        the agent's experiment branch exp/<direction>/<player>, failed runs included."""
        p = self._player(name)
        crew = self._crew(name)
        line = self._line(crew)
        if based_on is None or not 0 <= based_on <= self.baseline_version:
            based_on = line["version"]
        files = {k: v for k, v in (files or {}).items() if k in self.task.editable}
        entry = {
            "ts": time.time(),
            "player": name,
            "crew": crew.id,
            "score": score,
            "ok": ok,
            "note": note[:200],
            "description": description.strip()[:300],
            "idea": self.assignments.get(name),
            "baseline_version": based_on,
            "beats_best": bool(ok and score is not None and self._beats(score, line["score"])),
            "code": code_fingerprint(files) if files else None,
            "commit": None,
            "branch": None,
        }
        if ok and score is not None and entry["code"]:
            self._noise_add(entry["code"], score)
        if self.store and files:
            branch = branch_name(entry["idea"], name)
            title = f"{name}: {score:g}" if ok and score is not None else f"{name}: failed ({note[:60] or 'no score'})"
            body = "\n".join(x for x in (entry["description"], f"direction {entry['idea']}" if entry["idea"] else "",
                                          f"based on v{based_on}") if x)
            try:
                async with self._io:
                    entry["commit"] = await asyncio.to_thread(self.store.commit_experiment, branch, f"v{based_on}",
                                                              files, name, title, body)
                entry["branch"] = branch
            except Exception as e:
                await self._emit("error", to=[], message=f"experiment commit failed: {e!r}")
        p.last_eval = entry
        self.evals.append(entry)
        del self.evals[:-KEEP["evals"]]
        # crew-mates only hear about runs that beat the best (a report may be coming); spectators get all
        await self._emit("eval", to=crew.members if entry["beats_best"] else [], **entry)
        if self.store:
            async with self._io:
                try:
                    await asyncio.to_thread(self.store.save, None, [("evals", entry)])
                except Exception as e:
                    await self._emit("error", to=[], message=f"saving the eval failed: {e!r}")
            self._persist()
        self.evals_since_progress += 1
        crew.evals_since_progress += 1
        await self._maybe_stagnation(crew)
        await self._maybe_fork()
        return entry

    def _stagnating(self, crew: Group) -> bool:
        s = self.task.meeting
        if crew.state != "working" or crew.queue or self._cooldown_left(crew) > 0 or not crew.evals_since_progress:
            return False
        if not any(n not in self._busy for n in self._online(crew)):
            return False
        return bool((s.stagnation_evals and crew.evals_since_progress >= s.stagnation_evals)
                    or (s.stagnation_sec and time.time() - crew.last_progress >= s.stagnation_sec))

    async def _maybe_stagnation(self, crew: Group) -> None:
        """No new shared best from a crew for a while: it gets a meeting that only accepts new ideas."""
        async with self._lock:
            if not self._stagnating(crew):
                return
            finding = Finding(id=uuid.uuid4().hex[:8], reporter="room", kind="stagnation",
                              summary=self._stagnation_summary(crew), score=None, files={}, created=time.time(),
                              crew=crew.id)
            self.findings.append(finding)
            del self.findings[:-KEEP["findings"]]
            self._save_finding(finding)
            m = self._open_meeting(crew, [(finding, None)])
        await self._start_meeting(crew, m)

    def _stagnation_summary(self, crew: Group) -> str:
        minutes = (time.time() - crew.last_progress) / 60
        better = min if self.task.direction == "minimize" else max
        who = f" from crew {crew.id}" if len(self._crews()) > 1 else ""
        line = self._line(crew)
        lines = [f"STAGNATION: no new best{who} in {crew.evals_since_progress} runs ({minutes:.0f} min). "
                 f"The best is still {line['score']} (v{line['version']}, by {line['by'] or 'the start'})."]
        for d in crew.directions:
            runs = [e["score"] for e in self.evals if e.get("idea") == d["id"] and e["ok"] and e["score"] is not None]
            lines.append(f"- {d['id']} ({', '.join(d['agents']) or 'nobody'}): {d['text'][:100]} -> "
                         + (f"best {better(runs):g} over {len(runs)} runs" if runs else "no successful runs"))
        clues = [n for n in self.notes if n["kind"] == "clue" and self.crew_of.get(n["by"]) == crew.id][-3:]
        if clues:
            lines.append("Recent dead ends and clues: " + " | ".join(f"{n['by']}: {n['text'][:120]}" for n in clues))
        lines.append("Variations of these directions are not working. Propose something FUNDAMENTALLY "
                     "different: a new representation, algorithm family, or framing of the problem. "
                     "Only ideas proposed in this meeting can be voted for or claimed.")
        return "\n".join(lines)

    def experiments(self, player: str | None = None, idea: str | None = None, limit: int = 20) -> list[dict]:
        """Committed experiments, best score first (failed runs after), optionally filtered. With a
        store this covers the room's whole history, not just the evals kept in memory."""
        if self.store:
            return self.store.experiments(player, idea, limit, minimize=self.task.direction == "minimize")
        rows = [e for e in self.evals if e.get("commit")
                and (not player or e["player"] == player) and (not idea or e.get("idea") == idea.upper())]
        sign = 1 if self.task.direction == "minimize" else -1
        rows.sort(key=lambda e: (not e["ok"] or e["score"] is None,
                                 sign * e["score"] if e["ok"] and e["score"] is not None else 0, -e["ts"]))
        return rows[: max(1, min(limit, 200))]

    async def report(
        self,
        name: str,
        kind: str,
        summary: str,
        score: float | None = None,
        files: dict[str, str] | None = None,
        propose: str | None = None,
        holdout: dict | None = None,
    ) -> Finding:
        p = self._player(name)
        if kind not in ("improvement", "clue"):
            raise GameError("kind must be 'improvement' or 'clue'")
        if not summary.strip():
            raise GameError("summary required")
        crew = self._crew(name)
        baseline_changed = record = False
        m = None
        async with self._lock:
            files = files or {}
            if kind == "improvement":
                if score is None:
                    raise GameError("improvement needs a score; run_eval first")
                if not p.last_eval or not p.last_eval["ok"] or p.last_eval["score"] != score:
                    raise GameError("score must match your most recent successful eval")
                line = self._line(crew)
                if not self._beats(score, line["score"]):
                    raise GameError(
                        f"{score} does not beat {self._best_label(crew)} {line['score']} by the margin "
                        f"{self.margin():.4g} ({self.task.direction}; {self._margin_text()}); "
                        "report it as a clue instead"
                    )
                bad = [f for f in files if f not in self.task.editable]
                if bad:
                    raise GameError(f"only editable files can be submitted: {bad}")
                if not files:
                    raise GameError("improvement must include the editable files")
                if self.task.holdout_command:
                    holdout = self._check_holdout(name, holdout, files)
                # A measured improvement needs no vote: it becomes the crew's baseline right away.
                parent = line["version"]
                self.baseline_version += 1
                line.update(score=score, by=name, files={**line["files"], **files}, version=self.baseline_version)
                record = self.best_score is None or self._rank_key(score) < self._rank_key(self.best_score)
                if record:
                    self.best_score, self.best_by, self.best_crew = score, name, crew.id
                    self.last_progress, self.evals_since_progress = time.time(), 0
                baseline_changed = True
                crew.last_progress, crew.evals_since_progress = time.time(), 0
                commit = await self._commit(name, score, summary, files, holdout, self.baseline_version, parent)
            finding = Finding(
                id=uuid.uuid4().hex[:8],
                reporter=name,
                kind=kind,
                summary=summary[:MAX_SUMMARY_CHARS],
                score=score,
                files=files if kind == "improvement" else {},
                created=time.time(),
                commit=commit if baseline_changed else None,
                holdout=holdout if baseline_changed and self.task.holdout_command else None,
                crew=crew.id,
            )
            self.findings.append(finding)
            del self.findings[:-KEEP["findings"]]
            note = {"by": name, "kind": kind, "text": finding.summary, "score": score, "crew": crew.id,
                    "idea": self.assignments.get(name), "finding": finding.id, "ts": time.time()}
            self.notes.append(note)
            del self.notes[:-KEEP["notes"]]
            self._record("notes", note)
            if (crew.state == "working" and self._cooldown_left(crew) <= 0 and not crew.queue
                    and name not in self._busy):
                m = self._open_meeting(crew, [(finding, propose)])
            else:  # a meeting is running or we're in cooldown: present it at the next meeting
                finding.queued = True
                crew.queue.append((finding, propose))
            self._save_finding(finding)
        if baseline_changed:  # to the line's crew, without the files: `sync_baseline` fetches them
            b = self.baseline(name)
            await self._emit("baseline_updated", to=self._line_members(crew),
                             baseline={**b, "files": None, "changed": sorted(files)})
            if record and self._line_members(crew) is not None and len(self._crews()) > 1:
                others = [n for n in self.players if self.crew_of.get(n) != crew.id]
                await self._emit("room_record", to=others, score=score, by=name, crew=crew.id,
                                 version=self.baseline_version)
        if m:
            await self._start_meeting(crew, m)
        else:
            await self._emit("finding_queued", to=crew.members, finding=finding.public(), queue=len(crew.queue))
            self._schedule_drain(crew)
        self._persist()
        return finding

    async def _commit(self, name: str, score: float, summary: str, files: dict[str, str],
                      holdout: dict | None, version: int, parent: int) -> str | None:
        """Record the new baseline as a git commit + tag vN on its lineage's branch (main for a
        shared baseline, crew/<id> for a crew's own). A git failure never breaks the game."""
        if not self.store:
            return None
        idea = self.assignments.get(name)
        d = next((d for d in self._crew(name).directions if d["id"] == idea), None)
        direction = f"{idea}: {d['text']}" if d else None
        exp = self.players[name].last_eval or {}
        if exp.get("commit"):
            summary = f"{summary}\n\nFrom experiment {exp['branch']}@{exp['commit'][:8]}"
        if holdout:
            summary += (f"\nHoldout check (seed {holdout['seed']}): {holdout['candidate']:g} "
                        f"vs baseline {holdout['baseline']:g}")
            if holdout.get("samples", 1) > 1:
                summary += (f"; pooled over {holdout['samples']} checks of this code: "
                            f"{holdout['mean_candidate']:g} vs {holdout['mean_baseline']:g}")
        try:
            async with self._io:
                crew = self._crew(name)
                own = self.task.meeting.lineage == "crew"
                sha = await asyncio.to_thread(self.store.commit_improvement, version, files, name, score, summary,
                                              direction, parent=f"v{parent}",
                                              # the first crew's line is main, so a room that
                                              # never splits keeps the plain history
                                              branch=f"crew/{crew.id}" if own and crew.index else "main",
                                              lineage=crew.id if own and len(self._crews()) > 1 else None)
        except Exception as e:
            await self._emit("error", to=[], message=f"git commit failed: {e!r}")
            return None
        self.version_commits[version] = sha
        return sha

    # ----------------------------------------------------------- persistence

    def _record(self, table: str, row: dict) -> None:
        """A new record for the database (an upsert, for findings), written with the next save."""
        if self.store:
            self._rows.append((table, row))
            self._persist()

    def _save_finding(self, f: Finding) -> None:
        if f.kind != "council":  # council meetings' findings are their decisions, saved as such
            self._record("findings", f.public())

    def _persist(self) -> None:
        """Save state shortly after a change (coalesces bursts of events into one write)."""
        if not self.store or (self._save_task and not self._save_task.done()):
            return
        self._save_task = asyncio.create_task(self._save_soon())

    async def _save_soon(self) -> None:
        await asyncio.sleep(0.2)
        await self._save_now()

    async def _save_now(self) -> None:
        rows, self._rows = self._rows, []
        try:
            await asyncio.to_thread(self.store.save, self.live_state(), rows)
        except Exception as e:
            self._rows[:0] = rows  # try again with the next save
            await self._emit("error", to=[], message=f"saving room state failed: {e!r}")

    def to_state(self) -> dict:
        """Everything needed to restore the room (what `store.load_state` gives back): the live state
        plus the records kept in memory. Meetings in progress are not saved: their findings are
        already recorded, and the restored room starts with every group working."""
        return {
            **self.live_state(),
            "evals": self.evals, "findings": [{**f.public(), "files": f.files} for f in self.findings],
            "notes": self.notes, "decisions": self.decisions, "forks": self.forks,
            "holdout_samples": {k: v for k, v in self._holdout_samples.items()  # older baselines are moot
                                if k.split(":")[0] in {f"v{l['version']}" for l in self.lines.values()}},
        }

    def live_state(self) -> dict:
        """The room's current state without its growing records (those are rows in the database)."""
        return {
            "id": self.id, "created": self.created, "task": self.task.to_dict(),
            "players": [{"name": p.name, "seq": p.seq, "token_hash": p.token_hash} for p in self.players.values()],
            "seq": self._seq,
            "best_score": self.best_score, "best_by": self.best_by, "best_crew": self.best_crew,
            "baseline_version": self.baseline_version, "version_commits": self.version_commits,
            "lines": self.lines, "last_fork": self.last_fork, "assignments": self.assignments,
            "groups": [g.to_state() for g in self.groups.values()], "crew_of": self.crew_of,
            "last_progress": self.last_progress, "evals_since_progress": self.evals_since_progress,
            "idea_seq": self._idea_seq,
        }

    @classmethod
    def from_state(cls, state: dict, store: RoomStore | None = None) -> "Room":
        room = cls(state["id"], TaskSpec.from_dict(state["task"]), store)
        room.created = state["created"]
        for p in state["players"]:
            room.players[p["name"]] = Player(name=p["name"], send=None, seq=p["seq"], connected=False,
                                             token_hash=p.get("token_hash"))
        room._seq = state["seq"]
        room.best_score, room.best_by = state["best_score"], state["best_by"]
        room.best_crew, room.baseline_version = state.get("best_crew"), state["baseline_version"]
        room.version_commits = {int(k): v for k, v in state["version_commits"].items()}
        room.evals, room.notes = state["evals"], state["notes"]
        room.forks, room.last_fork = state.get("forks", []), state.get("last_fork", time.time())
        for e in room.evals:
            if e.get("ok") and e.get("score") is not None and e.get("code"):
                room._noise_add(e["code"], e["score"])
        by_id = {}
        for f in state["findings"]:
            names = isinstance(f["files"], list)  # the database keeps file names; the code is in git
            finding = Finding(id=f["id"], reporter=f["reporter"], kind=f["kind"], summary=f["summary"],
                              score=f["score"], files={} if names else f["files"], created=f["created"],
                              chosen_ideas=f["chosen_ideas"], queued=f["queued"], commit=f.get("commit"),
                              holdout=f.get("holdout"), crew=f.get("crew"), paths=f["files"] if names else [])
            room.findings.append(finding)
            by_id[finding.id] = finding
        room.assignments = state["assignments"]
        room.last_progress = state.get("last_progress", time.time())
        room.evals_since_progress = state.get("evals_since_progress", 0)
        if "groups" in state:
            room.groups = {}
            for g in state["groups"]:
                group = Group(g["id"], g["level"], g["index"], members=g["members"], directions=g["directions"],
                              last_meeting=g["last_meeting"], last_meeting_end=g["last_meeting_end"],
                              last_progress=g["last_progress"], evals_since_progress=g["evals_since_progress"])
                group.queue = ([(by_id[fid], p) for fid, p in g["queue"] if fid in by_id] if group.level == 0
                               else g["queue"])
                room.groups[group.id] = group
            room.crew_of, room.decisions = state["crew_of"], state.get("decisions", [])
        else:  # saved before crews existed: everyone is one crew
            c = room.groups["C1"]
            c.members = [p["name"] for p in sorted(state["players"], key=lambda p: p["seq"])]
            c.directions, c.last_meeting = state["directions"], state["last_meeting"]
            c.last_meeting_end, c.last_progress = state["last_meeting_end"], room.last_progress
            c.evals_since_progress = room.evals_since_progress
            c.queue = [(by_id[fid], propose) for fid, propose in state["queue"] if fid in by_id]
            room.crew_of = {n: "C1" for n in c.members}
        room._idea_seq = state["idea_seq"]
        room._holdout_samples = state.get("holdout_samples", {})
        if "lines" in state:
            room.lines = state["lines"]
        else:  # saved with one shared baseline: every crew's line starts from it
            shared = {"score": state["best_score"], "by": state["best_by"], "version": state["baseline_version"],
                      "files": state["baseline_files"]}
            for c in room._crews():
                room.lines[room._line_key(c)] = {**shared, "files": dict(shared["files"])}
        return room

    def challenge(self, name: str) -> dict:
        """A fresh secret seed for one holdout check. Agents never see it before they report, so a
        solver can't be tuned to the hidden instances."""
        self._player(name)
        if not self.task.holdout_command:
            raise GameError("this task has no holdout check")
        seed = secrets.randbelow(10**9)
        self._challenges[name] = seed
        return {"seed": seed, "baseline_version": self._line(self._crew(name))["version"]}

    def _check_holdout(self, name: str, holdout: dict | None, files: dict[str, str]) -> dict:
        h = holdout or {}
        seed = self._challenges.pop(name, None)
        if seed is None or h.get("seed") != seed:
            raise GameError("improvements need a holdout check with a fresh seed from the server; "
                            "report again (the bridge runs it for you)")
        version = self._line(self._crew(name))["version"]
        if h.get("baseline_version") != version:
            raise GameError("your baseline changed during your holdout check; sync and report again")
        cand, base = h.get("candidate"), h.get("baseline")
        if not isinstance(cand, (int, float)) or not isinstance(base, (int, float)):
            raise GameError(f"holdout check failed to produce scores (candidate {cand}, baseline {base})")
        # Every sample of the same code against the same baseline counts, failed ones included, so
        # re-reporting until one lucky seed passes doesn't work: the verdict is on the pooled mean.
        key = f"v{version}:{code_fingerprint(files)}"
        samples = self._holdout_samples.setdefault(key, [])
        samples.append({"seed": seed, "candidate": cand, "baseline": base, "by": name})
        self._record("holdout", {**samples[-1], "key": key})
        n = len(samples)
        mean_cand = sum(s["candidate"] for s in samples) / n
        mean_base = sum(s["baseline"] for s in samples) / n
        if not self.task.is_better(mean_cand, mean_base):
            pooled = (f" Pooled over all {n} holdout checks of this exact code: {mean_cand:g} vs {mean_base:g}; "
                      f"re-reporting the same code adds a sample to that pool, it doesn't start over."
                      if n > 1 else "")
            raise GameError(
                f"beats {self._best_label(self._crew(name))} on the public instances, but not on fresh hidden ones "
                f"(yours {cand:g} vs the baseline's {base:g} on seed {seed}).{pooled} It's likely overfit to the "
                f"public instances. Report it as a clue, or make it generalize")
        return {"seed": seed, "candidate": cand, "baseline": base, "baseline_version": h["baseline_version"],
                "samples": n, "mean_candidate": mean_cand, "mean_baseline": mean_base}

    # ------------------------------------------------------- opening meetings

    def _cooldown_left(self, g: Group) -> float:
        """Councils rest longer, so explorers of a pick get to try it before the next one lands."""
        s = self.task.meeting
        rest = max(s.cooldown_sec, s.council_wait_sec) if g.level else s.cooldown_sec
        return rest - (time.time() - g.last_meeting_end)

    def _open_meeting(self, crew: Group, items: list[tuple[Finding, str | None]]) -> Meeting:
        """A crew meeting: every member who's online and not in a council meeting takes part."""
        m = Meeting(
            id=items[0][0].id,
            finding=items[0][0],
            findings=[f for f, _ in items],
            participants=[n for n in self._online(crew) if n not in self._busy],
            rounds=self.task.meeting.rounds,
            group=crew.id,
        )
        for f, propose in items:
            m.transcript.append({"round": 0, "player": f.reporter, "text": f.summary, "kind": f.kind,
                                 "score": f.score, "ts": time.time()})
            if propose and propose.strip():
                m.transcript[-1]["idea"] = self._propose(m, f.reporter, propose, 0)["id"]
        return self._begin(crew, m)

    def _open_council(self, g: Group, picked: list[tuple[dict, str]]) -> Meeting:
        """A council meeting: one delegate per nominated idea, each presenting it in round 0."""
        names = ", ".join(n["from"] for n, _ in picked)
        top = self._parent_id(g) is None
        finding = Finding(id=uuid.uuid4().hex[:8], reporter="council", kind="council", score=None, files={},
                          created=time.time(), crew=g.id,
                          summary=f"COUNCIL {g.id}: delegates of {names} each bring the idea their vote picked. "
                                  f"The pick gets an explorer in every crew of "
                                  + ("the room." if top else f"{g.id}'s part of the room, and goes up a level."))
        m = Meeting(id=finding.id, finding=finding, findings=[finding], participants=[d for _, d in picked],
                    rounds=self.task.meeting.rounds, group=g.id, level=g.level)
        for n, delegate in picked:
            idea = n["idea"]
            m.ideas.append({"id": idea["id"], "by": idea["by"], "text": idea["text"], "round": 0,
                            "meeting": idea.get("meeting"), "from": n["from"], "delegate": delegate,
                            "nominated": True})
            m.transcript.append({
                "round": 0, "player": delegate, "kind": "nomination", "idea": idea["id"], "ts": time.time(),
                "text": f"{n['from']} picked {idea['id']} ({n['votes']} of {n['voters']} votes): {idea['text']}"
                        + (f"\nContext: {n['context']}" if n.get("context") else "")})
        return self._begin(g, m)

    def _begin(self, g: Group, m: Meeting) -> Meeting:
        for n in m.participants:
            self._busy[n] = g.id
        g.meeting, g.state = m, "meeting"
        return m

    async def _start_meeting(self, g: Group, m: Meeting) -> None:
        await self._emit("meeting_started", to=m.participants, meeting=m.public())
        g.meeting_task = asyncio.create_task(self._run_meeting(g, m))

    def _schedule_drain(self, g: Group) -> None:
        if g.drain_task is None or g.drain_task.done():
            g.drain_task = asyncio.create_task(self._drain_council(g) if g.level else self._drain(g))

    async def _drain(self, crew: Group) -> None:
        """Open a meeting for queued findings as soon as the crew is free and the cooldown is over."""
        while crew.queue:
            if crew.state != "working":
                await asyncio.sleep(0.5)
                continue
            left = self._cooldown_left(crew)
            if left > 0:
                await asyncio.sleep(min(left, 5))
                continue
            async with self._lock:
                if crew.state != "working" or not crew.queue:
                    continue
                items, crew.queue = crew.queue, []
                for f, _ in items:
                    f.queued = False
                    self._save_finding(f)
                m = self._open_meeting(crew, items)
            await self._start_meeting(crew, m)
            return

    async def _drain_council(self, g: Group) -> None:
        """Meet once half the council's children have nominated an idea with a free delegate, or
        once two have and the oldest waited council_wait_sec. Ideas nobody could take up within
        nomination_ttl_sec are dropped (they're still directions in their own crews)."""
        s = self.task.meeting
        while g.queue:
            if g.state != "working" or self._cooldown_left(g) > 0:
                await asyncio.sleep(0.5)
                continue
            m = None
            async with self._lock:
                now = time.time()
                g.queue = [n for n in g.queue if now - n["ts"] < s.nomination_ttl_sec]
                picked, used = [], set()
                for n in g.queue:
                    d = next((p for p in n["delegates"] if p not in used and p in self.players
                              and self.players[p].connected and p not in self._busy), None)
                    if d:
                        picked.append((n, d))
                        used.add(d)
                    if len(picked) == self._fan():
                        break
                children = sum(1 for x in self.groups.values() if self._parent_id(x) == g.id)
                ready = len(picked) >= max(2, children // 2) or (
                    len(picked) >= 2 and now - min(n["ts"] for n, _ in picked) >= s.council_wait_sec)
                if ready and g.state == "working":
                    ids = {id(n) for n, _ in picked}
                    g.queue = [n for n in g.queue if id(n) not in ids]
                    m = self._open_council(g, picked)
            if m is None:
                await asyncio.sleep(1)
                continue
            await self._start_meeting(g, m)
            return

    def _propose(self, m: Meeting, by: str, text: str, round_: int) -> dict:
        text = text.strip()[:MAX_IDEA_CHARS]
        if not text:
            raise GameError("empty idea")
        for idea in m.ideas:
            if idea["by"] == by and not idea.get("nominated"):
                idea.update(text=text, round=round_)
                return idea
        self._idea_seq += 1  # ids are unique for the whole room, so directions never collide
        idea = {"id": f"I{self._idea_seq}", "by": by, "text": text, "round": round_, "meeting": m.id}
        m.ideas.append(idea)
        return idea

    # -------------------------------------------------------------- meeting

    async def _run_meeting(self, g: Group, m: Meeting) -> None:
        s = self.task.meeting
        try:
            for r in range(1, m.rounds + 1):
                m.round = r
                for name in m.participants:  # may grow while we iterate: latecomers go last
                    p = self.players.get(name)
                    if not p or not p.connected:
                        continue
                    g.turn_done.clear()
                    m.turn = name
                    m.turn_deadline = time.time() + s.turn_timeout_sec
                    await self._emit(
                        "turn", to=m.participants, meeting_id=m.id, group=g.id, round=r, player=name,
                        deadline=m.turn_deadline, participants=m.participants, transcript=m.transcript,
                        ideas=m.ideas,
                    )
                    try:
                        await asyncio.wait_for(g.turn_done.wait(), s.turn_timeout_sec)
                    except asyncio.TimeoutError:
                        m.transcript.append({"round": r, "player": name, "text": None, "skipped": True, "ts": time.time()})
                        await self._emit("turn_skipped", to=m.participants, meeting_id=m.id, group=g.id, round=r,
                                         player=name)
            m.turn = m.turn_deadline = None

            g.state = "voting"
            g.votes_done.clear()
            m.vote_deadline = time.time() + s.vote_timeout_sec
            await self._emit("voting_started", to=m.participants, meeting_id=m.id, group=g.id,
                             deadline=m.vote_deadline, transcript=m.transcript, ideas=m.ideas,
                             carried=self._options(g, m)[len(m.ideas):])
            self._check_votes_done(g)
            try:
                await asyncio.wait_for(g.votes_done.wait(), s.vote_timeout_sec)
            except asyncio.TimeoutError:
                pass
            if g.level:
                await self._conclude_council(g, m)
            else:
                await self._conclude(g, m)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # never leave a group stuck in a meeting
            self._end(g, m)
            await self._emit("error", to=[], message=f"meeting crashed in {g.id}: {e!r}")

    def _meeting_of(self, name: str) -> tuple[Group | None, Meeting | None]:
        g = self.groups.get(self._busy.get(name, ""))
        return (g, g.meeting) if g and g.meeting else (None, None)

    async def say(self, name: str, text: str, propose: str | None = None) -> dict | None:
        self._player(name)
        g, m = self._meeting_of(name)
        if g is None or g.state != "meeting":
            raise GameError("no discussion in progress")
        if m.turn != name:
            raise GameError(f"it's {m.turn}'s turn, not yours; call `wait`")
        text = text.strip()[:MAX_SAY_CHARS]
        if not text:
            raise GameError("empty message")
        idea = self._propose(m, name, propose, m.round) if propose and propose.strip() else None
        entry = {"round": m.round, "player": name, "text": text, "ts": time.time()}
        if idea:
            entry["idea"] = idea["id"]
        m.transcript.append(entry)
        await self._emit("said", to=m.participants, meeting_id=m.id, group=g.id, round=m.round, player=name,
                         text=text, idea=idea)
        g.turn_done.set()
        return idea

    def _options(self, g: Group, m: Meeting) -> list[dict]:
        """What a vote can pick: this meeting's new ideas, then the crew's current directions (so an
        agent can keep its direction with work_on instead of being forced to switch). Councils and
        stagnation meetings only pick among their own ideas."""
        if g.level or m.finding.kind == "stagnation":
            return list(m.ideas)
        new = {i["id"] for i in m.ideas}
        carried = [{"id": d["id"], "by": d["by"], "text": d["text"], "round": d.get("round", 0),
                    "meeting": d.get("meeting"), "carried": True}
                   for d in g.directions if d["id"] not in new]
        return m.ideas + carried

    async def vote(self, name: str, idea: str, reason: str = "", work_on: str | None = None) -> None:
        self._player(name)
        g, m = self._meeting_of(name)
        if g is None:
            crew = self._crew(name)
            raise GameError("you weren't in this meeting" if crew.state == "voting" else "voting isn't open")
        if g.state != "voting":
            raise GameError("voting isn't open")
        if name in m.votes:
            raise GameError("you already voted")
        if not m.ideas:
            raise GameError("no ideas were proposed in this meeting; nothing to vote on")
        options = self._options(g, m)
        listed = ", ".join(i["id"] for i in options)
        idea = (idea or "").strip().upper()
        target = next((i for i in options if i["id"] == idea), None)
        if target is None:
            raise GameError(f"unknown idea {idea!r}; options: {listed}")
        # In a crew you can't vote for your own idea. A council has no such rule (a ban made two
        # delegates who agreed vote against what they agreed on); it needs a majority instead,
        # so delegates who only back their own crew's idea just split the vote and nothing changes.
        if not g.level and target["by"] == name and len(options) > 1:
            raise GameError("you can't vote for your own idea; pick the most promising of the others")
        work_on = (work_on or "").strip().upper() or None
        if g.level:
            work_on = None  # councils pick for the crews they cover; the delegates go back to theirs
        elif work_on and not any(i["id"] == work_on for i in options):
            raise GameError(f"unknown idea {work_on!r} for work_on; options: {listed}")
        m.votes[name] = {"idea": idea, "work_on": work_on, "reason": reason[:500]}
        await self._emit("voted", to=m.participants, meeting_id=m.id, group=g.id, player=name)
        self._check_votes_done(g)

    def _check_votes_done(self, g: Group) -> None:
        m = g.meeting
        if g.state == "voting" and m:
            if not m.ideas:
                g.votes_done.set()  # nothing to vote on
                return
            pending = [n for n in m.participants if self.players[n].connected and n not in m.votes]
            if not pending:
                g.votes_done.set()

    def _end(self, g: Group, m: Meeting) -> None:
        for n in m.participants:
            if self._busy.get(n) == g.id:
                del self._busy[n]
        g.state, g.meeting = "working", None
        g.last_meeting_end = time.time()

    async def _finish(self, g: Group, m: Meeting) -> None:
        self._end(g, m)
        for x in self.groups.values():  # the freed delegates may unblock a waiting council
            if x.queue:
                self._schedule_drain(x)
        self._persist()

    async def _conclude(self, crew: Group, m: Meeting) -> None:
        agents = [n for n in m.participants if self.players[n].connected]
        directions, assignments = assign(self._options(crew, m), {n: v["idea"] for n, v in m.votes.items()},
                                         agents, {n: v.get("work_on") for n, v in m.votes.items()})
        changed = bool(directions)
        if changed:  # the vote replaces the crew's plan (copied: latecomers get added to it later)
            crew.directions = [{**d, "agents": list(d["agents"])} for d in directions]
            self.assignments.update(assignments)
            for n in crew.members:  # members who weren't in the meeting (at a council, or offline)
                if n in assignments:
                    continue
                if not self.players[n].connected:
                    self.assignments.pop(n, None)
                    continue
                d = next((d for d in crew.directions if d["id"] == self.assignments.get(n)), None)
                d = d or min(crew.directions, key=lambda d: len(d["agents"]))
                d["agents"].append(n)
                self.assignments[n] = d["id"]
        m.finding.chosen_ideas = [d["id"] for d in directions]
        self._save_finding(m.finding)
        m.result = {
            "tally": dict(Counter(v["idea"] for v in m.votes.values())),
            "abstain": len(m.participants) - len(m.votes),
            "directions": directions,
            "assignments": assignments,
            "changed": changed,
            "level": 0,
            "group": crew.id,
        }
        parent = self._parent(crew)
        new = {i["id"] for i in m.ideas}
        top = next((d for d in directions if d["id"] in new and d["votes"]), None) if parent else None
        if top:  # the crew's best new idea goes up to the council
            voters = [n for n, v in m.votes.items() if v["idea"] == top["id"]]
            m.result["nominated"] = {"idea": top["id"], "to": parent.id}
            self._nominate(parent, top, crew, m, [top["by"], *voters, *top["agents"]])
        crew.last_meeting = m.public(reveal_votes=True)
        await self._emit("meeting_ended", to=m.participants, meeting=crew.last_meeting)
        if changed:
            await self._emit_assignments(crew)
        if m.finding.kind == "stagnation":  # a fresh window for the new directions
            crew.last_progress, crew.evals_since_progress = time.time(), 0
        await self._finish(crew, m)

    def _nominate(self, council: Group, idea: dict, src: Group, m: Meeting, delegates: list[str]) -> None:
        """Queue a group's pick for its council. Each group holds one nomination: a newer pick
        replaces the older one, so a council's delegates always speak for different crews."""
        council.queue = [n for n in council.queue if n["from"] != src.id and n["idea"]["id"] != idea["id"]]
        context = "; ".join(f"{f.reporter}'s {f.kind}: {f.summary[:200]}" for f in m.findings
                            if f.kind not in ("council",))
        council.queue.append({
            "idea": {"id": idea["id"], "by": idea["by"], "text": idea["text"], "meeting": idea.get("meeting")},
            "from": src.id, "delegates": list(dict.fromkeys(d for d in delegates if d in self.players)),
            "votes": idea.get("votes", 0), "voters": len(m.participants), "context": context[:600],
            "ts": time.time()})
        self._schedule_drain(council)

    async def _conclude_council(self, g: Group, m: Meeting) -> None:
        tally = Counter(v["idea"] for v in m.votes.values())
        order = {i["id"]: n for n, i in enumerate(m.ideas)}
        ranked = sorted((i for i in m.ideas if tally[i["id"]]), key=lambda i: (-tally[i["id"]], order[i["id"]]))
        # A pick needs a majority of the delegates at the meeting: a tie is a coin flip, and a
        # delegate who didn't vote isn't a yes (in a live run one vote of two moved the room).
        majority = bool(ranked) and tally[ranked[0]["id"]] * 2 > len(m.participants)
        tied = bool(ranked) and not majority
        winner = ranked[0] if majority else None
        crews = self._covered(g)
        parent = self._parent(g)
        moved, touched, decision = {}, [], None
        if winner:
            moved, touched = self._spread(winner, crews, g)
            decision = {"idea": winner["id"], "text": winner["text"], "by": winner["by"],
                        "from": winner.get("from") or g.id, "council": g.id, "level": g.level, "meeting": m.id,
                        "votes": tally[winner["id"]], "voters": len(m.participants), "crews": len(crews),
                        "scope": "room" if parent is None else g.id, "moved": moved, "ts": time.time()}
            self.decisions.append(decision)
            del self.decisions[:-MAX_DECISIONS]
            self._record("decisions", decision)
            if parent:
                voters = [n for n, v in m.votes.items() if v["idea"] == winner["id"]]
                self._nominate(parent, {**winner, "votes": tally[winner["id"]]}, g, m,
                               [winner.get("delegate"), winner["by"], *voters])
        m.finding.chosen_ideas = [winner["id"]] if winner else []
        m.result = {
            "tally": dict(tally),
            "abstain": len(m.participants) - len(m.votes),
            "directions": [{**winner, "votes": tally[winner["id"]], "agents": list(moved)}] if winner else [],
            "assignments": moved,
            "changed": bool(winner),
            "level": g.level,
            "group": g.id,
            "decision": decision,
            "split": tied,
        }
        g.last_meeting = m.public(reveal_votes=True)
        await self._emit("meeting_ended", to=m.participants, meeting=g.last_meeting)
        if decision:
            await self._emit("decision", to=[n for c in crews for n in c.members], decision=decision)
        for c in touched:
            await self._emit_assignments(c)
        await self._finish(g, m)

    def _spread(self, idea: dict, crews: list[Group], source: Group) -> tuple[dict[str, str], list[Group]]:
        """A council's pick gets an explorer in every crew it covers that isn't exploring it yet.

        Each crew has one council slot, so picks can't splinter a crew into one-agent directions:
        a new pick from the same or a higher level takes the slot over, explorers included (a crew
        that votes to keep a council pick makes it its own, freeing the slot). Without a slot, the
        explorer is a member without a direction, or one from the most crowded direction (never the
        last explorer of a direction), preferring members who aren't in a meeting."""
        moved, touched = {}, []
        for c in crews:
            if any(d["id"] == idea["id"] for d in c.directions):
                continue
            online = self._online(c)
            slot = next((d for d in c.directions if d.get("adopted")), None)
            if slot and slot.get("adopted_level", 0) > source.level:
                continue  # a lower council doesn't overrule a higher one
            agents = [a for a in slot["agents"] if a in online] if slot else []
            if not agents:
                staffed = {d["id"]: [a for a in d["agents"] if a in online] for d in c.directions if d is not slot}
                pool = [a for a in online if self.assignments.get(a) not in staffed]
                if not pool:
                    crowded = max(staffed.values(), key=len, default=[])
                    if len(crowded) >= 2:
                        pool = crowded
                    elif len(online) >= 2:
                        # everyone leads their own direction (small crews): the pick replaces the
                        # crew's lowest-ranked one, so a council decision is never a no-op
                        weakest = next((d for d in reversed(c.directions) if d is not slot and staffed[d["id"]]),
                                       None)
                        if weakest is None:
                            continue
                        c.directions.remove(weakest)
                        pool = staffed[weakest["id"]]
                    else:
                        continue
                agents = [([a for a in pool if a not in self._busy] or pool)[-1]]  # newest: veterans keep threads
                for d in c.directions:
                    if agents[0] in d["agents"]:
                        d["agents"].remove(agents[0])
            new = {"id": idea["id"], "by": idea["by"], "text": idea["text"], "round": idea.get("round", 0),
                   "meeting": idea.get("meeting"), "votes": 0, "agents": agents, "adopted": source.id,
                   "adopted_level": source.level}
            if slot:
                c.directions[c.directions.index(slot)] = new
            else:
                c.directions.append(new)
            for a in agents:
                self.assignments[a] = idea["id"]
                moved[a] = idea["id"]
            touched.append(c)
        return moved, touched

    # ------------------------------------------------------------- snapshot

    def _default_focus(self) -> Group:
        """What spectators see by default: the highest-level meeting running, else the latest one."""
        active = [g for g in self.groups.values() if g.meeting]
        if active:
            return max(active, key=lambda g: (g.level, g.meeting.started))
        done = [g for g in self.groups.values() if g.last_meeting]
        return max(done, key=lambda g: g.last_meeting_end) if done else self._crews()[0]

    def _group_summary(self, g: Group) -> dict:
        m = g.meeting
        out = {"id": g.id, "level": g.level, "state": g.state, "queue": len(g.queue),
               "parent": self._parent_id(g), "turn": m.turn if m else None, "round": m.round if m else None,
               "participants": m.participants if m else [],
               "topic": (f"{m.finding.reporter}'s {m.finding.kind}" if m and not g.level else
                         ", ".join(i["id"] for i in m.ideas) if m else None),
               "last_meeting_end": g.last_meeting_end}
        if g.level == 0:
            line = self._line(g)
            out.update(members=g.members, online=len(self._online(g)), best=line["score"], best_by=line["by"],
                       version=line["version"],
                       directions=[{"id": d["id"], "text": d["text"][:120], "agents": d["agents"]}
                                   for d in g.directions])
        else:
            out["children"] = [x.id for x in self.groups.values() if self._parent_id(x) == g.id]
        return out

    def player_status(self, name: str) -> dict:
        """What the meeting hook (and a launcher watching for reassignments) needs, without the
        whole snapshot."""
        g, m = self._meeting_of(name)
        me = {"crew": self.crew_of.get(name), "idea": self.assignments.get(name),
              "evals": sum(1 for e in self.evals if e["player"] == name),
              "council_moved": max((d["ts"] for d in self.decisions if name in d["moved"]), default=None),
              "crew_forked": max((f["ts"] for f in self.forks if f["crew"] == self.crew_of.get(name)), default=None)}
        if m is None:
            return {"state": "working", "meeting": None, **me}
        return {"state": g.state, **me,
                "meeting": {"group": g.id, "level": g.level, "kind": m.finding.kind,
                            "reporter": m.finding.reporter, "turn": m.turn, "voted": name in m.votes}}

    def snapshot(self, viewer: str | None = None, focus: str | None = None) -> dict:
        """The room as a player (viewer) sees it: their crew, their meeting, notes that matter to
        them. Without a viewer, as a spectator sees it, centred on one group (focus)."""
        crews = self._crews()
        if viewer in self.crew_of:
            crew = self._crew(viewer)
            g, meeting = self._meeting_of(viewer)
            state = g.state if g else "working"
            g = g or crew
            names = set(crew.members)
            notes = [n for n in self.notes if n["kind"] == "improvement" or n["by"] in names][-30:]
            evals = [e for e in self.evals[-3000:] if e["player"] in names][-300:]
            players = [self.players[n] for n in crew.members]
            findings = [f for f in self.findings if f.crew == crew.id][-50:]
        else:
            g = self.groups.get((focus or "").upper()) or self._default_focus()
            crew = g if g.level == 0 else None
            meeting, state = g.meeting, g.state
            notes, evals, findings = self.notes[-200:], self.evals[-500:], self.findings[-200:]
            players = sorted(self.players.values(), key=lambda p: p.seq)
        stag = crew or self  # a council's view: the room's own counter
        line = self._line(crew) if viewer in self.crew_of else None
        out = {
            "room": self.id,
            "created": self.created,
            "state": state,
            "focus": g.id,
            "crew": crew.id if crew else None,
            "crews": len(crews),
            "players_online": sum(p.connected for p in self.players.values()),
            "task": {
                "name": self.task.name,
                "description": self.task.description,
                "metric": self.task.metric_name,
                "direction": self.task.direction,
                "editable": self.task.editable,
                "rounds": self.task.meeting.rounds,
                # what joining runs on a player's machine, shown before they agree to it
                "commands": {"setup": self.task.setup_command, "eval": self.task.eval_command,
                             "holdout": self.task.holdout_command},
                "sandbox_image": self.task.sandbox_image,
            },
            "players": [
                {
                    "name": p.name,
                    "connected": p.connected,
                    "crew": self.crew_of.get(p.name),
                    "idea": self.assignments.get(p.name),
                    "evals": sum(1 for e in self.evals if e["player"] == p.name),
                }
                for p in players
            ],
            # a player sees their crew's line; a spectator the room's record
            "best_score": line["score"] if line else self.best_score,
            "best_by": line["by"] if line else self.best_by,
            "baseline_version": line["version"] if line else self.baseline_version,
            "room_best": {"score": self.best_score, "by": self.best_by, "crew": self.best_crew},
            "lineage": self.task.meeting.lineage,
            "margin": self.margin(),
            "noise": self.noise(),
            "forks": self.forks[-10:],
            "history": bool(self.store),
            "version_commits": self.version_commits,
            "directions": crew.directions if crew else [],
            "assignments": self._crew_assignments(crew) if crew else {},
            "evals": evals,
            "findings": [f.public() for f in findings],
            "notes": notes,
            "meeting": meeting.public() if meeting else None,
            "queue": [f.public() for f, _ in g.queue] if g.level == 0 else
                     [{"idea": n["idea"], "from": n["from"], "ts": n["ts"]} for n in g.queue],
            "last_meeting": g.last_meeting,
            "decisions": self.decisions[-20:],
            "stagnation": {"since": stag.last_progress, "evals": stag.evals_since_progress,
                           "limit_evals": self.task.meeting.stagnation_evals,
                           "limit_sec": self.task.meeting.stagnation_sec},
        }
        if viewer is None:
            out["groups"] = [self._group_summary(x) for x in
                             sorted(self.groups.values(), key=lambda x: (-x.level, x.index))]
        return out

    async def close(self) -> None:
        for g in self.groups.values():
            for t in (g.meeting_task, g.drain_task):
                if t:
                    t.cancel()
        await self._emit("room_closed")
        if self.store:
            if self._save_task:
                self._save_task.cancel()
            await self._save_now()
            await asyncio.to_thread(self.store.close)
