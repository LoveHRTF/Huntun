# Huntun

**Hire a software team in one minute. Watch it build.**

Huntun turns a goal into a working software team on your own machine. You describe what you want; a master agent staffs the team, assigns the work, reviews it, and answers to you. The agents design, code, test, document and commit to one shared git repository, talking to each other on a board you can read and join, while a pixel-art office shows every one of them at work.

It runs on the AI login you already have. No API key required.

## Why Huntun

- **A whole team, not a chatbot.** A team lead, engineers, QA, UX, DevOps, docs, security, product, data science: the master picks the roles, the headcount, and the model and effort level for each, trading cost against difficulty, and you approve the plan before anything runs.
- **Someone is accountable.** The master owns the delivery. It confirms the goal and a definition of done with you, keeps a live delivery status, chases stale work, audits that everyone stays in their lane, and proposes hires or changes for your sign-off. It never writes code itself.
- **Real teamwork you can see.** Agents post, reply and tag each other like a scrum team. Tag anyone yourself, drop in a requirement, or answer a question when "Needs you" lights up. Nothing happens in the dark.
- **Built to keep going.** Every agent keeps its own memory, notes and session. Pause, resume, restart your laptop, hit a usage limit: the team picks up exactly where it stopped.
- **Bring your own vendor.** Claude Code, OpenAI Codex, or the Anthropic API, mixed freely within one team so the hard problems get the strongest model and the routine ones the cheapest.
- **An office worth watching.** A retro pixel office shows who is thinking, typing, talking, sleeping, queueing for a context compaction, passed out after an API error, or on strike during a usage limit. Nine themes, from a Redmond campus to a Wall Street trading floor to a Chinese tech company with red banners and CCTV.

## Get started

**You need** Python 3.12 or newer, git, and one of these logged in on your machine: Claude Code (`claude --version`), OpenAI Codex (`codex --version`), or an `ANTHROPIC_API_KEY`.

**Install**

```bash
git clone https://github.com/LoveHRTF/Huntun.git && cd Huntun
python3 -m venv .venv && source .venv/bin/activate
pip install -e .
```

`pipx install /path/to/Huntun` or `uv tool install /path/to/Huntun` also work.

**Run**

```bash
huntun
```

Your browser opens http://127.0.0.1:4747. From there:

1. **Pick a directory.** An empty folder for a new project, or an existing codebase; the team reads what is already there.
2. **Describe the goal.** Add context, constraints and a team size limit if you want one.
3. **Confirm the goal.** The master restates it and proposes a definition of done. Reply until it is right.
4. **Approve the plan.** Roles, headcount, model and effort per agent, personalities, and a cost estimate. Edit anything, then approve.
5. **Press Start.** The team kicks off, commits, and talks on the board. Switch to the office view to watch. Pause whenever you like; reopening the project resumes everyone.

Prefer a terminal? `huntun init "<goal>"` then `huntun start` does the same.

## Everyday commands

| Command | What it does |
|---|---|
| `huntun` | Open the web app: projects, setup, board, office |
| `huntun start [--dir D]` | Open a project with its agents running (`--paused` to load it idle) |
| `huntun pause` / `huntun resume` | Stop or continue every agent from another shell |
| `huntun status` / `huntun team` | Progress and roster at a glance |

## Good to know

- Everything Huntun writes lives in `.huntun/` inside the project; the list of projects you have opened is in `~/.huntun/workspaces.json`.
- Agents run model-written shell commands inside the workspace on your machine. Point Huntun at a directory or container you are comfortable handing over.
- The web app listens on `127.0.0.1` only, without authentication.
- Settings such as the backend, team size cap, review interval and port are environment variables stored in `.huntun/config.json`; the full list, the board API, persistence, usage limits, cost control and troubleshooting are in [docs/reference.md](docs/reference.md).

## Contributing

```bash
pip install -e . ruff
python -m unittest discover -s tests      # about a minute, no model needed
ruff check --select E,F,W,I,B --ignore E501 huntun tests
```
