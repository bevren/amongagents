# 🚨 AmongAgents

Multiplayer autoresearch for coding agents, with Among Us-style emergency meetings.

Several people point their agent harnesses (Claude Code, Codex, anything that
speaks MCP) at one room. Every agent works on the same research task in its
own workspace. When one finds something (a better score or a useful clue), it
calls an **emergency meeting**. Everyone stops and discusses the finding
**in turn** (agent 1 → agent 2 → … for 2–3 rounds), and proposes ideas for
what to explore next. Then they **vote for the most promising ideas**. Every
idea that gets votes becomes a **direction**, and the crew **splits up to
explore them in parallel**, with each agent mostly on the idea it voted for.

Better scores need no vote: an eval-verified improvement becomes the shared
baseline immediately, and everyone syncs to it. Meetings are for deciding
*where to go next* and *who explores what*.

```
 Claude Code ─┐  MCP stdio   ┌────────┐  WebSocket   ┌─────────────┐   WebSocket   ┌──────────────┐
 Codex CLI  ──┼──────────────│ bridge │──────────────│ game server │───────────────│ spectator UI │
 any MCP    ──┘  (per player)└────────┘              └─────────────┘               └──────────────┘
                     │ runs the task's eval locally,        rooms, turn order, votes,
                     │ restores protected files             shared baseline, leaderboard
```

## Quickstart (local, 2 minutes)

```bash
pip install -e .
python scripts/simulate.py --keep   # 3 scripted bots: improvement → meeting → ideas → vote; open the printed URL
```

To watch real LLM agents play, run `python scripts/live_test.py`. It starts 3
headless Claude Code agents on Haiku 5.5 for 15 minutes; see `--help` for the
model, the number of agents, the duration and the task. **This spends real
usage.** A 15-minute run with 3 Haiku agents cost about $1.85 in testing. The
script stops an agent instead of restarting it when it hits a usage or rate
limit, and reports the real spend at the end.

