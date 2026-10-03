"""OpenAI Codex backend: runs each work cycle as a `codex exec` session (your ChatGPT / Codex login).

Codex's own tools cover files, shell, and web. Huntun's team tools (board, git commits, notes, cycle
control) are served to the session over a local streamable-HTTP MCP server started for the cycle.
A paused cycle keeps its Codex thread id and is resumed with `codex exec resume <id>` later.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import signal
import socket
import tempfile
import time
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

from ..models import codex_model_info
from ..tools import ToolContext, ToolSpec, available_tools, execute
from ..types import CycleResult, HuntunConfig
from . import continue_session, looks_like_limit
from .telemetry import LiveTelemetry, SessionTail


async def _event_lines(stream: asyncio.StreamReader) -> AsyncIterator[bytes]:
    """Read JSONL without StreamReader's 64 KiB per-line limit.

    Tool output and reasoning can occupy a single multi-megabyte event. Decode
    only complete events, so chunk boundaries cannot corrupt UTF-8 or JSON.
    """
    pending: list[bytes] = []
    while chunk := await stream.read(64 * 1024):
        parts = chunk.split(b"\n")
        if len(parts) == 1:
            pending.append(chunk)
            continue
        pending.append(parts[0])
        yield b"".join(pending)
        pending.clear()
        for part in parts[1:-1]:
            yield part
        if parts[-1]:
            pending.append(parts[-1])
    if pending:
        yield b"".join(pending)


async def _descendant_pids(parent: int) -> set[int]:
    """Include detached tool hosts, which can create their own process groups."""
    if os.name != "posix":
        return set()
    proc = await asyncio.create_subprocess_exec("ps", "-axo", "pid=,ppid=", stdout=asyncio.subprocess.PIPE,
                                                stderr=asyncio.subprocess.DEVNULL)
    raw, _ = await proc.communicate()
    rows = [tuple(map(int, line.split())) for line in raw.splitlines() if len(line.split()) == 2]
    owned = {parent}
    while True:
        found = {pid for pid, ppid in rows if ppid in owned} - owned
        if not found:
            return owned - {parent}
        owned.update(found)


CONTINUE_NOTE = "(New cycle in the same conversation. Your earlier cycles above are context only; the board, the repository and your notes are the truth now.)\n\n"
RESUME_PROMPT = (
    "You were paused mid-cycle by the orchestrator and are now resumed. Re-check the repository state (git status), "
    "continue exactly where you left off, and finish the cycle with the huntun finish_cycle tool."
)
DEFAULT_CONTEXT = 400_000
EFFORT_MAP = {"none": "none", "minimal": "minimal", "low": "low", "medium": "medium", "high": "high", "xhigh": "xhigh", "max": "xhigh"}


def context_settings(model: str) -> tuple[int, int]:
    """Maximum CLI window and a compaction threshold with room for the next turn."""
    info = codex_model_info(model)
    window = info.context if info else DEFAULT_CONTEXT
    cap = os.environ.get("HUNTUN_CODEX_CONTEXT", "").strip()
    if cap:
        try:
            requested = int(cap)
        except ValueError:
            raise ValueError("HUNTUN_CODEX_CONTEXT must be a positive integer") from None
        if requested <= 0:
            raise ValueError("HUNTUN_CODEX_CONTEXT must be a positive integer")
        window = min(window, requested)
    return window, max(1, window * 9 // 10)


def reasoning_effort(model: str, effort: str) -> str:
    info = codex_model_info(model)
    levels = info.reasoning_levels if info else ()
    if not levels:
        return EFFORT_MAP.get(effort, "xhigh" if effort == "ultra" else "high")
    if effort in levels:
        return effort
    order = ["none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"]
    requested = order.index(effort) if effort in order else order.index("high")
    lower = [v for v in levels if v in order and order.index(v) <= requested]
    return max(lower, key=order.index) if lower else levels[0]


def codex_binary() -> str | None:
    return os.environ.get("HUNTUN_CODEX_BIN") or shutil.which("codex")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class McpBridge:
    """Serves Huntun's team tools to a Codex process as a streamable-HTTP MCP server on localhost."""

    def __init__(self, specs: list[ToolSpec], ctx: ToolContext) -> None:
        self.specs = {s.name: s for s in specs}
        self.ctx = ctx
        self.port = _free_port()
        self.url = f"http://127.0.0.1:{self.port}/mcp"
        self._task: asyncio.Task[None] | None = None
        self._server: Any = None

    async def __aenter__(self) -> "McpBridge":
        import uvicorn
        from mcp import types
        from mcp.server.lowlevel import Server
        from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
        from starlette.applications import Starlette
        from starlette.routing import Mount

        specs, ctx = self.specs, self.ctx

        async def on_list_tools(_rc: Any, _params: Any) -> types.ListToolsResult:
            return types.ListToolsResult(tools=[types.Tool(name=s.name, description=s.description, inputSchema=s.input_schema) for s in specs.values()])

        async def on_call_tool(_rc: Any, params: types.CallToolRequestParams) -> types.CallToolResult:
            spec = specs.get(params.name)
            if spec is None:
                return types.CallToolResult(content=[types.TextContent(type="text", text=f"Unknown tool {params.name}")], isError=True)
            out, is_err = await execute(spec, params.arguments or {}, ctx)
            return types.CallToolResult(content=[types.TextContent(type="text", text=out)], isError=is_err)

        app = Server("huntun", on_list_tools=on_list_tools, on_call_tool=on_call_tool)
        manager = StreamableHTTPSessionManager(app=app, stateless=True, json_response=True)
        self._manager_cm = manager.run()
        await self._manager_cm.__aenter__()
        starlette = Starlette(routes=[Mount("/mcp", app=manager.handle_request)])
        config = uvicorn.Config(starlette, host="127.0.0.1", port=self.port, log_level="error", lifespan="off")
        self._server = uvicorn.Server(config)
        self._task = asyncio.create_task(self._server.serve())
        for _ in range(100):  # wait until the socket is bound
            if getattr(self._server, "started", False):
                break
            await asyncio.sleep(0.05)
        return self

    async def __aexit__(self, *_exc: Any) -> None:
        if self._server is not None:
            self._server.should_exit = True
        if self._task is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._task, timeout=5)
        with contextlib.suppress(Exception):
            await self._manager_cm.__aexit__(None, None, None)


