"""The hub web server: project picker and setup, plus the per-project discussion board.

Runs in a background thread; anything that touches agents is handed to the hub's asyncio loop.
"""
from __future__ import annotations

import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from .hub import Hub, browse
from .models import BACKEND_LABEL, available_backends, catalog_available, catalog_for
from .personalities import PERSONALITIES

PAGE = (Path(__file__).parent / "app.html").read_text()
WS_ROUTE = re.compile(r"^/api/w/([0-9a-f]{10})(/.*)?$")
WORKSPACE_ROUTE = re.compile(r"^/api/workspaces/([0-9a-f]{10})(/.*)?$")


def _totals(orch: Any, path: Path) -> dict[str, float]:
    """Tokens and dollars across every agent, from the live runtimes or the state files on disk."""
    from .config import agents_dir
    from .memory import AgentMemory

    tokens = cost = 0.0
    for a in orch.team:
        st = orch.runtimes[a.name].memory.state if a.name in orch.runtimes else AgentMemory(agents_dir(path), a.name).state
        u = st.usage_totals
        tokens += float(u.get("input", 0)) + float(u.get("output", 0)) + float(u.get("cache_read", 0))
        cost += float(u.get("cost_usd", 0))
    return {"tokens": round(tokens), "cost_usd": round(cost, 4)}


def _limits_from(store: Any) -> dict[str, Any]:
    g = store.get_control
    return {"paused": False, "backends": {}, "since": None, "resets_at": None, "reason": None, "pause_count": int(g("limit_pause_count", "0") or 0),
            "resume_count": int(g("limit_resume_count", "0") or 0), "gate": g("resume_gate", "") or None, "next_probe_at": None, "last_probe": None, "auto": False}


class HttpError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


