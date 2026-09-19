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
