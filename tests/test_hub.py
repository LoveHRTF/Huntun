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
    cwds: list[Any] = []
    models: list[str] = []                                                          # the model each master call asked for
    made: list[str] = []                                                            # the backend each make_backend call named

    async def structured(self, **kw: Any) -> dict[str, Any]:
        FakeBackend.cwds.append(kw.get("cwd"))
        FakeBackend.models.append(kw.get("model"))
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
        previous_home = os.environ.get("HUNTUN_HOME")
        os.environ["HUNTUN_HOME"] = str(self.home)
        self.addCleanup(lambda: os.environ.__setitem__("HUNTUN_HOME", previous_home) if previous_home is not None else os.environ.pop("HUNTUN_HOME", None))
        self.project = root / "myapp"
        self.project.mkdir()
        (self.project / "README.md").write_text("# myapp\nA sample project.\n")
        (self.project / "src").mkdir()
        (self.project / "src" / "main.py").write_text("print('hi')\n")
        os.environ["ANTHROPIC_API_KEY"] = "test-key"  # backend resolution picks 'api'; make_backend is faked below
        self._real_make_backend = hub_module.make_backend
        self.fake = None

        def fake_make_backend(name: str, config: Any) -> FakeBackend:
            FakeBackend.made.append(name)
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

    def test_pi_harness_new_and_existing_projects(self) -> None:
        from huntun.config import load_config, load_team

        wid = self.srv.call("/api/workspaces", {"path": str(self.project)})["id"]
        self.srv.call(f"/api/workspaces/{wid}/init", {"goal": "Add CLI", "backend": "pi-clm"})
        proposed = self.wait_state(wid, ("goal_proposed", "error"))
        self.assertEqual(proposed["state"], "goal_proposed", proposed.get("error"))
        self.assertEqual(load_config(self.project).backend, "pi-clm")
        self.assertEqual(FakeBackend.made[-1], "pi-clm")
        self.srv.call(f"/api/workspaces/{wid}/confirm-goal", {"goal": "Add CLI", "definition_of_done": "CLI runs\ntests pass"})
        self.wait_state(wid, ("proposed", "error"))
        self.srv.call(f"/api/workspaces/{wid}/approve", {})
        self.srv.call(f"/api/w/{wid}/harness", {"backend": "api", "model": "", "all_agents": False})
        overrides = [(a.model, a.backend) for a in load_team(self.project)]
        self.srv.call(f"/api/w/{wid}/harness", {"backend": "pi-clm", "model": "pi-clm:default", "all_agents": False})
        self.assertEqual([(a.model, a.backend) for a in load_team(self.project)], overrides)
        response = self.srv.call(f"/api/w/{wid}/harness", {"backend": "pi-clm", "model": "pi-clm:default", "all_agents": True})
        self.assertIn("next cycle", response["message"])
        self.assertTrue(all(not a.model and not a.backend for a in load_team(self.project)))
        state = self.srv.call(f"/api/w/{wid}/state")
        self.assertEqual((state["backend"], state["default_model"]), ("pi-clm", "pi-clm:default"))
        self.srv.call(f"/api/w/{wid}/agents/dev-1/model", {"model": "pi-clm:default", "effort": "high"})
        self.assertEqual(next(a.backend for a in load_team(self.project) if a.name == "dev-1"), "pi-clm")
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.srv.call(f"/api/w/{wid}/harness", {"backend": "pi-clm", "model": "claude-sonnet-5"})
        self.assertEqual(error.exception.code, 409)
        self.assertEqual(load_config(self.project).model, "pi-clm:default")

    def test_watchdog_http_history_and_model_selection(self) -> None:
        from unittest.mock import patch

        from huntun.config import HuntunConfig, save_config, save_team
        from huntun.models import model_info
        from huntun.types import AgentSpec
        save_config(self.project, HuntunConfig("Recovery", backend="api"))
        save_team(self.project, [AgentSpec("master", "master", "Master", "Lead")])
        wid = self.srv.call("/api/workspaces", {"path": str(self.project)})["id"]
        self.store_of(wid).set_control("plan_approved", "1")
        with patch("huntun.watchdog.make_backend", return_value=FakeBackend(None)), patch("huntun.watchdog.catalog_available", return_value=[(model_info("claude-sonnet-5"), "api")]):
            result = self.srv.call(f"/api/workspaces/{wid}/watchdog", {"message": "Inspect master", "model": "claude-sonnet-5", "effort": "high", "target": "master"})
            self.assertEqual(result["messages"][-1]["body"], "Inspect master")
            deadline = time.time() + 10
            while time.time() < deadline:
                result = self.srv.call(f"/api/w/{wid}/watchdog")
                if not result["busy"]:
                    break
                time.sleep(.05)
            self.assertEqual(result["messages"][-1]["body"], "noop")
            self.assertEqual(result["selection"]["model"], "claude-sonnet-5")
            self.assertEqual(len(self.srv.call(f"/api/w/{wid}/watchdog?before=2")["messages"]), 1)
            self.assertFalse(self.store_of(wid).is_running())

    def test_office_advances_without_viewers_and_restores_on_server_restart(self) -> None:
        from huntun.config import HuntunConfig, save_config, save_team
        from huntun.types import AgentSpec

        save_config(self.project, HuntunConfig("Office", backend="codex"))
        save_team(self.project, [AgentSpec("dev", "backend", "Dev", "Build", backend="codex")])
        wid = self.srv.call("/api/workspaces", {"path": str(self.project)})["id"]
        path = f"/api/w/{wid}/office"
        first = self.srv.call(path)
        # Only inspect persisted checkpoints while no browser is polling.
        deadline = time.monotonic() + 5
        saved = first
        while time.monotonic() < deadline:
            saved = json.loads(self.store_of(wid).get_control("office_scene", "{}"))
            if saved.get("revision", 0) > first["revision"]:
                break
            time.sleep(.05)
        self.assertGreater(saved["revision"], first["revision"])
        scene = self.srv.call(path + "/theme", {"theme": "chinese_tech", "initialize": True})
        self.assertEqual(scene["requested_theme"], "chinese_tech")
        self.assertEqual(self.srv.call(path + "/theme", {"theme": "regular", "initialize": True})["requested_theme"], "chinese_tech")
        with self.assertRaises(urllib.error.HTTPError) as cm:
            self.srv.call(path + "/theme", {"theme": "invalid"})
        self.assertEqual(cm.exception.code, 400)
        self.srv.close()
        self.srv = HubServer(self.home)
        restored = self.srv.call(path)
        self.assertEqual(restored["t0"], scene["t0"])
        self.assertGreaterEqual(restored["revision"], scene["revision"])
        self.assertEqual(restored["requested_theme"], "chinese_tech")
        with urllib.request.urlopen(self.srv.base + "/office.js") as response:
            self.assertIn("text/javascript", response.headers["content-type"])
            self.assertIn(b"function makeOffice", response.read())

    def test_describe_workspace_sees_existing_files(self) -> None:
        desc = describe_workspace(self.project)
        self.assertIn("src/main.py", desc)
        self.assertIn("A sample project.", desc)
        self.assertEqual(describe_workspace(Path(self.tmp.name) / "home"), "")

    def test_long_threads_use_bounded_http_pages(self) -> None:
        from huntun.config import default_config, save_config
        save_config(self.project, default_config("paging check"))
        wid = self.srv.call("/api/workspaces", {"path": str(self.project)})["id"]
        store = self.store_of(wid)
        tid = store.create_thread("human", "Long thread", "opening")["id"]
        ids = [store.add_comment(tid, "dev", "long reply " * 100)["id"] for _ in range(230)]
        path = f"/api/w/{wid}/threads/{tid}"
        latest = self.srv.call(path)
        self.assertEqual([c["id"] for c in latest["comments"]], ids[-100:])
        self.assertTrue(latest["has_more"])
        older = self.srv.call(path + f"?before={latest['first_id']}&limit=20&include_thread=0")
        self.assertIsNone(older["thread"])
        self.assertEqual([c["id"] for c in older["comments"]], ids[110:130])
        empty = self.srv.call(path + f"?after={ids[-1]}&include_thread=0")
        self.assertEqual(empty["comments"], [])
        self.assertEqual(empty["last_id"], ids[-1])
        for query in ("?after=no", "?after=-1", "?before=1&after=2", "?limit=0"):
            with self.assertRaises(urllib.error.HTTPError) as cm:
                self.srv.call(path + query)
            self.assertEqual(cm.exception.code, 400)

    def test_browse(self) -> None:
        info = browse(str(Path(self.tmp.name)))
        self.assertIn("myapp", info["dirs"])
        self.assertFalse(browse(str(self.project / "nope"))["exists"])

    def test_attention_http_full_requests_scoped_replies_and_pagination(self) -> None:
        from huntun.config import default_config, save_config
        save_config(self.project, default_config("attention check"))
        wid = self.srv.call("/api/workspaces", {"path": str(self.project)})["id"]
        store = self.store_of(wid)
        tid = store.create_thread("human", "Need decisions", "opening")["id"]
        body = "@human " + "The full scope of the requested decision. " * 100 + "Should we hire one QA?"
        ask = store.add_comment(tid, "qa-1", body, requires_confirmation=True)
        store.add_comment(tid, "dev-1", "@human Which hostname should we use?")
        path = f"/api/w/{wid}/attention"
        data = self.srv.call(path + "?limit=1")
        self.assertEqual((data["count"], len(data["pending_ids"]), len(data["items"])), (2, 2, 1))
        older = self.srv.call(path + f"?before={data['items'][0]['id']}&limit=1")
        item = older["items"][0]
        self.assertEqual(item["comment_id"], ask["id"])
        self.assertEqual(item["text"], body)
        for query in ("?before=-1", "?limit=0", "?limit=x"):
            with self.assertRaises(urllib.error.HTTPError) as cm:
                self.srv.call(path + query)
            self.assertEqual(cm.exception.code, 400)
        with self.assertRaises(urllib.error.HTTPError) as cm:
            self.srv.call(path + f"/{item['id']}/reply", {"body": " "})
        self.assertEqual(cm.exception.code, 400)
        reply = self.srv.call(path + f"/{item['id']}/reply", {"body": "Approved, hire one QA.", "thread_id": 9999})
        self.assertEqual((reply["author"], reply["thread_id"], reply["body"]),
                         ("human", tid, "@qa-1 Approved, hire one QA."))
        self.assertGreater(store.peek_inbox_count("qa-1"), 0)
        self.assertEqual(self.srv.call(path)["count"], 1)
        self.assertEqual(store.confirmation_status(tid, "qa-1"), (True, True))
        with self.assertRaises(urllib.error.HTTPError) as cm:
            self.srv.call(path + f"/{item['id']}/reply", {"body": "Again"})
        self.assertEqual(cm.exception.code, 409)
        remaining = self.srv.call(path)["items"][0]
        self.srv.call(path + f"/{remaining['id']}/resolve", {})
        self.assertEqual(self.srv.call(path)["count"], 0)

    def test_attention_inline_setup_reply_revises_goal_and_plan(self) -> None:
        wid = self.srv.call("/api/workspaces", {"path": str(self.project)})["id"]
        self.srv.call(f"/api/workspaces/{wid}/init", {"goal": "Add a CLI", "max_agents": 2})
        self.wait_state(wid, ("goal_proposed",))
        path = f"/api/w/{wid}/attention"
        item = self.srv.call(path)["items"][0]
        self.srv.call(path + f"/{item['id']}/reply", {"body": "Yes, add a JSON flag."})
        self.wait_state(wid, ("goal_proposed",))
        self.assertIn("Yes, add a JSON flag.", FakeBackend.goal_prompts[-1])
        replies = self.store_of(wid).get_comments(item["thread_id"])
        self.assertEqual(sum(c["author"] == "human" for c in replies), 1)
        self.assertEqual(replies[0]["body"], "@master Yes, add a JSON flag.")
        self.srv.call(f"/api/workspaces/{wid}/confirm-goal",
                      {"goal": "Add a CLI", "definition_of_done": "CLI with a JSON flag"})
        self.wait_state(wid, ("proposed",))
        item = self.srv.call(path)["items"][0]
        self.srv.call(path + f"/{item['id']}/reply", {"body": "Drop the writer."})
        self.wait_state(wid, ("proposed",))
        self.assertIn("Drop the writer.", FakeBackend.last_prompt)
        replies = self.store_of(wid).get_comments(item["thread_id"])
        self.assertEqual(sum(c["author"] == "human" for c in replies), 1)
        self.assertFalse(self.srv.call(f"/api/workspaces/{wid}")["approved"],
                         "replying or resolving is not automatic kickoff approval")

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
        w = self.srv.call(f"/api/workspaces/{wid}/init", {"goal": "Add a CLI to myapp", "context": "keep it small", "max_agents": 2})
        self.assertEqual(w["state"], "clarifying")
        w = self.wait_state(wid, ("goal_proposed", "error"))
        self.assertEqual(Path(FakeBackend.cwds[-1]).resolve(), self.project.resolve(), "the master's goal check runs in the project directory, not the server's")
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
        self.assertIn("at most 2 agents", FakeBackend.last_prompt)
        self.assertEqual(w["agents"], 3, "master + 2: the fake plan's third agent was trimmed to the cap")
        self.assertIn("src/main.py", FakeBackend.last_prompt)
        self.assertIn("keep it small", FakeBackend.last_prompt)
        self.assertIn("claude-haiku-4-5", FakeBackend.last_prompt, "model catalog is offered to the master")
        self.assertEqual((w["agents"], w["running"], w["initialized"], w["approved"]), (3, False, True, False))
        self.assertTrue((self.project / ".huntun" / "team.json").exists())
        self.assertTrue((self.project / ".git").exists())
        self.assertIn(".huntun/", (self.project / ".gitignore").read_text())

        # The proposal is on the board, tagging the human; per-agent model and effort are recorded
        st = self.srv.call(f"/api/w/{wid}/state")
        self.assertFalse(st["approved"])
        self.assertEqual((st["limits"]["paused"], st["limits"]["pause_count"]), (False, 0))
        self.assertEqual({k:st["totals"][k] for k in ("tokens", "cost_usd")}, {"tokens": 0, "cost_usd": 0})
        self.assertFalse(st["totals"]["usage_cost"]["complete"])
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
        self.assertEqual(est["tokens"], 8 * 120000 + 4 * 200000 + 10 * 300000)
        self.assertEqual(st["max_agents"], 2)
        self.assertGreaterEqual(st["attention_count"], 1, "the plan tagged @human, so it is on the attention list")
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
                                                                          {"name": "team-lead", "personality_preset": "skeptic", "personality": "ignored when a preset is chosen"}]})
        self.assertEqual((w["state"], w["approved"], w["running"]), ("ready", True, False))
        st = self.srv.call(f"/api/w/{wid}/state")
        self.assertTrue(st["approved"])
        self.assertEqual([a["name"] for a in st["agents"]], ["master", "team-lead", "dev-1"])
        self.assertEqual(st["attention_count"], 0, "approving (a human reply on the plan thread) clears the item")
        att = self.srv.call(f"/api/w/{wid}/attention")
        self.assertEqual(att["items"], [])
        dev = [a for a in st["agents"] if a["name"] == "dev-1"][0]
        self.assertEqual((dev["model"], dev["effort"], dev["info"]["model"], dev["info"]["effort"]), ("claude-opus-5", "xhigh", "claude-opus-5", "xhigh"))
        self.assertIn(dev["backend"], ("api", "claude-code"))
        self.assertEqual(st["estimate"]["tokens"], 8 * 120000 + 4 * 200000 + 10 * 300000)
        self.assertEqual((dev["personality"], dev["personality_preset"]), ("Grumpy but brilliant.", "custom"))
        lead = [a for a in st["agents"] if a["name"] == "team-lead"][0]
        self.assertEqual(lead["personality_preset"], "skeptic")
        self.assertTrue(lead["personality"].startswith("Dry and skeptical"))
        self.assertIn("personality -> custom", plan_comment := self.srv.call(f"/api/w/{wid}/threads/{st['plan_thread_id']}")["comments"][-1]["body"])
        self.assertIn("personality -> skeptic", plan_comment)
        self.assertEqual((st["agents"][0]["info"]["cycles"], st["agents"][0]["info"]["compactions"]), (0, 0))
        plan = self.srv.call(f"/api/w/{wid}/threads/{st['plan_thread_id']}")
        self.assertIn("approved", plan["comments"][-1]["body"].lower())
        self.assertIn("personality -> custom", plan["comments"][-1]["body"])

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

    def test_the_master_can_lead_without_a_team_lead(self) -> None:
        from huntun.config import load_config

        wid = self.srv.call("/api/workspaces", {"path": str(self.project)})["id"]
        self.srv.call(f"/api/workspaces/{wid}/init", {"goal": "Add a CLI to myapp", "team_lead": False})
        self.assertEqual(self.wait_state(wid, ("goal_proposed", "error"))["state"], "goal_proposed")
        self.srv.call(f"/api/workspaces/{wid}/confirm-goal", {"goal": "Add a CLI to myapp", "definition_of_done": "CLI runs"})
        w = self.wait_state(wid, ("proposed", "error"))
        self.assertEqual(w["state"], "proposed", w.get("error"))
        self.assertFalse(load_config(self.project).team_lead)
        self.assertIn('Do not include a "team-lead"', FakeBackend.last_prompt)
        st = self.srv.call(f"/api/w/{wid}/state")
        self.assertEqual([a["role"] for a in st["agents"]], ["master", "fullstack", "tech-writer"])   # the proposed lead was dropped
        w = self.srv.call(f"/api/workspaces/{wid}/approve", {"agents": []})
        self.assertEqual((w["state"], w["approved"]), ("ready", True))

    def test_the_human_picks_the_masters_model_and_the_team_limit_at_any_time(self) -> None:
        from huntun.config import load_config

        models = self.srv.call("/api/models")
        self.assertIn("claude-opus-5", [m["id"] for m in models["models"]])
        self.assertIn("api", models["backends"])
        wid = self.srv.call("/api/workspaces", {"path": str(self.project)})["id"]
        with self.assertRaises(urllib.error.HTTPError) as cm:
            self.srv.call(f"/api/workspaces/{wid}/init", {"goal": "Add a CLI to myapp", "master_model": "no-such-model"})
        self.assertEqual(cm.exception.code, 400)

        # The master's model runs the goal check and the planning, and the master's seat
        FakeBackend.models.clear()
        self.srv.call(f"/api/workspaces/{wid}/init", {"goal": "Add a CLI to myapp", "master_model": "claude-sonnet-5"})
        self.assertEqual(self.wait_state(wid, ("goal_proposed", "error"))["state"], "goal_proposed")
        self.srv.call(f"/api/workspaces/{wid}/confirm-goal", {"goal": "Add a CLI to myapp", "definition_of_done": "CLI runs"})
        self.assertEqual(self.wait_state(wid, ("proposed", "error"))["state"], "proposed")
        self.assertEqual(FakeBackend.models, ["claude-sonnet-5", "claude-sonnet-5"])
        st = self.srv.call(f"/api/w/{wid}/state")
        master = st["agents"][0]
        self.assertEqual((master["role"], master["model"]), ("master", "claude-sonnet-5"))
        self.assertEqual(load_config(self.project).master_model, "claude-sonnet-5")

        # On the plan: a limit below the proposed team is refused; the master's model and effort can be changed
        with self.assertRaises(urllib.error.HTTPError) as cm:
            self.srv.call(f"/api/workspaces/{wid}/approve", {"agents": [], "max_agents": 2})
        self.assertEqual(cm.exception.code, 409)
        self.assertIn("remove agents or raise the limit", json.loads(cm.exception.read())["error"])
        w = self.srv.call(f"/api/workspaces/{wid}/approve", {"max_agents": 2, "agents": [{"name": "docs-1", "remove": True},
                                                                                       {"name": "master", "model": "claude-opus-5", "effort": "max", "remove": True}]})
        self.assertTrue(w["approved"])
        st = self.srv.call(f"/api/w/{wid}/state")
        master = st["agents"][0]
        self.assertEqual((master["name"], master["model"], master["effort"], master["info"]["model"]), ("master", "claude-opus-5", "max", "claude-opus-5"))
        self.assertEqual((st["max_agents"], load_config(self.project).master_model), (2, "claude-opus-5"))
        self.assertIn("@master model -> claude-opus-5", self.srv.call(f"/api/w/{wid}/threads/{st['plan_thread_id']}")["comments"][-1]["body"])

        # While the team runs: change any agent's model (back to the default too) and the team limit
        self.srv.call(f"/api/w/{wid}/agents/master/model", {"model": "claude-sonnet-5", "effort": "xhigh"})
        self.srv.call(f"/api/w/{wid}/agents/dev-1/model", {"model": "default", "effort": "low"})
        with self.assertRaises(urllib.error.HTTPError) as cm:
            self.srv.call(f"/api/w/{wid}/agents/dev-1/model", {"model": "no-such-model"})
        self.assertEqual(cm.exception.code, 409)
        by_name = {a["name"]: a for a in self.srv.call(f"/api/w/{wid}/state")["agents"]}
        self.assertEqual((by_name["master"]["model"], by_name["master"]["effort"]), ("claude-sonnet-5", "xhigh"))
        self.assertEqual((by_name["dev-1"]["model"], by_name["dev-1"]["effort"]), (None, "low"))
        self.assertEqual(load_config(self.project).master_model, "claude-sonnet-5")

        self.assertEqual(self.srv.call(f"/api/workspaces/{wid}/max-agents", {"max_agents": 1})["max_agents"], 1)
        st = self.srv.call(f"/api/w/{wid}/state")
        self.assertEqual((st["max_agents"], load_config(self.project).max_agents), (1, 1))
        note = self.srv.call(f"/api/w/{wid}/threads/{st['plan_thread_id']}")["comments"][-1]
        self.assertEqual(note["author"], "human")
        self.assertIn("@master The team size limit is now 1 agent besides the master (was 2); the team has 2.", note["body"])
        self.assertIn("nobody is retired automatically", note["body"])
        self.assertEqual(len([a for a in st["agents"] if a["status"] != "retired"]), 3, "lowering the limit retires nobody")
        orch = self.srv.hub.get(wid).orchestrator
        refused = self.srv.hub.call(orch.hire({"name": "qa-1", "role": "qa", "brief": "test it"}))
        self.assertIn("capped the team at 1 agents", refused)
        self.srv.call(f"/api/workspaces/{wid}/max-agents", {"max_agents": 0})
        self.assertIn("no limit (was 1)", self.srv.call(f"/api/w/{wid}/threads/{st['plan_thread_id']}")["comments"][-1]["body"])

    def test_existing_project_task_api_migrates_dod_and_persists_progress(self) -> None:
        from huntun.config import HuntunConfig, save_config, save_team
        from huntun.types import AgentSpec
        save_config(self.project, HuntunConfig("Build", backend="api", definition_of_done="- API runs\n- Tests pass"))
        save_team(self.project, [AgentSpec("master", "master", "Master", "Lead"), AgentSpec("dev", "backend", "Dev", "Build")])
        wid = self.srv.call("/api/workspaces", {"path": str(self.project)})["id"]
        base = f"/api/w/{wid}/tasks"
        self.assertEqual(len(self.store_of(wid).list_tasks()), 2, "opening an existing project migrates DoD before visiting its task page")
        parents = self.srv.call(base)["tasks"]
        self.assertEqual(len(parents), 2)
        card = self.srv.call(base, {"title":"API handler", "acceptance":"GET returns 200", "owner":"dev", "parent_id":parents[0]["id"]})
        self.assertEqual(card["status"], "backlog")
        self.assertTrue(card["thread_id"])
        self.srv.call(base + f"/{card['id']}", {"status":"in_process"})
        for body in ({"status":"done"}, {"owner":"missing"}, {"status":"fake"}):
            with self.assertRaises(urllib.error.HTTPError) as cm:
                self.srv.call(base + f"/{card['id']}", body)
            self.assertEqual(cm.exception.code, 400)
        self.srv.call(base + f"/{card['id']}", {"status":"done", "evidence":"abc123; 4 tests passed"})
        self.assertEqual(len(self.srv.call(base)["tasks"]), 3)
        self.assertEqual(self.srv.call(base)["tasks"][-1]["status"], "done")

    def test_performance_http_imports_existing_project_and_rejects_bad_ranges(self) -> None:
        from datetime import datetime

        from huntun.config import (
            HuntunConfig,
            agents_dir,
            now_iso,
            save_config,
            save_team,
        )
        from huntun.types import AgentSpec
        save_config(self.project,HuntunConfig("Build",backend="api"))
        save_team(self.project,[AgentSpec("dev","backend","Developer","Build")])
        folder=agents_dir(self.project)/"dev"
        folder.mkdir(parents=True,exist_ok=True)
        (folder/"journal.jsonl").write_text(json.dumps({"at":now_iso(),"usage":{"input":100,"output":25},"commits":[]})+"\n")
        wid=self.srv.call("/api/workspaces",{"path":str(self.project)})["id"]
        self.store_of(wid).create_thread("dev","Progress","Actual update")
        result=self.srv.call(f"/api/w/{wid}/performance?range=24h")
        agent=next(a for a in result["agents"] if a["name"]=="dev")
        self.assertEqual(agent["totals"]["tokens"],125)
        self.assertEqual(agent["totals"]["messages"],1)
        self.assertIn(len(result["buckets"]),(24,25))
        rebinned=self.srv.call(f"/api/w/{wid}/performance?range=24h&bin=15m")
        self.assertEqual(rebinned["bucket_seconds"],900)
        self.assertLessEqual(abs(datetime.fromisoformat(rebinned["window_start"]).timestamp()-datetime.fromisoformat(result["window_start"]).timestamp()),1)
        self.assertEqual(rebinned["agents"][0]["totals"],result["agents"][0]["totals"])
        for asset, mime in (("performance.js","text/javascript"),("performance.css","text/css")):
            with urllib.request.urlopen(self.srv.base+"/"+asset) as response:
                self.assertIn(mime,response.headers["content-type"])
                self.assertGreater(len(response.read()),100)
        with self.assertRaises(urllib.error.HTTPError) as cm:
            self.srv.call(f"/api/w/{wid}/performance?range=30d&bin=1m")
        self.assertEqual(cm.exception.code,400)
        with self.assertRaises(urllib.error.HTTPError) as cm:
            self.srv.call(f"/api/w/{wid}/performance?range=bad")
        self.assertEqual(cm.exception.code,400)

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


    # ---- requests other web pages could make through the user's browser -------------------------------------------

    def raw(self, path: str, body: str | None = None, headers: dict[str, str] | None = None) -> tuple[int, Any]:
        """One request with exactly these headers (urllib adds Host, and a form content type for a body without one)."""
        req = urllib.request.Request(self.srv.base + path, data=body.encode() if body is not None else None,
                                     method="POST" if body is not None else "GET", headers=headers or {})
        try:
            with urllib.request.urlopen(req) as r:
                status, raw = r.status, r.read().decode()
        except urllib.error.HTTPError as e:
            status, raw = e.code, e.read().decode()
        try:
            return status, json.loads(raw)
        except ValueError:
            return status, raw

    def test_writes_from_other_sites_are_refused(self) -> None:
        body = json.dumps({"path": str(self.project)})
        port = self.srv.server.server_address[1]
        json_ct = {"content-type": "application/json"}
        attacks = {
            "text/plain, sent by any page without a preflight": ({"content-type": "text/plain"}, 415),
            "a form post": ({"content-type": "application/x-www-form-urlencoded"}, 415),
            "no content type": ({}, 415),
            "a foreign Origin": ({**json_ct, "origin": "https://evil.example"}, 403),
            "an opaque origin (sandboxed frame, file://)": ({**json_ct, "origin": "null"}, 403),
            "a page on another port of this machine": ({**json_ct, "origin": "http://127.0.0.1:8080"}, 403),
            "the browser flagging it cross-site": ({**json_ct, "sec-fetch-site": "cross-site"}, 403),
            "DNS rebinding (the page's own name, resolved to 127.0.0.1)": ({**json_ct, "host": f"evil.example:{port}", "origin": f"http://evil.example:{port}"}, 403),
        }
        for what, (headers, status) in attacks.items():
            with self.subTest(what):
                code, res = self.raw("/api/workspaces", body, headers)
                self.assertEqual(code, status, res)
        self.assertEqual(self.srv.call("/api/workspaces")["workspaces"], [], "none of them registered the project")

        # the page itself: same-origin JSON, whether opened as 127.0.0.1 or localhost
        code, w = self.raw("/api/workspaces", body, {**json_ct, "origin": f"http://127.0.0.1:{port}", "sec-fetch-site": "same-origin"})
        self.assertEqual(code, 201, w)
        code, res = self.raw(f"/api/workspaces/{w['id']}/init", json.dumps({"goal": "  "}),
                             {**json_ct, "host": f"localhost:{port}", "origin": f"http://localhost:{port}", "sec-fetch-site": "same-origin"})
        self.assertEqual(code, 400, res)                                                # reached the handler: the goal is empty
        # scripts and the documented curl calls: JSON without browser headers
        self.assertEqual(self.srv.call(f"/api/workspaces/{w['id']}/forget", {}), {"ok": True})

    def test_reads_from_other_sites_are_refused_but_the_page_opens_from_anywhere(self) -> None:
        port = self.srv.server.server_address[1]
        self.assertEqual(self.raw("/api/workspaces", headers={"sec-fetch-site": "cross-site"})[0], 403)   # e.g. <img src=...>
        self.assertEqual(self.raw("/api/workspaces", headers={"host": f"evil.example:{port}"})[0], 403)    # DNS rebinding
        self.assertEqual(self.raw("/api/workspaces", headers={"sec-fetch-site": "same-origin"})[0], 200)
        code, page = self.raw("/", headers={"sec-fetch-site": "cross-site"})                               # following a link
        self.assertEqual(code, 200)
        self.assertIn("<html", page.lower())

    def test_allowed_hosts_admit_a_proxy_name(self) -> None:
        port = self.srv.server.server_address[1]
        headers = {"host": f"huntun.lan:{port}", "origin": f"http://huntun.lan:{port}", "content-type": "application/json"}
        self.assertEqual(self.raw("/api/workspaces", json.dumps({"path": str(self.project)}), headers)[0], 403)
        os.environ["HUNTUN_ALLOWED_HOSTS"] = "huntun.lan"
        self.addCleanup(os.environ.pop, "HUNTUN_ALLOWED_HOSTS", None)
        self.assertEqual(self.raw("/api/workspaces", json.dumps({"path": str(self.project)}), headers)[0], 201)


if __name__ == "__main__":
    unittest.main()