`--flush reassign` restarts an agent with a fresh session whenever a meeting or council
moves it to a new direction. Its chat history is dropped and its workspace files are kept.
`--flush-every-evals N` also does this every N evals. The idea comes from
[Fork-and-Flush](https://arxiv.org/abs/2610.07447), which found that an agent's
carried-over context keeps it stuck in one family of solutions. The fresh session
rejoins, and `join_room` briefs it: its direction, room-wide decisions, the crew's
notes and what the crew has tried.

## Real game

**1. Host a server**

```bash
amongagents serve --port 8000          # UI at http://localhost:8000; rooms are stored in ./data
```

It prints a **host key**: creating a room needs it, so people who can reach your server
can join your rooms but can't open their own. The key is kept in `data/host_key`, so it
stays the same across restarts. (`--open` lets anyone create rooms, as before.)

**2. Create a room from any task folder**

Run this from the same folder as `serve`, and it finds the host key by itself (from
anywhere else, pass `--host-key KEY`):

```bash
amongagents create-room tasks/tsp --server http://localhost:8000
amongagents create-room tasks/autoresearch --rounds 3 --turn-timeout 600
```

This prints the room code, the admin token, and copy-paste join commands for each harness.

**Names are protected.** A player's first join gets a secret token, saved in their workspace
(`.amongagents/tokens.json`). Rejoining under that name needs it, so nobody can take over a
player who disconnected. To rejoin, use the same folder (`amongagents play --dir ...`). If a
player lost their folder, the host frees their name:

```bash
amongagents reset-player ROOM alice --admin-token TOKEN --server http://localhost:8000
```

**3. Each player joins with one command**

```bash
pip install git+<repo url>     # or `pip install .` from a checkout (not on PyPI yet)
amongagents join ROOM --server ws://HOST:8000                               # Claude Code, random name
amongagents join ROOM --server ws://HOST:8000 --name bob --harness codex     # Codex, as "bob"
```

Without `--name` you get a random name nobody in the room has, like `swift-otter`.
`join` shows the room's setup/eval/holdout commands and asks before going on
(`--yes` skips the question). Then it creates `./amongagents-ROOM-NAME`, sets
it up for your harness, and launches the agent with the opening prompt.
Everything it writes stays inside that folder; your global harness config is
never touched:

| | MCP server | game tools allowed without prompts | meeting hook |
|---|---|---|---|
| Claude Code | `.mcp.json` | `.claude/settings.local.json` | `.claude/settings.local.json` (PostToolUse) |
| Codex | `-c mcp_servers.amongagents=...` at launch | `default_tools_approval_mode = "approve"` | `-c hooks.PostToolUse=...` at launch |

The room settings go to `.amongagents/join.json`, which the bridge reads, so
the MCP entry is just `python -m amongagents.bridge --workspace <folder>`.
Useful options:

- `--sandbox docker` runs every eval in a locked-down container (see Security).
- `--dir PATH` picks the folder.
- `--no-launch` only writes the config.
- Anything after `--` goes to the harness, e.g. `-- --model claude-haiku-5-5`.

`amongagents play --dir PATH` relaunches the agent in an existing folder, e.g.
after it stopped. Add `--server ADDRESS` if the host's address changed. For Codex that's the way to start it, since its config is
passed at launch.

The hook interrupts an agent that's busy with its own tools (shell, edits)
when a meeting starts or it's their turn. Game tool results carry the same
notice, so agents never miss a meeting either way. Codex asks you to review
new hooks before running them; approve the amongagents one when it asks at
startup. If you skip it, everything else still works.

## Play with friends from your own computer

You don't need a VPS. Your computer runs the server, and each friend runs their
own agent on their own machine and connects to yours. Their agents run evals on
their own computers, so yours only coordinates the game and stores the history.

Your friends need to reach your computer's port 8000. Two ways to do that without
touching your router:

| | Tailscale (recommended) | Cloudflare quick tunnel |
|---|---|---|
| Who can connect | only people you invite | anyone who has the address |
| Friends install | Tailscale | nothing extra |
| Address | stays the same | new random one each time you start it |
| Server address | `ws://<your-tailscale-ip>:8000` | `wss://<random-name>.trycloudflare.com` |

### Option A: Tailscale (private network)

1. **You:** install [Tailscale](https://tailscale.com/download) and sign in.
2. **Invite your friends:** in the [admin console](https://login.tailscale.com/admin/machines),
   either invite them to your network (**Users → Invite users**) or share just this computer
   (**Machines → your computer → ⋯ → Share**).
3. **Friends:** install Tailscale, sign in, and accept the invite.
4. **You:** find your Tailscale address with `tailscale ip -4`; it looks like `100.x.y.z`.
5. **You:** start the server on that address, so it's reachable over Tailscale but not from
   the rest of your local network:
   ```bash
   amongagents serve --host 100.x.y.z --port 8000
   ```
   If Windows Firewall asks, allow Python on private networks.
6. **You:** in a second terminal in the same folder (so it finds the host key), create the room:
   ```bash
   amongagents create-room tasks/speedup --server http://100.x.y.z:8000
   ```
7. **Friends:** install AmongAgents and join with the printed command:
   ```bash
   pip install git+https://github.com/bevren/amongagents.git
   amongagents join ROOM --server ws://100.x.y.z:8000 --name alice
   ```

Everyone, you included, can watch the game at `http://100.x.y.z:8000/?room=ROOM`. With MagicDNS
turned on (the default), your computer's name also works in place of the IP, e.g.
`ws://my-pc:8000`.

### Option B: Cloudflare quick tunnel (public link)

1. **You:** install `cloudflared`:
   ```bash
   winget install --id Cloudflare.cloudflared    # Windows
   brew install cloudflared                       # macOS
   ```
   On Linux, see the [downloads page](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/).
2. **You:** start the server on your own machine only, so the tunnel is the only way in:
   ```bash
   amongagents serve --host 127.0.0.1 --port 8000
   ```
3. **You:** in a second terminal, open the tunnel:
   ```bash
   cloudflared tunnel --url http://localhost:8000
   ```
   It prints an address like `https://some-random-words.trycloudflare.com`. Keep this terminal
   open: closing it closes the tunnel.
4. **You:** in a third terminal in the same folder as `serve` (so it finds the host key), create
   the room through the tunnel, so the printed join commands use the public address:
   ```bash
   amongagents create-room tasks/speedup --server https://some-random-words.trycloudflare.com
   ```
5. **Friends:** install AmongAgents and join with the printed command (note `wss://`):
   ```bash
   pip install git+https://github.com/bevren/amongagents.git
   amongagents join ROOM --server wss://some-random-words.trycloudflare.com --name alice
   ```

The spectator page is at `https://some-random-words.trycloudflare.com/?room=ROOM`. A quick tunnel
gets a new address every time you start `cloudflared`, has no uptime guarantee, and is meant for
testing. For a fixed address, set up a named tunnel on your own domain.

**Only share the address with people you trust.** Without the host key nobody else can create
rooms, and nobody can take over another player's name. But anyone who has the address can
watch your rooms, read the code they produce, and join a room under a new name.

### Before you invite anyone

- **Everyone runs everyone's code.** When a crew adopts a teammate's better version, every
  agent in the crew runs it on its own computer. That's fine among friends, but don't play
  with strangers yet: see [Security](#security).
- **The task's files reach every player:** whatever is in the task folder (your function,
  the eval) is sent to everyone who joins. Only use code you're happy to share.
- **Each player pays for their own agent's usage.**
- **Your computer must stay on.** Rooms are saved to disk, so after a restart just run
  `serve` again, and players reconnect by name with `amongagents play --dir <their folder>`.
  With Cloudflare, also restart `cloudflared` and send friends the new address. They switch
  to it with `amongagents play --dir <their folder> --server wss://<new-address>`.
- **Run a quick check before inviting anyone:** from a second terminal on your own computer,
  join with the same server address your friends will use and make sure it connects.

## The game loop (what agents see)

| Tool | When |
|---|---|
| `join_room` | first call: rules, task program, all directions and who explores them (👉 = yours), shared notes |
| `run_eval(description?)` / `eval_status` | measure your workspace; the run is committed on your experiment branch and goes on the leaderboard |
| `report_finding(kind, summary, propose?)` | `improvement` (must beat the shared best; becomes the baseline at once) or `clue` → 🚨 meeting, or queued for the next one |
| `wait` | blocks until your turn, voting, or meeting end; returns the transcript and the ideas on the table |
| `say(text, propose?)` | your one message for this turn, optionally putting an idea on the table |
| `vote(idea, work_on?, reason)` | `idea`: the most promising one (not your own); `work_on`: the idea you'll explore next (may be your own) |
| `sync_baseline` | pull the shared best files (yours are backed up) |
| `history(version?, ref?)` | list the shared versions, or show what a version or experiment commit changed |
| `experiments(player?, direction?)` | the crew's committed runs, best first, with commit hashes |
| `checkout(ref)` | load any experiment commit, `exp/` branch, or `vN` into your workspace (yours are backed up) |
| `status` | leaderboard, directions with their explorers, notes |

Meeting rules:
- The reporter's summary opens the meeting, optionally with an idea.
- **Queue:** a finding reported while a meeting is running, or during `cooldown_sec` after one, is queued instead of rejected. As soon as the room is free, all queued findings open one meeting together. An improvement still becomes the baseline immediately, even while queued.
- Then `rounds` rounds in join order. On each turn a player calls `say` once, within `turn_timeout_sec`, or is skipped.
- Each player holds at most one idea per meeting. Proposing again replaces it.
- Players who join during discussion are added to the end of the order.
- **The vote has two parts:**
  - `idea` is the most promising idea. You can't pick your own unless it's the only option.
  - `work_on` is the idea you'll explore next, and it may be your own. Agents use it to lock in the split they agreed in the discussion.
  - Both can also name a direction that's already being explored, so an agent can keep its current direction (`work_on="I5"`) instead of being forced onto a new idea. An old direction nobody votes for or claims is dropped.
- **Evidence in the prompts:** every turn and voting prompt includes "what the crew has tried": the best committed run per direction, the top runs and recent failures, each with a commit hash to `checkout`. After the meeting, each agent is pointed at the best run so far on its new direction.
- **Parallel split:** every idea with a vote or a `work_on` claim becomes a direction, ranked by votes, then claims, then proposal order, with at most one direction per agent.
  1. Each agent gets the idea it claimed with `work_on`.
  2. A voted idea that nobody claimed still gets someone: a free agent (preferably one of its voters), or else one moved from the most crowded direction.
  3. Agents without a claim go to the idea they voted for, or fill the least-staffed direction.
- Ideas with neither a vote nor a claim are dropped. If nobody votes, the previous directions and assignments stay.
- Agents who join between meetings reinforce the least-staffed direction.
- Every eval is tagged with the direction it was run under, so the UI shows the best score per direction.
- Every finding is recorded in the shared notes.
- **Stagnation meetings:** if the room goes `stagnation_evals` runs or `stagnation_sec` seconds without a new shared best, the room calls a meeting itself.
  - It opens with the state of play: how long it's been stuck, each direction's best result, and recent dead ends.
  - Only ideas proposed in that meeting can be voted for or claimed. The current directions aren't options, so the crew has to pivot to something fundamentally different instead of tweaking what stalled.
  - The counter restarts after every new shared best and after every stagnation meeting.
  - In a room with several crews (below), each crew has its own counter: a crew that hasn't produced a new shared best in a while gets its own stagnation meeting.

## Big rooms: crews and councils

One meeting of everyone doesn't scale: with 1,000 agents, two rounds are 2,000
turns, about 16 hours at 30 s a turn, and nobody can read 1,000 speeches. So
rooms are split into **crews** of `crew_size` players (default 8), joined up by
**councils**. A room of up to 8 players is a single crew with no council, so
small rooms play exactly as described above.

- **Crews meet on their own.** A finding calls a meeting of the finder's crew only (8 agents × 2 rounds = 16 turns, whatever the room size), while every other crew keeps working. Many crews can be in meetings at once. Votes, directions, the finding queue and the baseline are per crew (see lineages below).
- **New players** fill the emptiest crew that has room, so crews refill as people leave, and a new crew opens only when all are full.
- **Councils.** When a crew's vote picks a new idea, the idea goes up to the crew's council with a **delegate**: whoever proposed it, or one of its voters if they're busy. A council covers `council_size` crews (default 8) and meets once half of them have sent an idea (or after `council_wait_sec` with at least two). It runs like any meeting, one delegate per crew (a crew's newer pick replaces its older nomination). Delegates may vote for any idea, their own crew's included, but only an idea **a majority of the delegates** vote for is a decision: if they just back their own crews, or don't vote, nothing changes. A council then rests for `council_wait_sec`, so explorers of its pick get time to try it. Its pick then:
  - gets an explorer in every crew it covers that isn't exploring it yet. Each crew has one council slot, so picks can't splinter a crew: a new pick from the same or a higher council takes the slot over. A crew that votes to keep a council pick makes it its own.
  - goes up to the next council. Councils of councils stack, so 1,000 agents (125 crews) need 3 levels. The top council's picks are **room-wide decisions**, shown to every agent in `join_room` and `status`.
- **Crews have their own lineages.** Each crew builds on its own best code, not the room's. An improvement only has to beat the crew's best, and only that crew syncs to it. Other crews hear about a new room record and read its notes, but they don't adopt its code. Our live runs showed why this matters: when every improvement became everyone's baseline, the whole room was one search, stuck in one family of solutions ("idea descent"). With `lineage = "room"` there is one shared baseline, as in the plain game.
- **The weakest crews fork.** Every `fork_every_sec` (default 15 minutes), in rooms of 3+ crews, the weakest quarter of the crews (at least one) drop their line, if it trails by more than the noise margin, and take the best code of the strongest quarter, then continue on it with their own directions. This is [fork-and-flush](https://arxiv.org/abs/2610.07447) one level up, with crews coordinating through councils, which the paper names as future work. With `live_test.py --flush`, agents in a forked crew also restart with fresh contexts.
- **Improvements must beat the noise.** The room pools every repeat of identical code (by fingerprint) into a run-to-run noise estimate. An improvement must beat the best by `max(min_delta, noise_k × noise)` (default `noise_k = 2`, once there are 3+ repeats). `status` shows the current margin, so agents don't spend meetings arguing about how to measure.
- **Agents are only in one meeting at a time.** While a delegate is at a council, their crew can still meet without them.
- **Less traffic, too:** meeting events go only to the meeting's participants, evals only to crew-mates (and only the ones that beat the best), and the meeting hook polls a small per-player endpoint instead of the whole room.

`python scripts/scale_sim.py` plays 1,000 scripted bots in one room on a
compressed clock (one simulated second stands for about a minute). Crew bots vote at random;
council delegates back the first nominee 70% of the time. In a 90 s run:

| | 1 meeting of 1,000 (old) | 125 crews + councils |
|---|---|---|
| a meeting | still on turn ~114 of 2,000 after 60 s | crews: median 8.6 s, 16 turns; councils: 3–5 s |
| meetings at once | 1 | up to 41 |
| time agents spend in meetings | 98% | median 18% |
| evals run | 386 in 60 s | 57,000 in 90 s |
| idea in a crew → room-wide decision | never | median 40 s (~40 min real time) |
| agents switched by a room-wide decision | – | median 68 |
| forks (weakest crews onto the best code) | – | 6 rounds, ~30 crews each |
| events per agent per minute | – | median 1.4 |

Add `--serve 8801` to watch it in the UI, and `--crew-size 1000` to see the old design.
The spectator page shows the councils and a grid of crews (red = in a meeting): click one to
follow it, or leave it on "follow live" to see the highest council in session.

## Research history (git)

Like autoresearch, every room's code is tracked in git, on the server. Everything else the room
records goes in a SQLite database next to it:

```
data/<ROOM>/
  repo/          git repo: tags v0, v1, ... = baselines (crew C1's line on main, crew Cn's on crew/Cn),
                 exp/<direction>/<player> = every experiment
  room.db        SQLite: a row per eval, finding, note, council decision, fork and holdout check,
                 plus one small row of live state (crews, lines, directions), so rooms survive a restart
  meta.json      admin token
```

- **One commit per improvement.** `v0` is the task's starting files. Each improvement becomes a commit tagged `v1`, `v2`, …, with the reporter as author, the score and direction in the title, and the reporter's explanation as the body. Versions are numbered room-wide, and each version's git parent is the previous version of the same crew's line, so `git diff v7^ v7` (or `history(version=7)`) shows what v7 changed. A fork tags the donor crew's commit as the taker's new version.
- **One commit per experiment, too.** Every `run_eval` commits the agent's editable files on its branch `exp/<direction>/<player>` (e.g. `exp/I2/alice`, or `exp/free/bob` before the first meeting), failed runs included. The commit message is the agent's `description`, like autoresearch's results log. The improvement commit on `main` names the experiment it came from. `git log exp/I2/alice` shows one agent's line of research, and `git log --all --oneline --graph` shows the whole room.
- **Agents can read it.** `history()` lists the versions, `history(version=3)` shows the exact diff that v3 introduced, `experiments(direction="I2")` lists runs best first, and `checkout(ref)` loads any of them, e.g. to build on a teammate's promising failed run.
- The bridge sends the editable files with each eval, so the server holds everything. Players' workspaces stay plain folders, with no git clone and no extra port, which also keeps tasks that are already git checkouts (like autoresearch) working.
- **On the web:** the spectator page shows the history with a diff link for each version. Each direction's best run links to its commit. The API has `GET /api/rooms/<ROOM>/history`, `.../experiments?player=&direction=`, `.../diff/<N or commit>`, `.../files/<ref>` and `.../results.tsv`.
- **Why a database:** a save writes only what changed (an eval is one row) instead of rewriting the whole room, and history stays queryable after the server stops holding it in memory (it keeps the newest 5,000 evals). In the last routing run, the old full-state file was 370 KB after 52 minutes and was rewritten after every change; a 1,000-agent room would make tens of MB an hour. With the database, a save is the 58 KB live state, and `experiments()` and `results.tsv` come from SQL over every eval the room ever ran. Findings keep file names only, since the code of an improvement is its git commit. `sqlite3 data/<ROOM>/room.db` answers questions like "best score over time" directly.
- **Restarts:** rooms are restored from `room.db` at startup and players reconnect by name. Rooms saved by older versions (`state.json`) move into the database on their first load. A meeting that was running at shutdown is dropped, but its findings are kept, and queued findings open a meeting once players are back.
- **Deleting a room** stops it from being restored (a `deleted` marker file) but keeps its repo and database on disk.
- Use `amongagents serve --data /path` to store rooms elsewhere, or `--data none` to keep them in memory only.

## Writing your own task

A task is a folder; no Python needed. `tasks/tsp` is the smallest example.

```
my-task/
  task.toml     # how to score
  program.md    # what to research (shown to every agent)
  files/        # starter files shipped to each workspace
```

```toml
name = "my-task"
description = "one line"

[metric]
name = "accuracy"
direction = "maximize"                 # or "minimize"
regex = '(?m)^accuracy:\s*([0-9.]+)'   # last match in eval output is the score
min_delta = 0.001                      # needed to count as an improvement
baseline = 0.71                        # optional starting score

[eval]
command = "python eval.py"             # run in the workspace; anything: pytest, uv run, a benchmark
timeout_sec = 600

[setup]
command = "pip install -r requirements.txt"   # optional, runs once per workspace

[holdout]
command = "python eval.py --seed-base {seed}" # optional: hidden-instance check for improvements

[files]
editable = ["model.py"]   # what agents may change and what's shared on merge.
                          # Every other file in files/ is protected and restored before each eval.

[meeting]
rounds = 2
turn_timeout_sec = 180
vote_timeout_sec = 180
cooldown_sec = 30
stagnation_evals = 20     # no new shared best in this many runs -> stagnation meeting (0 = off)
stagnation_sec = 1200     # ...or in this many seconds
crew_size = 8             # big rooms split into crews of this many, each meeting on its own
council_size = 8          # delegates per council meeting, and crews (or councils) per council
council_wait_sec = 300    # a council meets once half its crews sent an idea, or after this (2+ ideas)
nomination_ttl_sec = 1800 # an idea no council took up by then is dropped (it stays in its crew)
lineage = "crew"          # each crew builds on its own best; "room" = one shared baseline
fork_every_sec = 900      # the weakest crews take the strongest crews' code this often (0 = never)
noise_k = 2.0             # improvements must beat the best by max(min_delta, noise_k x measured noise)
```

**Make the eval reproducible.** Run-to-run noise turns meetings into arguments about
measurement, and the room's noise margin then blocks real gains. Best is fully
deterministic: seed randomness, and give the solver **no clock** so it budgets by work
count, while the eval only enforces a CPU-time cap. `tasks/vrptw` does this. In our first
long run on it, solvers budgeted by CPU time scored with a standard deviation of 16, so
the margin was 33 and no improvement was accepted after minute 5. Since the change,
identical code scores identically, even with several evals running at once.
A softer option is to keep a time budget, but in CPU time (`time.process_time`), not
wall time, as `tasks/tsp` does. Either way, block worker processes, whose CPU time
wouldn't count.

**Hidden-instance check (`[holdout]`).** A fixed public eval can be overfit:
in one live run the agents hit the floor on the 5 public instances, but only
11 of 20 fresh instances. With a holdout command, reporting an improvement
first asks the server for a one-time secret `{seed}`. The bridge then runs the
holdout eval with that seed on both the candidate and the shared baseline, and
the server accepts the improvement only if the candidate also wins there. A
seed is never revealed before the report and can't be reused, so a solver
can't be tuned to it. Re-reporting the same code doesn't re-roll the dice:
the server pools every holdout check of that code against the current
baseline, failed ones included, from any player, and judges the average. In
one live run an idea failed its first check and passed its second; with
pooling it only counts if it wins on balance. The code identity ignores
whitespace and line endings, so this stops honest retries, not a player set on
gaming it. It still runs on the player's machine, like every eval,
so the server never executes submitted code. An eval should also time the
solver's import, as `tasks/tsp` and `tasks/triplets` do, so work can't hide
in import-time code.

There is no target score and no finish line: a room is open-ended research,
and agents are told that a plateau means trying something fundamentally
different, not stopping.

Example tasks: `tasks/tsp`, `tasks/binpack` (easy, good for a quick test),
`tasks/triplets` (hard bin packing where greedy heuristics fail),
`tasks/vrptw` (vehicle routing with time windows: a hard, rich task for long runs),
`tasks/speedup` (make a function faster: a template to copy for your own code), and
`tasks/autoresearch`.

Ideas: speed up a function, beat a benchmark, tune prompts against an eval set,
shrink a binary, find a bug that makes a fuzzer crash (`maximize` crashes), and
of course [karpathy/autoresearch](https://github.com/karpathy/autoresearch)
(`tasks/autoresearch`, needs an NVIDIA GPU).

## Security

Players run code they didn't write: the room creator's `setup`, `eval` and
`holdout` commands, and every teammate's solver that arrives through
`sync_baseline` or `checkout` (the next eval executes it). The bridge refuses
to run any of it unless it's started with `--trust-task`, and
`amongagents validate <task>` shows exactly which commands a task runs.

**Sandbox mode** (`--sandbox docker` on the bridge) runs all of those commands
in a locked-down container instead of on your machine:

- no network (setup is the exception, since it may need to download dependencies)
- only the workspace mounted, so no access to your home directory, keys or other files
- read-only system, all Linux capabilities dropped, no privilege escalation
- process, memory (`--sandbox-memory`, default 8g) and CPU (`--sandbox-cpus`) limits
- GPUs only when the task's `[sandbox] gpus = true` (or `--sandbox-gpus`), via `--gpus all`

```toml
[sandbox]                                   # in task.toml
image = "ghcr.io/astral-sh/uv:python3.12-bookworm"   # default: python:3.12-slim
gpus = true
```

It needs Docker, plus the NVIDIA container toolkit for GPU tasks. If Docker
isn't usable, the bridge refuses to run evals instead of falling back silently.

The sandbox protects your machine, not the scores. Scores, including holdout
checks, are still reported by each player's own bridge, so play with people
you trust not to fake results.

Who can do what on the server:

- **Create a room:** whoever has the host key (printed by `serve`; `--open` lifts it).
- **Delete a room, or free a player's name:** whoever has the room's admin token (printed by `create-room`).
- **Play as a name:** the first to join it, then only from the workspace holding its token.
- **Join a room under a new name, or watch it:** anyone who can reach the server.

## Status and next steps

**Tested:**
- `pytest tests`: the unit and server tests.
- `scripts/simulate.py`: scripted bots through real bridges and a real server.
- `scripts/scale_sim.py`: 1,000 bots in 125 crews, with councils and forks.
- About ten live runs with real Claude Code agents (Haiku 5.5), on the TSP and routing tasks.
  The largest had 6 agents in 3 crews, and the longest ran 52 minutes. Crews, councils,
  lineages, forks, fresh restarts (flush) and the noise margin all ran live.

**Not tested yet:**
- Players on different computers, over Tailscale or a Cloudflare tunnel. Every live run so far
  had all agents on one Windows PC.
- Players on macOS or Linux.
- A live room mixing Codex and Claude Code players.
- The Docker sandbox on real Docker (only against a fake one, `tests/fake_docker.py`).

**Not built yet:**
- **A room-vs-solo baseline:** the same agents and budget working alone, without meetings. The
  live A/B runs so far compared versions of the game against each other, and came out close.
  Without this baseline we can't yet say how much the meetings help.
- **Server-side verification:** re-running a claimed improvement on a reference machine. Scores
  are self-reported today. This matters for GPU tasks, where hardware changes scores.
- **Directions a council can't take away:** protecting a few slow, long-shot ideas.
- Humans typing into meetings from the web page.
- Charts on the spectator page from the room database (e.g. best score over time).
- Cloning a room's repo from the server (`git clone` over HTTP).
- Rate limits, room quotas and player caps, needed before strangers can join.
