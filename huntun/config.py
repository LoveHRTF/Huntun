from __future__ import annotations

import json
import os
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path

from .types import AgentSpec, HuntunConfig

HUNTUN_DIRNAME = ".huntun"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def huntun_dir(workspace: Path) -> Path:
    return workspace / HUNTUN_DIRNAME


def config_path(workspace: Path) -> Path:
    return huntun_dir(workspace) / "config.json"


def team_path(workspace: Path) -> Path:
    return huntun_dir(workspace) / "team.json"


def agents_dir(workspace: Path) -> Path:
    return huntun_dir(workspace) / "agents"


def db_path(workspace: Path) -> Path:
    return huntun_dir(workspace) / "board.sqlite"


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


def default_config(goal: str) -> HuntunConfig:
    return HuntunConfig(
        goal=goal,
        backend=_env("HUNTUN_BACKEND", "auto"),
        model=_env("HUNTUN_MODEL", ""),
        lead_effort=_env("HUNTUN_LEAD_EFFORT", "xhigh"),
        worker_effort=_env("HUNTUN_WORKER_EFFORT", "high"),
        review_interval_min=float(_env("HUNTUN_REVIEW_INTERVAL_MIN", "20")),
        idle_interval_sec=float(_env("HUNTUN_IDLE_INTERVAL_SEC", "90")),
        lead_idle_interval_sec=float(_env("HUNTUN_LEAD_IDLE_INTERVAL_SEC", "600")),
        max_tool_calls_per_cycle=int(_env("HUNTUN_MAX_TOOL_CALLS", "60")),
        max_tokens=int(_env("HUNTUN_MAX_TOKENS", "32000")),
        max_cycles_per_agent=int(_env("HUNTUN_MAX_CYCLES", "0")),
        port=int(_env("HUNTUN_PORT", "4747")),
        fallbacks=_env("HUNTUN_FALLBACKS", "on") != "off",
        created_at=now_iso(),
    )


def resolve_backend(config: HuntunConfig) -> str:
    """Picks the model backend: an explicit setting wins, otherwise API key -> api, Claude Code CLI -> claude-code."""
    choice = os.environ.get("HUNTUN_BACKEND") or config.backend
    if choice in ("api", "claude-code", "codex"):
        return choice
    if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"):
        return "api"
    if shutil.which("claude"):
        return "claude-code"
    if os.environ.get("HUNTUN_CODEX_BIN") or shutil.which("codex"):
        return "codex"
    raise RuntimeError(
        "No model backend available. Set ANTHROPIC_API_KEY (backend 'api'), or install and log in to Claude Code "
        "(`claude` on PATH, backend 'claude-code') or OpenAI Codex (`codex` on PATH, backend 'codex'). Force one with --backend or HUNTUN_BACKEND."
    )


def config_exists(workspace: Path) -> bool:
    return config_path(workspace).exists()


def is_initialized(workspace: Path) -> bool:
    """A project has a team once the goal is confirmed and the master has planned it."""
    return config_path(workspace).exists() and team_path(workspace).exists()


def load_config(workspace: Path) -> HuntunConfig:
    return HuntunConfig.from_dict(json.loads(config_path(workspace).read_text()))


def save_config(workspace: Path, config: HuntunConfig) -> None:
    huntun_dir(workspace).mkdir(parents=True, exist_ok=True)
    config_path(workspace).write_text(json.dumps(config.to_dict(), indent=2))


def load_team(workspace: Path) -> list[AgentSpec]:
    if not team_path(workspace).exists():
        return []
    return [AgentSpec.from_dict(d) for d in json.loads(team_path(workspace).read_text())]


def save_team(workspace: Path, team: list[AgentSpec]) -> None:
    huntun_dir(workspace).mkdir(parents=True, exist_ok=True)
    team_path(workspace).write_text(json.dumps([a.to_dict() for a in team], indent=2))


def slugify(text: str) -> str:
    return re.sub(r"^-+|-+$", "", re.sub(r"[^a-z0-9]+", "-", text.lower()))[:40]
