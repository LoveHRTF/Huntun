"""vLLM backend against a stub OpenAI-compatible server: discovery, the tool-use loop, structured answers, error handling."""
from __future__ import annotations

import asyncio
import json
import os
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from huntun import models
from huntun.backends import make_backend
from huntun.backends.api import ApiBackend
from huntun.backends.vllm import VllmBackend, _split_think, is_openai_transcript
from huntun.config import agents_dir, db_path, default_config, save_config, save_team
from huntun.gitops import ensure_repo
from huntun.master import master_spec
from huntun.memory import AgentMemory
from huntun.store import Store
from huntun.tools import ToolContext
from huntun.types import AgentSpec, CycleState

ENV = ("VLLM_BASE_URL", "VLLM_API_KEY", "HUNTUN_VLLM_MODELS", "HUNTUN_VLLM_CONTEXT")


class StubServer:
    """Answers GET /v1/models with a fixed list and POST /v1/chat/completions from a script of (status, body) pairs."""

    def __init__(self) -> None:
        self.script: list[tuple[int, dict[str, Any]]] = []
        self.requests: list[dict[str, Any]] = []
        self.headers: list[dict[str, str]] = []
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def _answer(self, status: int, body: dict[str, Any], extra: dict[str, str] | None = None) -> None:
                raw = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                for k, v in (extra or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self) -> None:  # noqa: N802
                stub.headers.append(dict(self.headers))
                if self.path == "/v1/models":
                    self._answer(200, {"object": "list", "data": [{"id": "Qwen/Qwen3-32B", "object": "model", "max_model_len": 40960}, {"id": "llama-4", "object": "model"}]})
                else:
                    self._answer(404, {"error": {"message": "not found"}})

            def do_POST(self) -> None:  # noqa: N802
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)))
                stub.requests.append(body)
                stub.headers.append(dict(self.headers))
                status, answer = stub.script.pop(0) if stub.script else (500, {"message": "script exhausted"})
                self._answer(status, answer, {"Retry-After": "30"} if status == 429 else None)

            def log_message(self, *a: Any) -> None:
                pass

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.srv.server_port}"

    def close(self) -> None:
        self.srv.shutdown()
        self.srv.server_close()


def completion(content: str | None = None, tool_calls: list[tuple[str, dict[str, Any] | str]] | None = None, finish: str = "stop",
               reasoning: str | None = None, prompt_tokens: int = 100, completion_tokens: int = 10) -> tuple[int, dict[str, Any]]:
    msg: dict[str, Any] = {"role": "assistant", "content": content}
    if reasoning:
        msg["reasoning_content"] = reasoning
    if tool_calls:
        msg["tool_calls"] = [{"id": f"chatcmpl-tool-{i}", "type": "function",
                              "function": {"name": n, "arguments": a if isinstance(a, str) else json.dumps(a)}} for i, (n, a) in enumerate(tool_calls)]
        finish = "tool_calls" if finish == "stop" else finish
    return 200, {"id": "chatcmpl-1", "object": "chat.completion", "choices": [{"index": 0, "message": msg, "finish_reason": finish}],
                 "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens, "total_tokens": prompt_tokens + completion_tokens,
                           "prompt_tokens_details": {"cached_tokens": 40}}}


class VllmTestCase(unittest.TestCase):
    def setUp(self) -> None:
        for k in ENV:
            os.environ.pop(k, None)
        self.server = StubServer()
        os.environ["VLLM_BASE_URL"] = self.server.url                                  # without /v1: the backend adds it
        self.config = default_config("Mock goal")

    def tearDown(self) -> None:
        self.server.close()
        for k in ENV:
            os.environ.pop(k, None)
        os.environ["VLLM_BASE_URL"] = "http://127.0.0.1:1"                             # nothing listening: empty the catalog for other tests
        models.refresh_vllm(force=True)
        os.environ.pop("VLLM_BASE_URL")


