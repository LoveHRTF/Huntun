"""The web-centric flow: register a directory, plan the team from the page (fake backend), load, control, board routes."""
from __future__ import annotations

import asyncio
import json
import os
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import huntun.hub as hub_module
from huntun.hub import Hub, browse, workspace_id
from huntun.master import describe_workspace
from huntun.server import start_server


class FakeBackend:
    """Stands in for a model backend: plans a two-agent team and runs no-op cycles."""

    name = "fake"
    last_prompt = ""

    def __init__(self, config: Any) -> None:
        self.config = config

    goal_prompts: list[str] = []

    async def structured(self, **kw: Any) -> dict[str, Any]:
        if kw["tool_name"] == "propose_goal":
            FakeBackend.goal_prompts.append(kw["prompt"])
            revised = "Human's reply" in kw["prompt"]
            return {"message": "Revised as asked." if revised else "Here is how I read it.", "goal": "Add a CLI to myapp (revised)" if revised else "Add a CLI to myapp",
                    "definition_of_done": ["CLI runs", "tests pass"] + (["docs updated"] if revised else []), "assumptions": ["Python 3.12"], "questions": [] if revised else ["Need a --json flag?"]}
        assert "propose_team" == kw["tool_name"]
        FakeBackend.last_prompt = kw["prompt"]
        return {"rationale": "Small goal, small team.", "estimate_notes": "rough guess",
                "agents": [{"name": "team-lead", "role": "team-lead", "title": "Team Lead", "brief": "lead", "model": "claude-opus-5", "effort": "high", "why": "architecture", "personality": "architect", "personality_note": "", "estimated_cycles": 4, "tokens_per_cycle": 200000},
                           {"name": "dev-1", "role": "fullstack", "title": "Developer", "brief": "build it", "model": "claude-sonnet-5", "effort": "medium", "why": "routine", "personality": "blunt", "personality_note": "Loves tiny commits.", "estimated_cycles": 10, "tokens_per_cycle": 300000},
                           {"name": "docs-1", "role": "tech-writer", "title": "Writer", "brief": "docs", "model": "claude-haiku-4-5", "effort": "low", "why": "cheap", "personality": "mentor", "personality_note": "", "estimated_cycles": 2, "tokens_per_cycle": 100000}]}

    async def run_cycle(self, **kw: Any) -> Any:
        from huntun.types import CycleResult

        kw["ctx"].cycle.finished = True
        return CycleResult("finished", "noop", "noop", None, {})


class HubServer:
    """Runs a Hub event loop in a thread, the way `huntun serve` does, so HTTP handlers can hand work to it."""

    def __init__(self, home: Path) -> None:
        self.hub = Hub(home)
        self.ready = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()
        self.ready.wait(5)
        self.server = start_server(self.hub, 0)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def _run(self) -> None:
        async def main() -> None:
            self.hub.loop = asyncio.get_running_loop()
            self.stop = asyncio.Event()
            self.ready.set()
            await self.stop.wait()
            await self.hub.shutdown()

        asyncio.run(main())

    def close(self) -> None:
        self.server.shutdown()
        self.hub.loop.call_soon_threadsafe(self.stop.set)
        self.thread.join(10)

    def call(self, path: str, body: dict | None = None) -> Any:
        req = urllib.request.Request(self.base + path, data=json.dumps(body).encode() if body is not None else None,
                                     headers={"content-type": "application/json"}, method="POST" if body is not None else "GET")
        with urllib.request.urlopen(req) as r:
            return json.loads(r.read())


class HubTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.home = root / "home"
        self.project = root / "myapp"
        self.project.mkdir()
        (self.project / "README.md").write_text("# myapp\nA sample project.\n")
        (self.project / "src").mkdir()
        (self.project / "src" / "main.py").write_text("print('hi')\n")
        os.environ["ANTHROPIC_API_KEY"] = "test-key"  # backend resolution picks 'api'; make_backend is faked below
        self._real_make_backend = hub_module.make_backend
        self.fake = None

        def fake_make_backend(name: str, config: Any) -> FakeBackend:
            self.fake = FakeBackend(config)
            return self.fake

        hub_module.make_backend = fake_make_backend
        import huntun.orchestrator as orch_module

        self._real_orch_backend = orch_module.make_backend
        orch_module.make_backend = fake_make_backend
        self.srv = HubServer(self.home)

    def tearDown(self) -> None:
        self.srv.close()
        hub_module.make_backend = self._real_make_backend
        import huntun.orchestrator as orch_module

        orch_module.make_backend = self._real_orch_backend
        self.tmp.cleanup()

    def store_of(self, wid: str):
        return self.srv.hub.get(wid).open_store()

    def wait_state(self, wid: str, states: tuple[str, ...], timeout: float = 30) -> dict:
        deadline = time.time() + timeout
        while time.time() < deadline:
            w = self.srv.call(f"/api/workspaces/{wid}")
            if w["state"] in states:
                return w
            time.sleep(0.2)
        self.fail(f"workspace never reached {states}: {w}")

    def test_describe_workspace_sees_existing_files(self) -> None:
        desc = describe_workspace(self.project)
        self.assertIn("src/main.py", desc)
        self.assertIn("A sample project.", desc)
        self.assertEqual(describe_workspace(Path(self.tmp.name) / "home"), "")

    def test_browse(self) -> None:
        info = browse(str(Path(self.tmp.name)))
        self.assertIn("myapp", info["dirs"])
        self.assertFalse(browse(str(self.project / "nope"))["exists"])

    def test_web_flow_open_plan_run_pause(self) -> None:
        # Home page and empty registry
        with urllib.request.urlopen(self.srv.base + "/") as r:
            self.assertIn(b"Open a project", r.read())
        self.assertEqual(self.srv.call("/api/workspaces")["workspaces"], [])

        # Point at the existing directory: registered as "new" (no .huntun yet)
        w = self.srv.call("/api/workspaces", {"path": str(self.project)})
        wid = w["id"]
        self.assertEqual((w["state"], w["initialized"], w["name"]), ("new", False, "myapp"))
        self.assertEqual(wid, workspace_id(self.project))
        with self.assertRaises(urllib.error.HTTPError) as cm:
            self.srv.call("/api/workspaces", {"path": str(self.project / "missing")})
        self.assertEqual(cm.exception.code, 400)

        # Phase 1: the master checks the goal with the human before any planning
        w = self.srv.call(f"/api/workspaces/{wid}/init", {"goal": "Add a CLI to myapp", "context": "keep it small"})
        self.assertEqual(w["state"], "clarifying")
        w = self.wait_state(wid, ("goal_proposed", "error"))
        self.assertEqual(w["state"], "goal_proposed", w.get("error"))
        self.assertFalse(w["initialized"])
        self.assertEqual((w["goal_draft"]["goal"], w["goal_draft"]["questions"]), ("Add a CLI to myapp", ["Need a --json flag?"]))
        self.assertIn("src/main.py", FakeBackend.goal_prompts[-1], "existing code is described to the master during the goal check")
        st = self.srv.call(f"/api/w/{wid}/state")
        self.assertFalse(st["goal_confirmed"])
        self.assertTrue(st["threads"][0]["title"].startswith("Goal check"))
        self.assertEqual(st["goal_thread_id"], st["threads"][0]["id"])
        self.assertEqual(self.store_of(wid).peek_inbox_count("human"), 1)
        with self.assertRaises(urllib.error.HTTPError) as cm:
            self.srv.call(f"/api/workspaces/{wid}/control", {"action": "start"})
        self.assertEqual(cm.exception.code, 409)

        # Human replies: the master revises the goal proposal in the same thread
        self.srv.call(f"/api/workspaces/{wid}/goal-reply", {"message": "Yes, add --json. Docs must be updated too."})
        w = self.wait_state(wid, ("goal_proposed", "error"))
        self.assertEqual(w["goal_draft"]["goal"], "Add a CLI to myapp (revised)")
        self.assertIn("Docs must be updated", FakeBackend.goal_prompts[-1])
        goal_thread = self.srv.call(f"/api/w/{wid}/threads/{st['goal_thread_id']}")
        self.assertEqual([c["author"] for c in goal_thread["comments"]], ["human", "master"])

        # Human confirms with an edited definition of done: planning starts
        w = self.srv.call(f"/api/workspaces/{wid}/confirm-goal", {"goal": "Add a CLI to myapp, final", "definition_of_done": "CLI runs\ntests pass\ndocs updated"})
        self.assertEqual(w["state"], "planning")
        w = self.wait_state(wid, ("proposed", "ready", "error"))
        self.assertEqual(w["state"], "proposed", w.get("error"))
        self.assertEqual((w["goal"], w["goal_confirmed"], w["definition_of_done"]), ("Add a CLI to myapp, final", True, "CLI runs\ntests pass\ndocs updated"))
        self.assertIn("docs updated", FakeBackend.last_prompt, "the plan prompt carries the agreed definition of done")
        self.assertIn("src/main.py", FakeBackend.last_prompt)
        self.assertIn("keep it small", FakeBackend.last_prompt)
        self.assertIn("claude-haiku-4-5", FakeBackend.last_prompt, "model catalog is offered to the master")
        self.assertEqual((w["agents"], w["running"], w["initialized"], w["approved"]), (4, False, True, False))
        self.assertTrue((self.project / ".huntun" / "team.json").exists())
        self.assertTrue((self.project / ".git").exists())
        self.assertIn(".huntun/", (self.project / ".gitignore").read_text())

        # The proposal is on the board, tagging the human; per-agent model and effort are recorded
        st = self.srv.call(f"/api/w/{wid}/state")
        self.assertFalse(st["approved"])
        self.assertEqual((st["limits"]["paused"], st["limits"]["pause_count"]), (False, 0))
        self.assertEqual(st["totals"], {"tokens": 0, "cost_usd": 0})
        self.assertEqual(st["threads"][0]["title"], "Proposed plan: please review")
        self.assertEqual(st["plan_thread_id"], st["threads"][0]["id"])
        self.assertIn("@human", st["threads"][0]["snippet"])
        by_name = {a["name"]: a for a in st["agents"]}
        self.assertEqual((by_name["dev-1"]["model"], by_name["dev-1"]["effort"]), ("claude-sonnet-5", "medium"))
        self.assertEqual((by_name["dev-1"]["personality_preset"], by_name["dev-1"]["estimated_cycles"]), ("blunt", 10))
        self.assertTrue(by_name["dev-1"]["personality"].startswith("Blunt and fast") and by_name["dev-1"]["personality"].endswith("Loves tiny commits."))
        self.assertEqual((by_name["master"]["personality"], by_name["master"]["personality_preset"]), ("", ""), "the master has no personality")
        self.assertEqual([p["id"] for p in st["personalities"]][:2], ["pragmatic", "meticulous"])
        self.assertIn("jokester", [p["id"] for p in st["personalities"] if p["group"] == "human"])
        est = st["estimate"]
        self.assertEqual(est["notes"], "rough guess")
        self.assertEqual(est["tokens"], 8 * 120000 + 4 * 200000 + 10 * 300000 + 2 * 100000)
        self.assertGreater(est["cost_usd"], 0)
        self.assertIn("Estimate to finish", st["threads"][0]["snippet"] + self.srv.call(f"/api/w/{wid}/threads/{st['plan_thread_id']}")["thread"]["body"])
        self.assertIn("Model choice: routine", by_name["dev-1"]["brief"])
        self.assertGreaterEqual(self.store_of(wid).peek_inbox_count("human"), 1)

        # Nothing can start before approval
        with self.assertRaises(urllib.error.HTTPError) as cm:
            self.srv.call(f"/api/workspaces/{wid}/control", {"action": "start"})
        self.assertEqual(cm.exception.code, 409)

        # Ask for changes: the master re-plans with the feedback and posts a revised proposal
        self.srv.call(f"/api/workspaces/{wid}/replan", {"feedback": "drop the writer"})
        w = self.wait_state(wid, ("proposed", "error"))
        self.assertEqual(w["state"], "proposed", w.get("error"))
        self.assertIn("drop the writer", FakeBackend.last_prompt)
        self.assertIn("Previous proposal", FakeBackend.last_prompt)
        st = self.srv.call(f"/api/w/{wid}/state")
        self.assertEqual(st["threads"][0]["title"], "Revised plan: please review")
        first_plan = [t for t in st["threads"] if t["title"].startswith("Proposed")][0]
        self.assertEqual(first_plan["last_comments"][-1]["author"], "human")

        # Approve with edits: change a model, remove an agent; the team loads paused
        st = self.srv.call(f"/api/w/{wid}/state")
        self.assertTrue(all(set(m) >= {"id", "backend", "vendor"} for m in st["models"]))
        self.assertIn("api", st["backends"])
        w = self.srv.call(f"/api/workspaces/{wid}/approve", {"agents": [{"name": "dev-1", "model": "claude-opus-5", "effort": "xhigh", "personality_preset": "custom", "personality": "Grumpy but brilliant."},
                                                                          {"name": "team-lead", "personality_preset": "skeptic", "personality": "ignored when a preset is chosen"},
                                                                          {"name": "docs-1", "remove": True}]})
        self.assertEqual((w["state"], w["approved"], w["running"]), ("ready", True, False))
        st = self.srv.call(f"/api/w/{wid}/state")
        self.assertTrue(st["approved"])
        self.assertEqual([a["name"] for a in st["agents"]], ["master", "team-lead", "dev-1"])
        dev = [a for a in st["agents"] if a["name"] == "dev-1"][0]
        self.assertEqual((dev["model"], dev["effort"], dev["info"]["model"], dev["info"]["effort"]), ("claude-opus-5", "xhigh", "claude-opus-5", "xhigh"))
        self.assertIn(dev["backend"], ("api", "claude-code"))
        self.assertEqual(st["estimate"]["tokens"], 8 * 120000 + 4 * 200000 + 10 * 300000, "estimate recomputed without the removed writer")
        self.assertEqual((dev["personality"], dev["personality_preset"]), ("Grumpy but brilliant.", "custom"))
        lead = [a for a in st["agents"] if a["name"] == "team-lead"][0]
        self.assertEqual(lead["personality_preset"], "skeptic")
        self.assertTrue(lead["personality"].startswith("Dry and skeptical"))
        self.assertIn("personality -> custom", plan_comment := self.srv.call(f"/api/w/{wid}/threads/{st['plan_thread_id']}")["comments"][-1]["body"])
        self.assertIn("personality -> skeptic", plan_comment)
        self.assertEqual((st["agents"][0]["info"]["cycles"], st["agents"][0]["info"]["compactions"]), (0, 0))
        plan = self.srv.call(f"/api/w/{wid}/threads/{st['plan_thread_id']}")
        self.assertIn("approved", plan["comments"][-1]["body"].lower())
        self.assertIn("removed @docs-1", plan["comments"][-1]["body"])

        # Human posts and tags from the page; thread previews carry a snippet and the last comments
        t = self.srv.call(f"/api/w/{wid}/threads", {"title": "Req", "body": "@team-lead add --json"})
        self.srv.call(f"/api/w/{wid}/comments", {"thread_id": t["id"], "body": "@dev-1 ack?"})
        self.assertEqual(len(self.srv.call(f"/api/w/{wid}/threads/{t['id']}")["comments"]), 1)
        top = self.srv.call(f"/api/w/{wid}/state")["threads"][0]
        self.assertEqual((top["title"], top["snippet"], top["last_comments"][0]["author"], top["last_comments"][0]["snippet"]), ("Req", "@team-lead add --json", "human", "@dev-1 ack?"))
        self.assertNotIn("body", top)

        # Agent activity endpoint (empty before any cycle) and 404 for unknown agents
        act = self.srv.call(f"/api/w/{wid}/agents/dev-1/activity?after=0")
        self.assertEqual((act["entries"], act["cursor"], act["agent"]["cycles"]), ([], 0, 0))
        with self.assertRaises(urllib.error.HTTPError) as cm:
            self.srv.call(f"/api/w/{wid}/agents/nobody/activity")
        self.assertEqual(cm.exception.code, 404)

        # Start and pause from the page
        self.assertTrue(self.srv.call(f"/api/workspaces/{wid}/control", {"action": "start"})["running"])
        self.assertFalse(self.srv.call(f"/api/workspaces/{wid}/control", {"action": "pause"})["running"])

        # Registry persists and lists the project; re-adding the same path returns the same id
        self.assertEqual(self.srv.call("/api/workspaces", {"path": str(self.project)})["id"], wid)
        self.assertEqual(json.loads((self.home / "workspaces.json").read_text())[0]["path"], str(self.project.resolve()))

        # Close unloads agents; the entry stays; forget removes it
        self.assertEqual(self.srv.call(f"/api/workspaces/{wid}/close", {})["state"], "ready")
        self.srv.call(f"/api/workspaces/{wid}/forget", {})
        self.assertEqual(self.srv.call("/api/workspaces")["workspaces"], [])

    def test_init_requires_goal_and_reports_errors(self) -> None:
        w = self.srv.call("/api/workspaces", {"path": str(self.project)})
        with self.assertRaises(urllib.error.HTTPError) as cm:
            self.srv.call(f"/api/workspaces/{w['id']}/init", {"goal": "  "})
        self.assertEqual(cm.exception.code, 400)

        async def boom(self: Any, **kw: Any) -> None:
            raise RuntimeError("model unavailable")

        original = FakeBackend.structured
        FakeBackend.structured = boom  # type: ignore[assignment]
        try:
            self.srv.call(f"/api/workspaces/{w['id']}/init", {"goal": "x"})
            got = self.wait_state(w["id"], ("goal_proposed", "proposed", "ready", "error"))
            self.assertEqual(got["state"], "error")
            self.assertIn("model unavailable", got["error"])
            self.assertFalse((self.project / ".huntun" / "config.json").exists(), "nothing is written until the master answers")
        finally:
            FakeBackend.structured = original  # type: ignore[assignment]


if __name__ == "__main__":
    unittest.main()
