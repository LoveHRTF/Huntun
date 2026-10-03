"""The hub web server: project picker and setup, plus the per-project discussion board.

Runs in a background thread; anything that touches agents is handed to the hub's asyncio loop.
"""
from __future__ import annotations

import json
import os
import re
import threading
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from . import auth
from .hub import Hub, browse
from .models import (
    BACKEND_LABEL,
    ROUTES,
    SERVER_STATUS,
    SERVER_TYPES,
    available_backends,
    catalog_available,
    catalog_for,
    delete_server,
    probe_server,
    refresh_local,
    save_server,
    saved_servers,
    server_label,
    servers,
)
from .office import SOURCE as OFFICE_SOURCE
from .office import OfficeService
from .performance import review as performance_review
from .usage import cost_summary, summarize_costs, token_total
from .personalities import PERSONALITIES

PAGE = (Path(__file__).parent / "app.html").read_text()
LOGIN_PAGE = (Path(__file__).parent / "login.html").read_text()
ASSETS = {"/office.js": (OFFICE_SOURCE, "text/javascript"),
          "/performance.js": ((Path(__file__).parent / "performance.js").read_text(), "text/javascript"),
          "/performance.css": ((Path(__file__).parent / "performance.css").read_text(), "text/css")}
WS_ROUTE = re.compile(r"^/api/w/([0-9a-f]{10})(/.*)?$")
WORKSPACE_ROUTE = re.compile(r"^/api/workspaces/([0-9a-f]{10})(/.*)?$")


def _totals(orch: Any, path: Path) -> dict[str, Any]:
    """Tokens and dollars across every agent, from the live runtimes or the state files on disk."""
    from .config import agents_dir
    from .memory import AgentMemory

    tokens = cost = 0.0
    counts: dict[str, int] = {}
    for a in orch.team:
        st = orch.runtimes[a.name].memory.state if a.name in orch.runtimes else AgentMemory(agents_dir(path), a.name).state
        u = st.usage_totals
        tokens += token_total(u)
        cost += float(u.get("cost_usd", 0))
        backend = a.backend or orch.backend_name
        for kind, count in cost_summary(st, backend)["counts"].items():
            counts[kind] = counts.get(kind, 0) + count
    return {"tokens": round(tokens), "cost_usd": round(cost, 4), "usage_cost": summarize_costs(counts)}


def _limits_from(store: Any) -> dict[str, Any]:
    g = store.get_control
    return {"paused": False, "backends": {}, "since": None, "resets_at": None, "reason": None, "pause_count": int(g("limit_pause_count", "0") or 0),
            "resume_count": int(g("limit_resume_count", "0") or 0), "gate": g("resume_gate", "") or None, "next_probe_at": None, "last_probe": None, "auto": False, "probing": False}


def _providers_view(probe: bool = True) -> dict[str, Any]:
    """The Model providers menu: every local server with what it serves, and the providers detected on this machine.
    A key never leaves the server, only whether one is set. Ollama and vLLM at their default local addresses are listed
    only while something answers there."""
    if probe:
        refresh_local(force=True)
    out = []
    for e in servers():
        st = SERVER_STATUS.get(e["id"])
        if e["source"] == "default" and not (st and st.get("ok")):
            continue
        out.append({"id": e["id"], "type": e["type"], "type_label": SERVER_TYPES[e["type"]], "name": e["name"], "label": server_label(e), "url": e["url"],
                    "has_key": bool(e["key"]), "note": e["note"], "context": e["context"], "source": e["source"], "pinned": bool(e["pinned"].strip()),
                    "status": st, "models": [mid for mid, r in list(ROUTES.items()) if r["server"] == e["id"]]})
    return {"servers": out, "types": SERVER_TYPES, "backends": {b: BACKEND_LABEL.get(b, b) for b in available_backends()}}


LOCAL_HOSTS = {"127.0.0.1", "localhost", "[::1]"}