class DiscoveryTests(VllmTestCase):
    def test_models_come_from_the_server_with_their_context(self) -> None:
        os.environ["VLLM_API_KEY"] = "secret"
        cat = models.refresh_vllm(force=True)
        self.assertEqual([(m.id, m.context, m.vendor) for m in cat], [("Qwen/Qwen3-32B", 40960, "vllm"), ("llama-4", 32768, "vllm")])
        self.assertEqual(self.server.headers[-1].get("Authorization"), "Bearer secret")
        self.assertIn("vllm", models.available_backends())
        self.assertEqual(models.backend_for_model("Qwen/Qwen3-32B", {"vllm": "x"}), "vllm")
        self.assertEqual(models.estimate_cost_usd("Qwen/Qwen3-32B", 1_000_000), 0.0)
        self.assertEqual(models.cost_usd("Qwen/Qwen3-32B", 1000, 1000), 0.0)
        self.assertIn("runs locally", models.catalog_text("vllm", {"vllm": "x"}))

    def test_forced_models_and_url_forms(self) -> None:
        os.environ["HUNTUN_VLLM_MODELS"] = "my-model@131072,other"
        os.environ["HUNTUN_VLLM_CONTEXT"] = "16384"
        self.assertEqual([(m.id, m.context) for m in models.refresh_vllm(force=True)], [("my-model", 131072), ("other", 16384)])
        self.assertEqual(models.vllm_base_url(), self.server.url + "/v1")
        os.environ["VLLM_BASE_URL"] = "http://gpu-box:8000/v1/"
        self.assertEqual(models.vllm_base_url(), "http://gpu-box:8000/v1")
        b = make_backend("vllm", self.config)
        self.assertIsInstance(b, VllmBackend)
        self.assertEqual(b._default_model(), "my-model")

    def test_no_server_means_no_backend(self) -> None:
        os.environ["VLLM_BASE_URL"] = "http://127.0.0.1:1"
        self.assertEqual(models.refresh_vllm(force=True), [])
        self.assertNotIn("vllm", models.available_backends())
        with self.assertRaises(RuntimeError):
            make_backend("vllm", self.config)._default_model()


