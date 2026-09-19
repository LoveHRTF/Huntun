"""Codex backend against a fake `codex` executable that speaks the JSONL protocol and calls the MCP bridge like the real CLI."""
from __future__ import annotations

import asyncio
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path

from huntun.backends.codex import CodexBackend
from huntun.config import agents_dir, db_path, default_config, save_config, save_team
from huntun.gitops import ensure_repo
from huntun.master import master_spec
from huntun.memory import AgentMemory
from huntun.store import Store
from huntun.tools import ToolContext
from huntun.types import AgentSpec, CycleState

FAKE_CODEX = r'''#!__PY__
"""Fake codex: parses the flags Huntun passes, calls the huntun MCP server over streamable HTTP, and emits JSONL events."""
import asyncio, json, sys, os
args = sys.argv[1:]
mode = "resume" if args[:2] == ["exec", "resume"] else "exec"
cfg = {}
for i, a in enumerate(args):
    if a == "-c" and "=" in args[i + 1]:
        k, v = args[i + 1].split("=", 1); cfg[k] = v.strip('"')
prompt = args[-1]
def emit(ev): print(json.dumps(ev), flush=True)
emit({"type": "thread.started", "thread_id": "thread-123"})
emit({"type": "turn.started"})
if "--output-schema" in args:
    out = args[args.index("-o") + 1]
    open(out, "w").write(json.dumps({"rationale": "tiny", "agents": [{"name": "team-lead", "role": "team-lead", "title": "Lead", "brief": "lead", "model": "gpt-5.3-codex", "effort": "high", "why": "lead"}]}))
    emit({"type": "item.completed", "item": {"id": "i1", "type": "agent_message", "text": "{}"}})
    emit({"type": "turn.completed", "usage": {"input_tokens": 5, "cached_input_tokens": 0, "output_tokens": 2}})
    sys.exit(0)
if os.environ.get("FAKE_CODEX_LIMIT"):
    emit({"type": "item.completed", "item": {"id": "e", "type": "error", "message": "You've hit your usage limit. Try again at 6pm."}})
    sys.exit(1)
url = cfg.get("mcp_servers.huntun.url")
async def main():
    from mcp.client.streamable_http import streamable_http_client
    from mcp.client.session import ClientSession
    async with streamable_http_client(url) as (read, write, *_):
        async with ClientSession(read, write) as s:
            await s.initialize()
            names = [t.name for t in (await s.list_tools()).tools]
            emit({"type": "item.completed", "item": {"id": "t0", "type": "agent_message", "text": "tools: " + ",".join(sorted(names))}})
            if mode == "exec":
                r = await s.call_tool("post_comment", {"thread_id": 1, "body": "On it."})
                emit({"type": "item.completed", "item": {"id": "t1", "type": "mcp_tool_call", "tool": "huntun__post_comment", "arguments": {"thread_id": 1}}})
                open("hello.py", "w").write("print('hi')\n")
                emit({"type": "item.completed", "item": {"id": "f1", "type": "file_change", "changes": [{"path": os.path.abspath("hello.py"), "kind": "add"}]}})
                emit({"type": "item.completed", "item": {"id": "c1", "type": "command_execution", "command": "python3 hello.py", "aggregated_output": "hi\n"}})
                r = await s.call_tool("git_commit", {"message": "feat: hello", "summary_title": "hello", "summary_body": "added hello.py, ran it"})
                emit({"type": "item.completed", "item": {"id": "t2", "type": "agent_message", "text": r.content[0].text[:120]}})
                if os.environ.get("FAKE_CODEX_PAUSE"):
                    emit({"type": "turn.completed", "usage": {"input_tokens": 100, "cached_input_tokens": 50, "output_tokens": 10}})
                    await asyncio.sleep(30)  # linger so the orchestrator can interrupt us
            await s.call_tool("finish_cycle", {"summary": "done via " + mode, "next_task": "more"})
    emit({"type": "turn.completed", "usage": {"input_tokens": 100, "cached_input_tokens": 50, "output_tokens": 10}})
asyncio.run(main())
'''


class CodexBackendTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.ws = root / "ws"
        self.ws.mkdir()
        fake = root / "codex"
        fake.write_text(FAKE_CODEX.replace("__PY__", sys.executable))
        fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
        os.environ["HUNTUN_CODEX_BIN"] = str(fake)
        os.environ.pop("FAKE_CODEX_LIMIT", None)
        os.environ.pop("FAKE_CODEX_PAUSE", None)
        self.config = default_config("Mock goal")
        self.config.backend = "codex"
        self.team = [master_spec(), AgentSpec("dev-1", "backend", "Dev", "dev")]
        save_config(self.ws, self.config)
        save_team(self.ws, self.team)
        asyncio.run(ensure_repo(self.ws))
        self.store = Store(db_path(self.ws))
        self.store.create_thread("master", "Task", "@dev-1 do it")

    def tearDown(self) -> None:
        self.store.close()
        os.environ.pop("HUNTUN_CODEX_BIN", None)
        os.environ.pop("FAKE_CODEX_LIMIT", None)
        os.environ.pop("FAKE_CODEX_PAUSE", None)
        self.tmp.cleanup()

    def ctx(self) -> ToolContext:
        return ToolContext(agent=self.team[1], team=lambda: self.team, workspace=self.ws, store=self.store,
                           memory=AgentMemory(agents_dir(self.ws), "dev-1"), config=self.config, cycle=CycleState())

    def run_cycle(self, ctx: ToolContext, should_stop=lambda: False):
        return asyncio.run(CodexBackend(self.config).run_cycle(ctx=ctx, system="SYS", prompt="go", model="gpt-5.3-codex", effort="medium", should_stop=should_stop, log=lambda s: None))

    def test_cycle_uses_mcp_bridge_and_records_everything(self) -> None:
        ctx = self.ctx()
        res = self.run_cycle(ctx)
        self.assertEqual((res.outcome, res.summary, res.next_task), ("finished", "done via exec", "more"), res.error)
        self.assertEqual((res.usage["input"], res.usage["cache_read"], res.usage["output"], res.usage["turns"]), (100, 50, 10, 1))
        st = ctx.memory.state
        self.assertEqual((st.session_id, st.context_tokens, st.resume_pending), ("thread-123", 160, False))
        self.assertEqual(self.store.get_comments(1)[0]["body"], "On it.")
        self.assertEqual(self.store.get_comments(1)[-1]["author"], "dev-1")  # commit summary landed in the working thread
        self.assertIn("added hello.py", self.store.get_comments(1)[-1]["body"])
        kinds = [e["kind"] for e in ctx.memory.read_activity()[0]]
        self.assertTrue({"text", "tool", "result"} <= set(kinds), kinds)
        texts = [e["text"] for e in ctx.memory.read_activity()[0] if e["kind"] == "text"]
        self.assertTrue(any("tools: check_inbox,finish_cycle,git_commit,git_status,list_agents,list_threads,post_comment,post_thread,read_thread,update_notes" == t for t in texts), texts)

    def test_pause_interrupts_and_resumes_by_thread_id(self) -> None:
        os.environ["FAKE_CODEX_PAUSE"] = "1"
        ctx = self.ctx()

        def should_stop() -> bool:  # stop once the commit has landed
            return len(self.store.get_comments(1)) >= 2

        res = self.run_cycle(ctx, should_stop)
        self.assertEqual(res.outcome, "paused", res.error)
        self.assertTrue(ctx.memory.state.resume_pending)
        os.environ.pop("FAKE_CODEX_PAUSE")
        ctx2 = self.ctx()
        res = self.run_cycle(ctx2)
        self.assertEqual((res.outcome, res.summary), ("finished", "done via resume"), res.error)
        self.assertFalse(ctx2.memory.state.resume_pending)

    def test_limit_is_reported(self) -> None:
        os.environ["FAKE_CODEX_LIMIT"] = "1"
        res = self.run_cycle(self.ctx())
        self.assertEqual(res.outcome, "limit")
        self.assertIn("usage limit", res.error)

    def test_structured_uses_output_schema(self) -> None:
        plan = asyncio.run(CodexBackend(self.config).structured(prompt="p", tool_name="propose_team", description="d", schema={"type": "object"}, model="", effort="low"))
        self.assertEqual(plan["agents"][0]["model"], "gpt-5.3-codex")


if __name__ == "__main__":
    unittest.main()
