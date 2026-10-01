"""Protocol regressions for backend-neutral Office activities and native context telemetry."""
from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
from hashlib import md5
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from huntun.backends.api import _stream_observer, _stream_reply
from huntun.backends.claude_code import ClaudeCodeBackend
from huntun.backends.codex import CodexBackend
from huntun.backends.kimi import KimiBackend
from huntun.backends.telemetry import LiveTelemetry, SessionTail
from huntun.memory import AgentMemory
from huntun.types import AgentSpec, CycleState, HuntunConfig


class _Bridge:
    url = "http://unused"

    def __init__(self, *_):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        pass


class TelemetryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.memory = AgentMemory(self.root / "agents", "dev")
        self.live = LiveTelemetry(self.memory, "test")
        self.ctx = SimpleNamespace(memory=self.memory, cycle=CycleState(), workspace=self.root,
                                   agent=AgentSpec("dev", "backend", "Dev", "work"))

    async def test_tail_skips_history_and_handles_partial_large_utf8_records(self):
        path = self.root / "wire.jsonl"
        path.write_text('{"type":"compacted"}\n')
        tail = SessionTail(lambda: path, self.live.codex_record)
        await tail.prime()
        data = json.dumps({"type": "unused", "payload": "恢复🙂" * 30000}, ensure_ascii=False).encode()
        with path.open("ab") as fh:
            fh.write(data[:100003])
        await tail.poll()
        self.assertEqual(self.memory.state.compactions, 0)
        with path.open("ab") as fh:
            fh.write(data[100003:] + b'\nmalformed\n{"type":"compacted"}\n')
        await tail.close()
        self.assertEqual(self.memory.state.compactions, 1)
        await tail.close()
        self.assertEqual(self.memory.state.compactions, 1, "cursor must prevent replay")

    async def test_snapshot_restores_context_without_replaying_compactions(self):
        path = self.root / "rollout.jsonl"
        token = {"type": "event_msg", "payload": {"type": "token_count", "info": {
            "last_token_usage": {"input_tokens": 90, "cached_input_tokens": 80, "output_tokens": 10},
            "total_token_usage": {"total_tokens": 999999}}}}
        path.write_text(json.dumps(token) + '\n{"type":"compacted"}\n')
        tail = SessionTail(lambda: path, self.live.codex_record)
        await tail.prime(lambda record: self.live.codex_record(record, snapshot=True))
        self.assertEqual((self.memory.state.context_tokens, self.memory.state.compactions), (100, 0))

    async def test_kimi_live_compaction_and_actual_context(self):
        def event(kind, **payload):
            self.live.kimi_record({"message": {"type": kind, "payload": payload}})
        event("CompactionBegin")
        event("CompactionBegin")
        self.assertTrue(self.memory.state.compacting)
        event("CompactionEnd")
        self.assertEqual((self.memory.state.compacting, self.memory.state.compactions), (False, 1))
        event("StatusUpdate", context_tokens=321, max_context_tokens=1000)
        self.assertEqual((self.memory.state.context_tokens, self.memory.state.context_limit), (321, 1000))
        event("StatusUpdate", context_usage=0.5)
        self.assertEqual(self.memory.state.context_tokens, 500)
        for kind, expected in [("ThinkPart", "thinking"), ("TextPart", "text"), ("ToolCall", "tool"), ("ToolResult", "result")]:
            event(kind, text="hello")
            self.assertEqual(self.memory.last_activity["kind"], expected)
        event("CompactionBegin")
        self.live.close()
        self.assertFalse(self.memory.state.compacting, "interrupted compaction must not stick")

    async def test_stream_observers_are_isolated_between_concurrent_agents(self):
        class Stream:
            def __init__(self, word):
                self.word = word

            async def __aiter__(self):
                await asyncio.sleep(0)
                yield {"delta": {"type": "text_delta", "text": self.word}}

            async def get_final_message(self):
                return self.word

        async def read(word):
            events = []
            token = _stream_observer.set(events.append)
            try:
                self.assertEqual(await _stream_reply(Stream(word)), word)
            finally:
                _stream_observer.reset(token)
            return events[0]["delta"]["text"]
        self.assertEqual(await asyncio.gather(read("agent-a"), read("agent-b")), ["agent-a", "agent-b"])
        self.assertIsNone(_stream_observer.get())

    async def test_codex_tool_start_and_rollout_usage_and_compaction(self):
        session = "test-session"
        path = self.root / "sessions/2026/10/01" / f"rollout-{session}.jsonl"
        path.parent.mkdir(parents=True)
        path.write_text('{"type":"compacted"}\n')
        self.memory.state.session_id = session
        self.memory.state.session_cycles = 1

        async def run(_args, _prompt, _cwd, on_event, **_kw):
            on_event({"type": "item.started", "item": {"type": "command_execution", "command": "sleep 100"}})
            self.assertEqual(self.memory.last_activity["kind"], "tool")
            self.assertTrue(self.memory.last_activity["active"])
            for kind in ("mcp_tool_call", "collab_tool_call", "web_search", "file_change", "future_tool"):
                on_event({"type": "item.started", "item": {"type": kind}})
                self.assertEqual(self.memory.last_activity["kind"], "tool", kind)
                on_event({"type": "item.completed", "item": {"type": kind}})
                self.assertEqual(self.memory.last_activity["kind"], "result", kind)
            on_event({"type": "item.updated", "item": {"type": "todo_list", "items": []}})
            self.assertEqual(self.memory.last_activity["kind"], "thinking")
            on_event({"type": "item.started", "item": {"type": "context_compaction"}})
            self.assertTrue(self.memory.state.compacting)
            on_event({"type": "item.completed", "item": {"type": "context_compaction"}})
            with path.open("a") as fh:
                fh.write('{"type":"compacted"}\n')
                fh.write(json.dumps({"type": "event_msg", "payload": {"type": "token_count", "info": {
                    "last_token_usage": {"total_tokens": 137}}}}) + "\n")
            on_event({"type": "item.completed", "item": {"type": "command_execution", "aggregated_output": ""}})
            self.assertEqual(self.memory.last_activity["kind"], "result")
            on_event({"type": "turn.completed", "usage": {"input_tokens": 90000, "cached_input_tokens": 80000, "output_tokens": 1000}})
            return 0, False

        with patch.dict(os.environ, {"CODEX_HOME": str(self.root)}), patch("huntun.backends.codex.McpBridge", _Bridge), patch("huntun.backends.codex.available_tools", return_value=[]):
            backend = CodexBackend(HuntunConfig("test"))
            with patch.object(backend, "_run", run):
                result = await backend.run_cycle(ctx=self.ctx, system="system", prompt="task", model="gpt-5.3-codex", effort="high", should_stop=lambda: False, log=lambda _: None)
        self.assertEqual(result.outcome, "finished", result.error)
        self.assertEqual((self.memory.state.context_tokens, self.memory.state.compactions, self.memory.state.compacting), (137, 1, False))
        self.assertFalse(self.memory.last_activity["active"])

    async def test_kimi_new_session_wire_and_structured_thinking(self):
        session = "new-session"
        path = self.root / "sessions" / md5(str(self.root.resolve()).encode()).hexdigest() / session / "wire.jsonl"

        async def run(_args, _prompt, _cwd, on_event, **_kw):
            path.parent.mkdir(parents=True)
            events = [("CompactionBegin", {}), ("CompactionEnd", {}), ("StatusUpdate", {"context_tokens": 1024, "max_context_tokens": 4000})]
            path.write_text("".join(json.dumps({"message": {"type": kind, "payload": payload}}) + "\n" for kind, payload in events))
            on_event({"role": "assistant", "content": [{"type": "think", "think": "reason"}, {"type": "text", "text": "finished"}]})
            on_event({"role": "meta", "type": "session.resume_hint", "session_id": session})
            return 0, False

        with patch.dict(os.environ, {"KIMI_SHARE_DIR": str(self.root), "HUNTUN_KIMI_BIN": "unused"}), patch("huntun.backends.kimi.McpBridge", _Bridge), patch("huntun.backends.kimi.available_tools", return_value=[]):
            backend = KimiBackend(HuntunConfig("test"))
            with patch.object(backend, "_run", run):
                result = await backend.run_cycle(ctx=self.ctx, system="system", prompt="task", model="", effort="high", should_stop=lambda: False, log=lambda _: None)
        self.assertEqual((result.outcome, result.summary), ("finished", "finished"))
        self.assertEqual((self.memory.state.context_tokens, self.memory.state.compactions), (1024, 1))
        self.assertIn("thinking", [event["kind"] for event in self.memory.read_activity()[0]])

    async def test_claude_hooks_expose_compaction_and_builtin_tool_starts(self):
        backend = ClaudeCodeBackend(HuntunConfig("test"))
        options = backend._options(workspace=self.root, system="system", mcp_tools=[], allowed=[], model="", effort="high", max_turns=10, resume=None,
                                   on_touch=None, on_activity=self.memory.activity, on_compact=self.live.begin_compaction)
        self.assertTrue(options.include_partial_messages)
        await options.hooks["PreCompact"][0].hooks[0]({}, None, {})
        self.assertTrue(self.memory.state.compacting)
        await options.hooks["PreToolUse"][0].hooks[0]({"tool_name": "Bash", "tool_input": {"command": "sleep 100"}}, None, {})
        self.assertEqual(self.memory.last_activity["kind"], "tool")
        self.live.close()
        self.assertFalse(self.memory.state.compacting)