class CycleTests(VllmTestCase):
    def setUp(self) -> None:
        super().setUp()
        os.environ["HUNTUN_VLLM_MODELS"] = "qwen3@32768"
        models.refresh_vllm(force=True)
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self.tmp.name)
        self.config.backend = "vllm"
        self.team = [master_spec(), AgentSpec("dev-1", "backend", "Dev", "dev")]
        save_config(self.ws, self.config)
        save_team(self.ws, self.team)
        asyncio.run(ensure_repo(self.ws))
        self.store = Store(db_path(self.ws))
        self.store.create_thread("master", "Task", "@dev-1 do it")

    def tearDown(self) -> None:
        self.store.close()
        self.tmp.cleanup()
        super().tearDown()

    def ctx(self) -> ToolContext:
        return ToolContext(agent=self.team[1], team=lambda: self.team, workspace=self.ws, store=self.store,
                           memory=AgentMemory(agents_dir(self.ws), "dev-1"), config=self.config, cycle=CycleState())

    def run_cycle(self, ctx: ToolContext, should_stop: Any = lambda: False) -> Any:
        return asyncio.run(make_backend("vllm", self.config).run_cycle(ctx=ctx, system="SYS", prompt="go", model="", effort="high", should_stop=should_stop, log=lambda s: None))

    def test_tool_loop_runs_huntun_tools_and_finishes(self) -> None:
        self.server.script = [
            completion(None, [("write_file", {"path": "hello.py", "content": "print('hi')\n"}), ("no_such_tool", {})], reasoning="plan: write the file"),
            completion("<think>now finish</think>Done.", [("finish_cycle", {"summary": "wrote hello.py", "next_task": "tests"})]),
        ]
        ctx = self.ctx()
        res = self.run_cycle(ctx)
        self.assertEqual((res.outcome, res.summary, res.next_task), ("finished", "wrote hello.py", "tests"))
        self.assertEqual((self.ws / "hello.py").read_text(), "print('hi')\n")
        first, second = self.server.requests
        self.assertEqual(first["model"], "qwen3")
        self.assertEqual(first["messages"][0], {"role": "system", "content": "SYS"})
        self.assertEqual(first["messages"][1], {"role": "user", "content": "go"})
        self.assertEqual(first["tool_choice"], "auto")
        self.assertEqual(first["max_tokens"], 8192)                                    # a quarter of the 32k window, below HUNTUN_MAX_TOKENS
        names = {t["function"]["name"] for t in first["tools"]}
        self.assertTrue({"write_file", "run_command", "finish_cycle", "post_comment"} <= names)
        self.assertNotIn("web_search", names)
        self.assertTrue(all(t["type"] == "function" and "parameters" in t["function"] for t in first["tools"]))
        assistant, tool1, tool2 = second["messages"][2:5]
        self.assertEqual(assistant["role"], "assistant")
        self.assertEqual([c["id"] for c in assistant["tool_calls"]], ["chatcmpl-tool-0", "chatcmpl-tool-1"])
        self.assertEqual((tool1["role"], tool1["tool_call_id"]), ("tool", "chatcmpl-tool-0"))
        self.assertEqual(tool2["tool_call_id"], "chatcmpl-tool-1")
        self.assertIn("unknown tool", tool2["content"])
        self.assertEqual(res.usage["input"], 120)                                       # prompt tokens minus the cached ones, over two calls
        self.assertEqual(res.usage["cache_read"], 80)
        self.assertEqual(res.usage["output"], 20)
        self.assertEqual(res.usage["cost_usd"], 0.0)
        self.assertIsNone(ctx.memory.load_transcript())
        kinds = [json.loads(ln)["kind"] for ln in (agents_dir(self.ws) / "dev-1" / "activity.jsonl").read_text().splitlines()]
        self.assertIn("thinking", kinds)

    def test_text_only_reply_ends_the_cycle_and_think_tags_are_split_off(self) -> None:
        self.server.script = [completion("<think>hmm</think>All set, nothing else to do.")]
        res = self.run_cycle(self.ctx())
        self.assertEqual((res.outcome, res.summary), ("finished", "All set, nothing else to do."))

    def test_broken_arguments_are_reported_back_and_endless_bad_calls_stop(self) -> None:
        self.server.script = [completion(None, [("write_file", "{not json")]), completion("ok, stopping.")]
        res = self.run_cycle(self.ctx())
        self.assertEqual(res.outcome, "finished")
        self.assertIn("not valid JSON", self.server.requests[1]["messages"][-1]["content"])
        self.server.requests.clear()
        self.server.script = [completion(None, [("write_file", "{bad")]) for _ in range(6)]
        res = self.run_cycle(self.ctx())
        self.assertEqual(res.outcome, "error")
        self.assertIn("tool-call-parser", res.error)
        self.assertEqual(len(self.server.requests), 5)

    def test_rate_limit_hands_over_to_the_watchdog_and_the_cycle_resumes(self) -> None:
        self.server.script = [completion(None, [("list_files", {"depth": 1})]), (429, {"error": {"message": "Too many requests"}})]
        ctx = self.ctx()
        res = self.run_cycle(ctx)
        self.assertEqual(res.outcome, "limit")
        self.assertAlmostEqual(res.resets_at or 0, time.time() + 30, delta=5)
        saved = ctx.memory.load_transcript()
        self.assertTrue(saved and is_openai_transcript(saved) and saved[-1]["role"] == "tool")
        self.server.script = [completion(None, [("finish_cycle", {"summary": "resumed", "next_task": ""})])]
        res = self.run_cycle(self.ctx())
        self.assertEqual((res.outcome, res.summary), ("finished", "resumed"))
        self.assertEqual(self.server.requests[-1]["messages"][1:], [{k: v for k, v in m.items()} for m in saved])

    def test_tool_calling_off_gives_a_clear_error(self) -> None:
        self.server.script = [(400, {"object": "error", "message": '"auto" tool choice requires --enable-auto-tool-choice and --tool-call-parser to be set', "code": 400})]
        res = self.run_cycle(self.ctx())
        self.assertEqual(res.outcome, "error")
        self.assertIn("Restart it with `--enable-auto-tool-choice", res.error)

    def test_context_overflow_drops_max_tokens_then_restarts_the_conversation(self) -> None:
        too_long = (400, {"error": {"message": "This model's maximum context length is 32768 tokens. However, you requested 40000 tokens.", "code": 400}})
        self.server.script = [completion(None, [("list_files", {})]), too_long, too_long, completion("fresh start, done.")]
        res = self.run_cycle(self.ctx())
        self.assertEqual((res.outcome, res.summary), ("finished", "fresh start, done."))
        _, with_cap, without_cap, restarted = self.server.requests
        self.assertIn("max_tokens", with_cap)
        self.assertNotIn("max_tokens", without_cap)
        self.assertEqual(len(restarted["messages"]), 2)                                # system + the task, with a note
        self.assertIn("overflowed the context window", restarted["messages"][1]["content"])

    def test_a_full_context_is_compacted_into_a_handoff_summary(self) -> None:
        self.server.script = [completion(None, [("list_files", {})], prompt_tokens=25_000), completion("SUMMARY: listed files"),
                              completion(None, [("finish_cycle", {"summary": "after compaction", "next_task": ""})])]
        ctx = self.ctx()
        res = self.run_cycle(ctx)
        self.assertEqual((res.outcome, res.summary), ("finished", "after compaction"))
        _, ask, after = self.server.requests
        self.assertEqual(ask["tool_choice"], "none")
        self.assertIn("handoff summary", ask["messages"][-1]["content"])
        self.assertEqual(len(after["messages"]), 2)
        self.assertIn("SUMMARY: listed files", after["messages"][1]["content"])
        self.assertEqual(ctx.memory.state.compactions, 1)

    def test_transient_errors_are_retried(self) -> None:
        self.server.script = [(503, {"message": "busy"}), completion("done.")]
        b = make_backend("vllm", self.config)
        waits: list[float] = []

        async def no_sleep(s: float) -> None:
            waits.append(s)

        orig = asyncio.sleep
        asyncio.sleep = no_sleep                                                            # type: ignore[assignment]
        try:
            res = asyncio.run(b.run_cycle(ctx=self.ctx(), system="S", prompt="go", model="", effort="high", should_stop=lambda: False, log=lambda s: None))
        finally:
            asyncio.sleep = orig                                                            # type: ignore[assignment]
        self.assertEqual(res.outcome, "finished")
        self.assertEqual(waits, [10])

    def test_foreign_transcripts_are_not_replayed(self) -> None:
        ctx = self.ctx()
        ctx.memory.save_transcript([{"role": "user", "content": "old"}, {"role": "assistant", "content": [{"type": "tool_use", "id": "t", "name": "x", "input": {}}]}])
        self.server.script = [completion("done.")]
        self.run_cycle(ctx)
        self.assertEqual(self.server.requests[0]["messages"][1:], [{"role": "user", "content": "go"}])
        self.assertFalse(is_openai_transcript([{"role": "assistant", "content": [{"type": "text", "text": "x"}]}]))
        self.assertTrue(is_openai_transcript([{"role": "user", "content": "x"}, {"role": "tool", "tool_call_id": "a", "content": "r"}]))

    def test_api_backend_ignores_a_vllm_transcript(self) -> None:
        os.environ["DEEPSEEK_API_KEY"] = "sk-test"
        try:
            b = make_backend("deepseek", self.config)
        finally:
            os.environ.pop("DEEPSEEK_API_KEY")
        self.assertIsInstance(b, ApiBackend)
        ctx = self.ctx()
        ctx.memory.save_transcript([{"role": "user", "content": "old"}, {"role": "assistant", "content": None, "tool_calls": []}, {"role": "tool", "tool_call_id": "a", "content": "r"}])
        seen: list[Any] = []

        class Stop(Exception):
            pass

        async def call(params: dict[str, Any], use_fallbacks: bool) -> Any:
            seen.append(params["messages"])
            raise Stop

        b._call = call                                                                      # type: ignore[method-assign]
        with self.assertRaises(Stop):
            asyncio.run(b.run_cycle(ctx=ctx, system="S", prompt="go", model="deepseek-v4-pro", effort="high", should_stop=lambda: False, log=lambda s: None))
        self.assertEqual(seen[0], [{"role": "user", "content": "go"}])