def allowed_hosts() -> set[str]:
    """Host names the server answers to: loopback only, plus HUNTUN_ALLOWED_HOSTS (comma-separated) for a proxy in front."""
    extra = {h.strip().lower() for h in os.environ.get("HUNTUN_ALLOWED_HOSTS", "").split(",") if h.strip()}
    return LOCAL_HOSTS | extra


def _hostname(host: str) -> str:
    """ "127.0.0.1:4747" -> "127.0.0.1", "[::1]:4747" -> "[::1]"."""
    host = host.strip().lower()
    if host.startswith("["):
        return host.split("]", 1)[0] + "]"
    return host.rsplit(":", 1)[0] if ":" in host else host


def bind_address() -> str:
    """The address the web app listens on: loopback unless HUNTUN_BIND says otherwise (0.0.0.0 for the whole network)."""
    return os.environ.get("HUNTUN_BIND", "").strip() or "127.0.0.1"


class HttpError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


def _error_body(e: HttpError) -> dict[str, Any]:
    return {"error": str(e), "login": True} if e.status == 401 else {"error": str(e)}   # the page shows the sign-in page again


def start_server(hub: Hub, port: int) -> ThreadingHTTPServer:
    office = OfficeService(hub)

    class HuntunServer(ThreadingHTTPServer):
        def shutdown(self) -> None:
            super().shutdown()
            office.close()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args: Any) -> None:  # keep the console for agent logs
            pass

        def _json(self, status: int, body: Any, cookie: str = "") -> None:
            data = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("content-type", "application/json; charset=utf-8")
            self.send_header("cache-control", "no-store")
            self.send_header("content-length", str(len(data)))
            if cookie:
                self.send_header("set-cookie", cookie)
            self.end_headers()
            self.wfile.write(data)

        # ---- sign-in: nothing below is asked for until a password is set in Settings (see auth.py)
        cookie_name = "huntun_session"                                           # made per port below: two servers on one host keep their own

        def _session_cookie(self, token: str) -> str:
            age = auth.SESSION_SEC if token else 0
            return f"{self.cookie_name}={token}; Path=/; Max-Age={age}; HttpOnly; SameSite=Lax"

        def _signed_in(self) -> bool:
            acct = auth.account()
            if acct is None:
                return True
            try:
                jar = SimpleCookie(self.headers.get("cookie") or "")
            except CookieError:
                return False
            morsel = jar.get(self.cookie_name)
            return auth.valid_session(acct, morsel.value if morsel else "")

        def _auth_view(self, signed_in: bool | None = None) -> dict[str, Any]:
            acct = auth.account()
            signed_in = self._signed_in() if signed_in is None else signed_in
            return {"enabled": acct is not None, "signed_in": signed_in, "username": acct.get("username") if acct and signed_in else None,
                    "reset_command": "huntun auth reset"}

        def _check_password(self, username: str, password: str, status: int) -> None:
            """Refuses a wrong user name or password (with this status), and any try from an address that just made too many."""
            client = self.client_address[0]
            wait = auth.locked_for(client)
            if wait:
                raise HttpError(429, f"too many wrong passwords; try again in {wait}s")
            ok = auth.check_login(username, password)
            auth.note_attempt(client, ok)
            if not ok:
                wait = auth.locked_for(client)
                if wait:
                    raise HttpError(429, f"too many wrong passwords; try again in {wait}s")
                raise HttpError(status, "wrong user name or password" if status == 401 else "the current password is wrong")

        def _page(self, html: str) -> None:
            data = html.encode()
            self.send_response(200)
            self.send_header("content-type", "text/html; charset=utf-8")
            self.send_header("cache-control", "no-store")
            self.send_header("content-length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _body(self) -> dict[str, Any]:
            n = int(self.headers.get("content-length") or 0)
            raw = self.rfile.read(n) if n else b""
            return json.loads(raw) if raw else {}

        def _guard(self, write: bool) -> None:
            """Refuses requests another web page could make through the user's browser.

            Any site open in the browser can send requests to 127.0.0.1, and the API can add projects, approve plans, start
            agents and re-point model traffic. So: the Host must be this machine (a DNS-rebinding page names its own domain);
            the browser must not flag the request as coming from another site (Sec-Fetch-Site) or origin (Origin); and a
            write must carry a JSON body, which another site cannot send without a CORS preflight this server never answers.
            The web app, the documented curl calls and scripts using urllib all pass.
            """
            host = self.headers.get("host") or ""
            if _hostname(host) not in allowed_hosts():
                raise HttpError(403, f"unknown host {host!r}; open Huntun at http://127.0.0.1 (or set HUNTUN_ALLOWED_HOSTS)")
            if (self.headers.get("sec-fetch-site") or "").lower() in ("cross-site", "same-site"):
                raise HttpError(403, "cross-site request refused")
            origin = self.headers.get("origin")
            if origin is not None and urlparse(origin).netloc.lower() != host.lower():
                raise HttpError(403, "cross-origin request refused")
            if write and (self.headers.get("content-type") or "").split(";")[0].strip().lower() != "application/json":
                raise HttpError(415, "send JSON (content-type: application/json)")

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
                if path.startswith("/api/"):
                    self._guard(write=False)                                      # some reads load a project's agents
                if path == "/":
                    return self._page(PAGE if self._signed_in() else LOGIN_PAGE)
                if path in ASSETS:
                    source, mime = ASSETS[path]
                    data = source.encode()
                    self.send_response(200)
                    self.send_header("content-type", mime + "; charset=utf-8")
                    self.send_header("cache-control", "no-store")
                    self.send_header("content-length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                    return
                if path == "/api/auth":
                    return self._json(200, self._auth_view())
                if path.startswith("/api/") and not self._signed_in():
                    raise HttpError(401, "sign in first")
                if path == "/api/workspaces":
                    return self._json(200, {"workspaces": hub.list(), "home": str(Path.home()), "backends": {b: BACKEND_LABEL.get(b, b) for b in available_backends()}})
                if path == "/api/providers":
                    return self._json(200, _providers_view())
                if path == "/api/models":                                           # what a seat (the master's included) can run here
                    return self._json(200, {"models": [{"id": m.id, "backend": b, "vendor": m.vendor, "label": m.label, "context": m.context, "efforts": list(m.reasoning_levels)} for m, b in catalog_available()],
                                            "backends": {b: BACKEND_LABEL.get(b, b) for b in available_backends()}})
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
                self._json(e.status, _error_body(e))
            except Exception as e:
                self._json(500, {"error": f"{type(e).__name__}: {e}"})

        def _board_get(self, wid: str, sub: str) -> None:
            if sub == "/office":
                return self._json(200, office.snapshot(self._entry(wid)))
            if sub == "/watchdog":
                self._entry(wid)
                q = parse_qs(urlparse(self.path).query)
                async def view_watchdog() -> dict[str, Any]:
                    agent = await hub.watchdog(wid)
                    return agent.view(int((q.get("before") or [0])[0]))
                try:
                    return self._json(200, hub.call(view_watchdog()))
                except ValueError as ex:
                    raise HttpError(400, str(ex)) from None
            orch = self._board(wid)
            store = orch.store
            if sub == "/performance":
                q = parse_qs(urlparse(self.path).query)
                try:
                    return self._json(200, performance_review(store, self._entry(wid).path, orch.team, (q.get("range") or ["7d"])[0],
                                                             bin_size=(q.get("bin") or ["auto"])[0]))
                except ValueError as ex:
                    raise HttpError(400, str(ex)) from None
            if sub == "/tasks":
                return self._json(200, {"tasks": store.list_tasks()})
            if sub == "/state":
                statuses = store.agent_statuses()
                entry = self._entry(wid)
                threads = store.list_threads(100, preview=True)
                for t in threads:
                    t["snippet"] = t["body"][:400]
                    t["last_comments"] = [{"id": c["id"], "author": c["author"], "created_at": c["created_at"], "snippet": c["body"]} for c in store.last_comments(t["id"], 2, preview=True)]
                    del t["body"]
                return self._json(200, {
                    "workspace": entry.summary(),
                    "goal": orch.config.goal,
                    "backend": orch.backend_name,
                    "default_model": orch.config.model,
                    "running": store.is_running(),
                    "approved": entry.approved(),
                    "goal_confirmed": orch.config.goal_confirmed,
                    "definition_of_done": orch.config.definition_of_done,
                    "goal_thread_id": int(store.get_control("goal_thread_id", "0") or 0) or None,
                    "plan_thread_id": int(store.get_control("plan_thread_id", "0") or 0) or None,
                    "totals": _totals(orch, entry.path),
                    "estimate": orch.config.estimate or None,
                    "max_agents": orch.config.max_agents,
                    "attention": store.open_attention(20, full_text=False),
                    "attention_count": store.attention_count(),
                    "models": [{"id": m.id, "backend": b, "vendor": m.vendor, "label": m.label, "context": m.context, "efforts": list(m.reasoning_levels)} for m, b in (catalog_available() or [(m, orch.backend_name) for m in catalog_for(orch.backend_name)])],
                    "backends": {b: BACKEND_LABEL.get(b, b) for b in available_backends()},
                    "personalities": [{"id": p.id, "name": p.name, "text": p.text, "group": p.group} for p in PERSONALITIES],
                    "limits": entry.orchestrator.limits() if entry.orchestrator is not None else _limits_from(store),
                    "agents": [{**a.to_dict(), "live": statuses.get(a.name), "info": (orch.runtimes[a.name].info() if a.name in orch.runtimes else None)} for a in orch.team],
                    "threads": threads,
                    "tasks": store.list_tasks(),
                    "events": store.list_events(0, 40),
                    "office": office.snapshot(entry),
                })
            if sub == "/attention":
                q = parse_qs(urlparse(self.path).query)
                try:
                    before, limit = (int((q.get(k) or [str(d)])[0]) for k, d in (("before", 0), ("limit", 30)))
                    if before < 0 or limit < 1:
                        raise ValueError()
                except ValueError:
                    raise HttpError(400, "Use a non-negative before cursor and a positive limit") from None
                pending = store.pending_attention_ids()
                return self._json(200, {"items": store.open_attention(limit, before=before),
                                        "pending_ids": pending, "count": len(pending)})
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
                q = parse_qs(urlparse(self.path).query)
                include_thread = (q.get("include_thread") or ["1"])[0] != "0"
                t = store.get_thread(int(tm.group(1)), include_body=include_thread)
                if not t:
                    raise HttpError(404, "thread not found")
                try:
                    before, after, limit = (int((q.get(k) or [str(d)])[0]) for k, d in (("before", 0), ("after", 0), ("limit", 100)))
                except ValueError:
                    raise HttpError(400, "before, after and limit must be integers") from None
                if before < 0 or after < 0 or (before and after) or limit < 1:
                    raise HttpError(400, "Use either before or after, and a positive limit")
                page = store.comment_page(t["id"], before=before, after=after, limit=limit)
                return self._json(200, {"thread": t if include_thread else None, **page})
            raise HttpError(404, "not found")

        def do_POST(self) -> None:
            path = urlparse(self.path).path
            try:
                self._guard(write=True)
                body = self._body()
                if path.startswith("/api/auth/"):
                    return self._auth_post(path[len("/api/auth"):], body)
                if not self._signed_in():
                    raise HttpError(401, "sign in first")
                if path == "/api/providers/servers":                                # add a server, or change one ("id")
                    save_server(body)
                    return self._json(200, _providers_view())
                if path == "/api/providers/servers/delete":
                    if not delete_server(str(body.get("id") or "")):
                        raise HttpError(404, "unknown server")
                    return self._json(200, _providers_view())
                if path == "/api/providers/servers/test":                           # probe what the form holds, without saving it
                    saved = next((e for e in saved_servers() if e["id"] == body.get("id")), None)
                    key = "" if body.get("clear_key") else str(body.get("key") or "").strip() or (saved or {}).get("key", "")
                    try:
                        context = int(body.get("context") or 0)
                    except (TypeError, ValueError):
                        context = 0
                    return self._json(200, probe_server(str(body.get("type") or ""), str(body.get("url") or ""), key, context=context))
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
                self._json(e.status, _error_body(e))
            except ValueError as e:
                self._json(400, {"error": str(e)})
            except Exception as e:
                self._json(500, {"error": f"{type(e).__name__}: {e}"})

        def _auth_post(self, sub: str, body: dict[str, Any]) -> None:
            """/login and /logout for anyone; /account (set the user name and password) and /remove once signed in.
            Changing the password while one is set takes the current one, so a browser left signed in cannot lock its owner out."""
            if sub == "/login":
                acct = auth.account()
                if acct is None:
                    return self._json(200, self._auth_view())                    # no password set: nothing to sign in to
                self._check_password(str(body.get("username") or ""), str(body.get("password") or ""), 401)
                return self._json(200, self._auth_view(signed_in=True), cookie=self._session_cookie(auth.new_session(acct)))
            if sub == "/logout":
                return self._json(200, self._auth_view(signed_in=False), cookie=self._session_cookie(""))
            if not self._signed_in():
                raise HttpError(401, "sign in first")
            if sub not in ("/account", "/remove"):
                raise HttpError(404, "not found")
            current = auth.account()
            if current:                                                          # 403, not 401: a typo here must not sign the page out
                self._check_password(current["username"], str(body.get("current_password") or ""), 403)
            if sub == "/account":
                acct = auth.save(str(body.get("username") or ""), str(body.get("password") or "") or None)
                return self._json(200, self._auth_view(signed_in=True), cookie=self._session_cookie(auth.new_session(acct)))
            auth.reset()                                                          # /remove
            return self._json(200, self._auth_view(signed_in=True), cookie=self._session_cookie(""))

        def _workspace_post(self, wid: str, sub: str, body: dict[str, Any]) -> None:
            e = self._entry(wid)
            if sub == "/watchdog":
                async def send_watchdog() -> dict[str, Any]:
                    agent = await hub.watchdog(wid)
                    return agent.send(body)
                return self._json(202, hub.call(send_watchdog()))
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
                backend = body.get("backend") if body.get("backend") in ("api", "claude-code", "codex", "kimi", "pi-clm", "deepseek", "ollama", "vllm", "llamacpp") else None
                try:
                    e = hub.begin_init(wid, str(body.get("goal") or ""), backend, str(body.get("context") or ""), int(body.get("max_agents") or 0),
                                       team_lead=body.get("team_lead") is not False, master_model=str(body.get("master_model") or ""))
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
                    hub.call(hub.approve(wid, body.get("agents") or [], {"max_agents": body["max_agents"]} if "max_agents" in body else None), timeout=120)
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
            if sub == "/max-agents":
                try:
                    n = hub.call(hub.set_max_agents(wid, int(body.get("max_agents") or 0)), timeout=30)
                except ValueError as ex:
                    raise HttpError(409, str(ex)) from None
                return self._json(200, {"max_agents": n})
            if sub == "/close":
                hub.call(hub.close(wid), timeout=60)
                return self._json(200, self._entry(wid).summary())
            if sub == "/forget":
                hub.forget(wid)
                return self._json(200, {"ok": True})
            raise HttpError(404, "not found")

        def _board_post(self, wid: str, sub: str, body: dict[str, Any]) -> None:
            if sub == "/office/theme":
                try:
                    return self._json(200, office.theme(self._entry(wid), str(body.get("theme", "")), body.get("initialize") is True))
                except ValueError as ex:
                    raise HttpError(400, str(ex)) from None
            board = self._board(wid)
            store = board.store
            tm = re.fullmatch(r"/tasks/(\d+)", sub)
            if sub == "/tasks" or tm:
                try:
                    owner = body.get("owner", "")
                    if owner and owner not in {a.name for a in board.team if a.status != "retired"}:
                        raise ValueError("Assign the task to an active teammate")
                    if tm:
                        previous_owner = (store.get_task(int(tm.group(1))) or {}).get("owner", "")
                        task = store.update_task(int(tm.group(1)), "human", body)
                    else:
                        fields = {k: body[k] for k in ("description", "acceptance", "owner", "parent_id", "thread_id") if k in body}
                        task = store.create_task("human", str(body.get("title") or ""), **fields)
                    if owner and (not tm or owner != previous_owner):
                        notice = f"@{owner} Task #{task['id']}: {task['title']}. Acceptance: {task['acceptance']}"
                        if task["thread_id"]:
                            store.add_comment(task["thread_id"], "human", notice)
                        else:
                            t = store.create_thread("human", task["title"], notice)
                            task = store.update_task(task["id"], "human", {"thread_id": t["id"]})
                    return self._json(200 if tm else 201, task)
                except (ValueError, TypeError) as ex:
                    raise HttpError(400, str(ex)) from None
            if sub == "/threads":
                title, text = str(body.get("title") or "").strip(), str(body.get("body") or "").strip()
                if not title or not text:
                    raise HttpError(400, "title and body are required")
                return self._json(201, store.create_thread("human", title, text))
            mm = re.match(r"^/agents/([a-z0-9-]+)/model$", sub)
            if mm:
                try:
                    msg = hub.call(hub.set_agent_model(wid, mm.group(1), str(body.get("model") or ""), str(body.get("effort") or "")), timeout=30)
                except ValueError as ex:
                    raise HttpError(409, str(ex)) from None
                return self._json(200, {"ok": True, "message": msg})
            if sub == "/harness":
                try:
                    msg = hub.call(hub.set_harness(wid, str(body.get("backend") or ""), str(body.get("model") or ""),
                                                   body.get("all_agents") is True), timeout=30)
                except (ValueError, RuntimeError) as ex:
                    raise HttpError(409, str(ex)) from None
                return self._json(200, {"ok": True, "message": msg})
            rm = re.match(r"^/attention/(\d+)/resolve$", sub)
            if rm:
                return self._json(200, {"ok": store.resolve_attention(int(rm.group(1)))})
            rm = re.fullmatch(r"/attention/(\d+)/reply", sub)
            if rm:
                text = str(body.get("body") or "").strip()
                if not text:
                    raise HttpError(400, "Enter a reply before sending.")
                item_id = int(rm.group(1))
                e = self._entry(wid)
                try:
                    # Setup requests still revise the goal/plan just as Discussion replies do.
                    if not e.approved() and e.state not in ("planning", "clarifying"):
                        item = store.get_attention(item_id)
                        tid = item["thread_id"] if item else 0
                        goal_tid = int(store.get_control("goal_thread_id", "0") or 0)
                        plan_tid = int(store.get_control("plan_thread_id", "0") or 0)
                        operation = None
                        if tid and tid == goal_tid and not e.summary().get("goal_confirmed"):
                            operation = hub.goal_reply(wid, text, attention_id=item_id)
                        elif tid and tid == plan_tid and e.summary().get("goal_confirmed"):
                            operation = hub.replan(wid, text, attention_id=item_id)
                        if operation is not None:
                            try:
                                hub.call(operation, timeout=0.5)
                            except TimeoutError:
                                pass
                            return self._json(202, {"revising": "goal" if tid == goal_tid else "plan", "thread_id": tid})
                    return self._json(201, store.reply_attention(item_id, text))
                except ValueError as ex:
                    raise HttpError(409, str(ex)) from None
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

    try:
        server = HuntunServer((bind_address(), port), Handler)
    except Exception:
        office.close()
        raise
    Handler.cookie_name = f"huntun_session_{server.server_port}"
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, name="huntun-web", daemon=True).start()
    return server
