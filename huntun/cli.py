from __future__ import annotations

import argparse
import asyncio
import os
import signal
import sys
import webbrowser
from pathlib import Path

from . import __version__
from .backends import make_backend
from .config import (
    agents_dir,
    db_path,
    default_config,
    is_initialized,
    load_config,
    load_team,
    resolve_backend,
    save_config,
    save_team,
)
from .gitops import ensure_repo
from .master import (
    describe_workspace,
    draft_goal,
    goal_thread_body,
    master_spec,
    plan_team,
    plan_thread_body,
)
from .memory import AgentMemory
from .store import Store

EPILOG = """environment:
  ANTHROPIC_API_KEY              use the Anthropic API backend (or `ant auth login`)
  HUNTUN_BACKEND=api|claude-code force a backend (default: api if a key is set, else claude-code)
  HUNTUN_MODEL (claude-opus-5-5)   HUNTUN_LEAD_EFFORT (xhigh)  HUNTUN_WORKER_EFFORT (high)
  HUNTUN_REVIEW_INTERVAL_MIN (20)  HUNTUN_IDLE_INTERVAL_SEC (90)  HUNTUN_LEAD_IDLE_INTERVAL_SEC (600)
  HUNTUN_MAX_TOOL_CALLS (60)  HUNTUN_MAX_CYCLES (0=unlimited)  HUNTUN_PORT (4747)  HUNTUN_FALLBACKS (on|off)
"""


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="huntun", description="A persistent multi-agent software team.", epilog=EPILOG,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--version", action="version", version=f"huntun {__version__}")
    sub = p.add_subparsers(dest="cmd")

    def add_dir(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--dir", default=".", help="workspace directory (default: current directory)")

    s = sub.add_parser("serve", help="open the Huntun web app (default): pick a project directory, set a goal, run the team")
    s.add_argument("--port", type=int, default=int(os.environ.get("HUNTUN_PORT", "4747")), help="port (default 4747)")
    s.add_argument("--dir", help="also open this project directory right away")
    s.add_argument("--no-open", action="store_true", help="do not open a browser")

    s = sub.add_parser("init", help="plan the team for a goal from the command line (the web app can do this too)")
    add_dir(s)
    s.add_argument("--backend", choices=["api", "claude-code", "codex", "kimi"], help="model backend (default: auto-detect)")
    s.add_argument("--context", default="", help="extra context for the master: constraints, stack preferences, existing code")
    s.add_argument("goal", nargs="+", help="what the team should build")

    s = sub.add_parser("start", help="run all agents and the local discussion board")
    add_dir(s)
    s.add_argument("--port", type=int, help="board port (default from config, 4747)")
    s.add_argument("--paused", action="store_true", help="start with agents paused")

    for name, help_ in (("approve", "approve the proposed plan as-is (the web app lets you edit it first)"), ("pause", "pause every agent"), ("resume", "resume every agent"),
                        ("status", "show team, progress, and board summary"), ("team", "print the roster and briefs")):
        add_dir(sub.add_parser(name, help=help_))
    return p


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    if not args.cmd:
        args.cmd, args.port, args.dir, args.no_open = "serve", int(os.environ.get("HUNTUN_PORT", "4747")), None, False
    workspace = Path(args.dir).resolve() if args.dir else Path.cwd()
    try:
        if args.cmd == "serve":
            asyncio.run(cmd_serve(args.port, Path(args.dir).resolve() if args.dir else None, running=None, open_browser=not args.no_open))
        elif args.cmd == "init":
            asyncio.run(cmd_init(workspace, " ".join(args.goal).strip(), args.backend, args.context))
        elif args.cmd == "start":
            asyncio.run(cmd_start(workspace, args.port, args.paused))
        elif args.cmd == "approve":
            cmd_approve(workspace)
        elif args.cmd in ("pause", "resume"):
            cmd_control(workspace, args.cmd == "resume")
        elif args.cmd == "status":
            cmd_status(workspace)
        elif args.cmd == "team":
            cmd_team(workspace)
    except KeyboardInterrupt:
        pass
    except Exception as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)


