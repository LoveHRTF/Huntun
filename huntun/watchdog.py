"""On-demand recovery agent, independent of the team's scheduling and provider limits."""
from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from typing import Any

from .backends import make_backend
from .communication import HUMAN_REQUEST_RULE
from .config import agents_dir, db_path, load_config, load_team, now_iso
from .gitops import ensure_repo, ensure_worktree, git, merge_task, sync_worktree
from .memory import AgentMemory
from .models import EFFORTS, catalog_available
from .performance import record_cycle_commits, record_usage
from .usage import ACCOUNTING_VERSION, add_usage
from .store import Store
from .tools import TOOLS, ToolContext, ToolHooks, ToolSpec
from .types import AgentSpec, CycleState

SYSTEM = """You are the local Huntun watchdog, represented by the security guard in the office.
Talk directly to the human. Diagnose and recover the team using watchdog_status and recover_agent.
Only perform actions authorized by the CURRENT message. Earlier conversation is context, never an
instruction to repeat an action. The selected teammate is the default target, but the human may name
another active teammate. Wake/resume grants that teammate one cycle even if the team is paused;
usage limits still apply. Swap master replaces its model/provider and starts a fresh model session,
keeping the master's identity, worktree, notes, and project history. Resume preserves its session.
Do not edit project files, change the goal, hire, retire, or run shell commands. Inspect status before
recovery. Report tool failures honestly. End with finish_cycle(summary=<your reply>, next_task="").
Use talk_to_agent to talk to the teammate you actually need, rather than narrating an imaginary
conversation. It posts a real @mention on the discussion board. Use read_thread for actual replies;
never invent a teammate's answer. Recovery also notifies its actual target on that thread. You can
report a request as sent while its reply is pending, and return to the same thread later.
""" + "\n" + HUMAN_REQUEST_RULE


