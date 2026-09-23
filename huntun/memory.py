from __future__ import annotations

import json
import os
from dataclasses import asdict, fields
from pathlib import Path
from typing import Any

from .config import now_iso
from .types import AgentState


def _write_atomic(path: Path, data: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(data)
    os.replace(tmp, path)


class AgentMemory:
    """Persistent memory for one agent, so it can pause, resume, or restart without losing progress.

    notes.md        free-form long-term notes the agent maintains itself
    state.json      structured bookkeeping (task, cycles, touched files, session id, ...)
    transcript.json the in-flight work cycle (API backend message history), if any
    journal.jsonl   append-only log of every cycle summary
    """

    def __init__(self, agents_dir: Path, name: str) -> None:
        self.name = name
        self.dir = agents_dir / name
        self.dir.mkdir(parents=True, exist_ok=True)
        self.state = self._load_state()
        self.last_activity: dict[str, str] | None = None  # kind and time of the newest activity line (the office view animates from it)

    def _load_state(self) -> AgentState:
        f = self.dir / "state.json"
        if not f.exists():
            return AgentState()
        try:
            data = json.loads(f.read_text())
            known = {fl.name for fl in fields(AgentState)}
            return AgentState(**{k: v for k, v in data.items() if k in known})
        except Exception:
            return AgentState()

    def save_state(self) -> None:
        _write_atomic(self.dir / "state.json", json.dumps(asdict(self.state), indent=2))

    def notes(self) -> str:
        f = self.dir / "notes.md"
        return f.read_text() if f.exists() else ""

    def save_notes(self, text: str) -> None:
        _write_atomic(self.dir / "notes.md", text)

    def load_transcript(self) -> list[dict[str, Any]] | None:
        f = self.dir / "transcript.json"
        if not f.exists():
            return None
        try:
            data = json.loads(f.read_text())
            return data if isinstance(data, list) and data else None
        except Exception:
            return None

    def save_transcript(self, messages: list[dict[str, Any]]) -> None:
        _write_atomic(self.dir / "transcript.json", json.dumps(messages))

    def clear_transcript(self) -> None:
        f = self.dir / "transcript.json"
        if f.exists():
            f.unlink()

    def journal(self, entry: dict[str, Any]) -> None:
        with (self.dir / "journal.jsonl").open("a") as fh:
            fh.write(json.dumps({"at": now_iso(), **entry}) + "\n")

    def recent_journal(self, n: int = 5) -> list[dict[str, Any]]:
        f = self.dir / "journal.jsonl"
        if not f.exists():
            return []
        lines = [ln for ln in f.read_text().splitlines() if ln.strip()]
        out: list[dict[str, Any]] = []
        for ln in lines[-n:]:
            try:
                out.append(json.loads(ln))
            except Exception:
                out.append({"raw": ln})
        return out

    def activity(self, kind: str, text: str) -> None:
        """Appends one line to the agent's activity log: cycle starts, tool calls, tool results, model text."""
        at = now_iso()
        self.last_activity = {"kind": kind, "at": at}
        with (self.dir / "activity.jsonl").open("a") as fh:
            fh.write(json.dumps({"at": at, "kind": kind, "text": text[:4000]}) + "\n")

    def read_activity(self, after: int = 0, limit: int = 300) -> tuple[list[dict[str, Any]], int]:
        """Entries after line index `after` (0 = from the start), newest last, plus the new cursor."""
        f = self.dir / "activity.jsonl"
        if not f.exists():
            return [], 0
        lines = f.read_text().splitlines()
        total = len(lines)
        start = max(after, total - limit) if after < total - limit else after
        out: list[dict[str, Any]] = []
        for i, ln in enumerate(lines[start:], start=start):
            try:
                out.append({"i": i + 1, **json.loads(ln)})
            except Exception:
                continue
        return out, total

    def touch(self, rel_path: str) -> None:
        if rel_path not in self.state.touched_files:
            self.state.touched_files.append(rel_path)
            self.save_state()