def start_server(hub: Hub, port: int) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args: Any) -> None:  # keep the console for agent logs
            pass

        def _json(self, status: int, body: Any) -> None:
            data = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("content-type", "application/json; charset=utf-8")
            self.send_header("cache-control", "no-store")
            self.send_header("content-length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _body(self) -> dict[str, Any]:
            n = int(self.headers.get("content-length") or 0)
            raw = self.rfile.read(n) if n else b""
            return json.loads(raw) if raw else {}

        def _entry(self, wid: str):
            e = hub.get(wid)
            if e is None:
                raise HttpError(404, "unknown workspace")
            return e

        def _orch(self, wid: str):
            """The workspace's orchestrator, loading it (paused) on first use. Requires an approved plan."""
            e = self._entry(wid)
            if e.orchestrator is None:
                if e.state in ("planning", "clarifying"):
                    raise HttpError(409, "the master is still working on the plan")
                hub.call(hub.load(wid), timeout=120)
                e = self._entry(wid)
                if e.orchestrator is None:
                    raise HttpError(409, e.error or ("the plan has not been approved yet" if e.state == "proposed" else "workspace is not initialized"))
            return e.orchestrator

        def _board(self, wid: str):
            """Board data; loads the agents when the plan is approved, otherwise reads the store directly."""
            e = self._entry(wid)
            if e.orchestrator is None and e.approved() and e.state not in ("planning", "clarifying"):
                hub.call(hub.load(wid), timeout=120)
            try:
                return hub.board(wid)
            except ValueError as ex:
                raise HttpError(409, str(ex)) from None

        def do_GET(self) -> None:
            url = urlparse(self.path)
            path = url.path
            try:
                if path == "/":
                    data = PAGE.encode()
                    self.send_response(200)
                    self.send_header("content-type", "text/html; charset=utf-8")
                    self.send_header("content-length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                    return
                if path == "/api/workspaces":
                    return self._json(200, {"workspaces": hub.list(), "home": str(Path.home())})
                if path == "/api/fs":
                    q = parse_qs(url.query)
                    return self._json(200, browse((q.get("path") or [None])[0]))
                m = WORKSPACE_ROUTE.match(path)
                if m and not m.group(2):
                    return self._json(200, self._entry(m.group(1)).summary())
                m = WS_ROUTE.match(path)
                if m:
                    return self._board_get(m.group(1), m.group(2) or "")
                self._json(404, {"error": "not found"})
            except HttpError as e:
                self._json(e.status, {"error": str(e)})
            except Exception as e:
                self._json(500, {"error": f"{type(e).__name__}: {e}"})

        def _board_get(self, wid: str, sub: str) -> None:
            orch = self._board(wid)
            store = orch.store
            if sub == "/state":
                statuses = store.agent_statuses()
                entry = self._entry(wid)
                threads = store.list_threads(100)
                for t in threads:
                    t["snippet"] = t["body"][:400]
                    t["last_comments"] = [{"id": c["id"], "author": c["author"], "created_at": c["created_at"], "snippet": c["body"][:240]} for c in store.last_comments(t["id"], 2)]
                    del t["body"]
                return self._json(200, {
                    "workspace": entry.summary(),
                    "goal": orch.config.goal,
                    "backend": orch.backend_name,
                    "running": store.is_running(),
                    "approved": entry.approved(),
                    "goal_confirmed": orch.config.goal_confirmed,
                    "definition_of_done": orch.config.definition_of_done,
                    "goal_thread_id": int(store.get_control("goal_thread_id", "0") or 0) or None,
                    "plan_thread_id": int(store.get_control("plan_thread_id", "0") or 0) or None,
                    "totals": _totals(orch, entry.path),
                    "estimate": orch.config.estimate or None,
                    "max_agents": orch.config.max_agents,
                    "attention": store.open_attention(20),
                    "attention_count": store.attention_count(),
                    "models": [{"id": m.id, "backend": b, "vendor": m.vendor, "label": m.label} for m, b in (catalog_available() or [(m, orch.backend_name) for m in catalog_for(orch.backend_name)])],
                    "backends": {b: BACKEND_LABEL.get(b, b) for b in available_backends()},
                    "personalities": [{"id": p.id, "name": p.name, "text": p.text, "group": p.group} for p in PERSONALITIES],
                    "limits": entry.orchestrator.limits() if entry.orchestrator is not None else _limits_from(store),
                    "agents": [{**a.to_dict(), "live": statuses.get(a.name), "info": (orch.runtimes[a.name].info() if a.name in orch.runtimes else None)} for a in orch.team],
                    "threads": threads,
                    "events": store.list_events(0, 40),
                })
            if sub == "/attention":
                return self._json(200, {"items": store.open_attention(100), "count": store.attention_count()})
            am = re.match(r"^/agents/([a-z0-9-]+)/activity$", sub)
            if am:
                rt = orch.runtimes.get(am.group(1))
                if rt is None:
                    raise HttpError(404, "unknown agent")
                q = parse_qs(urlparse(self.path).query)
                after = int((q.get("after") or ["0"])[0])
                entries, cursor = rt.memory.read_activity(after)
                return self._json(200, {"agent": rt.info(), "live": store.agent_statuses().get(rt.name), "entries": entries, "cursor": cursor, "notes": rt.memory.notes(), "journal": rt.memory.recent_journal(8)})
            tm = re.match(r"^/threads/(\d+)$", sub)
            if tm:
                t = store.get_thread(int(tm.group(1)))
                if not t:
                    raise HttpError(404, "thread not found")
                return self._json(200, {"thread": t, "comments": store.get_comments(t["id"])})
            raise HttpError(404, "not found")

        def do_POST(self) -> None:
            path = urlparse(self.path).path
            try:
                body = self._body()
                if path == "/api/workspaces":
                    e = hub.add(str(body.get("path") or ""))
                    if e.orchestrator is None and e.state == "ready":
                        hub.call(hub.load(e.id), timeout=120)
                    return self._json(201, e.summary())
                m = WORKSPACE_ROUTE.match(path)
                if m:
                    return self._workspace_post(m.group(1), m.group(2) or "", body)
                m = WS_ROUTE.match(path)
                if m:
                    return self._board_post(m.group(1), m.group(2) or "", body)
                self._json(404, {"error": "not found"})
            except HttpError as e:
                self._json(e.status, {"error": str(e)})
            except ValueError as e:
                self._json(400, {"error": str(e)})
            except Exception as e:
                self._json(500, {"error": f"{type(e).__name__}: {e}"})

        def _workspace_post(self, wid: str, sub: str, body: dict[str, Any]) -> None:
            e = self._entry(wid)
            if sub == "/confirm-goal":
                try:
                    hub.call(hub.confirm_goal(wid, str(body.get("goal") or ""), str(body.get("definition_of_done") or "")), timeout=30)
                except ValueError as ex:
                    raise HttpError(409, str(ex)) from None
                return self._json(202, self._entry(wid).summary())
            if sub == "/goal-reply":
                try:
                    hub.call(hub.goal_reply(wid, str(body.get("message") or "")), timeout=0.5)
                except TimeoutError:
                    pass  # the master keeps revising in the background; the page polls
                except ValueError as ex:
                    raise HttpError(409, str(ex)) from None
                return self._json(202, self._entry(wid).summary())
            if sub == "/init":
                backend = body.get("backend") if body.get("backend") in ("api", "claude-code", "codex", "kimi", "deepseek", "ollama") else None
                try:
                    e = hub.begin_init(wid, str(body.get("goal") or ""), backend, str(body.get("context") or ""), int(body.get("max_agents") or 0))
                except ValueError as ex:
                    raise HttpError(409 if "progress" in str(ex) else 400, str(ex)) from None
                return self._json(202, e.summary())
            if sub == "/control":
                action = body.get("action")
                if action not in ("start", "pause"):
                    raise HttpError(400, "action must be start or pause")
                if not e.approved():
                    raise HttpError(409, "the plan has not been approved yet")
                self._orch(wid)
                running = hub.set_running(wid, action == "start")
                return self._json(200, {"running": running, "workspace": self._entry(wid).summary()})
            if sub == "/probe-limit":
                self._orch(wid)
                try:
                    return self._json(200, hub.call(hub.probe_limit(wid, body.get("backend") or None), timeout=300))
                except ValueError as ex:
                    raise HttpError(409, str(ex)) from None
            if sub == "/approve":
                try:
                    hub.call(hub.approve(wid, body.get("agents") or []), timeout=120)
                except ValueError as ex:
                    raise HttpError(409, str(ex)) from None
                return self._json(200, self._entry(wid).summary())
            if sub == "/replan":
                try:
                    hub.call(hub.replan(wid, str(body.get("feedback") or "")), timeout=0.5)
                except TimeoutError:
                    pass  # planning continues in the background; the page polls
                except ValueError as ex:
                    raise HttpError(409, str(ex)) from None
                return self._json(202, self._entry(wid).summary())
            if sub == "/close":
                hub.call(hub.close(wid), timeout=60)
                return self._json(200, self._entry(wid).summary())
            if sub == "/forget":
                hub.forget(wid)
                return self._json(200, {"ok": True})
            raise HttpError(404, "not found")

        def _board_post(self, wid: str, sub: str, body: dict[str, Any]) -> None:
            store = self._board(wid).store
            if sub == "/threads":
                title, text = str(body.get("title") or "").strip(), str(body.get("body") or "").strip()
                if not title or not text:
                    raise HttpError(400, "title and body are required")
                return self._json(201, store.create_thread("human", title, text))
            rm = re.match(r"^/attention/(\d+)/resolve$", sub)
            if rm:
                return self._json(200, {"ok": store.resolve_attention(int(rm.group(1)))})
            if sub == "/comments":
                text = str(body.get("body") or "").strip()
                tid = int(body.get("thread_id") or 0)
                if not text or not tid:
                    raise HttpError(400, "thread_id and body are required")
                e = self._entry(wid)
                if not e.approved() and e.state not in ("planning", "clarifying"):
                    # Before kickoff, replying on the goal or plan thread is how the human asks the master to revise.
                    goal_tid = int(store.get_control("goal_thread_id", "0") or 0)
                    plan_tid = int(store.get_control("plan_thread_id", "0") or 0)
                    try:
                        if tid == goal_tid and not e.summary().get("goal_confirmed"):
                            hub.call(hub.goal_reply(wid, text), timeout=0.5)
                            return self._json(202, {"revising": "goal", "thread_id": tid})
                        if tid == plan_tid and e.summary().get("goal_confirmed"):
                            hub.call(hub.replan(wid, text), timeout=0.5)
                            return self._json(202, {"revising": "plan", "thread_id": tid})
                    except TimeoutError:
                        return self._json(202, {"revising": "goal" if tid == goal_tid else "plan", "thread_id": tid})
                    except ValueError as ex:
                        raise HttpError(409, str(ex)) from None
                return self._json(201, store.add_comment(tid, "human", text))
            raise HttpError(404, "not found")

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, name="huntun-web", daemon=True).start()
    return server
