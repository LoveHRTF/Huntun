"""Pi installation and cached, credential-free model discovery."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

INSTALL = "npm install -g --ignore-scripts @earendil-works/pi-coding-agent; pi install npm:@lolipopshock/pi-clm"
CLAUDE_INSTALL = "pi install npm:pi-claude-code-provider@0.6.0"


def claude_extension() -> Path | None:
    if os.environ.get("HUNTUN_PI_REUSE_CLI_AUTH", "").lower() in {"0", "false", "off"}:
        return None
    override = os.environ.get("HUNTUN_PI_CLAUDE_EXTENSION")
    path = (
        Path(override).expanduser()
        if override
        else agent_dir() / "npm/node_modules/pi-claude-code-provider/index.ts"
    )
    return path.resolve() if path.is_file() else None


def claude_launcher(directory: Path) -> Path | None:
    """A process-local launcher for the pinned adapter's CLI compatibility fix."""
    executable = os.environ.get("PI_CLAUDE_CODE_PROVIDER_PATH") or shutil.which(
        "claude"
    )
    if not executable or not claude_extension():
        return None
    path = directory / "claude-launcher.js"
    helper = Path(__file__).with_name("pi_claude_cli.js").as_uri()
    path.write_text(
        "#!/usr/bin/env node\nimport {runClaude} from "
        + json.dumps(helper)
        + ";\nrunClaude("
        + json.dumps(executable)
        + ");\n"
    )
    path.chmod(0o700)
    return path


def binary() -> str | None:
    return os.environ.get("HUNTUN_PI_BIN") or shutil.which("pi")


def agent_dir() -> Path:
    return Path(
        os.environ.get("PI_CODING_AGENT_DIR") or Path.home() / ".pi" / "agent"
    ).expanduser()


def clm_extension() -> Path | None:
    override = os.environ.get("HUNTUN_PI_CLM_EXTENSION")
    path = (
        Path(override).expanduser()
        if override
        else agent_dir() / "npm/node_modules/@lolipopshock/pi-clm/index.ts"
    )
    return path.resolve() if path.is_file() else None


def sdk_entry() -> Path | None:
    executable = binary()
    if not executable:
        return None
    for parent in Path(executable).resolve().parents:
        try:
            package = json.loads((parent / "package.json").read_text())
            if package.get("name") == "@earendil-works/pi-coding-agent":
                entry = parent / "dist/index.js"
                return entry if entry.is_file() else None
        except (OSError, ValueError):
            pass
    return None


_signature: Any = None
_checked_at = 0.0
_rows: list[dict[str, Any]] = []


def model_rows() -> list[dict[str, Any]]:
    """Refresh on config/auth changes, at most once a minute otherwise.

    No provider inference or catalog network requests. Node prints an allowlist
    of model metadata only; credentials and request headers never reach Huntun's UI.
    """
    global _signature, _checked_at, _rows

    def stamp(path: Path) -> tuple[int, int] | None:
        try:
            st = path.stat()
            return st.st_mtime_ns, st.st_size
        except OSError:
            return None

    home, entry = agent_dir(), sdk_entry()
    codex_home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
    signature = (
        str(home),
        str(entry),
        os.environ.get("HUNTUN_PI_MODELS"),
        os.environ.get("HUNTUN_PI_REUSE_CLI_AUTH"),
        os.environ.get("PI_CLAUDE_CODE_PROVIDER_PATH"),
        os.environ.get("HUNTUN_CODEX_BIN"),
        str(claude_extension()),
        str(codex_home),
        *(stamp(codex_home / name) for name in ("auth.json", "config.toml")),
        stamp(
            Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
            / ".credentials.json"
        ),
        *(stamp(home / name) for name in ("auth.json", "models.json", "settings.json")),
    )
    if signature == _signature and time.monotonic() - _checked_at < 60:
        return _rows
    rows: list[dict[str, Any]] = []
    if entry and shutil.which("node"):
        try:
            proc = subprocess.run(
                [
                    "node",
                    str(Path(__file__).with_name("pi_catalog.js")),
                    entry.as_uri(),
                ],
                capture_output=True,
                text=True,
                timeout=20,
                env={
                    **os.environ,
                    "PI_OFFLINE": "1",
                    "HUNTUN_PI_CLAUDE_EXTENSION": str(claude_extension() or ""),
                },
            )
            value = json.loads(proc.stdout) if proc.returncode == 0 else []
            if isinstance(value, list):
                rows = [
                    r
                    for r in value
                    if isinstance(r, dict) and r.get("id") and r.get("provider")
                ]
        except (OSError, subprocess.SubprocessError, ValueError):
            pass
    _signature, _checked_at, _rows = signature, time.monotonic(), rows
    return rows
