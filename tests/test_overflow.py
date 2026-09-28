"""Context overflows on llama.cpp servers: a KV pool several sessions share fills up ("Context size has been exceeded."),
or one request is longer than the context. The API loop learns about shared pools, compacts (trimming what it sends if
it has to) or waits, instead of failing the cycle and replaying the same transcript on the next one."""
from __future__ import annotations

import asyncio
import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from unittest import mock

try:
    from test_providers import LLAMACPP_ENV, isolate_huntun_home  # when discovered with -s tests
except ImportError:  # pragma: no cover
    from tests.test_providers import LLAMACPP_ENV, isolate_huntun_home

from huntun import models
from huntun.backends import looks_like_limit, looks_like_overflow, make_backend
from huntun.backends.api import _handoff, _trimmed
from huntun.config import agents_dir, db_path, default_config, save_config, save_team
from huntun.gitops import ensure_repo
from huntun.master import master_spec
from huntun.memory import AgentMemory
from huntun.store import Store
from huntun.tools import ToolContext
from huntun.types import AgentSpec, CycleState

ALIAS = "qwen3.8-27b-uncensored"
POOL_FULL = {"code": 500, "message": "Context size has been exceeded.", "type": "server_error"}
TOO_LONG = {"code": 400, "message": "request (140000 tokens) exceeds the available context size (131072 tokens), try increasing it",
            "type": "exceed_context_size_error", "n_prompt_tokens": 140000, "n_ctx": 131072}


class _Llama(BaseHTTPRequestHandler):
    """A single-model llama-server with --kv-unified: /props gives each of its 2 sessions the whole 128K pool. Each
    POST /v1/messages plays the next scripted step: ("pool_full",) fails mid-stream as llama-server does when the pool
    fills during decoding, ("too_long",) is the 400 for a prompt longer than the context, ("text", s) and
    ("tool", name, input) are replies."""
    script: list[tuple[Any, ...]] = []
    requests: list[dict[str, Any]] = []

    def log_message(self, *a: Any) -> None:
        pass

    def _json(self, code: int, body: dict[str, Any]) -> None:
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/v1/models":
            self._json(200, {"object": "list", "data": [{"id": ALIAS, "object": "model"}]})
        elif self.path == "/props":
            self._json(200, {"default_generation_settings": {"n_ctx": 131072}, "total_slots": 2})
        else:
            self._json(404, {})

    def do_POST(self) -> None:  # noqa: N802
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        type(self).requests.append(body)
        step = type(self).script.pop(0) if type(self).script else ("text", "done")
        if step[0] == "too_long":
            self._json(400, {"error": TOO_LONG})
            return
        message = {"id": "msg_1", "type": "message", "role": "assistant", "model": body["model"], "content": [],
                   "stop_reason": None, "stop_sequence": None, "usage": {"input_tokens": 1200, "output_tokens": 10}}
        events: list[tuple[str, dict[str, Any]]] = [("message_start", {"message": message})]
        if step[0] == "pool_full":
            events.append(("error", POOL_FULL))                                           # llama-server: event: error, then it closes the stream
        elif step[0] == "text":
            events += [("content_block_start", {"index": 0, "content_block": {"type": "text", "text": ""}}),
                       ("content_block_delta", {"index": 0, "delta": {"type": "text_delta", "text": step[1]}}),
                       ("content_block_stop", {"index": 0}),
                       ("message_delta", {"delta": {"stop_reason": "end_turn", "stop_sequence": None}, "usage": {"output_tokens": 10}}),
                       ("message_stop", {})]
        else:
            events += [("content_block_start", {"index": 0, "content_block": {"type": "tool_use", "id": f"toolu_{len(type(self).requests)}", "name": step[1], "input": {}}}),
                       ("content_block_delta", {"index": 0, "delta": {"type": "input_json_delta", "partial_json": json.dumps(step[2])}}),
                       ("content_block_stop", {"index": 0}),
                       ("message_delta", {"delta": {"stop_reason": "tool_use", "stop_sequence": None}, "usage": {"output_tokens": 10}}),
                       ("message_stop", {})]
        data = "".join(f"event: {name}\ndata: {json.dumps(ev if name == 'error' else {'type': name, **ev})}\n\n" for name, ev in events).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


FINISH = ("tool", "finish_cycle", {"summary": "ran the demo", "next_task": "look at the eyes model"})