def _require_init(workspace: Path) -> None:
    if not is_initialized(workspace):
        raise RuntimeError(f"{workspace} is not initialized; run `huntun init \"<goal>\"` there first")


async def cmd_init(workspace: Path, goal: str, backend: str | None, context: str) -> None:
    if not goal:
        raise RuntimeError('init needs a goal, e.g.  huntun init "Build a CLI todo app in Go with a TUI"')
    if is_initialized(workspace):
        raise RuntimeError(f"{workspace} is already initialized (delete .huntun/ to start over)")
    workspace.mkdir(parents=True, exist_ok=True)
    config = default_config(goal)
    config.context = context.strip()
    if backend:
        config.backend = backend
    backend_name = resolve_backend(config)
    config.backend = backend_name
    print(f"Workspace: {workspace}\nGoal: {goal}\nBackend: {backend_name}   Model: {config.model or '(backend default)'}\n")
    existing = describe_workspace(workspace)
    backend_obj = make_backend(backend_name, config)
    print("Master agent is checking the goal...")
    draft = await draft_goal(backend_obj, config, existing)
    print(f"\n{draft['message']}\n\nGoal as the master understands it:\n  {draft['goal']}\n\nDefinition of done:")
    for d in draft["definition_of_done"]:
        print(f"  - {d}")
    for label, items in (("Assumptions", draft["assumptions"]), ("Questions", draft["questions"])):
        if items:
            print(f"\n{label}:")
            for x in items:
                print(f"  - {x}")
    answer = input("\nConfirm this goal and definition of done? [Y/n, or type a reply for the master] ").strip()
    while answer and answer.lower() not in ("y", "yes"):
        if answer.lower() in ("n", "no"):
            raise RuntimeError("goal not confirmed; nothing was written")
        draft = await draft_goal(backend_obj, config, existing, conversation=f"Your previous proposal:\n{draft}\n\nHuman's reply:\n{answer}")
        print(f"\n{draft['message']}\n\nGoal: {draft['goal']}\nDefinition of done:\n" + "\n".join(f"  - {d}" for d in draft["definition_of_done"]))
        answer = input("\nConfirm now? [Y/n, or reply again] ").strip()
    config.goal, config.definition_of_done, config.goal_confirmed = draft["goal"], "\n".join(draft["definition_of_done"]), True
    save_config(workspace, config)
    await ensure_repo(workspace)
    store = Store(db_path(workspace))
    gt = store.create_thread("master", "Goal check: please confirm the goal and definition of done", goal_thread_body(draft))
    store.set_control("goal_thread_id", str(gt["id"]))
    store.add_comment(gt["id"], "human", "@master Confirmed (from the command line).")
    store.close()

    print("\nMaster agent is planning the team...")
    full_context = "\n\n".join(part for part in (context.strip(), existing) if part)
    rationale, agents = await plan_team(backend_obj, config, full_context)
    specs = [master_spec(), *agents]
    save_team(workspace, specs)
    store = Store(db_path(workspace))
    store.set_control("running", "0")
    store.set_control("plan_approved", "0")
    t = store.create_thread("master", "Proposed plan: please review", plan_thread_body(config, rationale, specs, revised=False))
    store.set_control("plan_thread_id", str(t["id"]))
    for a in specs:
        store.set_agent_status(a.name, "idle", "")
    store.close()

    print(f"\n{rationale}\n")
    for a in specs:
        print(f"  @{a.name:<16} {a.title:<26} {a.model or '(default)':<18} {a.effort or ''}")
    print(f"\nSaved to {workspace / '.huntun'}. The plan needs your approval: review it in the web app (huntun) or run: huntun approve --dir {workspace}")