class StructuredTests(VllmTestCase):
    def setUp(self) -> None:
        super().setUp()
        os.environ["HUNTUN_VLLM_MODELS"] = "qwen3@32768"
        models.refresh_vllm(force=True)

    def ask(self) -> dict[str, Any]:
        return asyncio.run(make_backend("vllm", self.config).structured(prompt="plan", tool_name="propose_team", description="d",
                                                                          schema={"type": "object"}, model="", effort="high"))

    def test_named_tool_choice(self) -> None:
        self.server.script = [completion(None, [("propose_team", {"agents": [], "rationale": "small"})])]
        self.assertEqual(self.ask()["rationale"], "small")
        req = self.server.requests[0]
        self.assertEqual(req["tool_choice"], {"type": "function", "function": {"name": "propose_team"}})
        self.assertEqual(req["tools"][0]["function"]["name"], "propose_team")

    def test_json_in_text_and_a_rejected_tool_choice(self) -> None:
        self.server.script = [(400, {"error": {"message": "tool_choice is not supported"}}), completion('Here: {"agents": [], "rationale": "x"}')]
        self.assertEqual(self.ask()["rationale"], "x")
        self.assertIn("tool_choice", self.server.requests[0])
        self.assertNotIn("tool_choice", self.server.requests[1])

    def test_probe(self) -> None:
        self.server.script = [(429, {"error": {"message": "rate limit"}}), completion("pong")]
        b = make_backend("vllm", self.config)
        self.assertFalse(asyncio.run(b.probe()))
        self.assertTrue(asyncio.run(b.probe()))


