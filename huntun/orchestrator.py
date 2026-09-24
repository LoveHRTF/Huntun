"""Runs every agent as an independent asyncio task: wake-ups on @mentions, the pause gate,
staggered start, periodic leadership reviews, and hiring / retiring at runtime."""
from __future__ import annotations

import asyncio
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from .backends import Backend, make_backend
from .config import (
    agents_dir,
    db_path,
    load_config,
    load_team,
    now_iso,
    resolve_backend,
    save_config,
    save_team,
    slugify,
)
from .gitops import ensure_repo, recent_log
from .gitops import status as git_status
from .memory import AgentMemory
from .models import EFFORTS, backend_for_model, model_info
from .roles import ROLE_CATALOG, build_system_prompt, is_lead
from .store import Store
from .tools import ToolContext, ToolHooks, collect_inbox, format_inbox
from .types import AgentSpec, CycleState, HuntunConfig


def _ts() -> str:
    return datetime.now().strftime("%H:%M:%S")


def _age_sec(iso: str | None) -> float:
    if not iso:
        return float("inf")
    try:
        return time.time() - datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return float("inf")


class Orchestrator:
    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace
        self.config: HuntunConfig = load_config(workspace)
        self.team: list[AgentSpec] = load_team(workspace)
        self.backend_name = resolve_backend(self.config)
        self.backend: Backend = make_backend(self.backend_name, self.config)
        self.backends: dict[str, Backend] = {self.backend_name: self.backend}
        self.watchdogs: dict[str, asyncio.Task[None]] = {}
        self.store = Store(db_path(workspace))
        self.runtimes: dict[str, AgentRuntime] = {}
        self.stopping = False
        self.probe_interval_sec = 120.0  # once the announced reset time is near, retry this often until the window reopens
        self.probe_lead_sec = 45.0  # start checking this long before the announced reset
        self._probing = False
        self.loop: asyncio.AbstractEventLoop | None = None
        self.store.on("mention", self._on_mention)
        self.store.on("control", lambda _running: self._wake_all())

    # Store listeners may fire on the web server thread, so wake-ups hop onto the event loop.
    def _on_mention(self, target: str) -> None:
        if target == "all":
            self._wake_all()
        else:
            self._wake(target)

    def _wake(self, name: str) -> None:
        rt = self.runtimes.get(name)
        if rt and self.loop:
            self.loop.call_soon_threadsafe(rt.wake)

    def _wake_all(self) -> None:
        for name in list(self.runtimes):
            self._wake(name)

    def backend_name_for(self, spec: AgentSpec) -> str:
        return spec.backend or self.backend_name

    def backend_for(self, spec: AgentSpec) -> Backend:
        """The backend that runs this agent (agents may sit on different vendors); created on first use."""
        name = self.backend_name_for(spec)
        if name not in self.backends:
            try:
                self.backends[name] = make_backend(name, self.config)
            except Exception as e:
                self.log(spec.name, f"backend {name} unavailable ({e}); falling back to {self.backend_name}")
                spec.backend = ""
                return self.backend
        return self.backends[name]

    def log(self, agent: str, line: str) -> None:
        print(f"{_ts()} [{agent}] {line}", flush=True)

    def running(self) -> bool:
        return not self.stopping and self.store.is_running()

    def active_team(self) -> list[AgentSpec]:
        return [a for a in self.team if a.status != "retired"]

    async def start(self) -> None:
        self.loop = asyncio.get_running_loop()
        await ensure_repo(self.workspace)
        for spec in self.team:
            self.store.set_agent_status(spec.name, "idle" if spec.status == "active" else spec.status, "")
            if spec.status != "retired":
                self.spawn(spec)

    def spawn(self, spec: AgentSpec) -> None:
        if spec.name in self.runtimes:
            return
        rt = AgentRuntime(self, spec)
        self.runtimes[spec.name] = rt
        rt.task = asyncio.create_task(rt.loop(), name=f"agent:{spec.name}")

    # ---- token-limit watchdog: not an agent, so it keeps working when no model call can succeed ----

    def _lk(self, backend: str, key: str) -> str:
        return f"limit:{backend}:{key}"

    def limited_backends(self) -> list[str]:
        return [b for b in self.backends if self.store.get_control(self._lk(b, "paused"), "0") == "1"]

    def backend_limited(self, backend: str) -> bool:
        return self.store.get_control(self._lk(backend, "paused"), "0") == "1"

    def limits(self) -> dict[str, Any]:
        """Usage-limit status: aggregate counters plus one entry per limited backend (vendor)."""
        g = self.store.get_control
        per: dict[str, Any] = {}
        for b in self.limited_backends():
            resets = float(g(self._lk(b, "resets_at"), "0") or 0) or None
            per[b] = {"since": g(self._lk(b, "since"), "") or None, "resets_at": resets, "reason": g(self._lk(b, "reason"), "") or None,
                      "next_probe_at": float(g(self._lk(b, "next_probe"), "0") or 0) or None, "last_probe": g(self._lk(b, "last_probe"), "") or None, "auto": bool(resets)}
        first = next(iter(per.values()), {})
        return {
            "paused": bool(per),
            "backends": per,
            "since": first.get("since"),
            "resets_at": first.get("resets_at"),
            "reason": "; ".join(f"{b}: {v['reason']}" for b, v in per.items() if v.get("reason")) or None,
            "pause_count": int(g("limit_pause_count", "0") or 0),
            "resume_count": int(g("limit_resume_count", "0") or 0),
            "gate": g("resume_gate", "") or None,
            "next_probe_at": first.get("next_probe_at"),
            "last_probe": first.get("last_probe"),
            "auto": bool(first.get("auto")),
        }

    def on_limit(self, agent: str, backend: str, reason: str, resets_at: float | None) -> None:
        """An agent hit its vendor's usage limit: pause every agent on that backend and wait for the window to reopen."""
        if self.backend_limited(backend):
            return
        s = self.store.set_control
        s(self._lk(backend, "paused"), "1")
        s(self._lk(backend, "since"), now_iso())
        s(self._lk(backend, "resets_at"), str(resets_at or 0))
        s(self._lk(backend, "reason"), f"@{agent}: {reason}"[:300])
        s("limit_pause_count", str(int(self.store.get_control("limit_pause_count", "0")) + 1))
        when = f", provider says it resets at {datetime.fromtimestamp(resets_at).strftime('%H:%M')}" if resets_at else ""
        affected = [a.name for a in self.active_team() if self.backend_name_for(a) == backend]
        self.store.log_event("watchdog", "limit", f"Usage limit reached on {backend} (by @{agent}); paused {', '.join('@' + n for n in affected)}{when}")
        self.log("watchdog", f"usage limit on {backend} hit by {agent}: {reason}")
        self._wake_all()
        wd = self.watchdogs.get(backend)
        if wd is None or wd.done():
            self.watchdogs[backend] = asyncio.create_task(self._watch_limits(backend), name=f"limit-watchdog:{backend}")

    async def _watch_limits(self, backend: str) -> None:
        """Sleeps until just before the provider's announced reset, then retries every couple of minutes.
        With no announced reset there is nothing to wait for: the human checks with the button (probe_now)."""
        while not self.stopping and self.backend_limited(backend):
            resets_at = float(self.store.get_control(self._lk(backend, "resets_at"), "0") or 0)
            if not resets_at:
                self.store.set_control(self._lk(backend, "next_probe"), "0")
                return
            wait = max(5.0, resets_at - self.probe_lead_sec - time.time()) if resets_at - self.probe_lead_sec > time.time() else self.probe_interval_sec
            self.store.set_control(self._lk(backend, "next_probe"), str(time.time() + wait))
            await asyncio.sleep(wait)
            if self.stopping:
                return
            await self.probe_now(backend)

    async def probe_now(self, backend: str | None = None) -> dict[str, Any]:
        """One check of a limited vendor (automatic near the reset time, or on the human's request)."""
        targets = [backend] if backend else self.limited_backends()
        targets = [b for b in targets if self.backend_limited(b)]
        if not targets:
            return {"ok": True, "paused": False}
        if self._probing:
            return {"ok": False, "paused": True, "message": "a check is already running"}
        self._probing = True
        results: dict[str, bool] = {}
        try:
            for b in targets:
                try:
                    results[b] = await asyncio.wait_for(self.backends[b].probe(), timeout=120)
                except Exception as e:
                    self.log("watchdog", f"probe of {b} failed: {e}")
                    results[b] = False
                self.store.set_control(self._lk(b, "last_probe"), now_iso())
                if results[b]:
                    self._lift_limit(b)
                else:
                    self.log("watchdog", f"usage limit on {b} still in force")
                    self.store.log_event("watchdog", "limit", f"Checked {b}: usage limit still in force")
        finally:
            self._probing = False
        ok = all(results.values())
        return {"ok": ok, "paused": bool(self.limited_backends()), "results": results, "message": "" if ok else "still limited"}

    def _lift_limit(self, backend: str) -> None:
        s = self.store.set_control
        s(self._lk(backend, "paused"), "0")
        s(self._lk(backend, "next_probe"), "0")
        s(self._lk(backend, "resets_at"), "0")
        s("limit_resume_count", str(int(self.store.get_control("limit_resume_count", "0")) + 1))
        master = self.runtimes.get("master")
        master_affected = master is not None and self.backend_name_for(master.current()) == backend
        if master_affected:
            # Master-first resume: the rest of the team waits for the master's first post-limit cycle.
            s("resume_gate", "master")
            s("resume_gate_since", now_iso())
            s("resume_gate_cycles", str(master.memory.state.cycles))
        self.store.log_event("watchdog", "resume", f"Tokens available again on {backend}; " + ("master resumes first and releases the team" if master_affected else "its agents resume"))
        self.log("watchdog", f"usage limit on {backend} lifted")
        self._wake_all()

    async def set_goal(self, goal: str, definition_of_done: str) -> str:
        goal = goal.strip()
        if not goal:
            return "ERROR: goal cannot be empty"
        self.config.goal = goal
        if definition_of_done.strip():
            self.config.definition_of_done = definition_of_done.strip()
        save_config(self.workspace, self.config)
        self.store.log_event("master", "goal", f"Goal updated: {goal[:200]}")
        return "Goal updated. Post an @all note so the team knows what changed."

    async def release_team(self) -> str:
        if self.store.get_control("resume_gate", "") != "master":
            return "The team is not waiting on you; nothing to release."
        self.store.set_control("resume_gate", "")
        self.store.log_event("master", "resume", "Master released the team after the usage-limit pause")
        self._wake_all()
        return "Team released; everyone resumes their cycles."

    def gate_blocks(self, name: str) -> bool:
        """True while non-master agents must wait for the master's first post-limit cycle."""
        if name == "master" or self.store.get_control("resume_gate", "") != "master":
            return False
        master = self.runtimes.get("master")
        if master is None:
            return False
        if master.memory.state.cycles > int(self.store.get_control("resume_gate_cycles", "0") or 0):
            self.store.set_control("resume_gate", "")  # master finished a cycle: auto-release
            return False
        since = self.store.get_control("resume_gate_since", "")
        if since and _age_sec(since) > 900:  # safety valve: never hold the team longer than 15 minutes
            self.store.set_control("resume_gate", "")
            return False
        return True

    async def stop(self) -> None:
        self.stopping = True
        for wd in self.watchdogs.values():
            wd.cancel()
        self._wake_all()
        await asyncio.gather(*(rt.task for rt in self.runtimes.values() if rt.task), return_exceptions=True)
        self.store.close()

    async def hire(self, spec_in: dict[str, str]) -> str:
        name = slugify(spec_in["name"])
        if not name:
            return "ERROR: invalid name"
        if any(a.name == name and a.status != "retired" for a in self.team):
            return f"ERROR: @{name} already exists"
        if self.config.max_agents and len([a for a in self.active_team() if a.role != "master"]) >= self.config.max_agents:
            return f"ERROR: the human capped the team at {self.config.max_agents} agents; retire someone first or ask @human to raise the cap"
        role = spec_in["role"] if spec_in["role"] in ROLE_CATALOG and spec_in["role"] != "master" else "fullstack"
        existing = next((a for a in self.team if a.name == name), None)
        model = spec_in.get("model") or None
        effort = spec_in.get("effort") or None
        spec = AgentSpec(name=name, role=role, title=spec_in.get("title") or ROLE_CATALOG[role].title, brief=spec_in["brief"],
                         model=model if model_info(model) else None, effort=effort if effort in EFFORTS else None,
                         backend=backend_for_model(model, default="") if model_info(model) else "",
                         created_at=existing.created_at if existing else now_iso())
        self.team = [a for a in self.team if a.name != name] + [spec]
        save_team(self.workspace, self.team)
        self.store.set_agent_status(name, "idle", "")
        self.store.log_event("master", "hire", f"@{name} ({spec.title})")
        self.spawn(spec)
        return f"Hired @{name} as {spec.title}. Tag them on the board to give them work."

    async def set_model(self, name: str, model: str | None, effort: str | None) -> str:
        spec = next((a for a in self.team if a.name == name), None)
        if not spec:
            return f"ERROR: no agent named @{name}"
        if model and not model_info(model):
            return f"ERROR: unknown model {model}"
        if effort and effort not in EFFORTS:
            return f"ERROR: effort must be one of {', '.join(EFFORTS)}"
        if model:
            spec.model = model
            spec.backend = backend_for_model(model, default="")
        if effort:
            spec.effort = effort
        save_team(self.workspace, self.team)
        self.store.log_event("master", "model", f"@{name} -> {spec.model or 'default'} / {spec.effort or 'default'}")
        return f"@{name} now uses model {spec.model or 'default'} at {spec.effort or 'default'} effort (from its next cycle)."

    async def retire(self, name: str) -> str:
        spec = next((a for a in self.team if a.name == name), None)
        if not spec:
            return f"ERROR: no agent named @{name}"
        if spec.role == "master":
            return "ERROR: the master cannot retire itself"
        spec.status = "retired"
        save_team(self.workspace, self.team)
        self.store.set_agent_status(name, "retired", "")
        self.store.log_event("master", "retire", f"@{name}")
        self._wake(name)
        return f"Retired @{name}."


