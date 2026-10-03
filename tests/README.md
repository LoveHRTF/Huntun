# Tests

`python -m unittest discover -s tests -v` runs everything that does not need a model:
the board store, mentions and inbox delivery, tools and path sandboxing, git commits posting threads,
memory persistence, the HTTP API, the API backend's tool loop (pause mid-cycle, resume from transcript),
and a three-agent orchestration run with a scripted fake model.

`tests.test_hub_lifecycle` covers simultaneous project loads, close/reload, preservation of the running flag, and cleanup before retrying a failed startup. `tests.test_codex_process` uses real local fake writer/host processes to verify pause and cancellation terminate detached descendants without calling a model. Provider activity regressions in `tests.test_orchestrator` verify actual output corrects an overwritten error status.

Staffing confirmation regressions in `tests.test_core` and `tests.test_thread_pages` cover direct human hiring instructions referenced by exact comment ID, invalid/cross-thread/non-human authorization sources, changed-scope boundaries, human confirmation followed by acknowledgements/quoted errors, repeated hires under unchanged approved scope, persistence after reopening, explicit changed proposals awaiting a fresh reply, and rejection of replies before the proposal or in another thread. Legacy boards acquire the proposal marker and its index automatically; comment paging keeps its existing public response shape.

`tests.test_usage` checks model-specific cache prices, mixed cache TTLs, unknown-price handling, persistent cost provenance across harness changes, four-category header totals, and idempotent legacy repairs with immutable journals and original-state backups. Codex/Claude session tests verify cache normalization/creation in native cycle results; the API loop covers fallback-model prices and compaction usage. The browser task-board check covers cost labels and cache breakdowns alongside Performance Review.

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

Needs You is covered by `python -m unittest tests.test_attention tests.test_core tests.test_hub`
and `node tests/js/attention_check.mjs` with the same Playwright/Chromium variables.
Checks cover full legacy requests, cursor paging, human replies and requester wakeups,
per-request resolution, confirmation identity, transaction visibility and concurrent-send
deduplication, rejected conversation-reference asks across roles/harnesses, and inline
goal/plan revisions. Browser checks exercise full Markdown requests, send failures/retries,
keyboard submission, persistent project-specific drafts, focus/selection/scroll preservation,
lazy context history, resolution in another browser and late responses after dialog changes.


## Office animation coverage

`tests.test_office_layout` checks minimum total room area and exact seat counts across all nine themes and 0–65 agents, reachability of every workstation/bed and public fixture, interior retirements with a surviving last seat, automatic shrinking and deterministic reload, migration of oversized legacy rooms without losing speech or scripts, and hiring within a room that already fits. The browser state check verifies a live room shrink replaces both the background and buffered coordinates and retains all remaining people after reload.

`python -m unittest tests.test_office_layout tests.test_office_traffic tests.test_office_state tests.test_office_telemetry`
checks saved half-step salute stalls, boss acknowledgement, stationary guards blocking hidden hires in all nine themes with mixed harnesses, treatment beside patients whose saved routes moved, casualties on the door/entry, doctors leaving after recovery, errors interrupting listening/yielding/setup, Chinese despair poses without doctors, paused script recovery, doctors yielding and resuming treatment, opposing walkers, occupied passing bays, circular waits, collision-free tile reservations and deterministic restart during yielding. Original scripts and normal walking speed are retained; completed walkers may subsequently step aside for another actor.

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

`python -m unittest tests.test_office_state` runs the real embedded V8 backend
against temporary stores: snapshots cannot mutate or reroll the scene, restarts
preserve movement/scripts/random choices, idle choices cover all eight providers,
compaction and watchdog events remain authoritative, thinking poses persist, and
theme migration accepts only the first browser preference.
Speech checks resolve exact full messages while keeping activity previews short,
migrate existing event tables, queue posts in order, cover every six-second page
without the old 45-second cutoff, deduplicate speech history, and preserve a
guard greeting after the boss leaves and across restart.
Queued visit-to-local-speech and coffee-to-work transitions verify that pathfinding
uses the newly selected mode, including after restoring an existing scene.
Boss departures cross the doorway before hiding; removed actors retain their
appearance for buffered frames, survive reload/restart, and expire with history.
Simultaneous Codex and mixed-harness hires complete greeting and furniture delivery;
saved entry/exit deadlocks recover at normal walking speed, including a restart
during the passing maneuver. Existing smokers clear the introduction tile.
Clock regressions verify monotonic movement history despite system-time changes,
including restoring a saved scene after the wall clock moves backwards.
The long-running check advances nine agents through 24,000 backend ticks (20
simulated minutes) and verifies that V8 stays within its memory limit.

`tests/js/office_state_check.mjs` paints a real backend fixture in two independent
Chrome contexts and verifies refresh/reopen continuity during movement and thinking,
immutable rendering, no browser randomness, and `/huntun/` reverse-proxy paths.
It also replays real 20 Hz backend movement over polling with delays up to 600 ms
and checks rendered frames for stalls, position jumps, and clock resets. Canvas
checks ensure buffered poses display the corresponding full speech page rather
than a future or empty bubble, and system-clock changes do not jump playback.
The boss-exit fixture starts after the backend has removed the boss and verifies
that the browser still paints the last walk through the door before hiding it.

