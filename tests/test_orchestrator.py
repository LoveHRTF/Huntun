"""End-to-end orchestration with a scripted fake model: staggered start, mention wake-ups,
commits posting threads, mid-cycle pause / resume, and the API backend loop."""
from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from huntun.backends.api import ApiBackend
from huntun.config import agents_dir, db_path, default_config, save_config, save_team
from huntun.gitops import ensure_repo, recent_log
from huntun.master import master_spec
from huntun.memory import AgentMemory
from huntun.orchestrator import Orchestrator
from huntun.store import Store
from huntun.tools import ToolContext
from huntun.types import AgentSpec, CycleState

USAGE = SimpleNamespace(input_tokens=10, output_tokens=5, cache_read_input_tokens=0, cache_creation_input_tokens=0)


def block(**kw: Any) -> SimpleNamespace:
    ns = SimpleNamespace(**kw)
    ns.model_dump = lambda exclude_none=False: dict(kw)  # what the real SDK block offers
    return ns


def tool_use(tid: str, name: str, inp: dict[str, Any]) -> SimpleNamespace:
    return block(type="tool_use", id=tid, name=name, input=inp)


def message(content: list[Any], stop: str = "tool_use") -> SimpleNamespace:
    return SimpleNamespace(content=content, stop_reason=stop, usage=USAGE, model="claude-sonnet-5")


class FakeStream:
    def __init__(self, fn, params):
        self.fn, self.params = fn, params

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get_final_message(self):
        await asyncio.sleep(0.01)
        return self.fn(self.params)


def fake_client(script):
    """Mimics AsyncAnthropic.beta.messages.stream(**params) -> async ctx manager with get_final_message()."""
    return SimpleNamespace(beta=SimpleNamespace(messages=SimpleNamespace(stream=lambda **p: FakeStream(script, p))))


def n_assistant(messages: list[dict[str, Any]]) -> int:
    return sum(1 for m in messages if m["role"] == "assistant")


class ApiLoopTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self.tmp.name)
        self.config = default_config("Mock goal")
        self.team = [master_spec(), AgentSpec("solo", "backend", "B", "b")]
        save_config(self.ws, self.config)
        save_team(self.ws, self.team)
        asyncio.run(ensure_repo(self.ws))
        self.store = Store(db_path(self.ws))

    def tearDown(self) -> None:
        self.store.close()
        self.tmp.cleanup()

    def test_pause_mid_cycle_then_resume_from_transcript(self) -> None:
        memory = AgentMemory(agents_dir(self.ws), "solo")
        cycle = CycleState()
        ctx = ToolContext(agent=self.team[1], team=lambda: self.team, workspace=self.ws, store=self.store, memory=memory, config=self.config, cycle=cycle)

        def script(p):
            if n_assistant(p["messages"]) == 0:
                return message([tool_use("s1", "write_file", {"path": "a.txt", "content": "1"}), tool_use("s2", "write_file", {"path": "b.txt", "content": "2"})])
            return message([tool_use("s3", "finish_cycle", {"summary": "ok", "next_task": "next"})])

        backend = ApiBackend.__new__(ApiBackend)
        backend.config = self.config
        backend.client = fake_client(script)
        calls = {"n": 0}

        def should_stop_after_first_tool() -> bool:
            calls["n"] += 1
            # the loop checks before the API call, then before each tool: stop before the 2nd tool
            return calls["n"] >= 3

        def run(ss):
            return asyncio.run(backend.run_cycle(ctx=ctx, system="sys", prompt="go", model="m", effort="high", should_stop=ss, log=lambda s: None))

        res = run(should_stop_after_first_tool)
        self.assertEqual(res.outcome, "paused")
        self.assertEqual(len(memory.load_transcript() or []), 1, "assistant turn rolled back so the transcript stays valid")
        self.assertTrue((self.ws / "a.txt").exists())
        res = run(lambda: False)
        self.assertEqual((res.outcome, res.summary, res.next_task), ("finished", "ok", "next"))
        self.assertIsNone(memory.load_transcript())
        self.assertEqual(res.usage["input"], 20)
        self.assertGreater(res.usage["cost_usd"], 0)
        self.assertEqual((memory.state.context_tokens, memory.state.context_limit), (15, 200_000))  # unknown model id falls back to 200k

    def test_pause_cancels_a_waiting_provider_request_and_resume_keeps_history(self) -> None:
        self._assert_network_pause('request')

    def test_pause_interrupts_connection_retry_and_reports_destination(self) -> None:
        self._assert_network_pause('retry')

    def _assert_network_pause(self, phase: str) -> None:
        import anthropic

        memory = AgentMemory(agents_dir(self.ws), 'solo')
        saved = [{'role':'user', 'content':'Preserve the original assignment.'}]
        memory.save_transcript(saved)
        ctx = ToolContext(agent=self.team[1], team=lambda:self.team, workspace=self.ws,
                          store=self.store, memory=memory, config=self.config, cycle=CycleState())
        backend = ApiBackend.__new__(ApiBackend)
        backend.config = self.config
        logs = []

        async def exercise() -> None:
            stop, started, cleaned = asyncio.Event(), asyncio.Event(), asyncio.Event()

            async def waiting_call(*_args):
                started.set()
                try:
                    if phase == 'retry':
                        request = SimpleNamespace(url='http://user:secret@gpu.local:8080/v1/messages?api_key=secret')
                        raise anthropic.APIConnectionError(request=request) from ConnectionRefusedError(61, 'Connection refused')
                    await asyncio.Future()
                finally:
                    cleaned.set()

            backend._call = waiting_call
            run = asyncio.create_task(backend.run_cycle(ctx=ctx, system='s', prompt='', model='m',
                                      effort='high', should_stop=stop.is_set, log=logs.append))
            await started.wait()
            await asyncio.sleep(.03)
            stop.set()
            result = await asyncio.wait_for(run, timeout=1)
            self.assertEqual(result.outcome, 'paused')
            self.assertTrue(cleaned.is_set(), 'the interrupted request releases its resources')
            self.assertEqual(memory.load_transcript(), saved)
            if phase == 'retry':
                self.assertIn('http://gpu.local:8080/v1/messages', logs[0])
                self.assertIn('Connection refused', logs[0])
                self.assertIn('retry 1/6 in 10s', logs[0])
                self.assertNotIn('secret', logs[0])
                self.assertEqual(memory.last_activity['kind'], 'retry')
            stop.clear()

            async def resumed_call(params, *_args):
                self.assertEqual(params['messages'], saved)
                return message([block(type='text', text='Recovered.')], 'end_turn')

            backend._call = resumed_call
            result = await backend.run_cycle(ctx=ctx, system='s', prompt='', model='m',
                                            effort='high', should_stop=stop.is_set, log=logs.append)
            self.assertEqual((result.outcome,result.summary), ('finished','Recovered.'))
            self.assertIsNone(memory.load_transcript())

        asyncio.run(exercise())

    def test_end_turn_without_finish_cycle_uses_text(self) -> None:
        memory = AgentMemory(agents_dir(self.ws), "solo")
        ctx = ToolContext(agent=self.team[1], team=lambda: self.team, workspace=self.ws, store=self.store, memory=memory, config=self.config, cycle=CycleState())
        backend = ApiBackend.__new__(ApiBackend)
        backend.config = self.config
        backend.client = fake_client(lambda p: message([block(type="text", text="All done.")], "end_turn"))
        res = asyncio.run(backend.run_cycle(ctx=ctx, system="s", prompt="go", model="m", effort="high", should_stop=lambda: False, log=lambda s: None))
        self.assertEqual((res.outcome, res.summary), ("finished", "All done."))


    def test_fallback_model_mixed_cache_ttls_and_compaction_are_accounted(self) -> None:
        ctx = ToolContext(agent=self.team[1], team=lambda: self.team, workspace=self.ws, store=self.store,
                          memory=AgentMemory(agents_dir(self.ws), 'solo'), config=self.config, cycle=CycleState())
        backend = ApiBackend.__new__(ApiBackend)
        backend.config = self.config
        calls = []

        def script(params):
            calls.append(params)
            # Force native compaction after this turn's tool call. The response
            # model overrides the requested Opus model for billing.
            summary = not params.get('tools')
            content = [block(type='text', text='handoff')] if summary else [tool_use('n', 'git_status', {})] if len(calls) == 1 else [block(type='text', text='done')]
            response = message(content, 'end_turn' if summary or len(calls) > 2 else 'tool_use')
            response.model = 'claude-sonnet-5'
            response.usage = SimpleNamespace(input_tokens=10, output_tokens=20,
                cache_read_input_tokens=950_000 if len(calls) == 1 else 30,
                cache_creation_input_tokens=100, cache_creation=SimpleNamespace(ephemeral_1h_input_tokens=40))
            return response

        backend.client = fake_client(script)
        result = asyncio.run(backend.run_cycle(ctx=ctx, system='s', prompt='go', model='claude-opus-5-5',
                                              effort='high', should_stop=lambda: False, log=lambda _:None))
        self.assertEqual(result.outcome, 'finished', result.error)
        self.assertEqual(len(calls),3, 'one tool request, one summary, one final request')
        self.assertEqual(result.usage['input'],30)
        self.assertEqual(result.usage['cache_write'],300)
        self.assertEqual(result.usage['output'],60)
        self.assertEqual(result.usage['cache_read'],950_060)
        self.assertEqual(result.cost_status,'estimated')
        self.assertAlmostEqual(result.usage['cost_usd'],.191602,places=6)


class OrchestratorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self.tmp.name)
        self.config = default_config("Mock goal")
        self.config.idle_interval_sec = 1
        self.config.lead_idle_interval_sec = 1
        self.config.max_cycles_per_agent = 1
        self.config.backend = "api"
        self.team = [master_spec(), AgentSpec("team-lead", "team-lead", "Lead", "lead"), AgentSpec("backend-1", "backend", "Backend", "api")]
        save_config(self.ws, self.config)
        save_team(self.ws, self.team)
        asyncio.run(ensure_repo(self.ws))

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_a_resumed_cycle_is_working_again_at_its_first_thought(self) -> None:
        """"resuming" lasts while the model reads the saved conversation back in; the office has those agents on a coffee break."""
        import os

        from huntun.orchestrator import AgentRuntime
        from huntun.types import CycleResult

        os.environ["ANTHROPIC_API_KEY"] = "test-key"
        orch = Orchestrator(self.ws)
        spec = next(a for a in orch.team if a.name == "backend-1")
        seen: list[str] = []

        class Fake:
            async def run_cycle(self, *, ctx, **kw):  # noqa: ANN001, ANN003
                status = lambda: orch.store.agent_statuses()["backend-1"]["status"]  # noqa: E731
                seen.append(status())
                ctx.memory.activity("cycle", "compacted")                            # not the model yet
                seen.append(status())
                ctx.memory.activity("retry", "Connection refused; retry in 10s")
                seen.append(status())
                ctx.memory.activity("thinking", "where was I")
                seen.append(status())
                ctx.memory.activity("tool", "git_status")
                orch.store.set_agent_status('backend-1','error','stale competing writer error')
                ctx.memory.activity('result','Actual tool output')
                seen.append(status())
                ctx.memory.activity('text','Produced the implementation')
                seen.append(status())
                return CycleResult("finished", "ok", "next", None, {}, None)

        orch.backend_for = lambda _spec: Fake()  # type: ignore[method-assign]
        rt = AgentRuntime(orch, spec)
        from huntun.gitops import ensure_worktree
        rt.memory.state.worktree_path = str(asyncio.run(ensure_worktree(self.ws, spec.name)))
        rt.memory.save_transcript([{"role": "user", "content": "go"}])
        asyncio.run(rt._run_one("resume"))
        self.assertEqual(seen, ["resuming", "resuming", "retrying", "working", "working", "working"])
        self.assertIsNone(rt.memory.on_activity, "the watch ends with the cycle")
        orch.store.close()

    def test_team_runs_end_to_end(self) -> None:
        import os

        os.environ["ANTHROPIC_API_KEY"] = "test-key"

        def script(p):
            agent = p["system"][0]["text"].split("You are @")[1].split(",")[0]
            msgs = p["messages"]
            n = n_assistant(msgs)
            if agent == "master":
                if n == 0:
                    return message([block(type="text", text="Kicking off."), tool_use("m1", "post_thread", {"title": "Kickoff", "body": "Goal restated. @team-lead write the plan. @all read this."})])
                return message([tool_use("m2", "update_notes", {"notes": "kickoff posted"}), tool_use("m3", "finish_cycle", {"summary": "Posted kickoff", "next_task": "Review later"})])
            if agent == "team-lead":
                if n == 0:
                    return message([tool_use("l1", "write_file", {"path": "docs/ARCHITECTURE.md", "content": "# Arch\n"}),
                                    tool_use("l2", "git_commit", {"message": "docs: architecture", "summary_title": "Architecture", "summary_body": "Initial architecture. @backend-1 please scaffold the API."})])
                return message([tool_use("l3", "finish_cycle", {"summary": "Wrote architecture", "next_task": "review"})])
            if n == 0:
                assert "scaffold the API" in msgs[0]["content"], "mention was delivered in the prompt"
                return message([tool_use("b1", "write_file", {"path": "api/main.py", "content": "app = 1\n"}),
                                tool_use("b2", "git_commit", {"message": "feat: scaffold api", "summary_title": "API scaffold", "summary_body": "Scaffolded."})])
            return message([tool_use("b3", "finish_cycle", {"summary": "Scaffolded API", "next_task": "endpoints"})])

        async def run() -> None:
            orch = Orchestrator(self.ws)
            orch.backend.client = fake_client(script)  # type: ignore[attr-defined]
            orch.store.set_running(True, "test")
            await orch.start()
            for _ in range(300):
                if all(orch.runtimes[n].memory.state.cycles >= 1 for n in ("master", "team-lead", "backend-1")):
                    break
                await asyncio.sleep(0.1)
            for n in ("master", "team-lead", "backend-1"):
                self.assertGreaterEqual(orch.runtimes[n].memory.state.cycles, 1, f"{n} completed a cycle")
            log = await recent_log(self.ws)
            self.assertIn("docs: architecture", log)
            self.assertIn("feat: scaffold api", log)
            roots = [rt.memory.state.worktree_path for rt in orch.runtimes.values()]
            self.assertEqual(len(set(roots)), 3)
            self.assertTrue(all(Path(root) != self.ws.resolve() for root in roots))
            self.assertTrue((self.ws / "api/main.py").exists(), "task completion integrated the worker's private commit")
            titles = [t["title"] for t in orch.store.list_threads()]
            self.assertIn("Kickoff", titles)
            self.assertTrue(any("API scaffold" in t for t in titles), titles)
            self.assertEqual(orch.runtimes["master"].memory.state.cycles, 1, "no self-wake loop from @all")
            lead = orch.runtimes["team-lead"]
            self.assertEqual({k: v for k, v in lead.memory.state.usage_totals.items() if k != "cost_usd"}, {"input": 20.0, "output": 10.0, "cache_read": 0.0, "cache_write": 0.0})
            self.assertEqual(lead.info()["context_limit"], 1_000_000)
            kinds = [e["kind"] for e in lead.memory.read_activity()[0]]
            self.assertEqual(kinds[:4], ["cycle", "prompt", "waiting", "tool"])
            self.assertIn("result", kinds)
            self.assertEqual(kinds[-1], "cycle")
            self.assertEqual(lead.info()["cycles"], 1)
            orch.store.set_running(False, "test")
            await asyncio.sleep(0.3)
            statuses = orch.store.agent_statuses()
            self.assertTrue(all(s["status"] in ("paused", "idle", "done (cycle cap reached)") for s in statuses.values()), statuses)
            await orch.stop()

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
