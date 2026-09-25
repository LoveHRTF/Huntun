"""The hub: the web-centric entry point that manages any number of project workspaces in one process.

A workspace is any directory. Pointing the hub at a directory registers it; if it already contains
`.huntun/` it is loaded (agents paused until you press Start), otherwise the page asks for a goal and
the master agent plans the team right there.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .backends import make_backend
from .config import (
    config_exists,
    db_path,
    default_config,
    is_initialized,
    load_config,
    load_team,
    now_iso,
    resolve_backend,
    save_config,
    save_team,
)
from .gitops import ensure_repo
from .master import (
    describe_workspace,
    draft_goal,
    estimate_for,
    goal_thread_body,
    master_spec,
    plan_team,
    plan_thread_body,
)
from .models import EFFORTS, backend_for_model, model_info
from .personalities import PERSONALITY_IDS, personality_text
from .store import Store


def huntun_home() -> Path:
    return Path(os.environ.get("HUNTUN_HOME") or Path.home() / ".huntun")


def workspace_id(path: Path) -> str:
    return hashlib.sha1(str(path.resolve()).encode()).hexdigest()[:10]


@dataclass
class WorkspaceEntry:
    id: str
    path: Path
    added_at: str
    state: str = "new"  # new | planning | loading | ready | error
    error: str | None = None
    progress: str = ""
    started_at: float = 0.0
    orchestrator: Any = None
    last_used: str = ""
    store: Store | None = None
    log: list[dict[str, Any]] = field(default_factory=list)   # what the master is doing during a goal check or a planning run

    def note(self, text: str) -> None:
        self.log.append({"at": time.strftime("%H:%M:%S"), "text": str(text)[:2000]})
        del self.log[:-300]

    def open_store(self) -> Store:
        if self.orchestrator is not None:
            return self.orchestrator.store
        if self.store is None:
            self.store = Store(db_path(self.path))
        return self.store

    def approved(self) -> bool:
        return is_initialized(self.path) and self.open_store().get_control("plan_approved", "0") == "1"

    def derived_state(self) -> str:
        """Lifecycle from what is on disk: new -> goal_proposed -> proposed -> ready."""
        if is_initialized(self.path):
            return "ready" if self.approved() else "proposed"
        if config_exists(self.path):
            return "goal_proposed"
        return "new"

    def goal_draft(self) -> dict[str, Any] | None:
        if not config_exists(self.path):
            return None
        raw = self.open_store().get_control("goal_draft", "")
        try:
            return json.loads(raw) if raw else None
        except Exception:
            return None

    def summary(self) -> dict[str, Any]:
        goal, backend, running, agents, dod, goal_confirmed = "", "", False, 0, "", False
        approved = self.approved() if is_initialized(self.path) else False
        plan_thread = int(self.open_store().get_control("plan_thread_id", "0") or 0) if config_exists(self.path) else 0
        goal_thread = int(self.open_store().get_control("goal_thread_id", "0") or 0) if config_exists(self.path) else 0
        if self.orchestrator is not None:
            cfg = self.orchestrator.config
            goal, backend, dod, goal_confirmed = cfg.goal, self.orchestrator.backend_name, cfg.definition_of_done, cfg.goal_confirmed
            running = self.orchestrator.store.is_running()
            agents = len(self.orchestrator.active_team())
        elif config_exists(self.path):
            try:
                cfg = load_config(self.path)
                goal, backend, dod, goal_confirmed = cfg.goal, cfg.backend, cfg.definition_of_done, cfg.goal_confirmed
                agents = len([a for a in load_team(self.path) if a.status != "retired"])
            except Exception:
                pass
        return {
            "id": self.id,
            "path": str(self.path),
            "name": self.path.name or str(self.path),
            "added_at": self.added_at,
            "last_used": self.last_used,
            "state": self.state,
            "error": self.error,
            "progress": self.progress,
            "elapsed": round(time.time() - self.started_at) if self.started_at and self.state in ("planning", "loading", "clarifying") else 0,
            "log": self.log[-200:],
            "initialized": is_initialized(self.path),
            "exists": self.path.is_dir(),
            "goal": goal,
            "backend": backend,
            "running": running,
            "agents": agents,
            "approved": approved,
            "plan_thread_id": plan_thread or None,
            "goal_thread_id": goal_thread or None,
            "goal_confirmed": goal_confirmed,
            "definition_of_done": dod,
            "goal_draft": self.goal_draft() if not goal_confirmed and config_exists(self.path) else None,
        }


class Hub:
    def __init__(self, home: Path | None = None) -> None:
        self.home = home or huntun_home()
        self.home.mkdir(parents=True, exist_ok=True)
        self.registry_file = self.home / "workspaces.json"
        self.loop: asyncio.AbstractEventLoop | None = None
        self.lock = threading.RLock()
        self.workspaces: dict[str, WorkspaceEntry] = {}
        self._load_registry()

    # ---- registry -------------------------------------------------------------

    def _load_registry(self) -> None:
        if not self.registry_file.exists():
            return
        try:
            for item in json.loads(self.registry_file.read_text()):
                p = Path(item["path"])
                e = WorkspaceEntry(workspace_id(p), p, item.get("added_at", ""), last_used=item.get("last_used", ""))
                e.state = e.derived_state()
                self.workspaces[e.id] = e
        except Exception:
            pass

    def _save_registry(self) -> None:
        data = [{"path": str(e.path), "added_at": e.added_at, "last_used": e.last_used} for e in self.workspaces.values()]
        self.registry_file.write_text(json.dumps(data, indent=2))

    def list(self) -> list[dict[str, Any]]:
        with self.lock:
            items = [e.summary() for e in self.workspaces.values()]
        return sorted(items, key=lambda s: s["last_used"] or s["added_at"], reverse=True)

    def get(self, wid: str) -> WorkspaceEntry | None:
        return self.workspaces.get(wid)

    def add(self, raw_path: str) -> WorkspaceEntry:
        path = Path(raw_path).expanduser().resolve()
        if not path.is_dir():
            raise ValueError(f"{path} is not a directory")
        with self.lock:
            wid = workspace_id(path)
            e = self.workspaces.get(wid)
            if e is None:
                e = WorkspaceEntry(wid, path, now_iso())
                self.workspaces[wid] = e
            e.last_used = now_iso()
            if e.orchestrator is None and e.state not in ("planning", "loading", "clarifying"):
                e.state = e.derived_state()
            self._save_registry()
            return e

    def forget(self, wid: str) -> None:
        with self.lock:
            e = self.workspaces.pop(wid, None)
            self._save_registry()
        if e and e.orchestrator is not None and self.loop:
            asyncio.run_coroutine_threadsafe(e.orchestrator.stop(), self.loop)
        elif e and e.store is not None:
            e.store.close()
            e.store = None

    # ---- lifecycle ------------------------------------------------------------

    # ---- phase 1: confirm the goal and the definition of done with the human --------------------

    def begin_init(self, wid: str, goal: str, backend: str | None, context: str, max_agents: int = 0) -> WorkspaceEntry:
        """Validates and starts the goal check in the background (callable from any thread)."""
        e = self.workspaces[wid]
        with self.lock:
            if is_initialized(e.path):
                raise ValueError("workspace is already initialized")
            if e.state in ("planning", "clarifying"):
                raise ValueError("the master is already working on this; planning is already in progress")
            goal = goal.strip()
            if not goal:
                raise ValueError("a goal is required")
            e.state, e.error, e.progress, e.started_at = "clarifying", None, "Inspecting the directory", time.time()
        self.submit(self.clarify(wid, goal, backend, context, max_agents=max_agents))
        return e

    async def clarify(self, wid: str, goal: str, backend: str | None, context: str, conversation: str = "", max_agents: int = 0) -> WorkspaceEntry:
        """The master restates the goal, proposes a definition of done, and asks the human to confirm."""
        e = self.workspaces[wid]
        e.state, e.error, e.started_at = "clarifying", None, e.started_at or time.time()
        e.log.clear()
        e.note("Inspecting the directory")
        try:
            if config_exists(e.path):
                config = load_config(e.path)
                config.backend = resolve_backend(config)
            else:
                config = default_config(goal)
                config.context = context.strip()
                config.max_agents = max(0, int(max_agents or 0))
                if backend in ("api", "claude-code", "codex", "kimi", "deepseek", "ollama", "vllm"):
                    config.backend = backend
                config.backend = resolve_backend(config)
            existing = await asyncio.to_thread(describe_workspace, e.path)
            e.note(f"Workspace inspected ({len(existing)} characters of context)")
            e.progress = f"Master agent is checking the goal via {config.backend}"
            e.note(f"Asking the master via {config.backend}" + (" (revising after your reply)" if conversation else ""))
            draft = await draft_goal(make_backend(config.backend, config), config, existing, conversation, log=e.note, workspace=e.path)
            e.note("The master drafted the goal and definition of done; over to you")
            save_config(e.path, config)
            await ensure_repo(e.path)
            store = e.open_store()
            store.set_control("running", "0")
            store.set_control("plan_approved", "0")
            store.set_control("goal_draft", json.dumps(draft))
            store.set_agent_status("master", "idle", "")
            tid = int(store.get_control("goal_thread_id", "0") or 0)
            if tid and store.get_thread(tid):
                store.add_comment(tid, "master", goal_thread_body(draft, revised=True))
            else:
                t = store.create_thread("master", "Goal check: please confirm the goal and definition of done", goal_thread_body(draft))
                store.set_control("goal_thread_id", str(t["id"]))
            store.log_event("master", "goal", "Waiting for @human to confirm the goal and definition of done")
            e.progress, e.state = "", "goal_proposed"
        except Exception as ex:
            e.state, e.error, e.progress = "error", f"{type(ex).__name__}: {ex}", ""
            e.note(f"Error: {type(ex).__name__}: {ex}")
        return e

    async def goal_reply(self, wid: str, message: str) -> WorkspaceEntry:
        """Human answered the master's questions or asked for changes: revise the goal proposal."""
        e = self.workspaces[wid]
        if not config_exists(e.path) or is_initialized(e.path):
            raise ValueError("this project is not waiting on a goal check")
        if e.state in ("planning", "clarifying"):
            raise ValueError("the master is already working on this")
        message = message.strip()
        if not message:
            raise ValueError("say what should change")
        store = e.open_store()
        tid = int(store.get_control("goal_thread_id", "0") or 0)
        if tid:
            store.add_comment(tid, "human", f"@master {message}")
        draft = e.goal_draft()
        previous = json.dumps(draft, indent=1) if draft else "(none)"
        e.state, e.progress, e.started_at = "clarifying", "Master agent is revising the goal", time.time()
        config = load_config(e.path)
        return await self.clarify(wid, config.goal, None, config.context, conversation=f"Your previous proposal:\n{previous}\n\nHuman's reply:\n{message}")

    async def confirm_goal(self, wid: str, goal: str, definition_of_done: str) -> WorkspaceEntry:
        """Human confirmed (and possibly edited) the goal and definition of done: record them and plan the team."""
        e = self.workspaces[wid]
        if not config_exists(e.path) or is_initialized(e.path):
            raise ValueError("this project is not waiting on a goal check")
        if e.state in ("planning", "clarifying"):
            raise ValueError("the master is already working on this")
        goal = goal.strip()
        if not goal:
            raise ValueError("a goal is required")
        config = load_config(e.path)
        config.goal, config.definition_of_done, config.goal_confirmed = goal, definition_of_done.strip(), True
        save_config(e.path, config)
        store = e.open_store()
        tid = int(store.get_control("goal_thread_id", "0") or 0)
        if tid:
            store.add_comment(tid, "human", f"@master Confirmed. Goal: {goal}\n\nDefinition of done:\n{definition_of_done.strip() or '(as proposed)'}")
        store.log_event("human", "goal", "Goal and definition of done confirmed")
        e.state, e.error, e.progress, e.started_at = "planning", None, "Master agent is planning the team", time.time()
        self.submit(self.init(wid))
        return e

    # ---- phase 2: propose the team; the human approves it -----------------------------------------

    async def init(self, wid: str) -> WorkspaceEntry:
        """Plans the team for a workspace whose goal is confirmed. Runs on the event loop."""
        e = self.workspaces[wid]
        e.state, e.error, e.started_at = "planning", None, e.started_at or time.time()
        e.log.clear()
        e.note("Inspecting the directory")
        try:
            config = load_config(e.path)
            if not config.goal_confirmed:
                raise ValueError("the goal has not been confirmed yet")
            existing = await asyncio.to_thread(describe_workspace, e.path)
            full_context = "\n\n".join(part for part in (config.context, existing) if part)
            e.progress = f"Master agent is planning the team via {config.backend}"
            e.note(f"Asking the master to staff the team via {config.backend}")
            rationale, agents = await plan_team(make_backend(config.backend, config), config, full_context, log=e.note, workspace=e.path)
            e.note(f"The master proposed {len(agents)} agents; over to you")
            specs = [master_spec(), *agents]
            save_team(e.path, specs)
            config.estimate = estimate_for(specs, rationale.split("Estimate notes: ")[-1] if "Estimate notes: " in rationale else "")
            save_config(e.path, config)
            await ensure_repo(e.path)
            store = e.open_store()
            store.set_control("running", "0")
            store.set_control("plan_approved", "0")
            for a in specs:
                store.set_agent_status(a.name, "idle", "")
            self._post_plan(e, config, rationale, specs, revised=False)
            e.progress = ""
            e.state = "proposed"
        except Exception as ex:
            e.state, e.error, e.progress = "error", f"{type(ex).__name__}: {ex}", ""
        return e

    def _post_plan(self, e: WorkspaceEntry, config: Any, rationale: str, specs: list[Any], revised: bool) -> int:
        store = e.open_store()
        t = store.create_thread("master", "Revised plan: please review" if revised else "Proposed plan: please review", plan_thread_body(config, rationale, specs, revised))
        store.set_control("plan_thread_id", str(t["id"]))
        store.log_event("master", "plan", "Waiting for @human to approve the team, roles, and models")
        return int(t["id"])

    async def replan(self, wid: str, feedback: str) -> WorkspaceEntry:
        """Human asked for changes before approving: the master plans again with the feedback and the previous proposal."""
        e = self.workspaces[wid]
        if not is_initialized(e.path) or e.approved():
            raise ValueError("this project is not awaiting plan approval")
        if e.state == "planning":
            raise ValueError("planning is already in progress")
        feedback = feedback.strip()
        if not feedback:
            raise ValueError("say what should change")
        config = load_config(e.path)
        previous = load_team(e.path)
        store = e.open_store()
        plan_thread = int(store.get_control("plan_thread_id", "0") or 0)
        if plan_thread:
            store.add_comment(plan_thread, "human", f"@master please revise: {feedback}")
        e.state, e.error, e.progress, e.started_at = "planning", None, "Master agent is revising the plan", time.time()
        try:
            existing = await asyncio.to_thread(describe_workspace, e.path)
            prev = "\n".join(f"- @{a.name} ({a.role}, model {a.model or 'default'}, effort {a.effort or 'default'}): {a.brief.splitlines()[0]}" for a in previous if a.role != "master")
            full_context = "\n\n".join(p for p in (config.context, existing,
                                                    f"# Previous proposal (rejected by the human)\n{prev}\n\n# Human feedback on it\n{feedback}\nRevise the plan to address the feedback.") if p)
            rationale, agents = await plan_team(make_backend(config.backend, config), config, full_context, log=e.note, workspace=e.path)
            specs = [master_spec(), *agents]
            save_team(e.path, specs)
            config.estimate = estimate_for(specs, rationale.split("Estimate notes: ")[-1] if "Estimate notes: " in rationale else "")
            save_config(e.path, config)
            for a in specs:
                store.set_agent_status(a.name, "idle", "")
            self._post_plan(e, config, rationale, specs, revised=True)
            e.state, e.progress = "proposed", ""
        except Exception as ex:
            e.state, e.error, e.progress = "error", f"{type(ex).__name__}: {ex}", ""
        return e

    async def approve(self, wid: str, edits: list[dict[str, Any]] | None = None) -> WorkspaceEntry:
        """Human approved the plan, optionally after editing models / effort or removing agents. Loads the team (paused)."""
        e = self.workspaces[wid]
        if not is_initialized(e.path):
            raise ValueError("project is not set up")
        if e.state == "planning":
            raise ValueError("planning is in progress")
        team = load_team(e.path)
        changes: list[str] = []
        for edit in edits or []:
            spec = next((a for a in team if a.name == edit.get("name")), None)
            if spec is None or spec.role == "master":
                continue
            if edit.get("remove"):
                team = [a for a in team if a.name != spec.name]
                changes.append(f"removed @{spec.name}")
                continue
            if edit.get("model") and model_info(edit["model"]) and edit["model"] != spec.model:
                spec.model = edit["model"]
                spec.backend = backend_for_model(spec.model, default="")
                changes.append(f"@{spec.name} model -> {spec.model}" + (f" via {spec.backend}" if spec.backend else ""))
            if edit.get("effort") in EFFORTS and edit["effort"] != spec.effort:
                spec.effort = edit["effort"]
                changes.append(f"@{spec.name} effort -> {spec.effort}")
            if "personality" in edit or "personality_preset" in edit:
                preset = str(edit.get("personality_preset") or "custom")
                text = personality_text(preset, str(edit.get("personality") or "")) if preset in PERSONALITY_IDS else str(edit.get("personality") or "").strip()
                if text and (text != spec.personality or preset != spec.personality_preset):
                    spec.personality, spec.personality_preset = text, preset if preset in PERSONALITY_IDS else "custom"
                    changes.append(f"@{spec.name} personality -> {preset if preset in PERSONALITY_IDS else 'custom'}")
        if not any(a.role == "team-lead" for a in team):
            raise ValueError("the team needs a team-lead")
        save_team(e.path, team)
        config = load_config(e.path)
        config.estimate = estimate_for(team, (config.estimate or {}).get("notes", ""))
        save_config(e.path, config)
        store = e.open_store()
        store.set_control("plan_approved", "1")
        plan_thread = int(store.get_control("plan_thread_id", "0") or 0)
        note = "@master Plan approved." + (" Changes: " + "; ".join(changes) + "." if changes else " No changes.") + " Go ahead and kick off when started."
        if plan_thread:
            store.add_comment(plan_thread, "human", note)
        store.log_event("human", "approve", "Plan approved" + (f" with changes: {'; '.join(changes)}" if changes else ""))
        e.state = "ready"
        await self.load(wid, running=False)
        return e

    def board(self, wid: str) -> "BoardView":
        """Everything the board page needs, whether or not the agents are loaded (pre-approval they are not)."""
        e = self.workspaces[wid]
        if e.orchestrator is not None:
            o = e.orchestrator
            return BoardView(o.store, o.config, o.team, o.backend_name, o.runtimes)
        if not config_exists(e.path):
            raise ValueError("project is not set up")
        config = load_config(e.path)
        return BoardView(e.open_store(), config, load_team(e.path) or [master_spec()], config.backend, {})

    async def load(self, wid: str, running: bool | None = False) -> WorkspaceEntry:
        """Loads an initialized workspace: opens its board and spawns its agents (paused unless running=True)."""
        from .orchestrator import Orchestrator

        e = self.workspaces[wid]
        if e.orchestrator is not None:
            return e
        if not is_initialized(e.path):
            e.state = "new"
            return e
        if not e.approved():
            e.state = "proposed"
            return e
        e.state, e.error, e.started_at, e.progress = "loading", None, time.time(), "Loading agents"
        try:
            if e.store is not None:
                e.store.close()
                e.store = None
            orch = Orchestrator(e.path)
            if running is not None:
                orch.store.set_control("running", "1" if running else "0")
            await orch.start()
            e.orchestrator = orch
            e.state, e.progress = "ready", ""
            e.last_used = now_iso()
            self._save_registry()
        except Exception as ex:
            e.state, e.error, e.progress = "error", f"{type(ex).__name__}: {ex}", ""
        return e

    async def probe_limit(self, wid: str, backend: str | None = None) -> dict[str, Any]:
        """Human pressed "Check now" during a usage-limit pause."""
        e = self.workspaces[wid]
        if e.orchestrator is None:
            raise ValueError("workspace is not loaded")
        return await e.orchestrator.probe_now(backend)

    def set_running(self, wid: str, running: bool) -> bool:
        e = self.workspaces[wid]
        if running and not e.approved():
            raise ValueError("the plan has not been approved yet")
        if e.orchestrator is None:
            raise ValueError("workspace is not loaded")
        e.orchestrator.store.set_running(running)
        e.last_used = now_iso()
        return e.orchestrator.store.is_running()

    async def close(self, wid: str) -> None:
        e = self.workspaces.get(wid)
        if e and e.orchestrator is not None:
            orch, e.orchestrator = e.orchestrator, None
            e.state = "ready"
            await orch.stop()

    async def shutdown(self) -> None:
        await asyncio.gather(*(self.close(w) for w in list(self.workspaces)), return_exceptions=True)

    # ---- helpers for the HTTP layer (called from server threads) ----------------

    def submit(self, coro: Any) -> None:
        """Schedules a coroutine on the hub's event loop without waiting for it."""
        if self.loop is None:
            raise RuntimeError("hub loop is not running")
        asyncio.run_coroutine_threadsafe(coro, self.loop)

    def call(self, coro: Any, timeout: float = 60) -> Any:
        """Runs a coroutine on the hub's event loop and waits for its result."""
        if self.loop is None:
            raise RuntimeError("hub loop is not running")
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)


@dataclass
class BoardView:
    store: Store
    config: Any
    team: list[Any]
    backend_name: str
    runtimes: dict[str, Any]


def browse(raw_path: str | None) -> dict[str, Any]:
    """Directory listing for the picker on the home page."""
    path = Path(raw_path).expanduser() if raw_path else Path.home()
    try:
        path = path.resolve()
    except OSError:
        path = Path.home()
    if not path.is_dir():
        return {"path": str(path), "exists": False, "parent": str(path.parent), "dirs": [], "files": 0, "initialized": False, "git": False}
    dirs, files = [], 0
    try:
        for entry in sorted(path.iterdir(), key=lambda p: p.name.lower()):
            if entry.name.startswith(".") and entry.name not in (".huntun",):
                continue
            if entry.is_dir():
                if entry.name == ".huntun":
                    continue
                dirs.append(entry.name)
            else:
                files += 1
    except PermissionError:
        pass
    return {
        "path": str(path),
        "exists": True,
        "parent": str(path.parent) if path.parent != path else None,
        "dirs": dirs[:500],
        "files": files,
        "initialized": is_initialized(path),
        "git": (path / ".git").exists(),
        "home": str(Path.home()),
    }