For all nine themes, it opens, reloads and reopens a furnished nine-agent room,
compares its desks, bunks and map against the real backend layout, checks visible
characters, and verifies the cached background pixel by pixel against a repaint.

```sh
HUNTUN_PLAYWRIGHT=/path/to/playwright HUNTUN_CHROMIUM=/path/to/chrome node tests/js/office_state_check.mjs
HUNTUN_PLAYWRIGHT=/path/to/playwright HUNTUN_CHROMIUM=/path/to/chrome node tests/js/office_playback_recovery_check.mjs
```

The recovery suite reproduces slow first responses (2.8 seconds), sustained slow
polling (600 milliseconds), playback clocks ahead/behind, and network outages.
After recovery it checks every rendered frame for stalls, jumps and exhausted
history, and ensures unchanged labels do not repeat text measurement per frame.
The full-page task board suite also verifies that Office refreshes defer hidden
thread previews, reuse unchanged sidebar nodes, retain the renderer during failed
state refreshes, and show fresh threads on return.

The server requires the packaged `mini-racer` dependency; Node and Chrome are
needed only for the browser checks. All fixtures are temporary and call no models.

## Delivery board, project tabs and watchdog conversations

`python -m unittest tests.test_tasks tests.test_watchdog tests.test_codex_permissions tests.test_office_state`
checks DoD migration, durable cards, owner permissions, completion evidence, merge failures, parent consistency,
real watchdog target threads and replies, unrestricted Codex flags for new/resumed sessions, the 500 unique
greetings, salute boundaries and preservation of the interrupted guard script on restart.
The task API is covered by `tests.test_hub`. `HUNTUN_TEST_CODEX_SANDBOX=1` also checks the installed CLI's
new/resume flag compatibility without authentication or a model request.

Run `node tests/js/task_board_check.mjs` with `HUNTUN_PLAYWRIGHT` and `HUNTUN_CHROMIUM` as above.
It exercises the full page under a proxy prefix: creating, assigning, dragging and completing cards,
evidence validation, reload persistence, project switching, and tabs in task/Office/setup views.


## Project performance and targeted boss visits

`tests/test_performance.py` covers hourly aggregation, token categories, historical
journal cursors, partial append/restart safety, sample and commit SHA deduplication,
retired agents, project isolation, native commits without counting peer merges, exact rolling-window totals across bin sizes, partial-bin coverage, current/future boundaries, UTC Monday weeks, and interval validation.
`tests/test_hub.py` verifies the project-scoped HTTP endpoint and range validation.
`tests/js/task_board_check.mjs` exercises Discussion/Office navigation beside Task board, direct view selection from other project pages, reload persistence, review metrics,
all-metric cells and a single compact grouped histogram with adjacent T/M/C bars, independent granularity, tooltips, partial bins, scope/timezone controls, keyboard drilldown, CSV export, complete agent lists, reload/localization, project switching, responsive chart/table containment, desktop/mobile automatic-refresh scroll retention, and K/M/B token labels with exact hover/export values. The UX scenarios also check vertical bar orientation and common baselines, consistent per-metric heights across agents, a compact desktop/phone inspector, activity filtering, useful initial selection, roving focus and arrow/Home/End navigation, a single continuous table with horizontal scrolling, fixed agent labels and no pagination, cell size and unchanged-cell reuse without page jumps, 390px navigation/column fit, and empty/loading/error/recovery states that retain last-good data.

The Office state suite verifies explicit single/multiple boss recipients, `@all`,
500 unique acknowledgement lines, complete saved reply durations, four-tile salute
boundaries, stopping smoking and moving aside, and exact scene restoration with
saved boss particle phases. The Office browser suite also paints the detailed boss
and particles during buffered entry/exit and checks continuous motion.

## Pi + CLM

`python -m unittest tests.test_pi_auth` verifies the real Pi auth runtime against synthetic Codex JWTs and a fake native CLI: account precedence, logout, live token rotation, serialized refresh across worker processes, canonical-store persistence and secret redaction. With the Claude CLI adapter installed, it also checks CLI-only catalog discovery and the temporary launcher’s builtin-plugin settings. These tests never use real credentials or make inference calls.

`python -m unittest tests.test_pi_clm` checks MCP team/recovery tools, native session resume and rotation, pauses/limits, structured planning, usage without double counting, JSONL records over 64 KB, installation failures and existing-project harness changes. When Pi + CLM is installed it also runs the real CLI against a local fixture HTTP provider: native file tools, commit/merge from a private worktree, CLM prompt loading, catalog context/prices, and resumed conversation. No billable model requests are made.

`python -m unittest tests.test_hub.HubTests.test_pi_harness_new_and_existing_projects` checks setup and existing-project changes through the HTTP API. `node tests/js/harness_check.mjs` checks the setup, project-level and per-agent Pi selectors through a proxy prefix.

Recovery checks in `tests/test_office_state.py` reproduce doctors blocking the Chinese office entrance with a stale patient route and verify departure after a saved-scene restart. `tests/test_orchestrator.py` checks interruption of a stalled model request and retry backoff, transcript-preserving resume, credential-free socket diagnostics, and resuming → retrying → working status transitions.
