"""Kimi backend against a fake `kimi` executable that speaks the print-mode JSONL protocol and calls the MCP bridge like the real CLI."""
from __future__ import annotations

import asyncio
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path

from huntun.backends.kimi import KimiBackend
from huntun.config import agents_dir, db_path, default_config, save_config, save_team
from huntun.gitops import ensure_repo
from huntun.master import master_spec
from huntun.memory import AgentMemory
from huntun.store import Store
from huntun.tools import ToolContext
from huntun.types import AgentSpec, CycleState

FAKE_KIMI = r'''#!__PY__
"""Fake kimi: parses the flags Huntun passes, reads the project's .kimi-code/mcp.json, calls the huntun MCP server, and emits JSONL."""
import asyncio, json, sys, os
args = sys.argv[1:]
if args == ["--version"]:
    print("2.1.0"); sys.exit(0)
prompt = args[args.index("-p") + 1]
resumed = "--session" in args
def emit(ev): print(json.dumps(ev), flush=True)
emit({"role": "meta", "type": "system.version", "version": "2.1.0"})
if os.environ.get("FAKE_KIMI_LIMIT"):
    sys.stderr.write("error: failed to run prompt: You've hit your usage limit. Try again at 6pm.\n"); sys.exit(1)
if "JSON schema" in prompt:
    emit({"role": "assistant", "content": "Here you go:\n```json\n" + json.dumps({"rationale": "tiny", "agents": [{"name": "team-lead", "role": "team-lead", "title": "Lead", "brief": "lead", "model": "kimi-k3", "effort": "high"}]}) + "\n```"})
    emit({"role": "meta", "type": "session.resume_hint", "session_id": "sess-json", "command": "kimi -r sess-json", "content": "To resume"}); sys.exit(0)
if "single word OK" in prompt:
    emit({"role": "assistant", "content": "OK"}); sys.exit(0)
url = json.load(open(os.path.join(os.getcwd(), ".kimi-code", "mcp.json")))["mcpServers"]["huntun"]["url"]
async def main():
    from mcp.client.streamable_http import streamable_http_client
    from mcp.client.session import ClientSession
    async with streamable_http_client(url) as (read, write, *_):
        async with ClientSession(read, write) as s:
            await s.initialize()
            names = [t.name for t in (await s.list_tools()).tools]
            emit({"role": "assistant", "content": "tools: " + ",".join(sorted(names))})
            if not resumed:
                await s.call_tool("post_comment", {"thread_id": 1, "body": "On it."})
                emit({"role": "assistant", "tool_calls": [{"type": "function", "id": "c1", "function": {"name": "mcp__huntun__post_comment", "arguments": json.dumps({"thread_id": 1})}}]})
                emit({"role": "tool", "tool_call_id": "c1", "content": "posted"})
                open("hello.py", "w").write("print('hi')\n")
                emit({"role": "assistant", "tool_calls": [{"type": "function", "id": "c2", "function": {"name": "WriteFile", "arguments": json.dumps({"path": os.path.abspath("hello.py"), "content": "print('hi')"})}}]})
                emit({"role": "tool", "tool_call_id": "c2", "content": "ok"})
                r = await s.call_tool("git_commit", {"message": "feat: hello", "summary_title": "hello", "summary_body": "added hello.py, ran it"})
                emit({"role": "assistant", "content": r.content[0].text[:120]})
                if os.environ.get("FAKE_KIMI_PAUSE"):
                    emit({"role": "meta", "type": "session.resume_hint", "session_id": "sess-123", "command": "kimi -r sess-123", "content": "To resume"})
                    await asyncio.sleep(30)  # linger so the orchestrator can interrupt us
            await s.call_tool("finish_cycle", {"summary": "done via " + ("resume" if resumed else "print"), "next_task": "more"})
    emit({"role": "meta", "type": "session.resume_hint", "session_id": "sess-123", "command": "kimi -r sess-123", "content": "To resume"})
asyncio.run(main())
'''


class KimiBackendTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.ws = root / "ws"
        self.ws.mkdir()
        fake = root / "kimi"
        fake.write_text(FAKE_KIMI.replace("__PY__", sys.executable))
        fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
        os.environ["HUNTUN_KIMI_BIN"] = str(fake)
        os.environ["HUNTUN_KIMI_MODELS"] = "kimi-k3,kimi-k2.7-code"
        os.environ.pop("FAKE_KIMI_LIMIT", None)
        os.environ.pop("FAKE_KIMI_PAUSE", None)
        self.config = default_config("Mock goal")
        self.config.backend = "kimi"
        self.team = [master_spec(), AgentSpec("dev-1", "backend", "Dev", "dev")]
        save_config(self.ws, self.config)
        save_team(self.ws, self.team)
        asyncio.run(ensure_repo(self.ws))
        self.store = Store(db_path(self.ws))
        self.store.create_thread("master", "Task", "@dev-1 do it")

    def tearDown(self) -> None:
        self.store.close()
        for k in ("HUNTUN_KIMI_BIN", "HUNTUN_KIMI_MODELS", "FAKE_KIMI_LIMIT", "FAKE_KIMI_PAUSE"):
            os.environ.pop(k, None)
        self.tmp.cleanup()

    def ctx(self, memory: AgentMemory | None = None) -> ToolContext:
        return ToolContext(agent=self.team[1], team=lambda: self.team, workspace=self.ws, store=self.store,
                           memory=memory or AgentMemory(agents_dir(self.ws), "dev-1"), config=self.config, cycle=CycleState())

    def run_cycle(self, ctx: ToolContext, should_stop=lambda: False):
        return asyncio.run(KimiBackend(self.config).run_cycle(ctx=ctx, system="SYS", prompt="go", model="kimi-k3", effort="high", should_stop=should_stop, log=lambda m: None))

    def test_cycle_uses_mcp_bridge_and_records_everything(self) -> None:
        ctx = self.ctx()
        res = self.run_cycle(ctx)
        self.assertEqual((res.outcome, res.summary, res.next_task), ("finished", "done via print", "more"), res.error)
        st = ctx.memory.state
        self.assertEqual((st.session_id, st.session_cycles, st.resume_pending), ("sess-123", 1, False))
        self.assertEqual(self.store.get_comments(1)[0]["body"], "On it.")
        self.assertIn("added hello.py", self.store.get_comments(1)[-1]["body"])          # commit summary landed in the working thread
        kinds = [e["kind"] for e in ctx.memory.read_activity()[0]]
        self.assertTrue({"text", "tool", "result"} <= set(kinds), kinds)
        self.assertFalse((self.ws / ".kimi-code" / "mcp.json").exists(), "the per-cycle MCP declaration is cleaned up")
        self.assertIn(".kimi-code/", (self.ws / ".gitignore").read_text())
        res = self.run_cycle(self.ctx(ctx.memory))                                            # the next cycle resumes the same session
        self.assertEqual((res.outcome, res.summary, ctx.memory.state.session_cycles), ("finished", "done via resume", 2), res.error)

    def test_pause_interrupts_and_resumes_by_session_id(self) -> None:
        os.environ["FAKE_KIMI_PAUSE"] = "1"
        ctx = self.ctx()
        res = self.run_cycle(ctx, lambda: len(self.store.get_comments(1)) >= 2)
        self.assertEqual(res.outcome, "paused", res.error)
        self.assertTrue(ctx.memory.state.resume_pending)
        os.environ.pop("FAKE_KIMI_PAUSE")
        res = self.run_cycle(self.ctx(ctx.memory))
        self.assertEqual((res.outcome, res.summary), ("finished", "done via resume"), res.error)
        self.assertFalse(ctx.memory.state.resume_pending)

    def test_limit_is_reported(self) -> None:
        os.environ["FAKE_KIMI_LIMIT"] = "1"
        res = self.run_cycle(self.ctx())
        self.assertEqual(res.outcome, "limit")
        self.assertIn("usage limit", (res.error or "").lower())

    def test_structured_parses_json_from_the_answer(self) -> None:
        data = asyncio.run(KimiBackend(self.config).structured(prompt="plan", tool_name="propose_team", description="team plan", schema={"type": "object"}, model="kimi-k3", effort="high"))
        self.assertEqual(data["agents"][0]["name"], "team-lead")

    def test_probe(self) -> None:
        self.assertTrue(asyncio.run(KimiBackend(self.config).probe()))


if __name__ == "__main__":
    unittest.main()