async def cmd_serve(port: int, preopen: Path | None, running: bool | None, open_browser: bool) -> None:
    """Runs the hub web app. With `preopen`, that project is registered and loaded immediately."""
    from .hub import Hub
    from .server import start_server

    hub = Hub()
    hub.loop = asyncio.get_running_loop()
    server = start_server(hub, port)
    url = f"http://127.0.0.1:{port}"
    print(f"Huntun web app: {url}")
    if preopen is not None:
        entry = hub.add(str(preopen))
        if entry.state in ("proposed", "goal_proposed"):
            print(f"Project: {preopen} is waiting for you in the web app ({'plan approval' if entry.state == 'proposed' else 'goal confirmation'}).")
            url += f"#/w/{entry.id}"
        elif entry.state == "ready":
            await hub.load(entry.id, running=running)
            orch = entry.orchestrator
            if orch is not None:
                print(f"Project: {preopen}  Backend: {orch.backend_name}  Model: {orch.config.model or '(backend default)'}")
                print(f"Agents:  {', '.join('@' + a.name for a in orch.active_team())}")
                print("Agents are running. Ctrl-C to stop (progress is persisted; restarting resumes)." if orch.store.is_running()
                      else "Agents are paused. Press Start in the web app or run: huntun resume")
            url += f"#/w/{entry.id}"
        else:
            print(f"Project: {preopen} is not set up yet; finish the setup in the web app.")
            url += f"#/w/{entry.id}/setup"
    if open_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass
    print("Ctrl-C to stop.")

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    await stop.wait()
    print("\nStopping: waiting for agents to reach a safe point (in-flight tool calls finish first)...", flush=True)
    server.shutdown()
    try:
        await asyncio.wait_for(hub.shutdown(), timeout=30)
    except asyncio.TimeoutError:
        print("Some agents did not stop in time; their in-flight cycle will resume on the next start.")


async def cmd_start(workspace: Path, port: int | None, paused: bool) -> None:
    _require_init(workspace)
    await cmd_serve(port or load_config(workspace).port, workspace, running=not paused, open_browser=False)


def cmd_approve(workspace: Path) -> None:
    _require_init(workspace)
    store = Store(db_path(workspace))
    if store.get_control("plan_approved", "0") == "1":
        print("Plan is already approved.")
    else:
        store.set_control("plan_approved", "1")
        tid = int(store.get_control("plan_thread_id", "0") or 0)
        if tid:
            store.add_comment(tid, "human", "@master Plan approved. No changes. Go ahead and kick off when started.")
        store.log_event("human", "approve", "Plan approved")
        print("Plan approved. Next: huntun start --dir", workspace)
    store.close()


def cmd_control(workspace: Path, running: bool) -> None:
    _require_init(workspace)
    store = Store(db_path(workspace))
    store.set_running(running, "cli")
    store.close()
    print("Agents resumed." if running else "Agents paused (each finishes its current tool call, then waits).")


def cmd_status(workspace: Path) -> None:
    _require_init(workspace)
    config, specs = load_config(workspace), load_team(workspace)
    store = Store(db_path(workspace))
    live = store.agent_statuses()
    print(f"Goal: {config.goal}")
    print(f"State: {'running' if store.is_running() else 'paused'}   Backend: {config.backend}   Board: http://127.0.0.1:{config.port}\n")
    for a in specs:
        mem = AgentMemory(agents_dir(workspace), a.name)
        st = live.get(a.name, {}).get("status", a.status)
        print(f"@{a.name} ({a.title}) [{st}] cycles={mem.state.cycles}")
        if mem.state.current_task:
            print(f"   next: {mem.state.current_task}")
        if mem.state.last_summary:
            print(f"   last: {mem.state.last_summary[:160]}")
    print("\nRecent threads:")
    for t in store.list_threads(8):
        print(f"  #{t['id']} {t['title']} (@{t['author']}, {t['comment_count']} comments)")
    store.close()


def cmd_team(workspace: Path) -> None:
    _require_init(workspace)
    for a in load_team(workspace):
        print(f"@{a.name} — {a.title} [{a.status}]\n  {a.brief}\n")