class AgentRuntime:
    def __init__(self, orch: Orchestrator, spec: AgentSpec) -> None:
        self.orch = orch
        self.spec = spec
        self.memory = AgentMemory(agents_dir(orch.workspace), spec.name)
        self._wake_event = asyncio.Event()
        self.task: asyncio.Task[None] | None = None

    @property
    def name(self) -> str:
        return self.spec.name

    def current(self) -> AgentSpec:
        return next((a for a in self.orch.team if a.name == self.name), self.spec)

    def info(self) -> dict[str, Any]:
        """Summary for the web UI: effective model, effort, cycles, usage totals, last summary."""
        spec, cfg, st = self.current(), self.orch.config, self.memory.state
        b = self.orch.backend_name_for(spec)
        return {
            "backend": b,
            "model": spec.model or cfg.model or ("claude-opus-5-5" if b == "api" else "Codex default" if b == "codex" else "Kimi default" if b == "kimi" else "DeepSeek default" if b == "deepseek" else "Ollama default" if b == "ollama" else "Claude Code default"),
            "effort": spec.effort or (cfg.lead_effort if self.lead else cfg.worker_effort),
            "cycles": st.cycles,
            "reviews": st.review_count,
            "usage": st.usage_totals,
            "current_task": st.current_task,
            "last_summary": st.last_summary,
            "last_cycle_at": st.last_cycle_at,
            "waiting": st.waiting,
            "resume_pending": st.resume_pending,
            "touched_files": st.touched_files,
            "context_tokens": st.context_tokens,
            "context_limit": st.context_limit,
            "compactions": st.compactions,
            "compacting": st.compacting,
            "activity": self.memory.last_activity,
        }

    @property
    def lead(self) -> bool:
        return is_lead(self.spec)

    def wake(self) -> None:
        self._wake_event.set()

    async def _wait(self, seconds: float) -> None:
        self._wake_event.clear()
        try:
            await asyncio.wait_for(self._wake_event.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass

    def _status(self, status: str, task: str = "") -> None:
        self.orch.store.set_agent_status(self.name, status, task)

    async def _wait_for_turn(self) -> None:
        """Master kicks off first, then the team lead, then everyone else (or after a 5 minute timeout)."""
        orch = self.orch

        def cycles(name: str) -> int:
            rt = orch.runtimes.get(name)
            return rt.memory.state.cycles if rt else 1

        def gate() -> bool:
            if self.spec.role == "master":
                return True
            if self.spec.role == "team-lead":
                return cycles("master") > 0
            lead = next((a for a in orch.team if a.role == "team-lead"), None)
            return cycles("master") > 0 and (lead is None or cycles(lead.name) > 0)

        deadline = time.time() + 300
        while not gate() and time.time() < deadline and not orch.stopping:
            await asyncio.sleep(3)

    async def loop(self) -> None:
        orch = self.orch
        try:
            await self._wait_for_turn()
            backoff = 30.0
            while not orch.stopping:
                spec = self.current()
                if spec.status == "retired":
                    self._status("retired")
                    return
                if not orch.running():
                    self._status("paused", self.memory.state.current_task)
                    await self._wait(2)
                    continue
                my_backend = orch.backend_name_for(spec)
                if orch.backend_limited(my_backend):
                    self._status(f"paused (usage limit: {my_backend})", self.memory.state.current_task)
                    await self._wait(5)
                    continue
                if orch.gate_blocks(self.name):
                    self._status("waiting for master", self.memory.state.current_task)
                    await self._wait(5)
                    continue

                cfg, st = orch.config, self.memory.state
                inbox = orch.store.peek_inbox_count(self.name) > 0
                resume = self.memory.load_transcript() is not None or (st.resume_pending and bool(st.session_id))
                review_due = self.lead and st.cycles > 0 and _age_sec(st.last_review_at) > cfg.review_interval_min * 60
                idle_sec = cfg.lead_idle_interval_sec if self.lead else cfg.idle_interval_sec
                since_cycle = _age_sec(st.last_cycle_at)
                idle_due = since_cycle > idle_sec and not st.waiting
                capped = cfg.max_cycles_per_agent > 0 and st.cycles >= cfg.max_cycles_per_agent

                kind = None
                if resume:
                    kind = "resume"
                elif inbox:
                    kind = "work"
                elif review_due:
                    kind = "review"
                elif idle_due and not capped:
                    kind = "work"

                if kind is None:
                    label = "done (cycle cap reached)" if capped else ("waiting for mention" if st.waiting else "idle")
                    self._status(label, st.current_task)
                    wait = 60.0 if (capped or st.waiting) else max(2.0, idle_sec - since_cycle)
                    await self._wait(min(wait, 60.0))
                    continue

                outcome = await self._run_one(kind)
                if outcome == "error":
                    await self._wait(backoff)
                    backoff = min(backoff * 2, 600)
                else:
                    backoff = 30.0
        except asyncio.CancelledError:
            raise
        except Exception as e:  # keep one crashed agent from taking the team down
            orch.log(self.name, f"loop crashed: {type(e).__name__}: {e}")
            self._status("crashed", str(e)[:200])

    async def _run_one(self, kind: str) -> str:
        orch = self.orch
        spec = self.current()
        cfg = orch.config
        cycle = CycleState()
        is_master = spec.role == "master"
        ctx = ToolContext(
            agent=spec, team=orch.active_team, workspace=orch.workspace, store=orch.store, memory=self.memory, config=cfg, cycle=cycle,
            hooks=ToolHooks(hire_agent=orch.hire if is_master else None, retire_agent=orch.retire if is_master else None,
                            set_agent_model=orch.set_model if is_master else None, resume_team=orch.release_team if is_master else None,
                            set_goal=orch.set_goal if is_master else None),
        )
        prompt = "" if kind == "resume" else await self._build_prompt(kind)
        label = "progress review" if kind == "review" else (self.memory.state.current_task or spec.brief[:80])
        self._status("resuming" if kind == "resume" else "working", label)
        orch.log(self.name, f"cycle {self.memory.state.cycles + 1} ({kind}) started")
        self.memory.activity("cycle", f"Cycle {self.memory.state.cycles + 1} ({kind}) started")
        if prompt:
            self.memory.activity("prompt", prompt)
        if kind == "review":
            self.memory.state.last_review_at = now_iso()
            self.memory.state.review_count += 1
            self.memory.save_state()

        backend = orch.backend_for(spec)
        result = await backend.run_cycle(
            ctx=ctx,
            system=build_system_prompt(spec, cfg, orch.team, orch.backend_name_for(spec)),
            prompt=prompt,
            model=spec.model or cfg.model,
            effort=spec.effort or (cfg.lead_effort if self.lead else cfg.worker_effort),
            should_stop=lambda: not orch.running() or self.current().status == "retired",
            log=lambda line: orch.log(self.name, line),
        )

        usage = " ".join(f"{k}={v:g}" for k, v in result.usage.items())
        totals = self.memory.state.usage_totals
        for k, v in result.usage.items():
            totals[k] = round(totals.get(k, 0.0) + float(v), 6)
        self.memory.save_state()
        if result.outcome == "paused":
            orch.log(self.name, f"cycle paused ({usage})")
            self.memory.activity("cycle", "Paused mid-cycle; will resume where it left off")
            self._status("paused", self.memory.state.current_task)
            return "paused"
        if result.outcome == "limit":
            orch.log(self.name, f"usage limit: {result.error} ({usage})")
            self.memory.activity("error", f"Usage limit: {result.error}. Cycle will resume when tokens are available.")
            self._status(f"paused (usage limit: {orch.backend_name_for(spec)})", self.memory.state.current_task)
            orch.on_limit(self.name, orch.backend_name_for(spec), result.error or "usage limit", result.resets_at)
            return "paused"
        if result.outcome == "error":
            orch.log(self.name, f"cycle error: {result.error} ({usage})")
            orch.store.log_event(self.name, "error", result.error or "unknown error")
            self.memory.activity("error", result.error or "unknown error")
            self._status("error", result.error or "")
            return "error"

        st = self.memory.state
        st.cycles += 1
        st.last_summary = result.summary
        if result.next_task:
            st.current_task = result.next_task
        st.waiting = cycle.wait_for_mention
        st.last_cycle_at = now_iso()
        if not st.last_review_at:  # the review clock starts after the first cycle
            st.last_review_at = st.last_cycle_at
        self.memory.save_state()
        self.memory.journal({"cycle": st.cycles, "kind": kind, "summary": result.summary, "next": result.next_task,
                             "commits": cycle.commits, "tool_calls": cycle.tool_calls, "usage": result.usage})
        orch.store.log_event(self.name, "cycle", f"#{st.cycles} {kind}: {result.summary[:300]} [{usage}]")
        orch.log(self.name, f"cycle {st.cycles} finished: {result.summary[:120]} ({usage})")
        self.memory.activity("cycle", f"Cycle {st.cycles} finished ({usage}). Summary: {result.summary}\nNext: {result.next_task}")
        self._status("idle", st.current_task)
        return "finished"

    async def _build_prompt(self, kind: str) -> str:
        orch, st, spec = self.orch, self.memory.state, self.current()
        ctx_for_inbox = ToolContext(agent=spec, team=orch.active_team, workspace=orch.workspace, store=orch.store, memory=self.memory, config=orch.config, cycle=CycleState())
        items = collect_inbox(ctx_for_inbox)
        if items:
            st.waiting = False
        inbox_text = format_inbox(items) if items else "(empty)"
        log, status = await asyncio.gather(recent_log(orch.workspace, 12), git_status(orch.workspace))
        threads = "\n".join(f'- #{t["id"]} "{t["title"]}" by @{t["author"]} ({t["comment_count"]} comments, {t["last_activity"]})' for t in orch.store.list_threads(15))
        notes = self.memory.notes().strip()
        journal = "\n".join(f"- cycle {j.get('cycle')} ({j.get('kind')}): {str(j.get('summary', ''))[:400]}" for j in self.memory.recent_journal(3))
        first = st.cycles == 0
        ack_note = ""
        if spec.role == "master" and any(i.author == "human" for i in items):
            ack_note = ("\n\n@human tagged you. Before anything else, reply on that thread to confirm you received the message and say what you will do about it and by when. "
                        "Then act: anything technical (architecture, design, stack, code, technical questions or estimates) goes to @team-lead on that thread or a task "
                        "thread, with the context and the ask, asking them to tag you when done; staffing and goal matters are yours. If it implies a staffing change "
                        "or a change of goal or definition of done, propose the change on the board and wait for @human to confirm in that thread before applying it "
                        "(hire_agent, retire_agent, set_agent_model, and set_goal require the confirmation thread id). Finally get back to @human on the same thread "
                        "with the outcome; if you are waiting on the team, say so and set wait_for_mention so you return to close the loop when they report back.")
        gate_note = ""
        if spec.role == "master" and orch.store.get_control("resume_gate", "") == "master":
            gate_note = (f"\n\nNOTE: the team (on your vendor) was paused automatically when the provider's usage limit was hit (at {orch.store.get_control('resume_gate_since', '?')}) "
                         "and tokens are available again. You have been resumed first; the others are waiting for you. Check the board for anything that needs "
                         "redirecting, post a short @all note that work resumes (and any change of priorities), then call resume_team to release everyone.")

        if kind == "review":
            extra = ("Then update the Delivery status thread (create it if missing) with milestones against the definition of done, what is done / in progress / "
                     "blocked / at risk, owners, and the next checkpoint. Audit scope discipline: compare who did what (commits, files touched, threads) against each agent's role "
                     "and the tasks assigned to them; anyone working outside their role or their assigned scope, or work that no role on the team owns, is a planning "
                     "mistake of yours to correct now (have out-of-scope changes reverted or handed to the owner, propose a hire or a brief change to @human, or "
                     "reassign to the right owner, and say so on the board). Then decide whether the team shape is right: "
                     "propose hires, retirements, or model changes to @human if the delivery needs them. You own the outcome; do not fix anything yourself, assign it."
                     if spec.role == "master" else "Update the plan thread if the roadmap changed.")
            instructions = (
                f"This is a scheduled PROGRESS REVIEW (#{st.review_count}). Inspect the git log and recent commits (git show / diff as needed), "
                "read the recent board threads, and judge whether the team is converging on the goal at a high quality bar. Then post a "
                f'"Progress review #{st.review_count}" thread that states: what is done, what is off-track or missing, concrete asks per agent '
                f"with @mentions, and the raised bar for the next period. {extra} Finish with finish_cycle."
            )
        elif first and spec.role == "master":
            instructions = (
                "This is your FIRST cycle. The human confirmed the goal and definition of done and approved your proposed plan (see the goal and plan "
                "threads on the board; the roster reflects any changes they made). Post a short kickoff thread: the goal and definition of done, who "
                "owns what, and the working agreements (commit often, tests required, review flow). Tag @team-lead to write the architecture/plan "
                "thread and assign the first tasks, and tag @all so everyone reads it. Open a \"Delivery status\" thread you will keep current: milestones "
                "against the definition of done, owners, next checkpoint. You own the team and the delivery; you manage, you do not build. Then update "
                "your notes and finish_cycle."
            )
        elif first and spec.role == "team-lead":
            instructions = (
                "This is your FIRST cycle. Read the kickoff thread from @master (if any). Decide the architecture and stack, write it into the repo "
                "(e.g. docs/ARCHITECTURE.md and a README skeleton) and commit. Post a plan thread with milestones and assign first tasks to each "
                "teammate by @mentioning them with specific, small, independent tasks. Update your notes and finish_cycle."
            )
        elif first:
            instructions = (
                "This is your FIRST cycle. Read the board (kickoff and plan threads) and the repository to understand the current state. Then start "
                "on your brief or the task assigned to you. If nothing is assigned yet and the plan is unclear, do preparatory work that is "
                "unambiguously yours (research, scaffolding in your area) and ask @team-lead a concise question on the board. Commit, update your "
                "notes, and finish_cycle."
            )
        elif spec.role == "master":
            instructions = (
                "React to inbox items first (reply, act, or explicitly ignore). Then do a management sweep: read the recent threads and git log, check that "
                "every open task has an owner and is moving, chase anything stale with a concrete ask to its owner, unblock or decide where the team is stuck, "
                "and note anything that puts the definition of done at risk in the Delivery status thread. You manage and own the delivery; if something "
                "needs building or fixing, assign it (or propose a hire to @human), never do it yourself. Update your notes and finish_cycle; if nothing "
                "needs you until someone reports back, set wait_for_mention."
            )
        else:
            instructions = (
                "React to inbox items first (reply, act, or explicitly ignore). Then continue with the task assigned to you, touching only what it "
                "covers; anything it needs outside your scope is asked for on the board from its owner (or @team-lead), never done by you. Verify your "
                "work by running it, commit with a clear summary, update your notes, and finish_cycle."
            )

        return f"""# Cycle {st.cycles + 1} ({kind}) — {now_iso()}

## Your notes (persistent memory)
{notes or "(empty — you have no notes yet)"}

## Recent cycles
{journal or "(none)"}
Current task: {st.current_task or "(none set)"}

## Inbox
{inbox_text}

## Repository
Recent commits:
{log}
Uncommitted changes:
{status or "(clean)"}
Files you touched since your last commit: {", ".join(st.touched_files) or "(none)"}

## Board (recent threads)
{threads or "(no threads yet)"}

## Instructions
{instructions}{ack_note}{gate_note}"""
