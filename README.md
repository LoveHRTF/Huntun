# Huntun · 馄饨

English · [简体中文](README.zh-CN.md)

**The autonomous software team that runs on your machine and reports to you.**

*Huntun* is 馄饨, the wonton: many small pieces, wrapped together, served as one dish.

<img width="1589" height="1104" alt="Screenshot 2026-09-23 at 9 35 05 PM" src="https://github.com/user-attachments/assets/7f3e0837-9e08-4739-ba87-452b496361a1" />

<img width="1589" height="1104" alt="Screenshot 2026-09-23 at 9 36 57 PM" src="https://github.com/user-attachments/assets/45f0a7e8-90c7-48cb-adb5-61335d60e4e7" />

Huntun turns a written goal into a fully staffed engineering team. A master agent assembles the right roles, assigns the work, reviews the output and stays accountable for delivery. The team designs, builds, tests, documents and commits to a single git repository, coordinating on a shared board you can read and join at any time, while a live office view shows every agent at work.


Huntun runs on the AI subscription you already have. No API key, no new vendor relationship, no data leaving your machine except the model calls you already make today.

## What you get

**A complete team from a single brief.** Describe the outcome. The master proposes the roles the project needs, the headcount, and the model and effort level for each seat, balancing cost against difficulty, and presents a plan with a cost estimate. Nothing runs until you approve it.

**Accountable leadership.** The master owns the delivery and the team. It confirms the goal and a definition of done with you, maintains a live delivery status, escalates blockers, keeps every agent within its remit, and brings staffing changes to you for sign-off. It manages; it never writes code.

**Transparent collaboration.** Agents post, review and tag each other on a board that works like the tools your team already uses. Add a requirement, ask a question, or resolve a decision when it is flagged for you. Every discussion, decision and commit is on record.

**Continuity by design.** Each agent keeps its own memory, notes and session. Pauses, restarts and vendor usage limits are absorbed without losing context: work resumes exactly where it stopped, and you can step away for as long as you need.

**Vendor choice, per seat.** Claude Code, OpenAI Codex, Kimi Code, DeepSeek, local models through Ollama and the Anthropic API can be combined within one team, so critical work gets the strongest model while routine tasks run at the lowest cost.

**Operational visibility.** A pixel-art office renders the team's state in real time: who is reasoning, coding, talking, waiting, compacting context, or blocked by an error or usage limit. Nine environments are available, from a corporate campus to a trading floor.

## Getting started

**Requirements.** Python 3.12 or newer, git, and at least one model provider set up on this machine (see below). Huntun detects every provider that is ready and lets the master mix them within one team.

**Set up a provider** (any one is enough; more gives the master more choice):

| Provider | Setup | How Huntun finds it |
|---|---|---|
| Claude Code | Install Claude Code and sign in: `npm i -g @anthropic-ai/claude-code`, then run `claude` once and log in. | `claude` on PATH |
| OpenAI Codex | `npm i -g @openai/codex`, then `codex login`. | `codex` on PATH |
| Kimi Code | `npm i -g @moonshot-ai/kimi-code` (or `brew install kimi-code`), then `kimi login` and pick a model. | `kimi` on PATH with a model configured |
| DeepSeek | Create an API key at platform.deepseek.com and export it: `export DEEPSEEK_API_KEY=sk-...`. | `DEEPSEEK_API_KEY` set |
| Ollama (local) | Install Ollama 0.14 or newer, pull a model that supports tools, and run the server with a context that fits your memory: `ollama pull qwen3:27b`, then `OLLAMA_CONTEXT_LENGTH=32768 ollama serve`. Optionally pin the models and contexts Huntun should use: `export HUNTUN_OLLAMA_MODELS="qwen3:27b@32768"`. | A server answering at `OLLAMA_HOST` (default `http://127.0.0.1:11434`) with models |
| Anthropic API | `export ANTHROPIC_API_KEY=sk-ant-...`. | `ANTHROPIC_API_KEY` set |

Force a particular provider with `HUNTUN_BACKEND=claude-code|codex|kimi|deepseek|ollama|api` or pick it on the setup page.

**Install**

```bash
git clone https://github.com/LoveHRTF/Huntun.git && cd Huntun
python3 -m venv .venv && source .venv/bin/activate
pip install -e .
```

`pipx install /path/to/Huntun` and `uv tool install /path/to/Huntun` are also supported.

**Launch**

```bash
huntun
```

The web app opens at http://127.0.0.1:4747. From there:

1. **Select a directory.** Start from an empty folder or an existing codebase; the team reviews what is already in place.
2. **State the goal.** Add context, constraints and, if you wish, a limit on team size.
3. **Confirm the goal.** The master restates it and proposes a definition of done. Refine it until it is exactly right.
4. **Approve the plan.** Roles, headcount, model and effort per agent, working styles and a cost estimate. Adjust anything, then approve.
5. **Start.** The team begins work, commits progress and coordinates on the board. Open the office view to observe. Pause at any time; reopening the project resumes every agent.

The same workflow is available from the command line: `huntun init "<goal>"` followed by `huntun start`.

## Everyday commands

| Command | Purpose |
|---|---|
| `huntun` | Open the web app: projects, setup, board and office |
| `huntun start [--dir D]` | Open a project with its team running (`--paused` to load it idle) |
| `huntun pause` / `huntun resume` | Halt or continue the whole team from another shell |
| `huntun status` / `huntun team` | Progress and roster at a glance |

## Operating notes

- All Huntun state lives in `.huntun/` inside the project; the list of projects you have opened is kept in `~/.huntun/workspaces.json`.
- Agents execute model-generated shell commands within the workspace on your machine. Run Huntun in a directory or container you are prepared to delegate.
- The web app binds to `127.0.0.1` only and has no authentication layer.
- Backend selection, team size cap, review interval, port and other settings are environment variables persisted in `.huntun/config.json`. The full reference, including the board API, persistence, usage limits, cost control and troubleshooting, is in [docs/reference.md](docs/reference.md).

## Contributing

```bash
pip install -e . ruff
python -m unittest discover -s tests      # about a minute, no model required
ruff check --select E,F,W,I,B --ignore E501 huntun tests
```
