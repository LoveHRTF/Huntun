"""Backend-independent tests: board store, mentions, tools, git commits, memory, and the HTTP API."""
from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from huntun.config import agents_dir, db_path, default_config, save_config, save_team
from huntun.gitops import ensure_repo, recent_log
from huntun.master import master_spec
from huntun.memory import AgentMemory
from huntun.roles import build_system_prompt
from huntun.store import Store, parse_mentions
from huntun.tools import TOOLS, ToolContext, available_tools, execute
from huntun.types import AgentSpec, CycleState


def team_of_three() -> list[AgentSpec]:
    return [
        master_spec(),
        AgentSpec("team-lead", "team-lead", "Team Lead", "lead"),
        AgentSpec("backend-1", "backend", "Backend", "api"),
    ]


class CoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self.tmp.name)
        self.config = default_config("Build a tiny CLI")
        self.team = team_of_three()
        save_config(self.ws, self.config)
        save_team(self.ws, self.team)
        asyncio.run(ensure_repo(self.ws))
        self.store = Store(db_path(self.ws))

    def tearDown(self) -> None:
        self.store.close()
        self.tmp.cleanup()

    def ctx(self, agent: AgentSpec) -> ToolContext:
        return ToolContext(agent=agent, team=lambda: self.team, workspace=self.ws, store=self.store,
                           memory=AgentMemory(agents_dir(self.ws), agent.name), config=self.config, cycle=CycleState())

    def run_tool(self, ctx: ToolContext, tool: str, **args):
        spec = next(t for t in TOOLS if t.name == tool)
        return asyncio.run(execute(spec, args, ctx))

    def test_repo_ignores_huntun_dir_and_caches(self) -> None:
        text = (self.ws / ".gitignore").read_text()
        for entry in (".huntun/", "__pycache__/", ".venv/", "node_modules/", ".DS_Store", ".pytest_cache/"):
            self.assertIn(entry, text)

    def test_commit_summary_can_reply_in_existing_thread(self) -> None:
        ctx = self.ctx(self.team[2])
        task = self.store.create_thread("team-lead", "Task: API scaffold", "@backend-1 please scaffold the API")
        self.run_tool(ctx, "write_file", path="api.py", content="x = 1\n")
        out, err = self.run_tool(ctx, "git_commit", message="feat: scaffold", summary_title="unused", summary_body="Scaffolded, ran it, next: endpoints", thread_id=task["id"])
        self.assertFalse(err, out)
        self.assertIn(f"reply on thread #{task['id']}", out)
        comments = self.store.get_comments(task["id"])
        self.assertEqual(comments[-1]["author"], "backend-1")
        self.assertIn("Scaffolded", comments[-1]["body"])
        self.assertEqual(len(self.store.list_threads()), 1, "no extra commit thread was created")

    def test_commit_defaults_to_the_thread_being_worked_in(self) -> None:
        from huntun.tools import collect_inbox

        ctx = self.ctx(self.team[2])
        task = self.store.create_thread("team-lead", "Task: docs", "@backend-1 write the README")
        collect_inbox(ctx)  # the cycle prompt delivers the mention; that thread becomes the working thread
        self.assertEqual(ctx.cycle.active_thread, task["id"])
        self.run_tool(ctx, "write_file", path="README.md", content="# hi\n")
        out, err = self.run_tool(ctx, "git_commit", message="docs: readme", summary_title="README", summary_body="README added")
        self.assertFalse(err, out)
        self.assertIn(f"reply on thread #{task['id']}", out)
        self.assertEqual(self.store.get_comments(task["id"])[-1]["author"], "backend-1")
        out, _ = self.run_tool(ctx, "git_commit", message="x", summary_title="x", summary_body="x", thread_id=0)
        self.assertIn("Nothing to commit", out)

    def test_parse_mentions(self) -> None:
        self.assertEqual(parse_mentions("hey @backend-1 and @Team-Lead, email a@b.com @all"), ["backend-1", "team-lead", "all"])

    def test_mentions_and_inbox(self) -> None:
        woken: list[str] = []
        self.store.on("mention", woken.append)
        t = self.store.create_thread("team-lead", "Plan", "@backend-1 please build the API. @master FYI")
        self.assertEqual(sorted(woken), ["backend-1", "master"])
        self.store.add_comment(t["id"], "backend-1", "On it. @qa-1 will you test?")
        lead_inbox = self.store.take_inbox("team-lead")
        self.assertEqual([i.kind for i in lead_inbox], ["reply"])
        be_inbox = self.store.take_inbox("backend-1")
        self.assertEqual([i.kind for i in be_inbox], ["mention"])
        self.assertEqual(self.store.take_inbox("backend-1"), [], "inbox is marked seen")
        self.assertEqual(self.store.peek_inbox_count("qa-1"), 1)

    def test_tagging_human_creates_attention_until_they_reply(self) -> None:
        self.assertEqual(self.store.attention_count(), 0)
        t = self.store.create_thread("master", "Need a decision", "@human should we use Postgres or SQLite?")
        self.assertEqual(self.store.attention_count(), 1)
        self.store.add_comment(t["id"], "team-lead", "I'd go SQLite; the owner said small.")  # no @human: no new item
        self.assertEqual(self.store.attention_count(), 1)
        self.store.add_comment(t["id"], "qa-1", "@human also, which browsers matter?")
        items = self.store.open_attention()
        self.assertEqual([i["agent"] for i in items], ["qa-1", "master"])
        self.assertEqual(items[0]["title"], "Need a decision")
        self.store.add_comment(t["id"], "human", "SQLite, Chrome only.")
        self.assertEqual(self.store.attention_count(), 0, "a human reply in the thread clears its items")
        t2 = self.store.create_thread("master", "Approve?", "@human ok?")
        self.assertTrue(self.store.resolve_attention(self.store.open_attention()[0]["id"]))
        self.assertEqual(self.store.attention_count(), 0)
        self.assertIsNotNone(t2)

    def test_broadcast_does_not_wake_author(self) -> None:
        self.store.create_thread("master", "Kickoff", "@all read this")
        self.assertEqual(self.store.peek_inbox_count("master"), 0)
        self.assertEqual(self.store.peek_inbox_count("backend-1"), 1)

    def test_tool_availability_per_backend(self) -> None:
        worker = self.ctx(self.team[2])
        api_names = {t.name for t in available_tools(worker, "api")}
        cc_names = {t.name for t in available_tools(worker, "claude-code")}
        self.assertIn("write_file", api_names)
        self.assertNotIn("write_file", cc_names, "Claude Code has its own file tools")
        self.assertIn("git_commit", cc_names)
        self.assertNotIn("hire_agent", api_names, "workers cannot hire")
        self.assertIn("hire_agent", {t.name for t in available_tools(self.ctx(self.team[0]), "api")})

    def test_file_tools_and_sandbox(self) -> None:
        ctx = self.ctx(self.team[2])
        out, err = self.run_tool(ctx, "write_file", path="src/app.py", content="print('hi')\n")
        self.assertFalse(err, out)
        self.assertTrue(self.run_tool(ctx, "write_file", path="../escape.txt", content="x")[1])
        self.assertTrue(self.run_tool(ctx, "write_file", path=".huntun/hack", content="x")[1])
        self.assertFalse(self.run_tool(ctx, "edit_file", path="src/app.py", old_string="hi", new_string="hello")[1])
        out, _ = self.run_tool(ctx, "run_command", command="python3 src/app.py")
        self.assertIn("hello", out)
        self.assertIn("hello", self.run_tool(ctx, "read_file", path="src/app.py")[0])
        self.assertIn("src/app.py", self.run_tool(ctx, "search_files", pattern="print")[0])
        out, err = self.run_tool(ctx, "edit_file", path="src/app.py")  # missing required fields
        self.assertTrue(err)
        self.assertIn("INVALID_JSON", out)
        self.assertEqual(ctx.memory.state.touched_files, ["src/app.py"])

    def test_commit_posts_thread_and_returns_inbox(self) -> None:
        ctx = self.ctx(self.team[2])
        t = self.store.create_thread("team-lead", "Plan", "@backend-1 build the API")
        self.run_tool(ctx, "write_file", path="src/app.py", content="x = 1\n")
        self.store.add_comment(t["id"], "human", "@backend-1 also add tests please")
        out, err = self.run_tool(ctx, "git_commit", message="feat: app", summary_title="Initial app", summary_body="Adds app.py")
        self.assertFalse(err, out)
        self.assertIn("New board activity", out)
        self.assertIn("add tests", out)
        self.assertEqual(ctx.memory.state.touched_files, [])
        log = asyncio.run(recent_log(self.ws))
        self.assertIn("backend-1", log)
        self.assertIn("feat: app", log)
        newest = self.store.list_threads()[0]
        self.assertEqual(newest["title"], "Initial app")
        self.assertTrue(newest["commit_sha"])
        out, err = self.run_tool(ctx, "git_commit", message="x", summary_title="x", summary_body="x")
        self.assertTrue(err)
        self.assertIn("Nothing to commit", out)
        self.run_tool(ctx, "finish_cycle", summary="done", next_task="tests", wait_for_mention=True)
        self.assertTrue(ctx.cycle.finished and ctx.cycle.wait_for_mention)

    def test_memory_persistence(self) -> None:
        m = AgentMemory(agents_dir(self.ws), "backend-1")
        m.save_notes("# notes\nowns api")
        m.state.current_task = "tests"
        m.state.session_id = "abc"
        m.save_state()
        m.save_transcript([{"role": "user", "content": "hi"}])
        m2 = AgentMemory(agents_dir(self.ws), "backend-1")
        self.assertEqual(m2.notes(), "# notes\nowns api")
        self.assertEqual((m2.state.current_task, m2.state.session_id), ("tests", "abc"))
        self.assertEqual(len(m2.load_transcript() or []), 1)
        m2.clear_transcript()
        self.assertIsNone(m2.load_transcript())

    def test_activity_log_and_usage(self) -> None:
        m = AgentMemory(agents_dir(self.ws), "backend-1")
        self.assertEqual(m.read_activity(), ([], 0))
        for i in range(5):
            m.activity("tool", f"call {i}")
        entries, cursor = m.read_activity()
        self.assertEqual(([e["text"] for e in entries], cursor), (["call 0", "call 1", "call 2", "call 3", "call 4"], 5))
        m.activity("text", "done")
        entries, cursor = m.read_activity(after=cursor)
        self.assertEqual(([e["kind"] for e in entries], cursor), (["text"], 6))
        self.assertEqual(m.read_activity(after=6)[0], [])
        self.assertEqual(len(m.read_activity(after=0, limit=2)[0]), 2)
        ctx = self.ctx(self.team[2])
        self.run_tool(ctx, "update_notes", notes="n")
        self.assertEqual(ctx.memory.read_activity()[0][-1]["kind"], "result")

    def test_master_changes_need_human_confirmation(self) -> None:
        from huntun.tools import ToolHooks

        applied: list[str] = []

        async def hire(spec):
            applied.append("hire:" + spec["name"])
            return "hired"

        async def set_goal(goal, dod):
            applied.append("goal:" + goal)
            return "goal set"

        ctx = self.ctx(self.team[0])
        ctx.hooks = ToolHooks(hire_agent=hire, set_goal=set_goal)
        names = {t.name for t in available_tools(ctx, "api")}
        self.assertTrue({"hire_agent", "retire_agent", "set_agent_model", "set_goal", "resume_team"} <= names)
        self.assertFalse({"write_file", "edit_file", "git_commit"} & names, "the master leads; it does not build or commit")
        hire_args = dict(name="qa-2", role="qa", title="QA", brief="test things")
        out, err = self.run_tool(ctx, "hire_agent", confirmation_thread_id=0, **hire_args)
        self.assertTrue(err)
        self.assertIn("confirmation", out)
        # master proposes, but the human has not replied yet
        out, _ = self.run_tool(ctx, "post_thread", title="Staffing: add a second QA?", body="@human I propose hiring qa-2. OK?")
        tid = int(out.split("#")[1])
        out, err = self.run_tool(ctx, "hire_agent", confirmation_thread_id=tid, **hire_args)
        self.assertTrue(err)
        self.assertIn("has not replied", out)
        self.assertEqual(applied, [])
        # human confirms in that thread
        self.store.add_comment(tid, "human", "@master yes, go ahead")
        out, err = self.run_tool(ctx, "hire_agent", confirmation_thread_id=tid, **hire_args)
        self.assertFalse(err, out)
        out, err = self.run_tool(ctx, "set_goal", goal="New goal", definition_of_done="a\nb", confirmation_thread_id=tid)
        self.assertFalse(err, out)
        self.assertEqual(applied, ["hire:qa-2", "goal:New goal"])

    def test_backend_for_model(self) -> None:
        from huntun.models import backend_for_model, catalog_available

        both = {"claude-code": "x", "codex": "y"}
        self.assertEqual(backend_for_model("claude-sonnet-5", both), "claude-code")
        self.assertEqual(backend_for_model("gpt-5.3-codex", both), "codex")
        self.assertEqual(backend_for_model("claude-sonnet-5", {"api": "k"}), "api")
        self.assertEqual(backend_for_model("gpt-5.3-codex", {"api": "k"}, default="api"), "api")
        self.assertEqual({b for _, b in catalog_available(both)}, {"claude-code", "codex"})
        self.assertEqual(catalog_available({}), [])

    def test_model_catalog_and_cost(self) -> None:
        from huntun.models import context_limit, cost_usd, model_info
        self.assertEqual(model_info("claude-sonnet-5").tier, "strong")
        self.assertEqual(model_info("sonnet").id, "claude-sonnet-5")
        self.assertIsNone(model_info("gpt-9"))
        self.assertEqual(context_limit("claude-haiku-4-5"), 200_000)
        self.assertEqual(cost_usd("claude-opus-5", 1_000_000, 0), 5.0)
        self.assertEqual(cost_usd("claude-sonnet-5", 0, 1_000_000), 10.0)
        self.assertAlmostEqual(cost_usd("claude-opus-5", 0, 0, cache_read=1_000_000), 0.5)
        self.assertIn("set_agent_model", {t.name for t in available_tools(self.ctx(self.team[0]), "api")})
        self.assertIn("git_commit", {t.name for t in available_tools(self.ctx(self.team[2]), "api")})

    def test_system_prompt(self) -> None:
        self.config.definition_of_done = "CLI prints a quote\nTests pass"
        self.team[2].personality = "Grumpy but brilliant."
        worker = build_system_prompt(self.team[2], self.config, self.team, "api")
        self.assertIn("Grumpy but brilliant.", worker)
        self.assertNotIn("Your personality", build_system_prompt(self.team[0], self.config, self.team, "api"), "the master has no personality section")
        self.assertIn("Definition of done", worker)
        self.assertIn("Tests pass", worker)
        self.assertIn("@backend-1", worker)
        self.assertIn("(you)", worker)
        self.assertNotIn("Leadership duties", worker)
        self.assertIn("read_file", worker)
        lead_cc = build_system_prompt(self.team[1], self.config, self.team, "claude-code")
        self.assertIn("Leadership duties", lead_cc)
        self.assertIn("MCP tools", lead_cc)

if __name__ == "__main__":
    unittest.main()
