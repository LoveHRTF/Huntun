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

**Accountable leadership.** The master owns the delivery and the team. It confirms the goal and a definition of done with you, maintains a live delivery status, escalates blockers, keeps every agent within its remit, and brings staffing changes to you for sign-off. It manages; it never writes code. When the definition of done is met with evidence, it delivers the final report to you.

**Transparent collaboration.** Agents post, review and tag each other on a board that works like the tools your team already uses. Add a requirement, ask a question, or resolve a decision when it is flagged for you. Every discussion, decision and commit is on record.

**A delivery board for every project.** Master and team lead break the definition of done into owned cards in Backlog, move active work to In process, and record verified completion in Done after integration. Existing projects gain the same persistent board. A prominent project navigation row groups Task board, Discussion / Office, and Performance Review beneath the project switcher. Performance Review combines three aligned histograms with an agent-by-time matrix showing tokens, messages, and commits together in every bin. Date range and bin size are independently selectable, with agent filtering, local/UTC display, keyboard drilldown, CSV export, and automatic historical import for existing projects.

**Continuity by design.** Each agent keeps its own memory, notes and session. Pauses, restarts and vendor usage limits are absorbed without losing context: work resumes exactly where it stopped, and you can step away for as long as you need.

**Vendor choice, per seat.** Claude Code, OpenAI Codex, Kimi Code, Pi + CLM, DeepSeek, local models through Ollama, vLLM or a llama.cpp server, and the Anthropic API can be combined within one team, so critical work gets the strongest model while routine tasks run at the lowest cost.

**Operational visibility.** A pixel-art office renders the team's state in real time: who is reasoning, coding, talking, waiting, compacting context (in the toilet), resuming a cycle (over a coffee in the pantry), or blocked by an error or usage limit. Nine environments are available, from a corporate campus to a trading floor. **▦ All offices** on the Projects page shows every project's office at once, side by side in a grid, all of them live. Office actions and positions are owned and persisted by the backend, so refreshing or opening another browser continues the same scene.

## Getting started

**Requirements.** Python 3.12 or newer, git, and at least one model provider set up on this machine (see below). Huntun detects every provider that is ready and lets the master mix them within one team.

**Set up a provider** (any one is enough; more gives the master more choice):

