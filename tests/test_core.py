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

    def test_human_reference_only_posts_fail_and_can_be_rewritten(self) -> None:
        ctx = self.ctx(self.team[0])
        tid = self.store.create_thread("human", "Staffing", "Hire a QA")["id"]
        for tool, args in (
            ("post_thread", {"title": "Need approval", "body": "@human 请看原605确认。"}),
            ("post_comment", {"thread_id": tid, "body": "@human Please read comment #6974 and approve."}),
        ):
            out, err = self.run_tool(ctx, tool, **args)
            self.assertTrue(err)
            self.assertIn("self-contained", out)
        self.assertEqual(self.store.attention_count(), 0)
        out, err = self.run_tool(ctx, "post_comment", thread_id=tid,
                                 body="@human May I hire one QA using gpt-6.1-sol medium to verify Safari before release?")
        self.assertFalse(err, out)
        self.assertEqual(self.store.attention_count(), 1)

    def test_rejected_human_commit_request_has_no_git_side_effects(self) -> None:
        ctx = self.ctx(self.team[2])
        self.run_tool(ctx, "write_file", path="new_feature.py", content="value = 1\n")
        before = asyncio.run(recent_log(self.ws, 1))
        out, err = self.run_tool(ctx, "git_commit", message="feat: new feature",
                                 summary_title="Feature", summary_body="@human Please approve thread #605.")
        self.assertTrue(err, out)
        self.assertIn("self-contained", out)
        self.assertEqual(asyncio.run(recent_log(self.ws, 1)), before)
        self.assertEqual(ctx.cycle.commits, [])
        self.assertIn("new_feature.py", ctx.memory.state.touched_files)
        self.assertEqual(self.store.attention_count(), 0)

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

    def test_master_delivers_the_project_once_with_a_report_for_the_human(self) -> None:
        master = self.ctx(self.team[0])
        self.assertIn("deliver_project", {t.name for t in available_tools(master, "api")})
        self.assertNotIn("deliver_project", {t.name for t in available_tools(self.ctx(self.team[1]), "claude-code")}, "only the master delivers")
        out, err = self.run_tool(master, "deliver_project", title="ATM v1.0", report="All five items of the definition of done are met; run `make demo`.")
        self.assertFalse(err, out)
        self.assertIn("Delivered", out)
        threads = self.store.list_threads(5)
        self.assertEqual(threads[0]["title"], "Delivery report: ATM v1.0")
        self.assertTrue(threads[0]["body"].startswith("@human "), "the report is addressed to the human")
        ev = self.store.list_events(0, 5)[0]
        self.assertEqual((ev["agent"], ev["kind"], ev["detail"]), ("master", "delivery", f"#{threads[0]['id']}: ATM v1.0"))
        self.assertEqual(self.store.get_control("delivery_thread", ""), str(threads[0]["id"]))
        self.assertTrue(self.store.get_control("delivered_at", ""))
        self.assertTrue(self.run_tool(master, "deliver_project", title="x", report="  ")[1], "an empty report is refused")

    def test_direct_human_hiring_instruction_survives_master_acknowledgements(self) -> None:
        from huntun.tools import ToolHooks
        applied = []
        async def hire(spec):
            applied.append(spec['name'])
            return 'hired'
        ctx=self.ctx(self.team[0])
        ctx.hooks=ToolHooks(hire_agent=hire)
        tid=self.store.create_thread('master','Staffing discussion','Existing team')['id']
        instruction=self.store.add_comment(tid,'human','@master Hire two QA agents; choose effort yourself.')['id']
        self.store.add_comment(tid,'master','Received; I will recruit them.')
        args=dict(role='qa',title='QA',brief='Test',confirmation_thread_id=tid,authorization_comment_id=instruction)
        for name in ('qa-2','qa-3'):
            out,err=self.run_tool(ctx,'hire_agent',name=name,**args)
            self.assertFalse(err,out)
            self.store.add_comment(tid,'master','Progress: one authorized hire completed')
        self.assertEqual(applied,['qa-2','qa-3'])
        own=self.store.add_comment(tid,'master','I authorize myself')['id']
        for source in (own,999999,-1,'invalid'):
            out,err=self.run_tool(ctx,'hire_agent',name='qa-4',**{**args,'authorization_comment_id':source})
            self.assertTrue(err,out)
        other=self.store.create_thread('human','Other','Other topic')['id']
        out,err=self.run_tool(ctx,'hire_agent',name='qa-4',**{**args,'confirmation_thread_id':other})
        self.assertTrue(err,out)
        self.store.add_comment(tid,'master','New scope: hire a third QA?',requires_confirmation=True)
        out,err=self.run_tool(ctx,'hire_agent',name='qa-4',**args)
        self.assertTrue(err,out)
        self.assertIn('newer changed-scope',out)
        approved=self.store.add_comment(tid,'human','Approve that third QA')['id']
        out,err=self.run_tool(ctx,'hire_agent',name='qa-4',**{**args,'authorization_comment_id':approved})
        self.assertFalse(err,out)

    def test_direct_hiring_from_a_human_opening_post(self) -> None:
        from huntun.tools import ToolHooks
        async def hire(spec):
            return 'hired'
        ctx=self.ctx(self.team[0])
        ctx.hooks=ToolHooks(hire_agent=hire)
        tid=self.store.create_thread('human','Recruit','@master Recruit one QA, choose its name and effort.')['id']
        self.store.add_comment(tid,'master','Received, executing your instruction.')
        args=dict(name='qa-2',role='qa',title='QA',brief='Test',confirmation_thread_id=tid,authorization_comment_id=0)
        out,err=self.run_tool(ctx,'hire_agent',**args)
        self.assertFalse(err,out)
        self.store.add_comment(tid,'master','Changed scope: two QA instead?',requires_confirmation=True)
        out,err=self.run_tool(ctx,'hire_agent',**args)
        self.assertTrue(err,out)
        own=self.store.create_thread('master','Recruit','I decided to recruit')['id']
        out,err=self.run_tool(ctx,'hire_agent',**{**args,'confirmation_thread_id':own})
        self.assertTrue(err,out)

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
        self.run_tool(ctx, 'post_comment', thread_id=tid, body='Received; hiring now.')
        self.run_tool(ctx, 'post_comment', thread_id=tid, body='Previously got "@human has not replied since your proposal"; retrying unchanged approved scope.')
        out, err = self.run_tool(ctx, "hire_agent", confirmation_thread_id=tid, **hire_args)
        self.assertFalse(err, out)
        out, err = self.run_tool(ctx, "set_goal", goal="New goal", definition_of_done="a\nb", confirmation_thread_id=tid)
        self.assertFalse(err, out)
        self.assertEqual(applied, ["hire:qa-2", "goal:New goal"])
        out, err = self.run_tool(ctx, 'post_comment', thread_id=tid, body='@human Changed staffing scope: add another QA?', requires_confirmation=True)
        self.assertFalse(err,out)
        out, err = self.run_tool(ctx, 'hire_agent', confirmation_thread_id=tid, **{**hire_args,'name':'qa-3'})
        self.assertTrue(err)
        self.assertEqual(applied, ["hire:qa-2", "goal:New goal"])
        self.store.add_comment(tid, 'human', 'Confirmed the changed scope')
        self.run_tool(ctx, 'post_comment', thread_id=tid, body='Received again')
        out, err = self.run_tool(ctx, 'hire_agent', confirmation_thread_id=tid, **{**hire_args,'name':'qa-3'})
        self.assertFalse(err,out)
        self.assertEqual(applied[-1], 'hire:qa-3')

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

    def test_the_master_can_lead_without_a_team_lead(self) -> None:
        team = [self.team[0], self.team[2]]                                               # master and backend-1, no team lead
        master = build_system_prompt(team[0], self.config, team, "api")
        worker = build_system_prompt(team[1], self.config, team, "api")
        self.assertIn("This team has no team lead", master)
        self.assertNotIn("@team-lead", master + worker)
        self.assertIn("(by @master or @human)", worker)
        with_lead = build_system_prompt(self.team[2], self.config, self.team, "api")
        self.assertIn("(by @team-lead, @master, or @human)", with_lead)
        self.assertNotIn("This team has no team lead", build_system_prompt(self.team[0], self.config, self.team, "api"))

    def test_planning_follows_the_leadership_choice(self) -> None:
        from huntun.master import plan_team

        class Planner:
            name = "fake"
            prompt = ""

            async def structured(self, **kw):
                Planner.prompt = kw["prompt"]
                return {"rationale": "r", "agents": [{"name": "team-lead", "role": "team-lead", "title": "Lead", "brief": "lead"},
                                                     {"name": "dev-1", "role": "fullstack", "title": "Dev", "brief": "build"}]}

        self.config.team_lead = False
        _, agents = asyncio.run(plan_team(Planner(), self.config))
        self.assertEqual([a.role for a in agents], ["fullstack"])                         # the proposed lead is dropped, none is added
        self.assertIn('Do not include a "team-lead"', Planner.prompt)
        self.config.team_lead = True
        _, agents = asyncio.run(plan_team(Planner(), self.config))
        self.assertEqual([a.role for a in agents], ["team-lead", "fullstack"])
        self.assertIn('Always include exactly one "team-lead"', Planner.prompt)

if __name__ == "__main__":
    unittest.main()