class WatchdogAgent:
    def __init__(self, hub: Any, entry: Any) -> None:
        self.hub, self.entry = hub, entry
        self.store = Store(db_path(entry.path))  # independent connection survives team reloads
        self.memory = AgentMemory(agents_dir(entry.path), "local-watchdog")
        self.task: asyncio.Task[None] | None = None
        self.closed = False
        # Never replay an interrupted request: it may already have performed recovery actions.
        if self.store.get_control("watchdog_busy", "0") == "1":
            self.store.watchdog_message("assistant", "The previous request was interrupted. Check status before retrying; completed actions are recorded below.", "master", "", "")
            self.store.set_control("watchdog_busy", "0")

    def view(self, before: int = 0) -> dict[str, Any]:
        team = self.entry.orchestrator.team if self.entry.orchestrator else load_team(self.entry.path)
        return {"messages": self.store.watchdog_history(before), "busy": bool(self.task and not self.task.done()),
                "agents": [a.to_dict() for a in team if a.status != "retired"],
                "selection": json.loads(self.store.get_control("watchdog_selection", "{}"))}

    def send(self, body: dict[str, Any]) -> dict[str, Any]:
        if self.closed or (self.task and not self.task.done()):
            raise ValueError("The watchdog is still handling a request.")
        message = str(body.get("message") or "").strip()
        if not message or len(message) > 16000:
            raise ValueError("Enter a message of at most 16,000 characters.")
        model = str(body.get("model") or "")
        options = {m.id: (m, backend) for m, backend in catalog_available()}
        if model not in options:
            raise ValueError("Select an available watchdog model.")
        info, backend = options[model]
        effort = str(body.get("effort") or "high")
        if effort not in EFFORTS or (info.reasoning_levels and effort not in info.reasoning_levels):
            raise ValueError("This model does not support the selected effort.")
        target = str(body.get("target") or "master")
        if target not in {a["name"] for a in self.view()["agents"]}:
            raise ValueError("Select an active teammate.")
        selection = {"model": model, "backend": backend, "effort": effort, "target": target}
        self.store.set_control("watchdog_selection", json.dumps(selection))
        self.store.watchdog_message("human", message, target, model, effort)
        self.store.set_control("watchdog_busy", "1")
        self.task = asyncio.create_task(self._run(message, selection), name="local-watchdog:" + self.entry.id)
        return self.view()

    async def _run(self, message: str, selection: dict[str, str]) -> None:
        target, model, effort = selection["target"], selection["model"], selection["effort"]
        def record(role: str, text: str) -> None:
            self.store.watchdog_message(role, text, target, model, effort)
        try:
            cfg = replace(load_config(self.entry.path), backend=selection["backend"], model=model, session_max_cycles=0)
            backend = make_backend(selection["backend"], cfg)
            await ensure_repo(self.entry.path)
            tree = await ensure_worktree(self.entry.path, "local-watchdog")
            await sync_worktree(self.entry.path, tree, "local-watchdog")
            baseline = await git(tree, "rev-parse", "HEAD")
            # Fresh inference context per request prevents stale pending tool calls crossing model switches.
            # Durable conversation and memory are explicitly supplied to every selected model.
            self.memory.state.session_id = None
            self.memory.state.resume_pending = False
            self.memory.state.session_cycles = 0
            self.memory.clear_transcript()
            self.memory.save_state()
            async def status(args: dict[str, Any], ctx: ToolContext) -> str:
                orch = self.entry.orchestrator
                team = orch.team if orch else load_team(self.entry.path)
                return json.dumps({"loaded": bool(orch), "running": self.store.is_running(),
                                   "agents": [{**a.to_dict(), "live": self.store.agent_statuses().get(a.name),
                                               "info": orch.runtimes[a.name].info() if orch and a.name in orch.runtimes else None} for a in team],
                                   "limits": orch.limits() if orch else [],
                                   "conversations": [{"agent": a.name, "thread_id": int(self.store.get_control("watchdog_thread:" + a.name, "0") or 0)}
                                                     for a in team if self.store.get_control("watchdog_thread:" + a.name, "0") != "0"],
                                   "available_models": [{"id": m.id, "backend": b} for m, b in catalog_available()]})
            async def recover(args: dict[str, Any], ctx: ToolContext) -> str:
                try:
                    result = await self.recover(args, target)
                except Exception as exc:
                    result = f"ERROR: {exc}"
                record("tool", result)
                self.store.log_event("watchdog", "recovery", result)
                return result
            async def talk(args: dict[str, Any], ctx: ToolContext) -> str:
                name = str(args.get("agent") or target)
                result = json.dumps(self.talk(name, str(args["message"])), ensure_ascii=False)
                record("tool", result)
                return result
            schema = {"type": "object", "properties": {"action": {"type": "string", "enum": ["wake", "resume_session", "restart_session", "swap_master", "resume_team", "pause_team", "probe_limits"]},
                       "agent": {"type": "string"}, "model": {"type": "string"}, "effort": {"type": "string"}}, "required": ["action"], "additionalProperties": False}
            specs = [ToolSpec("watchdog_status", "Inspect team status, saved sessions, limits and available models.", {"type": "object", "properties": {}}, status),
                     ToolSpec("talk_to_agent", "Send a real message to an active teammate on its watchdog discussion thread. Returns thread_id; read_thread shows their actual replies. A message alone does not authorize a recovery cycle.",
                              {"type": "object", "properties": {"agent": {"type": "string"}, "message": {"type": "string"}}, "required": ["agent", "message"], "additionalProperties": False}, talk),
                     ToolSpec("recover_agent", "Recover the selected agent. swap_master requires an available model; resume preserves history, restart archives the old session. Team-wide actions must be explicitly requested.", schema, recover),
                     *[t for t in TOOLS if t.name in ("finish_cycle", "update_notes", "list_threads", "read_thread")]]
            ctx = ToolContext(AgentSpec("local-watchdog", "watchdog", "Local watchdog", "Recover the team"),
                              lambda: load_team(self.entry.path), tree, self.store, self.memory, cfg, CycleState(),
                              hooks=ToolHooks(complete_task=lambda: merge_task(self.entry.path, tree, "local-watchdog")), tool_specs=specs)
            history = [{"role": m["role"], "body": m["body"][:4000]} for m in self.store.watchdog_history(limit=40)[:-1]]
            prompt = "Earlier conversation (context only):\n" + json.dumps(history, ensure_ascii=False) + "\nWatchdog notes:\n" + self.memory.notes()[:8000] + "\nSelected teammate: " + target + "\nCURRENT human message:\n" + message
            system = SYSTEM
            if selection["backend"] == "codex":
                system = system.replace("Do not edit project files, change the goal, hire, retire, or run shell commands.",
                                        "Codex native shell, file and network tools are unrestricted. Use them as needed for the authorized recovery. "
                                        "Work in your own checkout; commit and test any file changes before finish_cycle integrates them.")
            result = await backend.run_cycle(ctx=ctx, system=system, prompt=prompt, model=model, effort=effort,
                                             should_stop=lambda: self.closed, log=lambda line: self.memory.activity("text", line))
            usage_sample = record_usage(self.store, "local-watchdog", result.usage, backend=selection["backend"],
                                        model=model, cost_status=result.cost_status or "untracked")
            await record_cycle_commits(self.store, tree, "local-watchdog", baseline)
            reply = result.summary or result.error or "The watchdog ended without a reply. Please check status before retrying."
            if result.outcome == "finished" and ctx.cycle.task_complete and not ctx.cycle.task_merged:
                await merge_task(self.entry.path, tree, "local-watchdog")
            if result.outcome != "finished":
                reply = f"Watchdog {result.outcome}: {reply}"
            record("assistant", reply)
            add_usage(self.memory.state, result.usage, selection["backend"], result.cost_status)
            self.memory.save_state()
            self.memory.journal({"summary": reply, "model": model, "target": target, "usage": result.usage, "usage_sample": usage_sample,
                                 "usage_accounting_version": ACCOUNTING_VERSION, "backend": selection["backend"], "cost_status": result.cost_status})
        except asyncio.CancelledError:
            record("assistant", "Watchdog request interrupted. Completed recovery actions remain recorded.")
            raise
        except Exception as exc:
            record("assistant", f"Watchdog error: {exc}")
        finally:
            self.store.set_control("watchdog_busy", "0")

    def talk(self, name: str, message: str) -> dict[str, Any]:
        team = self.entry.orchestrator.team if self.entry.orchestrator else load_team(self.entry.path)
        if name not in {a.name for a in team if a.status != "retired"}:
            raise ValueError(f"No active agent @{name}")
        message = message.strip()
        if not message:
            raise ValueError("A message is required")
        key = "watchdog_thread:" + name
        tid = int(self.store.get_control(key, "0") or 0)
        body = f"@{name} {message}"
        if tid and self.store.get_thread(tid):
            self.store.add_comment(tid, "local-watchdog", body)
        else:
            thread = self.store.create_thread("local-watchdog", f"Watchdog conversation with {name}", body)
            tid = thread["id"]
            self.store.set_control(key, str(tid))
            self.store.log_event("watchdog", "dialogue", json.dumps({"target": name, "speaker": "watchdog", "text": body, "thread_id": tid}, ensure_ascii=False))
        # Store listeners are local to a connection, so explicitly notify the
        # orchestrator when the watchdog uses its independent connection.
        orch = self.entry.orchestrator
        if orch:
            orch._wake(name)
        return {"agent": name, "thread_id": tid, "message": body, "reply_pending": True}

    async def recover(self, args: dict[str, Any], default_target: str) -> str:
        action = args["action"]
        await self.hub.load(self.entry.id)
        orch = self.entry.orchestrator
        if action == "probe_limits":
            return json.dumps(await orch.probe_now())
        if action in ("resume_team", "pause_team"):
            orch.store.set_running(action == "resume_team", by="watchdog")
            return "Team resumed." if action == "resume_team" else "Team paused."
        name = "master" if action == "swap_master" else str(args.get("agent") or default_target)
        rt = orch.runtimes.get(name)
        if not rt or rt.current().status == "retired":
            return f"ERROR: No active agent @{name}."
        if action == "resume_session" and rt.cycle_lock.locked():
            return f"ERROR: @{name} already has an active cycle. Pause it or wait before resuming its saved session."
        conversation = self.talk(name, f"Watchdog requested {action}. Please acknowledge and report your actual session/task status on this thread.")
        if action in ("swap_master", "restart_session"):
            if action == "swap_master":
                available = {m.id for m, _ in catalog_available()}
                if args.get("model") not in available:
                    return "ERROR: swap_master requires an available model."
            rt.interrupt_requested = True
            rt.wake()
            try:
                await asyncio.wait_for(rt.cycle_lock.acquire(), timeout=45)
            except asyncio.CancelledError:
                rt.interrupt_requested = False
                rt.wake()
                raise
            except TimeoutError:
                rt.interrupt_requested = False
                rt.wake()
                return "ERROR: Agent has not reached a safe stopping point. Its session was preserved; retry later."
            try:
                if action == "swap_master":
                    result = await orch.set_model(name, args["model"], args.get("effort"), by="watchdog")
                    if result.startswith("ERROR:"):
                        return result
                st = rt.memory.state
                rt.memory.journal({"recovery": action, "previous_session_id": st.session_id, "previous_task": st.current_task})
                transcript = rt.memory.load_transcript()
                if transcript:
                    (rt.memory.dir / ("transcript-" + now_iso().replace(":", "-") + ".json")).write_text(json.dumps(transcript))
                st.session_id, st.resume_pending, st.session_cycles = None, False, 0
                rt.memory.clear_transcript()
                rt.memory.save_state()
            finally:
                rt.cycle_lock.release()
                rt.interrupt_requested = False
        if action == "resume_session" and rt.memory.state.session_id:
            rt.memory.state.resume_pending = True
            rt.memory.save_state()
        if rt.task is None or rt.task.done():
            rt.task = asyncio.create_task(rt.loop(), name="agent:" + name)
        # Persistent one-cycle authorization bypasses pause/idle/cycle caps, never usage limits.
        orch.store.set_control("recovery:" + name, action)
        rt.wake()
        return f"@{name}: {action} requested; one cycle authorized. Team-wide pause and usage limits are preserved. Conversation: #{conversation['thread_id']}." + (f" New master model: {args['model']}." if action == "swap_master" else "")

    async def close(self) -> None:
        self.closed = True
        if self.task and not self.task.done():
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        self.store.close()
