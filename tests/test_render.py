"""Headless render checks of app.html on every page state, driven through the real web flow with a fake model.
Needs node and jsdom (set HUNTUN_JSDOM=/path/to/node_modules or install jsdom globally); skipped otherwise."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

try:
    from test_hub import HubTests  # when discovered with -s tests
except ImportError:  # pragma: no cover
    from tests.test_hub import HubTests

HARNESS = Path(__file__).parent / "js" / "render_check.mjs"


def jsdom_path() -> str | None:
    cand = os.environ.get("HUNTUN_JSDOM")
    if cand and (Path(cand) / "jsdom").exists():
        return cand
    try:
        root = subprocess.run(["npm", "root", "-g"], capture_output=True, text=True, timeout=20).stdout.strip()
        if root and (Path(root) / "jsdom").exists():
            return root
    except (OSError, subprocess.SubprocessError):
        pass
    return None


@unittest.skipUnless(shutil.which("node") and jsdom_path(), "node + jsdom not available")
class RenderTests(HubTests):
    """Reuses the hub test fixture (fake backend, HTTP server) to produce real page states, then renders each."""

    def render(self, route: str, responses: dict, office: bool = False, lang: str = "") -> dict:
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump(responses, f)
        try:
            env = {**os.environ, "NODE_PATH": jsdom_path() or "", "HUNTUN_RENDER_OFFICE": "1" if office else "", "HUNTUN_RENDER_LANG": lang}
            proc = subprocess.run(["node", str(HARNESS), route, f.name], capture_output=True, text=True, timeout=60, env=env)
        finally:
            os.unlink(f.name)
        try:
            out = json.loads(proc.stdout.strip().splitlines()[-1])
        except Exception:
            self.fail(f"harness produced no result for {route}: {proc.stdout[-500:]} {proc.stderr[-800:]}")
        self.assertEqual(out["errors"], [], f"{route}: {out['errors']}")
        self.assertGreater(out["mainChars"], 40, f"{route} rendered empty: {out}")
        return out

    def snapshot(self, wid: str) -> dict:
        st = self.srv.call(f"/api/w/{wid}/state")
        resp = {"/api/workspaces": self.srv.call("/api/workspaces"), f"/api/workspaces/{wid}": self.srv.call(f"/api/workspaces/{wid}"), f"/api/w/{wid}/state": st,
                "/api/fs": {"path": "/", "exists": True, "parent": None, "dirs": ["a"], "files": 0, "initialized": False, "git": False, "home": "/"}}
        for t in st["threads"]:
            resp[f"/api/w/{wid}/threads/{t['id']}"] = self.srv.call(f"/api/w/{wid}/threads/{t['id']}")
        for a in st["agents"]:
            resp[f"/api/w/{wid}/agents/{a['name']}/activity"] = {"agent": a["info"] or {"model": "m", "effort": "high", "backend": "api", "cycles": 0, "usage": {}}, "live": a["live"], "entries": [], "cursor": 0, "notes": "", "journal": []}
        return resp

    def test_every_page_state_renders(self) -> None:
        home = {"/api/workspaces": {"workspaces": [], "home": "/"}, "/api/fs": {"path": "/", "exists": True, "parent": None, "dirs": [], "files": 0, "initialized": False, "git": False, "home": "/"}}
        out = self.render("#/", home)                                                   # the model servers live in a dialog off the header
        self.assertNotIn("Server address", out["text"])
        self.assertIn("Model providers", out["header"])
        types = {"llamacpp": "llama.cpp server", "ollama": "Ollama server", "vllm": "vLLM / OpenAI-compatible server"}
        served = {"ok": True, "models": ["qwen3.8-flash-next-uncensored"], "context": 262144, "slots": 2, "error": ""}
        box = {"id": "a1", "type": "llamacpp", "name": "4090 box", "label": "4090 box", "url": "http://10.0.0.72:8080", "has_key": True, "note": "Flash-Next", "context": 0,
               "source": "saved", "pinned": False, "status": served, "models": ["qwen3.8-flash-next-uncensored"]}
        env = {**box, "id": "env-ollama", "type": "ollama", "name": "", "label": "127.0.0.1:11434", "url": "http://127.0.0.1:11434", "has_key": False, "note": "", "source": "default",
               "status": {"ok": False, "models": [], "context": 0, "slots": 0, "error": "cannot reach http://127.0.0.1:11434: refused"}}
        router = {**box, "id": "a2", "name": "4090 router", "label": "4090 router", "url": "http://10.0.0.73:8080", "models": ["qwen3.8-27b-uncensored", "qwen3.8-flash-next-uncensored"],
                  "status": {"ok": True, "models": ["qwen3.8-27b-uncensored", "qwen3.8-flash-next-uncensored"], "context": 0, "slots": 0, "error": "", "max_loaded": 1,
                             "details": {"qwen3.8-27b-uncensored": {"context": 65536, "slots": 3, "state": "unloaded", "pool": 196608},
                                         "qwen3.8-flash-next-uncensored": {"context": 262144, "slots": 2, "state": "loaded"}}}}
        out = self.render("#/providers", {**home, "/api/providers": {"servers": [box, router, env], "types": types, "backends": {"llamacpp": "llama.cpp server with a model"}}})
        dlg = out["dialog"]
        for text in ("4090 box", "qwen3.8-flash-next-uncensored", "256K context", "2 sessions at once", "API key *****", "Edit ›", "found locally", "cannot reach", "+ Add a server",
                     "qwen3.8-27b-uncensored", "64K context", "3 sessions at once", "192K pool shared", "loaded now", "One model loaded at a time"):
            self.assertIn(text, dlg)
        out = self.render("#/providers", {**home, "/api/providers": {"servers": [], "types": types, "backends": {}}})
        self.assertIn("No model servers yet", out["dialog"])
        w = self.srv.call("/api/workspaces", {"path": str(self.project)})
        wid = w["id"]
        out = self.render(f"#/w/{wid}/setup", {"/api/workspaces": {"workspaces": [w], "home": "/"}, f"/api/workspaces/{wid}": w, "/api/models": self.srv.call("/api/models")})
        self.assertIn("The master's model", out["text"])
        self.assertIn("auto: the backend's default model", out["text"])
        # goal check proposed
        self.srv.call(f"/api/workspaces/{wid}/init", {"goal": "Add a CLI to myapp", "context": ""})
        self.wait_state(wid, ("goal_proposed", "error"))
        out = self.render(f"#/w/{wid}", self.snapshot(wid))
        self.assertIn("Goal check", out["text"])
        # plan proposed (with estimates and personalities): the approval page
        self.srv.call(f"/api/workspaces/{wid}/confirm-goal", {"goal": "Add a CLI to myapp", "definition_of_done": "CLI runs"})
        self.wait_state(wid, ("proposed", "error"))
        out = self.render(f"#/w/{wid}", self.snapshot(wid))
        self.assertIn("awaiting approval", out["status"])
        self.assertIn("Estimate to finish", out["text"])
        self.assertIn("Plans and steers the team", out["text"], "the master's own seat is on the plan, with its model")
        self.assertIn("Team size limit (besides the master):", out["text"])
        self.assertIn("Personality", out["text"])
        # approved: the three-column board, a thread expanded, and the agent dialog
        self.srv.call(f"/api/workspaces/{wid}/approve", {"agents": []})
        snap = self.snapshot(wid)
        st = snap[f"/api/w/{wid}/state"]
        out = self.render(f"#/w/{wid}", snap)
        self.assertIn("paused", out["status"])
        self.assertIn("Needs you", out["header"])
        self.assertIn("Team size limit", out["text"], "the team limit can be changed while the team runs")
        self.render(f"#/w/{wid}/t/{st['threads'][0]['id']}", snap)
        out = self.render(f"#/w/{wid}", snap, office=True)
        self.assertIn("Threads", out["text"], "office view toggled on shows the switch-back button")

    # the inherited hub tests already run in test_hub; skip them here
    def test_interface_language_switch(self) -> None:
        """The page chrome renders in the chosen language; agent content is untouched."""
        canned = {"/api/workspaces": {"workspaces": [], "home": "/"}, "/api/fs": {"path": "/", "exists": True, "parent": None, "dirs": [], "files": 0, "initialized": False}, "*": {}}
        en = self.render("#/", canned)
        self.assertIn("Open a project", en["text"])
        for lang, expect in (("zh-CN", "打开项目"), ("zh-TW", "開啟專案"), ("ja", "プロジェクトを開く")):
            out = self.render("#/", canned, lang=lang)
            self.assertEqual(out["lang"], lang)
            self.assertIn(expect, out["text"], f"{lang}: {out['text'][:300]}")
            self.assertNotIn("Open a project", out["text"])

    def test_web_flow_open_plan_run_pause(self) -> None:
        pass

    def test_init_requires_goal_and_reports_errors(self) -> None:
        pass

    def test_describe_workspace_sees_existing_files(self) -> None:
        pass

    def test_browse(self) -> None:
        pass

    def test_writes_from_other_sites_are_refused(self) -> None:
        pass

    def test_reads_from_other_sites_are_refused_but_the_page_opens_from_anywhere(self) -> None:
        pass

    def test_allowed_hosts_admit_a_proxy_name(self) -> None:
        pass

    def test_the_master_can_lead_without_a_team_lead(self) -> None:
        pass

    def test_the_human_picks_the_masters_model_and_the_team_limit_at_any_time(self) -> None:
        pass


if __name__ == "__main__":
    unittest.main()