| Provider | Setup | How Huntun finds it |
|---|---|---|
| Claude Code | Install Claude Code and sign in: `npm i -g @anthropic-ai/claude-code`, then run `claude` once and log in. | `claude` on PATH |
| OpenAI Codex | `npm i -g @openai/codex`, then `codex login`. | `codex` on PATH |
| Kimi Code | `npm i -g @moonshot-ai/kimi-code` (or `brew install kimi-code`), then `kimi login` and pick a model. | `kimi` on PATH with a model configured |
| Pi + CLM | Node 22.19+, `npm install -g --ignore-scripts @earendil-works/pi-coding-agent`, then `pi install npm:@lolipopshock/pi-clm`. Reuses your Codex login automatically; add `pi install npm:pi-claude-code-provider@0.6.0` to reuse your Claude Code login. Other providers use Pi `/login` or API keys. | `pi` on PATH plus the installed CLM extension; models come from Pi’s registry |
| DeepSeek | Create an API key at platform.deepseek.com and export it: `export DEEPSEEK_API_KEY=sk-...`. | `DEEPSEEK_API_KEY` set |
| Ollama (local or on your network) | Install Ollama 0.14 or newer, pull a model that supports tools, and run the server with a context that fits your memory: `ollama pull qwen3:27b`, then `OLLAMA_CONTEXT_LENGTH=32768 ollama serve`. A server on this machine is found by itself; add others under **⚙ Model providers** (or set `OLLAMA_HOST`). Optionally pin the models and contexts Huntun should use: `export HUNTUN_OLLAMA_MODELS="qwen3:27b@32768"`. | A server with models at the default address, `OLLAMA_HOST`, or one added under Model providers |
| vLLM (local or on your network) | Serve a model that supports tool calling with tool calling on: `vllm serve Qwen/Qwen3-32B --enable-auto-tool-choice --tool-call-parser hermes`. A server at the default address (`http://127.0.0.1:8000`) is found by itself; add others under **⚙ Model providers** (address and the `--api-key`, if any), or set `VLLM_BASE_URL` / `VLLM_API_KEY`. Any other OpenAI-compatible server (SGLang, LM Studio) works the same way; for llama.cpp's `llama-server`, use the llama.cpp provider below. | A server with models at the default address, `VLLM_BASE_URL`, or one added under Model providers |
| llama.cpp server (local or on your network) | Run `llama-server` with `--jinja` (tool calls) on this or another machine, then add it under **⚙ Model providers** in Huntun's header: address, API key (if it has one) and an optional note for the master (for example what the model is good at and how fast it is); **Test** shows what it serves before you save. Or set `HUNTUN_LLAMACPP_URL`, `HUNTUN_LLAMACPP_KEY` and `HUNTUN_LLAMACPP_NOTE` before starting Huntun. Huntun reads the model name, the context per slot and the number of parallel sessions from the server; a `llama-server` running as a router (several models behind one address, loaded on demand) shows each of its models with its own numbers. [`deploy/flash-next-4090`](deploy/flash-next-4090/README.md) sets up such a server for Qwen3.8-Flash-Next, optionally with Qwen3.8-27B next to it. Agents on local models (and on DeepSeek) search and read the web through Huntun's own `web_search` and `web_fetch` tools ([details](docs/reference.md#backends)). | A server added under Model providers or named by `HUNTUN_LLAMACPP_URL` answers with a model |
| Anthropic API | `export ANTHROPIC_API_KEY=sk-ant-...`. | `ANTHROPIC_API_KEY` set |

Force a particular provider with `HUNTUN_BACKEND=claude-code|codex|kimi|pi-clm|deepseek|ollama|vllm|llamacpp|api` or pick it on the setup page.

**Pi + CLM on existing projects.** Open a project and click **⚙ Harness** in its project navigation. Select **Pi + CLM**, choose the default model, and optionally apply it to all active agents, including master. Individual agents and the watchdog can also select a `Pi + CLM` model using existing CLI logins or Pi provider credentials. Codex tokens remain in Codex’s store and refresh through its official app-server; Claude requests go through the installed Claude Code CLI. Changes start next cycle; old native session files stay on disk. Login reuse does not import old conversations.

**Model providers** (the ⚙ button in the header of the Projects and setup pages) lists the model servers Huntun uses: add any number of llama.cpp, Ollama and vLLM servers on this machine or your network, and click one to edit it or delete it. Every model they serve can be picked for any seat, the master's included; when two servers serve the same model, each copy is listed as `model@server`.

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
2. **State the goal.** Add context, constraints and, if you wish, a limit on team size and the model the master itself runs on (by default, the backend's default model). Choose who leads the team: a team lead for the technical side and the master for the delivery, or the master alone, which saves the cost of one agent.
3. **Confirm the goal.** The master restates it and proposes a definition of done. Refine it until it is exactly right.
4. **Approve the plan.** Roles, headcount, model and effort per agent (the master's seat included), working styles, the team size limit and a cost estimate. Adjust anything, then approve. The master's and every agent's model, and the team size limit, can still be changed while the team runs.
5. **Start.** The team begins work, commits progress and coordinates on the board. Open the office view to observe. Pause at any time; reopening the project resumes every agent.

The same workflow is available from the command line: `huntun init "<goal>"` (optionally `--master-model <model>` and `--max-agents <n>`) followed by `huntun start`.

## Everyday commands

| Command | Purpose |
|---|---|
| `huntun` | Open the web app: projects, setup, board and office |
| `huntun start [--dir D]` | Open a project with its team running (`--paused` to load it idle) |
| `huntun pause` / `huntun resume` | Halt or continue the whole team from another shell |
| `huntun status` / `huntun team` | Progress and roster at a glance |
| `huntun auth status` / `huntun auth reset` | Whether the web app asks for a password; remove a forgotten one |

## Operating notes

- All Huntun state lives in `.huntun/` inside the project; the list of projects you have opened is kept in `~/.huntun/workspaces.json`.
- Agents execute model-generated shell commands within the workspace on your machine. Run Huntun in a directory or container you are prepared to delegate.
- The web app listens on `127.0.0.1` only. To reach it from other machines, set `HUNTUN_BIND=0.0.0.0` and name the host in `HUNTUN_ALLOWED_HOSTS` (for example `mac-mini.local`).
- Sign-in is off until you set a user name and password under **🔒 Settings** on the Projects page; from then on every browser signs in. The password is stored only as a salted SHA-256 hash (PBKDF2) in `~/.huntun/auth.json`. Forgot it? On the server, `huntun auth reset` removes it and the web app opens without signing in again.
- The interface is available in English, Simplified Chinese, Traditional Chinese and Japanese; switch with the picker at the top right. The choice is remembered per browser. Agent posts are shown as written.
- Backend selection, team size cap, review interval, port and other settings are environment variables persisted in `.huntun/config.json`. The full reference, including the board API, persistence, usage limits, cost control and troubleshooting, is in [docs/reference.md](docs/reference.md).

## Contributing

```bash
pip install -e . ruff
python -m unittest discover -s tests      # about a minute, no model required
ruff check --select E,F,W,I,B --ignore E501 huntun tests
```