def transcript() -> list[dict[str, Any]]:
    """A cycle resumed after the pool overflowed: two tool calls, the first with a long output."""
    return [{"role": "user", "content": "TASK: make the demo pass"},
            {"role": "assistant", "content": [{"type": "text", "text": "Running the tests."},
                                              {"type": "tool_use", "id": "t1", "name": "run_command", "input": {"command": "pytest -q"}}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "PASS test_x\n" * 2500}]},
            {"role": "assistant", "content": [{"type": "tool_use", "id": "t2", "name": "run_command", "input": {"command": "python scripts/demo_annotate.py"}}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t2", "content": "DEGRADED: eyes output not parseable"}]}]


class OverflowTests(unittest.TestCase):
    def setUp(self) -> None:
        for k in LLAMACPP_ENV:
            os.environ.pop(k, None)
        isolate_huntun_home(self)
        _Llama.script, _Llama.requests = [], []
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), _Llama)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"
        os.environ["HUNTUN_LLAMACPP_URL"] = self.url
        models.SHARED_POOLS.clear()
        models.refresh_llamacpp(force=True)
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self.tmp.name)
        self.config = default_config("Annotate videos")
        self.config.backend = "llamacpp"
        self.team = [master_spec(), AgentSpec("dev-1", "backend", "Dev", "dev")]
        save_config(self.ws, self.config)
        save_team(self.ws, self.team)
        asyncio.run(ensure_repo(self.ws))
        self.store = Store(db_path(self.ws))
        self.memory = AgentMemory(agents_dir(self.ws), "dev-1")
        self.sleeps: list[float] = []

        async def no_sleep(s: float) -> None:
            self.sleeps.append(s)

        patcher = mock.patch("huntun.backends.api.asyncio.sleep", no_sleep)
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self) -> None:
        self.store.close()
        self.tmp.cleanup()
        self.srv.shutdown()
        for k in LLAMACPP_ENV:
            os.environ.pop(k, None)
        models.SHARED_POOLS.clear()
        models.refresh_llamacpp(force=True)

    def run_cycle(self, context_tokens: int) -> Any:
        self.memory.save_transcript(transcript())
        self.memory.state.context_tokens = context_tokens
        self.memory.save_state()
        ctx = ToolContext(agent=self.team[1], team=lambda: self.team, workspace=self.ws, store=self.store, memory=self.memory,
                          config=self.config, cycle=CycleState())
        return asyncio.run(make_backend("llamacpp", self.config).run_cycle(ctx=ctx, system="SYS", prompt="go", model=ALIAS, effort="high",
                                                                          should_stop=lambda: False, log=lambda s: None))

    def test_errors_are_told_apart(self) -> None:
        for text in (str(POOL_FULL), TOO_LONG["message"], "prompt is too long: 210000 tokens > 200000 maximum",
                     "This model's maximum context length is 32768 tokens"):
            self.assertTrue(looks_like_overflow(text), text)
            self.assertFalse(looks_like_limit(text), f"{text} is not a usage limit")
        self.assertFalse(looks_like_overflow("usage limit reached"))

    def test_a_full_shared_pool_teaches_the_share_and_compacts_the_agent_over_it(self) -> None:
        self.assertEqual(models.context_limit(ALIAS), 131072, "/props gives each session the whole pool")
        _Llama.script = [("pool_full",), ("text", "SUMMARY: tests pass, demo degraded"), FINISH]
        res = self.run_cycle(context_tokens=60_000)
        self.assertEqual(res.outcome, "finished", res.error)
        self.assertIn(self.url, models.SHARED_POOLS)
        self.assertEqual(models.context_limit(ALIAS), 65536, "each of the 2 sessions now counts on half the pool")
        self.assertEqual(self.memory.state.context_limit, 65536)
        self.assertEqual(self.memory.state.compactions, 1)
        summary_ask = _Llama.requests[1]["messages"]
        self.assertIn("characters cut to save context", json.dumps(summary_ask), "the summary request is trimmed so it fits")
        self.assertIn("DEGRADED: eyes output not parseable", json.dumps(summary_ask), "the latest tool output is kept whole")
        resumed = _Llama.requests[2]["messages"]
        self.assertEqual(len(resumed), 1)
        self.assertIn("SUMMARY: tests pass, demo degraded", resumed[0]["content"])
        st = models.probe_server("llamacpp", self.url)
        self.assertEqual((st["context"], st["slots"], st["pool"]), (65536, 2, 131072))

    def test_an_agent_within_its_share_waits_for_the_others_instead_of_compacting(self) -> None:
        _Llama.script = [("pool_full",), FINISH]
        res = self.run_cycle(context_tokens=20_000)
        self.assertEqual(res.outcome, "finished", res.error)
        self.assertEqual(self.memory.state.compactions, 0)
        self.assertEqual(self.sleeps, [15])
        self.assertEqual(len(_Llama.requests[1]["messages"]), 5, "the same conversation, retried")

    def test_a_request_too_long_even_to_summarize_gets_a_mechanical_handoff(self) -> None:
        _Llama.script = [("too_long",), ("too_long",), ("too_long",), FINISH]
        res = self.run_cycle(context_tokens=120_000)
        self.assertEqual(res.outcome, "finished", res.error)
        self.assertEqual(models.SHARED_POOLS, set(), "a request longer than the context says nothing about pools")
        resumed = _Llama.requests[3]["messages"]
        self.assertEqual(len(resumed), 1)
        text = resumed[0]["content"]
        self.assertTrue(text.startswith("TASK: make the demo pass"))
        self.assertIn('- run_command {"command": "python scripts/demo_annotate.py"}', text)
        self.assertIn("Running the tests.", text)

    def test_it_gives_up_when_the_pool_never_frees(self) -> None:
        _Llama.script = [("pool_full",)] * 6
        res = self.run_cycle(context_tokens=10_000)
        self.assertEqual(res.outcome, "error")
        self.assertIn("kept overflowing", res.error)
        self.assertEqual(self.sleeps, [15, 30, 45, 60])

    def test_trimming_keeps_the_task_and_the_latest_turns(self) -> None:
        msgs = transcript()
        t = _trimmed(msgs, keep=2, limit=100)
        self.assertEqual(t[0], msgs[0])
        self.assertEqual(t[3:], msgs[3:])
        self.assertLess(len(json.dumps(t[2])), 400)
        self.assertEqual(t[1]["content"][1]["name"], "run_command")
        self.assertEqual(json.loads(json.dumps(t)), t, "still a valid transcript")
        h = _handoff(msgs, "go")[0]["content"]
        self.assertIn("- run_command {\"command\": \"pytest -q\"}", h)


if __name__ == "__main__":
    unittest.main()
