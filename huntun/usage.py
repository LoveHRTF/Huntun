"""Disjoint token accounting and explicit cost provenance across harness changes."""
from __future__ import annotations

from typing import Any
import json
from pathlib import Path

TOKEN_KEYS = ("input", "output", "cache_read", "cache_write")
ACCOUNTING_VERSION = 2


def token_total(usage: dict[str, float]) -> float:
    return sum(float(usage.get(k, 0) or 0) for k in TOKEN_KEYS)


def cost_summary(state: Any, backend: str = "") -> dict[str, Any]:
    counts = dict(state.usage_cost_counts)
    if state.usage_totals and not state.usage_accounting_version:
        counts["legacy"] = 1
    if not counts:
        counts["local" if backend in ("ollama", "vllm", "llamacpp") else "untracked"] = 1
    return summarize_costs(counts)


def summarize_costs(counts: dict[str, int]) -> dict[str, Any]:
    kinds = sorted(k for k, n in counts.items() if n)
    incomplete = any(k in ("legacy", "untracked", "partial") for k in kinds)
    return {"kinds": kinds, "complete": not incomplete, "counts": counts}


def add_usage(state: Any, usage: dict[str, float], backend: str, cost_status: str = "") -> None:
    # Preserve old amounts and their uncertainty when seats have changed harness.
    if state.usage_totals and not state.usage_accounting_version:
        state.usage_cost_counts["legacy"] = 1
    state.usage_accounting_version = ACCOUNTING_VERSION
    status = cost_status or ("local" if backend in ("ollama", "vllm", "llamacpp") else
                             "reported" if backend == "claude-code" and usage.get("cost_usd", 0) > 0 else "untracked")
    if token_total(usage) or usage.get("turns") or usage.get("cost_usd"):
        state.usage_cost_counts[status] = state.usage_cost_counts.get(status, 0) + 1
    for key, value in usage.items():
        state.usage_totals[key] = round(state.usage_totals.get(key, 0) + float(value), 6)


def journal_usage(entry: dict[str, Any]) -> dict[str, float]:
    """Correct only identifiable legacy Codex records; never infer from a seat's current model."""
    usage = dict(entry.get("usage") or {})
    backend, model = entry.get("backend"), str(entry.get("model") or "")
    codex = backend == "codex" or (not backend and model.startswith(("gpt-", "codex-")))
    if entry.get("usage_accounting_version", 0) < ACCOUNTING_VERSION and codex:
        usage["input"] = max(0, float(usage.get("input", 0)) - float(usage.get("cache_read", 0)))
    return usage


def migrate_legacy_usage(workspace: Path, store: Any) -> None:
    """One-time, evidence-based repair. Keep original states and journal entries intact."""
    from .config import agents_dir
    from .memory import AgentMemory

    if store.get_control("usage:accounting-version", "0") == str(ACCOUNTING_VERSION):
        return
    root = agents_dir(workspace)
    for directory in sorted(root.iterdir()) if root.exists() else []:
        state_file = directory / "state.json"
        if not directory.is_dir() or not state_file.is_file():
            continue
        memory = AgentMemory(root, directory.name)
        state = memory.state
        if state.usage_accounting_version >= ACCOUNTING_VERSION:
            continue
        correction = 0.0
        journal = directory / "journal.jsonl"
        if journal.exists():
            with journal.open("rb") as fh:
                while True:
                    offset = fh.tell()
                    line = fh.readline()
                    if not line:
                        break
                    try:
                        entry = json.loads(line)
                        original = entry.get("usage") or {}
                        corrected = journal_usage(entry)
                        difference = float(original.get("input", 0)) - float(corrected.get("input", 0))
                        if difference <= 0:
                            continue
                        correction += difference
                        key = entry.get("usage_sample") or f"journal:{directory.name}:{offset}"
                        store._exec("UPDATE performance_usage SET input=?,accounting_version=2 WHERE key=? AND agent=?",
                                    corrected["input"], key, directory.name)
                    except (ValueError, TypeError, AttributeError):
                        continue
        backup = directory / "usage-v1-state.json"
        if not backup.exists():
            backup.write_bytes(state_file.read_bytes())
        state.usage_totals["input"] = max(0, float(state.usage_totals.get("input", 0)) - correction)
        if any(state.usage_totals.values()):
            state.usage_cost_counts["legacy"] = 1
            store.set_control("performance:legacy-usage", "1")
        state.usage_accounting_version = ACCOUNTING_VERSION
        memory.save_state()
    store.set_control("usage:accounting-version", str(ACCOUNTING_VERSION))