class ModelChoiceTests(VllmTestCase):
    """Local models are discovered at run time, so the model enums the master sees must include them."""

    def setUp(self) -> None:
        super().setUp()
        os.environ["HUNTUN_VLLM_MODELS"] = "qwen3@32768"
        models.refresh_vllm(force=True)
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self.tmp.name)
        self.config.backend = "vllm"
        self.store = Store(db_path(self.ws))

    def tearDown(self) -> None:
        self.store.close()
        self.tmp.cleanup()
        super().tearDown()

    def test_master_tools_offer_local_models(self) -> None:
        from huntun.tools import TOOLS, available_tools, validate

        ctx = ToolContext(agent=master_spec(), team=lambda: [master_spec()], workspace=self.ws, store=self.store,
                          memory=AgentMemory(agents_dir(self.ws), "master"), config=self.config, cycle=CycleState())
        specs = {t.name: t for t in available_tools(ctx, "vllm")}
        for name in ("hire_agent", "set_agent_model"):
            enum = specs[name].input_schema["properties"]["model"]["enum"]
            self.assertIn("qwen3", enum)
            self.assertIn("claude-opus-5-5", enum)
        self.assertIsNone(validate(specs["set_agent_model"].input_schema, {"name": "dev-1", "model": "qwen3", "confirmation_thread_id": 1}))
        static = next(t for t in TOOLS if t.name == "set_agent_model")
        self.assertNotIn("qwen3", static.input_schema["properties"]["model"]["enum"])      # the shared definition is left alone

    def test_team_plan_can_put_agents_on_the_vllm_server(self) -> None:
        from huntun.master import PLAN_SCHEMA, plan_team

        agent = {"name": "team-lead", "role": "team-lead", "title": "Lead", "brief": "b", "model": "qwen3", "effort": "high", "why": "local",
                 "personality": "", "personality_note": "", "estimated_cycles": 2, "tokens_per_cycle": 20000}
        self.server.script = [completion(None, [("propose_team", {"rationale": "r", "agents": [agent], "estimate_notes": "n"})])]
        _, agents = asyncio.run(plan_team(make_backend("vllm", self.config), self.config))
        lead = next(a for a in agents if a.role == "team-lead")
        self.assertEqual((lead.model, lead.backend), ("qwen3", "vllm"))
        sent = self.server.requests[0]["tools"][0]["function"]["parameters"]
        self.assertIn("qwen3", sent["properties"]["agents"]["items"]["properties"]["model"]["enum"])   # guided decoding may pick it
        self.assertNotIn("qwen3", PLAN_SCHEMA["properties"]["agents"]["items"]["properties"]["model"]["enum"])

    def test_only_the_anthropic_api_promises_web_tools(self) -> None:
        from huntun.roles import build_system_prompt

        dev = AgentSpec("dev-1", "backend", "Dev", "dev")
        for b in ("deepseek", "ollama", "vllm"):
            text = build_system_prompt(dev, self.config, [dev], b)
            self.assertNotIn("web_search", text)
            self.assertIn("no web search", text)
        self.assertIn("web_search", build_system_prompt(dev, self.config, [dev], "api"))


class HelperTests(unittest.TestCase):
    def test_split_think(self) -> None:
        self.assertEqual(_split_think("<think>a</think>b"), ("a", "b"))
        self.assertEqual(_split_think("a</think>b"), ("a", "b"))                           # the template opened <think> in the prompt
        self.assertEqual(_split_think("plain"), ("", "plain"))


if __name__ == "__main__":
    unittest.main()
