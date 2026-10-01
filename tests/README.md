# Tests

`python -m unittest discover -s tests -v` runs everything that does not need a model:
the board store, mentions and inbox delivery, tools and path sandboxing, git commits posting threads,
memory persistence, the HTTP API, the API backend's tool loop (pause mid-cycle, resume from transcript),
and a three-agent orchestration run with a scripted fake model.

## Manual live check of the Claude Code backend

Requires `claude` on PATH and a logged-in Claude Code. Costs one small session.

```bash
source .venv/bin/activate
env -u ANTHROPIC_API_KEY python - <<'EOF2'
import asyncio, tempfile
from pathlib import Path
from huntun.backends.claude_code import ClaudeCodeBackend
from huntun.config import default_config
from huntun.master import plan_team

config = default_config("Build a tiny Python CLI that prints a random quote, with tests")
config.backend = "claude-code"; config.lead_effort = "medium"
rationale, agents = asyncio.run(plan_team(ClaudeCodeBackend(config), config, "Keep the team as small as possible."))
print(rationale); print([(a.name, a.role) for a in agents])
EOF2
```

For a full cycle (file written with built-in tools, commit via the `huntun` MCP tools, board thread posted),
run `huntun init --backend claude-code "<goal>"` in an empty directory, then `huntun start` with
`HUNTUN_MAX_CYCLES=1`, and watch the board.

## Worktrees, Codex discovery, and long threads

`python -m unittest tests.test_worktrees tests.test_codex_models tests.test_thread_pages`
checks real Git isolation/integration, conflict retry, preservation of human edits,
legacy-agent migration, unfinished task cycles, Codex cache/config discovery and
reasoning levels, and indexed comment paging. The HTTP paging checks also run in
`tests.test_hub`. All model calls in these tests are fake.

For the discussion-board browser behavior, use an installed Node Playwright package:

```bash
HUNTUN_CHROMIUM="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" node tests/js/board_check.mjs
```

`HUNTUN_PLAYWRIGHT` can point to an existing Playwright package if it is outside
Node's normal module search path. Omit `HUNTUN_CHROMIUM` to use Playwright's installed
Chromium. The check runs the real thread rendering functions with controlled API
responses in a browser: pagination, incremental Markdown rendering, preservation
of message nodes and reply drafts, stale-fetch navigation races, empty-page cursors,
and the 500-reply live-tail bound. It does not launch a provider or read user settings.

Watchdog recovery is covered by `test_watchdog.py` (model switches, persisted history, session archives, paused/capped agents and usage limits), the hub HTTP tests, and the dialog checks in `tests/js/board_check.mjs`. These use fake model responses and never call a live provider.


## Office animation coverage

`python -m unittest tests.test_office_telemetry` checks Codex start/completion
events and actual per-request context, Kimi structured thinking and native wire
compaction/status, Claude pre-tool/compaction hooks, concurrent stream isolation,
and bounded JSONL tailing with historical, partial and oversized records. No live
models are called. `tests/js/board_check.mjs` also drives the actual Office state
machine for all eight backends: long-running thinking/text/tools, live and finished
compaction, recovery while globally paused, coffee, pause, limits, errors, sleep,
discussion announcements and master delivery.


`tests.test_limits.LimitScopeTests` covers stale limits on an unused project default
or retired seats, model switches and switches back, last-seat retirement, restored
watchdogs after restart, lazy backend probes, and ignored stale Check requests.
The mixed-provider checks also ensure a vendor still in use retains its limit.
