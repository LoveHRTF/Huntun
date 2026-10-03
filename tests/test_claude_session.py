"""The Claude Code backend when the conversation it resumes is gone (the project moved to another machine, whose Claude Code
never saw it): the cycle runs again in a fresh session instead of failing on the same session id forever."""
from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from claude_agent_sdk import ResultMessage
from claude_agent_sdk._errors import ResultError

from huntun.backends import claude_code
from huntun.backends.claude_code import CONTINUE_NOTE, RESUME_PROMPT, SESSION_LOST_NOTE, ClaudeCodeBackend
from huntun.config import agents_dir, db_path, default_config, save_config, save_team
from huntun.gitops import ensure_repo
from huntun.master import master_spec
from huntun.memory import AgentMemory
from huntun.store import Store
from huntun.tools import ToolContext
from huntun.types import AgentSpec, CycleState


def _result(session_id: str, error: str = "") -> ResultMessage:
    return ResultMessage(subtype="error_during_execution" if error else "success", duration_ms=1, duration_api_ms=1, is_error=bool(error),
                         num_turns=1, session_id=session_id, result=error or "done", total_cost_usd=0.01)


class FakeClient:
    """Stands in for ClaudeSDKClient. Sessions in `gone` are refused, the way the SDK reports it: `how` = "raise" (a
    ResultError while connecting) or "result" (an error result message); `error` refuses every session with other text."""

    gone: set[str] = set()
    how = "raise"
    error = ""
    opened: list[str | None] = []                                                     # the session each connection resumed (None: new)
    seen: list[tuple[str | None, str]] = []                                            # (session, first message) once connected

    def __init__(self, options: Any) -> None:
        self.resume = options.resume

    async def __aenter__(self) -> FakeClient:
        FakeClient.opened.append(self.resume)
        if self.error:
            raise ResultError(f"Claude Code returned an error result: {self.error}", exit_code=1)
        if self.resume in self.gone and self.how == "raise":
            raise ResultError(f"Claude Code returned an error result: No conversation found with session ID: {self.resume}", exit_code=1)
        return self

    async def __aexit__(self, *_a: Any) -> bool:
        return False

    async def query(self, text: str) -> None:
        FakeClient.seen.append((self.resume, text))

    async def interrupt(self) -> None:
        pass

    async def get_context_usage(self) -> dict[str, Any]:
        return {}

    async def receive_response(self):
        if self.resume in self.gone:
            yield _result("never-kept", f"No conversation found with session ID: {self.resume}")
            return
        yield _result(self.resume or "fresh-session")


class LostSessionTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.ws = Path(tmp.name) / "ws"
        self.ws.mkdir()
        self.config = default_config("Mock goal")
        self.config.backend = "claude-code"
        self.team = [master_spec(), AgentSpec("dev-1", "backend", "Dev", "dev")]
        save_config(self.ws, self.config)
        save_team(self.ws, self.team)
        asyncio.run(ensure_repo(self.ws))
        self.store = Store(db_path(self.ws))
        self.addCleanup(self.store.close)
        FakeClient.gone, FakeClient.how, FakeClient.error, FakeClient.opened, FakeClient.seen = {"old-session"}, "raise", "", [], []
        p = mock.patch.object(claude_code, "ClaudeSDKClient", FakeClient)
        p.start()
        self.addCleanup(p.stop)
        self.logs: list[str] = []

    def ctx(self, session_id: str | None, resume_pending: bool = False) -> ToolContext:
        memory = AgentMemory(agents_dir(self.ws), "dev-1")
        memory.state.session_id, memory.state.session_cycles, memory.state.resume_pending = session_id, 3, resume_pending
        memory.save_state()
        return ToolContext(agent=self.team[1], team=lambda: self.team, workspace=self.ws, store=self.store, memory=memory, config=self.config, cycle=CycleState())

    def run_cycle(self, ctx: ToolContext) -> Any:
        return asyncio.run(ClaudeCodeBackend(self.config).run_cycle(ctx=ctx, system="SYS", prompt="work on it", model="claude-sonnet-5", effort="medium",
                                                                   should_stop=lambda: False, log=self.logs.append))

    def assert_moved_on(self, ctx: ToolContext, res: Any, first_message: str | None) -> None:
        """first_message: what the refused session was sent, or None when it was refused while connecting."""
        self.assertEqual(res.outcome, "finished", res.error)
        self.assertEqual(FakeClient.opened, ["old-session", None])
        self.assertEqual(FakeClient.seen, ([("old-session", first_message)] if first_message else []) + [(None, SESSION_LOST_NOTE + "work on it")])
        st = AgentMemory(agents_dir(self.ws), "dev-1").state                               # as saved on disk
        self.assertEqual((st.session_id, st.session_cycles, st.resume_pending), ("fresh-session", 1, False))
        self.assertTrue(any("is gone from this machine" in line for line in self.logs), self.logs)
        notes = [e["text"] for e in ctx.memory.read_activity()[0] if e["kind"] == "cycle"]
        self.assertTrue(any("no longer has this agent's conversation" in t for t in notes), notes)

    def test_a_refused_resume_runs_the_cycle_in_a_fresh_session(self) -> None:
        ctx = self.ctx("old-session")
        self.assert_moved_on(ctx, self.run_cycle(ctx), None)
        res = self.run_cycle(self.ctx("fresh-session"))                                  # and the next cycle continues the new one
        self.assertEqual(res.outcome, "finished", res.error)
        self.assertEqual(FakeClient.seen[-1], ("fresh-session", CONTINUE_NOTE + "work on it"))

    def test_an_agent_paused_mid_cycle_is_not_stuck_either(self) -> None:
        FakeClient.how = "result"                                                          # reported as an error result instead
        ctx = self.ctx("old-session", resume_pending=True)
        self.assert_moved_on(ctx, self.run_cycle(ctx), RESUME_PROMPT)

    def test_other_errors_keep_the_session(self) -> None:
        FakeClient.error = "API Error: 529 overloaded"
        ctx = self.ctx("old-session")
        res = self.run_cycle(ctx)
        self.assertEqual(res.outcome, "error")
        self.assertIn("overloaded", res.error)
        self.assertEqual(ctx.memory.state.session_id, "old-session")
        self.assertEqual(FakeClient.opened, ["old-session"])                              # no second try

    def test_a_fresh_session_that_fails_is_not_retried(self) -> None:
        FakeClient.gone = {None}                                                           # even a new conversation is refused
        res = self.run_cycle(self.ctx(None))
        self.assertEqual(res.outcome, "error")
        self.assertEqual(FakeClient.opened, [None])

    def test_cache_creation_and_native_cost_are_preserved_on_resume(self) -> None:
        async def response(_client):
            message = _result('fresh-session')
            message.usage = {'input_tokens':10, 'output_tokens':20,
                             'cache_read_input_tokens':1000, 'cache_creation_input_tokens':200}
            message.total_cost_usd = .123
            yield message
        with mock.patch.object(FakeClient, 'receive_response', response):
            for session in (None, 'fresh-session'):
                result = self.run_cycle(self.ctx(session))
                self.assertEqual(result.usage['input'],10)
                self.assertEqual(result.usage['cache_read'],1000)
                self.assertEqual(result.usage['cache_write'],200)
                self.assertEqual(result.usage['cost_usd'],.123)
                self.assertEqual(result.cost_status,'reported')


if __name__ == "__main__":
    unittest.main()
