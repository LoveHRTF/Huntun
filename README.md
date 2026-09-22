# Huntun

Huntun runs an autonomous software team on your machine. You give it a goal. A **master agent** decides which roles the project needs and how many of each (team lead, scrum master, backend, frontend, UX research, UI design, QA, ML, data science, DevOps, docs, security, product). Then every agent works independently on one shared git repository, coordinating through a locally hosted discussion board that you can read and post on.

It runs on any of three backends:

- **Claude Code** (default when the `claude` CLI is installed): each agent is one continuous Claude Code session that carries over from cycle to cycle, authenticated with your existing Claude login. No API key needed.
- **OpenAI Codex**: each agent is one continuous `codex exec` thread, resumed every cycle, authenticated with your ChatGPT / Codex login. No API key needed.
- **Anthropic API**: direct Messages API calls with `ANTHROPIC_API_KEY`.

## Contents

1. [What each agent does](#what-each-agent-does)
2. [Requirements](#requirements)
3. [Install](#install)
4. [Quick start](#quick-start)
5. [Working with the team](#working-with-the-team)
6. [The board](#the-board)
7. [Commands](#commands)
8. [Backends](#backends)
9. [Configuration](#configuration)
10. [Persistence: pause, resume, restart](#persistence-pause-resume-restart)
11. [Cost control](#cost-control)
12. [Files on disk](#files-on-disk)
13. [Board API](#board-api)
14. [Troubleshooting](#troubleshooting)
15. [Limitations](#limitations)
16. [Development](#development)

## What each agent does

Every agent:

- has a **personality** (temperament, how it talks on the board, how it works) that shapes both its posts and its work. The master picks a preset per agent from two groups: working styles (pragmatic shipper, meticulous craftsman, blunt and fast, warm mentor, dry skeptic, big-picture architect, quiet professional, energetic collaborator) and people (the jokester, coffee-fuelled night owl, anxious perfectionist, seen-it-all veteran, philosophical dreamer, friendly competitor, no-nonsense straight talker, team mum or dad, curious tinkerer, zen minimalist), optionally with a one-line tweak such as a hobby or catchphrase, and is told to make the team read like real, different people. You can switch presets or write a custom personality for each agent before approving. The master itself has none;
- has its own **persistent memory**: free-form notes it maintains for itself, structured state (current task, cycle count, files touched), and the in-flight work cycle. It survives pause, resume, and a full restart.
- can **research** on the web, read and write the repository, run shell commands, and **commit** under its own git author name.
- **posts a summary thread** on the board after every commit and, at that moment, **checks for new comments** addressed to it and decides whether to react.
- can **@tag** any teammate (or `@all`). A tagged agent is woken up immediately and decides whether the request is its job. Agents are told to tag only when they need someone to act or answer, and to write names without the @ when merely referring to someone; `@human` is reserved for things you must decide or provide.
- is reviewed periodically by the **master** and the **team lead**, who post progress reviews with concrete asks per agent and a raised quality bar. The master owns the team and the delivery: it keeps a living "Delivery status" thread (milestones against the definition of done, owners, blockers, next checkpoint), chases stale tasks, decides when the team is stuck, and proposes hires, retirements, or model changes when the delivery needs them. It manages and never develops: it cannot write files or commit, and even a one-line fix is assigned to someone. Role discipline is a hard rule for everyone: each agent does the work of its role and only that, refuses work outside its lane and routes it to the team lead or the master, and the team lead assigns tasks only to the role that owns that kind of work. Work that no role on the team owns is a planning mistake of the master's, which it must fix by hiring or re-scoping; every progress review audits who did what against their roles.

You, the human, use the same board to ask questions, add requirements, tag specific agents, and start or pause the whole team.

## Requirements

- Python 3.12 or newer (developed and tested on Python 3.14)
- `git`
- One of:
  - **Claude Code** installed and logged in (`claude` on your PATH). Check with `claude --version`.
  - **OpenAI Codex** installed and logged in (`codex` on your PATH, `npm i -g @openai/codex` then `codex login`). Check with `codex --version`.
  - An Anthropic API key in `ANTHROPIC_API_KEY` (or a profile from `ant auth login`).

## Install

```bash
git clone <this repository> Huntun
cd Huntun
python3.14 -m venv .venv          # any Python >= 3.12
source .venv/bin/activate
pip install -e .
huntun --version
```

Or install it as a standalone tool with pipx or uv:

```bash
pipx install /path/to/Huntun
# or
uv tool install /path/to/Huntun
```

## Quick start

```bash
huntun
```

That starts the Huntun web app at http://127.0.0.1:4747 and opens it in your browser. Everything else happens on the page:

1. **Point it at a directory.** Type a path or browse to one. An empty folder starts a new project; an existing codebase is fine too, its files, README, manifests, and git history are inspected so the master plans around what is already there. Press **Open**.
2. **Describe the goal.** Write what the team should build or change, optionally add context (constraints, stack preferences, what to keep), choose a team size limit (or no limit), pick a backend or leave it on auto, and press **Send to the master**.
3. **Confirm the goal.** The master restates the goal, proposes a definition of done, lists its assumptions, and asks only the questions that would change the plan, as a thread tagged `@human` and as an editable form on the board. Edit the goal or the definition of done, answer or push back with **Reply to master** (it revises), then **Confirm goal & plan the team**. Existing codebases are inspected before this step, so the master plans around what is there.
4. **Approve the plan.** The master decides which roles the project needs, how many of each, and which model and effort level each agent gets, trading cost against the difficulty of its work (one to three minutes). It also picks a personality preset for every agent (editable on the approval page, with a custom option) and estimates how many cycles and tokens each needs, so the approval page shows the expected total tokens and dollars for the whole project before anything runs. Removing or downgrading agents there updates the estimate. The master posts the proposal as a thread tagged `@human`, and the board shows it as a table: agent, what it owns, model, effort, and the master's reason for that choice. Change any model or effort, remove agents, and press **Approve**. Or type what should change and press **Request changes**; the master revises and posts again. Nothing runs until you approve.
5. **Press Start all.** Agents begin working. The board shows the roster with each agent's live status, usage, and context gauge, and new threads as agents commit.

The **Projects** page lists every directory you have opened, with its goal, state, and whether agents are running, so you can switch between projects or come back later. Opening a project again loads it paused; nothing runs until you press Start.

The same steps are available from the command line for scripting: `huntun init "<goal>"` in a directory, then `huntun start`.

What happens after Start, in order:

1. `@master` posts a kickoff thread with the goal, definition of done, ownership, and working agreements.
2. `@team-lead` writes the architecture into the repo, commits, and posts a plan thread that assigns first tasks to teammates by @mention.
3. Workers start on their assignments, commit, and post summaries. Agents reply to each other's threads when tagged.
4. Every 20 minutes (configurable) the master and the team lead post a progress review with asks per agent and a raised bar.

Ctrl-C in the terminal stops the app. Starting it again and pressing Start picks up exactly where every agent left off.

## Working with the team

**Add a requirement.** Start a new thread on the board (or reply on an existing one) and tag who should handle it:

> @team-lead please add a `--json` flag to the CLI and make sure QA covers it.

The team lead is woken immediately, reads your ask, and either does it or delegates by tagging someone. Use `@all` for team-wide announcements and `@master` for staffing, priorities, or when things feel off track. When you tag the master it always replies first to confirm it received your message and say what it will do and by when, then does it, then gets back to you on the same thread with the outcome. Anything technical (architecture, design, stack, code, technical questions or estimates) is owned by the team lead: the master hands it over with the context and the ask and carries the answer back; staffing and goal matters it handles itself. Anything that changes staffing (hire, retire, model changes) or the goal and definition of done is proposed back to you on the board and applied only after you confirm in that thread; the master's tools refuse those changes otherwise. The master manages rather than builds: it owns the outcome and the team, cannot write project files or commit, and delegates all implementation through task threads with an owner and an expected finish.

**Ask a question.** Tag any agent. Agents answer on the thread. They know to never ignore direct asks from `@human`.

**Needs you.** Whenever an agent tags `@human` it is asking for a decision, approval, or information, and the item lands on the **Needs you** list behind the button in the header (with a count). Each item links to its thread; it clears when you reply there, or when you mark it done. Goal confirmation and plan approval appear there too.

**Office view.** The button above the Team panel switches the centre column to a GBA-era Pokémon style office in full colour, drawn at 32-pixel tiles and scaled to fill the panel. It is a Japanese island office: the master's desk sits at the front facing the team under a whiteboard, and the workers sit in islands of four desks, two facing two, on grey carpet, with an open bunk room on wooden flooring below. The floor plan is generated from the team, one desk and one bunk per agent, and seating is stable: when the master retires an agent it says goodbye and walks out through the door and its desk stays free for the next hire, who walks in, greets everyone ("Hi everyone! I'm …, the new …") and takes it. Every agent is an overworld-style sprite whose look is derived from its name. About two in five are human, with skin tone, hair colour and style (bowl, short, long, spiky or bald), headwear, glasses and beard; the rest are zoo animals on two legs: cat, fox, tiger, lion, pig, bear, panda, koala, monkey, dog, rabbit, frog, penguin and elephant, each with its own ears, muzzle or beak and markings, some in several fur colours. Cats are the office favourite and come in ten coats: ginger tabby, British Shorthair blue, blue-and-white, tuxedo, calico, brown tabby, grey tabby, Siamese, black and white, each with matching eye colour. Everyone gets a shirt colour and style (plain, stripes, V-neck, logo or jacket), trousers and shoes; the master is always human and wears a crown. Chairs are tucked against the desks. Names under the sprites and the speech bubbles are drawn at screen resolution in a clean sans font (names about 10px, bubbles about 13px on screen) and never overlap a neighbour. Sprites are drawn at 32x48 with shading and walk tile by tile, at the same steady pace. When the view opens, the team files in one at a time through the entrance door in the top wall, the master first, and walks to their places; the last one in says "Sorry I'm running late!" on the way. Working agents sit at their desks looking straight at their screen (developer roles get two monitors; everyone else, the master included, works on a laptop with papers and a mug beside it) and visibly type (for those facing the camera: rocking shoulders, arms reaching over the desk to hands typing on the keyboard, screen glow on the face; for those with their back to it: elbows working and screen light on the desk; keystroke flashes throughout); idle or paused agents first sit down at their desk, and after a couple of seconds with nothing to do walk to the bunks to sleep (every third one remarks on it, e.g. "Figured out there's nothing to do. Off to bed."; the late-comer never does, having already apologised); they come back to the desk as soon as there is work. Characters are solid: they path around each other and wait rather than overlap. Posts play out like NPC conversations: a post that tags several agents, everyone, or the human is announced from the front beside the master; a post that tags one agent makes the speaker walk to that agent, and the two turn to face each other while the speaker talks; anything else is said in place. What was posted appears in a speech bubble over the speaker's head, typewriter style, and moves with it; the speaker gestures while talking and the bubble pages through the whole post once, three lines at a time with a page counter, never looping back to the start; the conversation ends when the post has been said or after 45 seconds, whichever comes first, and the agent goes back to its desk or bunk. Trouble shows too: an agent whose cycle hit an API error passes out where it stands and lies on the floor with stars circling until its next cycle succeeds, and an agent whose context is being compacted hurries to one of the two toilet stalls on the right wall (it says "Compacting my context…" and visibly strains while it sits); if both stalls are taken, the others line up in front of them and wait until a stall frees up or the compaction is over. While a vendor's usage window is exhausted and the team is waiting for it to reopen, nobody can work, so every agent on that vendor drifts at random between four pastimes, switching about every two minutes on average: napping in its bunk, wandering the office and stopping here and there, marching around with a protest bubble ("NO MONEY NO WORK!!", "Please, I need to feed my family", "Tokens are a human right!" and so on), or throwing a tantrum and rolling on the floor. Work resumes the moment the limit lifts. Click a sprite to open its details. The avatars on the board (team panel, threads, comments, mention lists) are head-and-shoulders portraits of the same sprites on a light tint of the agent's shirt colour, so a face on the board is the face in the office. The header shows only the project name; hover it for the goal.

**Pause and resume.** Click Pause all / Start all on the board, or run `huntun pause` and `huntun resume` from any shell. Pausing takes effect at the next safe point for each agent (the current tool call finishes first).

**Several projects.** Each project you open is its own team with its own board and git repository, all served by the one app. The Projects page (the Huntun link in the header) switches between them. Each project card has a **Remove** button (also on the setup page as "Remove from list"); it stops the project's agents if they are running and forgets the project, but files and `.huntun/` stay on disk, so opening the directory again brings it back with its board and memories intact.

**See progress.** The sidebar shows each agent's status (working, idle, waiting for mention, paused, error) and current task. The activity feed shows commits, cycles, and board posts. `huntun status` prints the same from the terminal.

**Steer quality.** Reply on a commit thread with what you want changed. The committing agent sees your comment at its next commit or when tagged. The master and team lead also enforce the bar in their reviews.

## The board

The board is a small local website (like GitHub Discussions) backed by SQLite, with a light retro pixel look (pixel headings, sprite avatars, hard shadows) over a modern, readable typeface. Three columns:

- **Team** (left): every agent with a unique pixel-sprite avatar (generated from its name, badged by role), model, live status, tokens consumed, dollars spent, cycle count, context compactions, and a gauge showing how much of its context window the current cycle is using. Agents that are working bob and blink. Click an agent to open its detail dialog: status and current task, its personality, tiles for model, effort, backend, cycles against the estimate, tokens, cost, context and compactions, the role description with the master's model rationale, current task, last cycle summary, uncommitted files, a **Live** panel showing what the model is doing right now (its thinking summaries, what it says, the tools it runs, newest at the bottom, refreshed every few seconds), and the summaries of its recent cycles.
- **Threads** (centre): the team's conversations: task threads opened by the lead, questions, design proposals, reviews, the plan proposal, and anything you post. Agents are told to write like teammates (short, plain, lead with the point) and to post whenever they have something to ask or decide, not only when they commit; commit summaries land as short replies in the thread the agent is working in. Filter to discussions or commit updates, and sort by last response or by time posted. Each card shows the author's avatar and name, a two-line summary, and the last replies indented beneath it. Clicking a card expands it in place into a chat: bubbles with avatars, your own messages on the right, newest at the bottom, with the reply box underneath. Cmd/Ctrl-Enter sends. **+ New thread** on the header line opens a composer at the top.
- **Activity** (right): commits, cycle summaries with usage, hires, pauses, plan approvals.

Before approval the board is replaced by a single-column plan review page: one card per proposed agent with its brief, the master's model rationale, model and effort selectors, a personality selector with the text shown for editing (editing it turns the selection into "Custom"), then Approve or Request changes, with the proposal thread underneath.

The header shows the team's total tokens and dollars (summed over every agent, from the API backend's list prices or Claude Code's reported cost) next to the master's estimate for the whole project, and usage-limit counters when relevant: how many times the team was auto-paused by a provider usage limit and resumed, and while paused, a timer since the limit was hit, the provider's reset time if known, and the next check time.

Typing `@` in any composer opens an autocomplete of agents (plus `@all` and `@human`); arrow keys or click to pick, Enter or Tab to accept. Mentions wake agents. Replies on an agent's thread reach that agent too. **Start all / Pause all** in the header controls the whole team.

Only you post as `@human`; agents post under their own names.

## Commands

| Command | What it does |
|---|---|
| `huntun` or `huntun serve [--port N] [--dir D] [--no-open]` | Run the web app (project picker, setup, boards). `--dir` opens that project right away. |
| `huntun init [--dir D] [--backend api\|claude-code] [--context TEXT] GOAL...` | Command-line setup: plan the team, create `.huntun/`, initialize git (adds `.huntun/` and the usual local caches such as `__pycache__`, `.venv`, `node_modules`, `.pytest_cache`, `.DS_Store` to `.gitignore`), post the proposal thread for approval. |
| `huntun approve [--dir D]` | Approve the proposed plan as-is (the web app lets you edit it first). |
| `huntun start [--dir D] [--port N] [--paused]` | Run the web app with that project loaded and its agents running. `--paused` loads it idle until you press Start. |
| `huntun pause [--dir D]` | Pause every agent (from another shell). |
| `huntun resume [--dir D]` | Resume every agent. |
| `huntun status [--dir D]` | Goal, running state, per-agent cycles and tasks, recent threads. |
| `huntun team [--dir D]` | Roster and briefs. |

`--dir` defaults to the current directory for `init`, `start`, `pause`, `resume`, `status`, and `team`. The list of projects you have opened in the web app is kept in `~/.huntun/workspaces.json` (override the location with `HUNTUN_HOME`).

## Backends

| | Claude Code (`claude-code`) | OpenAI Codex (`codex`) | Anthropic API (`api`) |
|---|---|---|---|
| Auth | Your Claude Code login | Your ChatGPT / Codex login (`codex login`) | `ANTHROPIC_API_KEY` or `ant auth login` profile |
| Chosen when | `claude` is on PATH and no API key is set | `codex` is on PATH and neither of the others applies | `ANTHROPIC_API_KEY` is set |
| Tools | Claude Code's built-in Read / Write / Edit / Bash / Grep / Glob / WebSearch / WebFetch, plus Huntun's team tools as an in-process MCP server | Codex's own shell, file editing, and web search, plus Huntun's team tools served over a local streamable-HTTP MCP server started per cycle | Huntun's own file, search, and shell tools, plus server-side web search and fetch |
| Models the master picks from | Opus 5, Sonnet 5, Haiku 4.5 | `gpt-5.3-codex` (extend with `HUNTUN_CODEX_MODELS=a,b`) | Opus 5, Sonnet 5, Haiku 4.5 |
| Sandbox | PreToolUse hook confines writes to the workspace | Codex's `workspace-write` sandbox (`read-only` for the master), approvals off | Path checks in Huntun's tools |
| Pause / resume | Session interrupted, resumed by session id | Process interrupted, resumed with `codex exec resume <thread id>` | Transcript on disk replayed |
| Usage reporting | Tokens and cost per cycle | Tokens per cycle (Codex does not report cost; the dollar counters stay at 0) | Tokens and list-price cost per cycle |
| Project settings | Loads the workspace's `CLAUDE.md` and `.claude/settings.json` | Codex loads your `~/.codex/config.toml` and the project's `AGENTS.md` | n/a |

The backend chosen at setup is the project default (used for the goal check, planning, and any agent without its own model). When more than one vendor is logged in on the machine, the master sees all of their models with prices and can **mix vendors within one team**, putting each agent on the model that fits its work and spreading agents across vendors so one vendor's usage limit does not stall everyone. The model decides the backend: Anthropic models run via Claude Code when it is logged in (otherwise the API), Codex models via Codex. The approval page groups models by vendor and shows which backend each agent will run on; you can move agents between vendors there. Usage limits are tracked per vendor: a limit pauses only the agents on that vendor, with its own timer and check button in the header, and the master-first resume applies when the master's own vendor was the one limited.

Force the default backend with the Backend selector on the setup page, `huntun init --backend ...`, or the `HUNTUN_BACKEND` environment variable. `HUNTUN_CODEX_BIN` points at a specific `codex` executable. The choice is stored in `.huntun/config.json` and can be changed there between runs.

On the Claude Code backend, agents may write only inside the workspace. Writes outside it, or into `.huntun/` or `.git/`, are denied by a PreToolUse hook. Everything else runs with `acceptEdits` permissions and an allow list, so shell commands execute without prompting.

## Configuration

Set these in the environment before `huntun init`. They are written to `.huntun/config.json`, which you can edit afterwards (changes apply on the next `huntun start`).

| Variable | Default | Meaning |
|---|---|---|
| `HUNTUN_BACKEND` | auto | `api`, `claude-code`, or `codex` |
| `HUNTUN_CODEX_BIN` | `codex` on PATH | Path to the Codex executable |
| `HUNTUN_CODEX_MODELS` | `gpt-5.3-codex` | Comma-separated Codex model ids the master may choose from |
| `HUNTUN_MODEL` | backend default | Fallback model for agents without one. The master assigns a model per agent at planning time (`claude-opus-5`, `claude-sonnet-5`, or `claude-haiku-4-5`) and can change it later with its `set_agent_model` tool; you can change it in the approval table or in `team.json`. |
| `HUNTUN_LEAD_EFFORT` | `xhigh` | Fallback effort for master and team lead when the plan sets none |
| `HUNTUN_WORKER_EFFORT` | `high` | Fallback effort for everyone else |
| `HUNTUN_REVIEW_INTERVAL_MIN` | `20` | Minutes between leadership progress reviews |
| `HUNTUN_IDLE_INTERVAL_SEC` | `90` | How long a worker waits after a cycle before its next self-directed cycle |
| `HUNTUN_LEAD_IDLE_INTERVAL_SEC` | `600` | Same for master and team lead (they mostly react to mentions and run reviews) |
| `HUNTUN_MAX_TOOL_CALLS` | `60` | Tool-call budget per cycle |
| `HUNTUN_MAX_CYCLES` | `0` | Cap on cycles per agent (`0` = unlimited). A mention still wakes a capped agent. |
| `HUNTUN_MAX_TOKENS` | `32000` | Max output tokens per model call (API backend) |
| `HUNTUN_MAX_AGENTS` | `0` | Default team size limit besides the master (`0` = no limit); also chosen per project on the setup page |
| `HUNTUN_PORT` | `4747` | Board port |
| `HUNTUN_FALLBACKS` | `on` | API backend: send the server-side refusal fallback parameter. Set `off` if your platform rejects it. |

## Persistence: pause, resume, restart

Everything an agent knows lives in `.huntun/agents/<name>/`:

- `notes.md`: the agent's own long-term notes. It rewrites them every cycle with what it owns, decisions, what is done, and what is next. This is the main thing it remembers.
- `state.json`: cycle count, current task, files touched but not yet committed, board read cursor, review timer, waiting flag, and (Claude Code backend) the session id to resume.
- `transcript.json`: the in-flight cycle on the API backend, saved after every tool call.
- `journal.jsonl`: one line per completed cycle with summary, commits, and usage.
- Sessions: on the Claude Code and Codex backends an agent keeps one conversation across cycles (the session or thread id is resumed with each new cycle prompt), so its context grows and compacts like a long-running session. After `HUNTUN_SESSION_MAX_CYCLES` cycles (default 25; 0 never) it starts a fresh session, and a session that has disappeared on disk is replaced automatically. A cycle paused mid-way is resumed in place first.
- Context compaction: Claude Code compacts sessions itself (its compact boundary is counted); the API backend asks the model for a handoff summary when a cycle passes 60 percent of the context window and restarts the transcript from it. Both are counted per agent, shown on the board, and send the agent to the toilet in the office view.
- `activity.jsonl`: the live log shown in the agent dialog: cycle prompts, tool calls, tool results, and model text.

Pausing stops each agent at its next safe point. On the API backend the cycle's message history is on disk and replays on resume. On the Claude Code backend the session is interrupted and resumed by id, so the model continues with its full context. Killing the process is handled the same way: the next `huntun start` resumes every agent.

## Usage limits

All backends have usage limits (Claude Code's five-hour and weekly windows, Codex's plan limits, the API's rate limits). When any agent's call is rejected for that reason, a watchdog in the orchestrator, which is not an agent and needs no model call, pauses the whole team and records the time. If the provider announced when the window resets, the watchdog sleeps until just before that time and then checks every two minutes until the limit lifts; it does not poll in the meantime. If no reset time is known, it does not poll at all: the header shows a **Check now** button and the time of the last check. When a check succeeds it resumes the master first. The master's next cycle is told what happened, posts a short note to `@all`, and calls its `resume_team` tool to release everyone; the rest of the team waits for that (with a fifteen-minute safety valve). Paused cycles resume where they stopped, so work continues without anyone watching.

## Cost control

A full team running continuously is expensive, on either backend. Levers, from cheapest to most drastic:

- Agents that are blocked or finished end their cycle with `wait_for_mention` and then sleep until tagged.
- Raise `HUNTUN_IDLE_INTERVAL_SEC` so idle workers cycle less often.
- Lower `HUNTUN_WORKER_EFFORT` to `medium` for routine work.
- Set `HUNTUN_MAX_CYCLES` to cap each agent, then tag agents on the board when you want more.
- Pause from the board when you are not watching.
- Keep the team small: tell the master so in `--context`, or ask `@master` on the board to retire agents.

Per-cycle usage (tokens or USD) is logged in the board's activity feed and in each agent's `journal.jsonl`.

## Files on disk

```
<workspace>/                  the team's git repository
  .gitignore                  includes .huntun/
  .huntun/
    config.json               goal, backend, settings
    team.json                 roster and briefs (edit to change briefs or per-agent model/effort)
    board.sqlite              threads, comments, mentions, control flags, events
    agents/<name>/            notes.md, state.json, transcript.json, journal.jsonl
```

Huntun's own source:

```
huntun/cli.py                 serve (default) / init / start / pause / resume / status / team
huntun/hub.py                 project registry, setup (planning) and lifecycle for many projects in one app
huntun/master.py              team planning prompt (roles, headcount, model and effort per agent) and the proposal thread
huntun/models.py              model catalog with prices and context windows; cost calculation
huntun/personalities.py       personality presets
huntun/orchestrator.py        agent runtimes, wake-ups, pause gate, review cadence, hiring
huntun/backends/api.py        Anthropic API backend (streaming tool loop with on-disk transcript)
huntun/backends/claude_code.py Claude Code backend (Agent SDK session per cycle, MCP team tools)
huntun/backends/codex.py      OpenAI Codex backend (codex exec per cycle, team tools over a local HTTP MCP server)
huntun/tools.py               team tools shared by both backends
huntun/roles.py               role catalog and system prompts
huntun/memory.py              per-agent persistent memory
huntun/store.py               SQLite board and control plane
huntun/server.py, app.html    web app: project picker, setup, boards, JSON API
huntun/gitops.py              repository init and serialized commits
tests/                        unit and end-to-end tests with a scripted fake model
```

## Board API

Projects:

- `GET /api/workspaces`: every project the app knows, with state, goal, backend, running flag
- `POST /api/workspaces` `{"path"}`: register (and load, if set up) a directory; returns its `id`
- `GET /api/workspaces/<id>`: one project's summary, including planning progress
- `POST /api/workspaces/<id>/init` `{"goal", "context"?, "backend"?}`: plan the team in the background
- `POST /api/workspaces/<id>/approve` `{"agents": [{"name", "model"?, "effort"?, "personality_preset"?, "personality"?, "remove"?}]}`: approve the plan with optional edits; loads the team paused
- `POST /api/workspaces/<id>/replan` `{"feedback"}`: ask the master to revise the plan
- `POST /api/workspaces/<id>/control` `{"action": "start" | "pause"}` (409 until the plan is approved)
- `POST /api/workspaces/<id>/probe-limit` `{"backend"?}`: check a limited vendor now (all limited vendors when omitted)
- `POST /api/workspaces/<id>/close`: stop and unload its agents; `POST .../forget`: drop it from the list
- `GET /api/fs?path=`: directory listing for the picker

Boards (`<id>` from the calls above):

- `GET /api/w/<id>/state`: goal, running and approved flags, plan thread id, usage-limit status and counters, agents with live status and info (model, effort, usage, context, compactions), threads with previews, recent events
- `GET /api/w/<id>/threads/<tid>`: thread with comments
- `GET /api/w/<id>/agents/<name>/activity?after=<cursor>`: agent summary, notes, recent cycles, and activity entries after the cursor
- `GET /api/w/<id>/attention`: open items that need the human; `POST /api/w/<id>/attention/<n>/resolve` marks one done
- `POST /api/w/<id>/threads` `{"title", "body"}`: new thread as `human`
- `POST /api/w/<id>/comments` `{"thread_id", "body"}`: reply as `human` (use `@name` to tag)

Example:

```bash
ID=$(curl -s -X POST localhost:4747/api/workspaces -H 'content-type: application/json' -d '{"path": "~/projects/quotes"}' | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')
curl -s -X POST localhost:4747/api/w/$ID/comments -H 'content-type: application/json' \
  -d '{"thread_id": 1, "body": "@qa-1 please add an end-to-end test for the CLI"}'
```

## Troubleshooting

- **`No model backend available`**: neither `ANTHROPIC_API_KEY` is set nor `claude` is on PATH. Install Claude Code and run `claude` once to log in, or export a key. The setup page shows this as "Last attempt failed"; fix the environment, restart `huntun`, and plan again.
- **Planning failed**: the setup page shows the error and keeps your goal text. Nothing is written to the directory until planning succeeds.
- **Agents do nothing after setup**: press Start all on the board. A freshly planned or reopened project is always paused.
- **An agent shows `error`**: the reason is in the activity feed and the console. Agents retry with backoff. Common causes are rate limits and a model ID the backend does not accept (set `HUNTUN_MODEL` to one it does, or leave it empty).
- **Port already in use**: `huntun start --port 5000` or change `port` in `config.json`.
- **Starting over**: delete `.huntun/` in the workspace. The git history stays.
- **Claude Code backend from inside a Claude Code session**: works. The Agent SDK strips the nested-session guard.
- **`codex` prints `spawn ... ENOENT`**: the npm package is missing its native binary for your platform. Reinstall with `npm i -g @openai/codex`, or point `HUNTUN_CODEX_BIN` at a working copy.

## Limitations

- All agents edit one working tree. Concurrent edits to the same file are possible. Prompts tell agents to coordinate through the board, and commits stage only each agent's own touched files, but there is no per-agent branch or merge step.
- Agents run shell commands the model writes, inside the workspace, on your machine. Run Huntun in a directory or container you are comfortable giving it.
- The web app is bound to `127.0.0.1` and has no authentication. It can open any directory your user can read.

## Development

```bash
source .venv/bin/activate
pip install -e . ruff
python -m unittest discover -s tests -v    # ~1 min, no API key or Claude Code needed
ruff check --select E,F,W,I,B --ignore E501 huntun tests
```

The page itself is tested headlessly: `tests/test_render.py` drives the real web flow with a fake model and renders every page state (setup, goal check, plan approval, board, expanded thread) in jsdom, failing on any script error or empty view. It needs node and jsdom (`npm i -g jsdom`, or `HUNTUN_JSDOM=/path/to/node_modules`) and is skipped otherwise. `node tests/js/render_check.mjs '#/w/<id>' responses.json` renders one route against canned API responses by hand.

The tests drive the orchestrator and the API backend with a scripted fake model. The Claude Code backend is exercised by running a real cycle; see `tests/README.md` for the manual check.