class CodexBackend:
    name = "codex"

    def __init__(self, config: HuntunConfig) -> None:
        self.config = config
        self.codex = codex_binary()
        if not self.codex:
            raise RuntimeError("codex CLI not found on PATH (install with `npm i -g @openai/codex`, then `codex login`)")

    def _base_args(self, workspace: Path, model: str, effort: str, sandbox: str, mcp_url: str | None) -> list[str]:
        # Every Codex seat, planning call and resumed thread has unrestricted
        # native tools. Role is a team responsibility, not a CLI permission mode.
        args = [self.codex, "exec", "--json", "--skip-git-repo-check", "--strict-config", "-C", str(workspace),
                "--dangerously-bypass-approvals-and-sandbox", "--ignore-rules",
                "-c", 'approval_policy="never"',
                "-c", 'shell_environment_policy.inherit="all"',
                "-c", "shell_environment_policy.ignore_default_excludes=true",
                "-c", "shell_environment_policy.exclude=[]", "-c", "shell_environment_policy.include_only=[]"]
        info = codex_model_info(model)
        # Pin an otherwise implicit default so the window belongs to the model actually run.
        selected = model or (info.id if info else "")
        if selected:
            args += ["-m", selected]
        window, compact_at = context_settings(selected)
        args += ["-c", f"model_context_window={window}", "-c", f"model_auto_compact_token_limit={compact_at}"]
        if effort:
            args += ["-c", f'model_reasoning_effort="{reasoning_effort(model, effort)}"']
        if mcp_url:
            args += ["-c", f'mcp_servers.huntun.url="{mcp_url}"', "-c", 'mcp_servers.huntun.default_tools_approval_mode="approve"',
                     "-c", "mcp_servers.huntun.tool_timeout_sec=900", "-c", "mcp_servers.huntun.startup_timeout_sec=30"]
        args += ["-c", 'web_search="live"']
        return args

    def _resume_args(self, base: list[str], session_id: str) -> list[str]:
        # Resume inherits cwd from the process. Explicitly override the saved
        # thread's old sandbox and rules just as for newly created sessions.
        args = [base[0], "exec", "resume", session_id]
        i = 2
        while i < len(base):
            if base[i] == "-C":
                i += 2
            else:
                args.append(base[i])
                i += 1
        return args

    async def _run(self, args: list[str], prompt: str, cwd: Path, on_event: Callable[[dict[str, Any]], None], should_stop: Callable[[], bool] | None = None,
                   timeout: float | None = None) -> tuple[int | None, bool]:
        """Runs codex, streaming JSONL events to on_event. Returns (exit code, interrupted)."""
        proc = await asyncio.create_subprocess_exec(
            *args, prompt, cwd=str(cwd), stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            env={**os.environ, "NO_COLOR": "1"},
            start_new_session=os.name == "posix",
        )
        interrupted = False
        stderr_chunks: list[bytes] = []
        descendants: set[int] = set()

        def send(sig: int) -> None:
            if os.name == "posix":
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(proc.pid, sig)
                for pid in descendants:
                    with contextlib.suppress(ProcessLookupError):
                        os.kill(pid, sig)
            elif proc.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    proc.send_signal(sig)

        async def drain_err() -> None:
            assert proc.stderr
            while chunk := await proc.stderr.read(64 * 1024):
                stderr_chunks[:] = [(b"".join(stderr_chunks) + chunk)[-2000:]]

        async def watch() -> None:
            nonlocal interrupted
            started = time.time()
            while proc.returncode is None:
                await asyncio.sleep(1)
                if (should_stop and should_stop()) or (timeout and time.time() - started > timeout):
                    interrupted = True
                    descendants.update(await _descendant_pids(proc.pid))
                    send(signal.SIGINT)
                    await asyncio.sleep(3)
                    send(signal.SIGKILL)
                    return

        err_task, watch_task = asyncio.create_task(drain_err()), asyncio.create_task(watch())
        try:
            assert proc.stdout
            async for raw in _event_lines(proc.stdout):
                line = raw.decode(errors="replace").strip()
                if not line:
                    continue
                try:
                    on_event(json.loads(line))
                except json.JSONDecodeError:
                    on_event({"type": "stdout", "text": line})
            await proc.wait()
            await err_task
        finally:
            # Cancellation must release native writers and their tool hosts,
            # including hosts that detached from the writer's process group.
            if proc.returncode is None:
                descendants.update(await _descendant_pids(proc.pid))
            send(signal.SIGKILL)
            if proc.returncode is None:
                await proc.wait()
            watch_task.cancel()
            err_task.cancel()
            await asyncio.gather(watch_task, err_task, return_exceptions=True)
        if proc.returncode not in (0, None) and not interrupted:
            on_event({"type": "stderr", "text": b"".join(stderr_chunks).decode(errors="replace")[-2000:]})
        return proc.returncode, interrupted

    async def probe(self) -> bool:
        ok = True

        def on_event(ev: dict[str, Any]) -> None:
            nonlocal ok
            item = ev.get("item") or {}
            if item.get("type") == "error" and looks_like_limit(item.get("message")):
                ok = False
            if ev.get("type") == "error" and looks_like_limit(ev.get("message")):
                ok = False
            if ev.get("type") == "stderr" and looks_like_limit(ev.get("text")):
                ok = False

        with tempfile.TemporaryDirectory() as d:
            args = self._base_args(Path(d), "", "low", "read-only", None) + ["--ephemeral"]
            try:
                code, _ = await self._run(args, "Reply with the single word OK.", Path(d), on_event, timeout=120)
            except Exception:
                return False
        return ok and code == 0

    async def structured(self, *, prompt: str, tool_name: str, description: str, schema: dict[str, Any], model: str, effort: str,
                         log: Callable[[str], None] | None = None, cwd: Path | None = None) -> dict[str, Any]:
        """Uses Codex's --output-schema so the final message is JSON matching the tool's input schema."""
        errors: list[str] = []
        say = log or (lambda _t: None)
        say(f"Starting a Codex session ({model or 'default model'}, effort {effort})…")
        with tempfile.TemporaryDirectory() as d:
            schema_file, out_file = Path(d) / "schema.json", Path(d) / "last.json"
            schema_file.write_text(json.dumps(schema))
            args = self._base_args(cwd or Path(d), model, effort, "read-only", None) + ["--ephemeral", "--output-schema", str(schema_file), "-o", str(out_file)]

            def on_event(ev: dict[str, Any]) -> None:
                item = ev.get("item") or {}
                if item.get("type") == "agent_message" and str(item.get("text") or "").strip():
                    say(str(item.get("text")).strip()[:400])
                elif item.get("type") == "reasoning" and str(item.get("text") or "").strip():
                    say("thinking: " + str(item.get("text")).strip()[:300])
                if item.get("type") == "error":
                    errors.append(str(item.get("message")))
                if ev.get("type") in ("error", "stderr"):
                    errors.append(str(ev.get("message") or ev.get("text")))

            full_prompt = f"{prompt}\n\nAnswer with JSON only, matching the schema for `{tool_name}` ({description})."
            code, _ = await self._run(args, full_prompt, cwd or Path(d), on_event, timeout=600)
            if any(looks_like_limit(e) for e in errors):
                raise RuntimeError("Codex usage limit reached: " + "; ".join(errors)[:300])
            if not out_file.exists():
                raise RuntimeError(f"Codex produced no output (exit {code}): {'; '.join(errors)[:400]}")
            text = out_file.read_text().strip()
        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            raise RuntimeError(f"Codex output was not JSON: {text[:300]}") from e
        if not isinstance(data, dict):
            raise RuntimeError("Codex output was not a JSON object")
        return data

    async def run_cycle(self, *, ctx, system, prompt, model, effort, should_stop, log) -> CycleResult:  # type: ignore[override]
        memory, cycle, state = ctx.memory, ctx.cycle, ctx.memory.state
        usage: dict[str, float] = {"input": 0.0, "output": 0.0, "cache_read": 0.0, "cache_write": 0.0, "cost_usd": 0.0, "turns": 0.0}
        live = LiveTelemetry(memory, "Codex")
        state.context_limit, _ = context_settings(model)
        if state.context_tokens > state.context_limit:
            state.context_tokens = 0
        memory.save_state()
        limit_hit: dict[str, Any] = {}
        texts: list[str] = []
        errors: list[str] = []
        completed_compactions = 0
        compactions_before = state.compactions

        def result(outcome: str, error: str | None = None) -> CycleResult:
            return CycleResult(outcome, cycle.summary, cycle.next_task, error, usage, limit_hit.get("resets_at"), cost_status="untracked")

        def on_event(ev: dict[str, Any]) -> None:
            nonlocal completed_compactions
            t = ev.get("type")
            if t == "thread.started" and ev.get("thread_id"):
                state.session_id = str(ev["thread_id"])
                memory.save_state()
            elif t == "turn.completed":
                u = ev.get("usage") or {}
                details = u.get("input_tokens_details") or {}
                inp = float(u.get("input_tokens") or 0)
                cached = float(u.get("cached_input_tokens") or details.get("cached_tokens") or 0)
                written = float(u.get("cache_write_input_tokens") or details.get("cache_write_tokens") or 0)
                out = float(u.get("output_tokens") or 0)
                # Codex input includes cache hits. Internal input is uncached
                # so every token contributes to consumption exactly once.
                usage["input"] += max(0, inp - cached - written)
                usage["cache_read"] += cached
                usage["cache_write"] += written
                usage["output"] += out
                usage["turns"] += 1
                # Aggregate turn usage is billing data, not the current context.
                memory.save_state()
            elif t in ("item.started", "item.updated", "item.completed"):
                item = ev.get("item") or {}
                kind = item.get("type")
                completed = t == "item.completed"
                if kind in ("command_execution", "file_change", "mcp_tool_call", "collab_tool_call", "web_search") and not completed:
                    live.pulse("tool", str(item.get("command") or item.get("tool") or item.get("query") or kind))
                    return
                if kind == "context_compaction":
                    # The persisted compacted record is authoritative and counted once.
                    if not completed:
                        live.begin_compaction()
                    else:
                        completed_compactions += 1
                    return
                if kind == "todo_list":
                    memory.activity("result" if completed else "thinking", f"Plan: {json.dumps(item.get('items') or [])[:600]}")
                    return
                if not completed:
                    if kind in ("reasoning", "agent_message"):
                        live.pulse("thinking" if kind == "reasoning" else "text", str(item.get("text") or ""))
                    elif kind:
                        live.pulse("tool", str(kind))
                    return
                if kind == "agent_message":
                    texts.append(str(item.get("text") or ""))
                    memory.activity("text", str(item.get("text") or ""))
                elif kind == "reasoning":
                    memory.activity("thinking", str(item.get("text") or ""))
                elif kind == "command_execution":
                    memory.activity("tool", f"shell {item.get('command')}")
                    memory.activity("result", f"shell: {str(item.get('aggregated_output') or item.get('output') or item.get('status') or 'finished')[:600]}")
                elif kind == "file_change":
                    changes = item.get("changes") or []
                    paths = [str(c.get("path")) for c in changes if isinstance(c, dict) and c.get("path")]
                    memory.activity("tool", f"edit {', '.join(paths) or item.get('path') or ''}")
                    memory.activity("result", f"edit: {item.get('status') or 'finished'}")
                    for p in paths:
                        try:
                            rel = Path(p)
                            rel = rel.relative_to(ctx.workspace.resolve()) if rel.is_absolute() else rel
                            memory.touch(rel.as_posix())
                        except ValueError:
                            pass
                elif kind in ("mcp_tool_call", "collab_tool_call"):
                    memory.activity("result", f"{item.get('tool') or item.get('name') or 'MCP tool'}: {str(item.get('result') or item.get('error') or 'finished')[:600]}")
                elif kind == "web_search":
                    memory.activity("result", f"web_search: {item.get('query') or 'finished'}")
                elif kind == "error":
                    msg = str(item.get("message") or "")
                    errors.append(msg)
                    if looks_like_limit(msg):
                        limit_hit["reason"] = msg[:300]
                    memory.activity("error", msg)
                elif kind:
                    memory.activity("result", f"{kind}: {item.get('status') or 'finished'}")
            elif t in ("error", "stderr", "turn.failed"):
                msg = str(ev.get("message") or ev.get("text") or (ev.get("error") or {}).get("message") or "")
                errors.append(msg)
                if looks_like_limit(msg):
                    limit_hit["reason"] = msg[:300]
                memory.activity("error", msg)

        def locate_session() -> Path | None:
            sid = state.session_id
            if not sid or not all(c.isalnum() or c in "-_" for c in sid):
                return None
            sessions = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))) / "sessions"
            return next(sessions.glob(f"*/*/*/*-{sid}.jsonl"), None)

        tail = SessionTail(locate_session, live.codex_record)
        specs = available_tools(ctx, "codex")
        sandbox = "read-only" if ctx.agent.role in ("master", "watchdog") else "workspace-write"
        resuming = bool(state.resume_pending and state.session_id)                       # paused mid-cycle: pick up where it stopped
        continuing = continue_session(state, self.config.session_max_cycles)             # otherwise keep the same thread going across cycles
        if not continuing and state.session_id:
            log(f"starting a fresh thread after {state.session_cycles} cycles")
            state.session_id, state.session_cycles = None, 0
        thread_before = state.session_id
        await tail.prime(lambda record: live.codex_record(record, snapshot=True))
        live.pulse("waiting", "Waiting for Codex response")
        tail.start()
        try:
            async with McpBridge(specs, ctx) as bridge:
                base = self._base_args(ctx.workspace, model, effort, sandbox, bridge.url)
                if resuming:
                    args = self._resume_args(base, state.session_id)
                    text = f"{RESUME_PROMPT}\n\nCurrent team instructions:\n{system}"
                elif continuing:
                    # the system prompt was given when the thread started; repeat it so roster, goal, or brief changes reach the agent
                    args = self._resume_args(base, state.session_id)
                    text = f"{CONTINUE_NOTE}{system}\n\n---\n\n{prompt}"
                else:
                    args = base
                    text = f"{system}\n\n---\n\n{prompt}"
                code, interrupted = await self._run(args, text, ctx.workspace, on_event, should_stop=should_stop)
        except Exception as e:
            return result("error", f"{type(e).__name__}: {e}")
        finally:
            try:
                await tail.close()
                for _ in range(max(0, completed_compactions - (state.compactions - compactions_before))):
                    live.complete_compaction()
            finally:
                live.close()
        if state.session_id:
            state.session_cycles = state.session_cycles + 1 if state.session_id == thread_before else 1
        if continuing and code not in (0, None) and not cycle.finished and any("thread" in e.lower() and ("not found" in e.lower() or "no such" in e.lower()) for e in errors):
            log("the saved thread is gone; starting fresh next cycle")
            state.session_id, state.session_cycles = None, 0

        if limit_hit:
            state.resume_pending = bool(state.session_id)
            memory.save_state()
            return result("limit", limit_hit.get("reason", "usage limit reached"))
        if interrupted and not cycle.finished:
            state.resume_pending = bool(state.session_id)
            memory.save_state()
            return result("paused")
        state.resume_pending = False
        memory.save_state()
        if code not in (0, None) and not cycle.finished:
            return result("error", f"codex exited with {code}: {'; '.join(errors)[-500:]}")
        if not cycle.finished:
            cycle.finished = True
            cycle.summary = (texts[-1] if texts else "(cycle ended without a summary)")[:2000]
            cycle.next_task = cycle.next_task or state.current_task
        return result("finished")
