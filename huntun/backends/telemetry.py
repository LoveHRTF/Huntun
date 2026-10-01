"""Provider-neutral live activity and incremental native session telemetry.

Readers observe only this agent's session, never replay old compactions, and do
bounded reads off the event loop. Missing/older CLI logs leave stdout usable.
"""
from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any


class LiveTelemetry:
    def __init__(self, memory: Any, provider: str) -> None:
        self.memory, self.provider = memory, provider
        self.context_seen = False
        self._phase: str | None = None
        self._pulse_at = 0.0

    def pulse(self, kind: str, text: str = "") -> None:
        now = time.monotonic()
        current = (self.memory.last_activity or {}).get("kind")
        if kind != self._phase or kind != current or now - self._pulse_at >= 1:
            self.memory.activity(kind, text)
            self._phase, self._pulse_at = kind, now

    def anthropic_event(self, event: Any) -> None:
        def field(obj: Any, key: str) -> Any:
            return obj.get(key) if isinstance(obj, dict) else getattr(obj, key, None)
        block = field(event, "content_block") or field(event, "delta")
        kind = field(block, "type")
        if kind in ("thinking", "thinking_delta"):
            self.pulse("thinking", str(field(block, "thinking") or ""))
        elif kind in ("text", "text_delta"):
            self.pulse("text", str(field(block, "text") or ""))
        elif kind in ("tool_use", "server_tool_use", "input_json_delta"):
            self.pulse("tool", str(field(block, "name") or "Preparing tool call"))

    def begin_compaction(self) -> None:
        if not self.memory.state.compacting:
            self.memory.state.compacting = True
            self.memory.save_state()
            self.memory.activity("cycle", f"Compacting context ({self.provider})")

    def complete_compaction(self) -> None:
        state = self.memory.state
        state.compacting = False
        state.compactions += 1
        state.context_tokens = 0
        self.memory.save_state()
        self.memory.activity("cycle", f"Context compacted by {self.provider} (#{state.compactions})")

    def close(self) -> None:
        if self.memory.state.compacting:
            self.memory.state.compacting = False
            self.memory.save_state()
        if self.memory.last_activity:
            self.memory.last_activity["active"] = False

    def codex_record(self, record: dict[str, Any], *, snapshot: bool = False) -> None:
        payload = record.get("payload") or {}
        if record.get("type") == "event_msg" and payload.get("type") == "token_count":
            # Turn totals include multiple requests, and input already includes cache hits.
            last = (payload.get("info") or {}).get("last_token_usage")
            if isinstance(last, dict):
                self.memory.state.context_tokens = int(last.get("total_tokens") or
                                                      (last.get("input_tokens", 0) + last.get("output_tokens", 0)))
                self.context_seen = True
                self.memory.save_state()
        elif not snapshot and record.get("type") == "compacted":
            self.complete_compaction()

    def kimi_record(self, record: dict[str, Any], *, snapshot: bool = False) -> None:
        message = record.get("message") or {}
        kind, payload = message.get("type"), message.get("payload") or {}
        if snapshot and kind != "StatusUpdate":
            return
        if kind == "CompactionBegin":
            self.begin_compaction()
        elif kind == "CompactionEnd":
            self.complete_compaction()
        elif kind == "StatusUpdate":
            state = self.memory.state
            if payload.get("max_context_tokens"):
                state.context_limit = int(payload["max_context_tokens"])
            if payload.get("context_tokens") is not None:
                state.context_tokens = int(payload["context_tokens"])
                self.context_seen = True
            elif payload.get("context_usage") is not None:
                state.context_tokens = int(float(payload["context_usage"]) * state.context_limit)
                self.context_seen = True
            self.memory.save_state()
        elif kind in ("StepBegin", "ThinkPart"):
            self.pulse("thinking", str(payload.get("think") or payload.get("text") or "Waiting for Kimi response"))
        elif kind == "TextPart":
            self.pulse("text", str(payload.get("text") or ""))
        elif kind in ("ToolCall", "ToolCallPart"):
            fn = payload.get("function") or {}
            self.pulse("tool", f"{fn.get('name') or 'Kimi tool'} {str(fn.get('arguments') or '')[:300]}")
        elif kind == "ToolResult":
            self.pulse("result", str(payload.get("return_value") or "Tool finished")[:600])
        elif kind in ("StepInterrupted", "StepRetry"):
            self.pulse("error", str(payload.get("error_type") or kind))


class SessionTail:
    """Incremental JSONL tail; partial/oversized records cannot block the pipe loop."""
    def __init__(self, locate: Callable[[], Path | None], consume: Callable[[dict[str, Any]], None]) -> None:
        self.locate, self.consume = locate, consume
        self.path: Path | None = None
        self.offset = 0
        self.pending = bytearray()
        self.discarding = False
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()

    async def prime(self, snapshot: Callable[[dict[str, Any]], None] | None = None) -> None:
        def read() -> list[dict[str, Any]]:
            path = self.locate()
            if path is None or not path.exists():
                return []
            self.path = path
            with path.open("rb") as fh:
                self.offset = fh.seek(0, 2)
                start = max(0, self.offset - 262144)
                fh.seek(start)
                raw = fh.read()
            lines = raw.splitlines()
            if start:
                lines = lines[1:]
            return self._decode(lines) if snapshot else []
        try:
            records = await asyncio.to_thread(read)
        except OSError:
            records = []
        for record in records:
            if snapshot:
                try:
                    snapshot(record)
                except (ValueError, TypeError, AttributeError):
                    continue

    @staticmethod
    def _decode(lines: list[bytes]) -> list[dict[str, Any]]:
        records = []
        for line in lines:
            try:
                record = json.loads(line)
                if isinstance(record, dict):
                    records.append(record)
            except (ValueError, UnicodeError):
                pass
        return records

    def _read(self) -> tuple[list[dict[str, Any]], int]:
        try:
            if self.path is None:
                self.path = self.locate()
            if self.path is None:
                return [], 0
            with self.path.open("rb") as fh:
                if fh.seek(0, 2) < self.offset:
                    self.offset = 0
                    self.pending.clear()
                    self.discarding = False
                fh.seek(self.offset)
                raw = fh.read(262144)
                self.offset += len(raw)
        except OSError:
            return [], 0
        lines = raw.split(b"\n")
        complete: list[bytes] = []
        for index, part in enumerate(lines):
            end = index < len(lines) - 1
            if not self.discarding:
                self.pending.extend(part)
                if len(self.pending) > 8 * 1024 * 1024:
                    self.pending.clear()
                    self.discarding = True
            if end:
                if not self.discarding:
                    complete.append(bytes(self.pending))
                self.pending.clear()
                self.discarding = False
        return self._decode(complete), len(raw)

    async def poll(self) -> int:
        records, size = await asyncio.to_thread(self._read)
        for record in records:
            try:
                self.consume(record)
            except (ValueError, TypeError, AttributeError):
                # CLI telemetry is optional; malformed/changed records must not
                # abort the working agent or prevent later valid records.
                continue
        return size

    def start(self) -> None:
        async def watch() -> None:
            while not self._stop.is_set():
                await self.poll()
                try:
                    await asyncio.wait_for(self._stop.wait(), 0.5)
                except TimeoutError:
                    pass
        self._task = asyncio.create_task(watch())

    async def close(self) -> None:
        # Let an in-flight thread read finish before draining; cancellation of
        # to_thread alone would leave it racing another read of the same cursor.
        if self._task:
            self._stop.set()
            await asyncio.shield(self._task)
        for _ in range(256):
            if not await self.poll():
                break
