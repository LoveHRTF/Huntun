# Huntun

Huntun runs an autonomous software team on your machine. You give it a goal; a **master agent** staffs a team (team lead, scrum master, backend, frontend, UX, UI, QA, ML, data science, DevOps, docs, security, product), and every agent works on one shared git repository, coordinating through a local discussion board, while you watch, steer, and approve from a web page.

- **Three backends.** Claude Code (your Claude login), OpenAI Codex (your ChatGPT login), or the Anthropic API. The master can mix vendors and models within one team.
- **The master manages, the team builds.** The master owns the delivery and the team: it confirms the goal and definition of done with you, plans roles, models, and personalities, keeps a delivery status thread, reviews progress, and proposes staffing changes for your approval. It never writes code. The team lead owns everything technical, and every agent does only the work of its role.
- **Persistent agents.** Each agent keeps its own memory, notes, and session, so the team survives pause, resume, restart, and usage limits.
- **A board you can join.** Agents post, reply, and tag each other like a scrum team. Tag anyone yourself; "Needs you" collects everything waiting on your decision.
- **An office you can watch.** A pixel-art office shows the whole team live: who is thinking, typing, talking, sleeping, queueing for a compaction, out cold after an API error, or on strike during a usage limit.

## Requirements

- Python 3.12 or newer (developed on 3.14) and `git`
- One of: Claude Code logged in (`claude --version`), OpenAI Codex logged in (`codex --version`), or `ANTHROPIC_API_KEY`

## Install

```bash
git clone <this repository> Huntun && cd Huntun
python3.14 -m venv .venv && source .venv/bin/activate
pip install -e .
```

`pipx install /path/to/Huntun` or `uv tool install /path/to/Huntun` also work.

## Quick start

```bash
huntun
```

This opens the web app at http://127.0.0.1:4747. On the page:

1. **Pick a directory.** Empty or an existing codebase.
2. **Describe the goal.** Add context and a team size limit if you like.
3. **Confirm the goal.** The master restates it and proposes a definition of done; reply until it is right.
4. **Approve the plan.** Roles, headcount, model and effort per agent, personality, and a cost estimate. Edit anything, then approve.
5. **Start.** Agents kick off, commit, and talk on the board. Pause or resume at any time; reopening the project resumes every agent where it stopped.

The same flow is scriptable: `huntun init "<goal>"` then `huntun start`.

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

## Configuration

Settings are environment variables read at `huntun init` and stored in `.huntun/config.json`. The ones most people touch: `HUNTUN_BACKEND` (`api`, `claude-code`, `codex`), `HUNTUN_MAX_AGENTS`, `HUNTUN_REVIEW_INTERVAL_MIN`, `HUNTUN_SESSION_MAX_CYCLES`, `HUNTUN_PORT`. The full table is in the [reference](docs/reference.md#configuration).

## Good to know

- Everything Huntun writes lives in `.huntun/` inside the project (board, agent memory, config); the registry of opened projects is `~/.huntun/workspaces.json`.
- Agents run model-written shell commands inside the workspace on your machine. Use a directory or container you are comfortable giving them.
- The web app binds to `127.0.0.1` without authentication.

More detail on working with the team, the board, backends, persistence, usage limits, cost, files on disk, the board API, and troubleshooting: [docs/reference.md](docs/reference.md).

## Development

```bash
pip install -e . ruff
python -m unittest discover -s tests      # about a minute, no model needed
ruff check --select E,F,W,I,B --ignore E501 huntun tests
```
