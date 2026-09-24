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
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ..tools import ToolContext, ToolSpec, available_tools, execute
from ..types import CycleResult, HuntunConfig
from . import continue_session, looks_like_limit

CONTINUE_NOTE = "(New cycle in the same conversation. Your earlier cycles above are context only; the board, the repository and your notes are the truth now.)\n\n"
RESUME_PROMPT = (
    "You were paused mid-cycle by the orchestrator and are now resumed. Re-check the repository state (git status), "
    "continue exactly where you left off, and finish the cycle with the huntun finish_cycle tool."
)
DEFAULT_CONTEXT = int(os.environ.get("HUNTUN_CODEX_CONTEXT", "400000"))
EFFORT_MAP = {"low": "low", "medium": "medium", "high": "high", "xhigh": "xhigh", "max": "xhigh"}


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
        args = [self.codex, "exec", "--json", "--skip-git-repo-check", "-C", str(workspace), "-s", sandbox, "-c", 'approval_policy="never"']
        if model:
            args += ["-m", model]
        if effort:
            args += ["-c", f'model_reasoning_effort="{EFFORT_MAP.get(effort, "high")}"']
        if mcp_url:
            args += ["-c", f'mcp_servers.huntun.url="{mcp_url}"', "-c", 'mcp_servers.huntun.default_tools_approval_mode="approve"',
                     "-c", "mcp_servers.huntun.tool_timeout_sec=900", "-c", "mcp_servers.huntun.startup_timeout_sec=30"]
        args += ["-c", 'web_search="live"']
        return args

    async def _run(self, args: list[str], prompt: str, cwd: Path, on_event: Callable[[dict[str, Any]], None], should_stop: Callable[[], bool] | None = None,
                   timeout: float | None = None) -> tuple[int | None, bool]:
        """Runs codex, streaming JSONL events to on_event. Returns (exit code, interrupted)."""
        proc = await asyncio.create_subprocess_exec(
            *args, prompt, cwd=str(cwd), stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            env={**os.environ, "NO_COLOR": "1"},
        )
        interrupted = False
        stderr_chunks: list[bytes] = []

        async def drain_err() -> None:
            assert proc.stderr
            async for line in proc.stderr:
                stderr_chunks.append(line)

        async def watch() -> None:
            nonlocal interrupted
            started = time.time()
            while proc.returncode is None:
                await asyncio.sleep(1)
                if (should_stop and should_stop()) or (timeout and time.time() - started > timeout):
                    interrupted = True
                    with contextlib.suppress(ProcessLookupError):
                        proc.send_signal(signal.SIGINT)
                    await asyncio.sleep(3)
                    if proc.returncode is None:
                        with contextlib.suppress(ProcessLookupError):
                            proc.kill()
                    return

        err_task, watch_task = asyncio.create_task(drain_err()), asyncio.create_task(watch())
        assert proc.stdout
        async for raw in proc.stdout:
            line = raw.decode(errors="replace").strip()
            if not line:
                continue
            try:
                on_event(json.loads(line))
            except json.JSONDecodeError:
                on_event({"type": "stdout", "text": line})
        await proc.wait()
        watch_task.cancel()
        with contextlib.suppress(Exception):
            await err_task
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
                         log: Callable[[str], None] | None = None) -> dict[str, Any]:
        """Uses Codex's --output-schema so the final message is JSON matching the tool's input schema."""
        errors: list[str] = []
        say = log or (lambda _t: None)
        say(f"Starting a Codex session ({model or 'default model'}, effort {effort})…")
        with tempfile.TemporaryDirectory() as d:
            schema_file, out_file = Path(d) / "schema.json", Path(d) / "last.json"
            schema_file.write_text(json.dumps(schema))
            args = self._base_args(Path(d), model, effort, "read-only", None) + ["--ephemeral", "--output-schema", str(schema_file), "-o", str(out_file)]

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
            code, _ = await self._run(args, full_prompt, Path(d), on_event, timeout=600)
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
        usage: dict[str, float] = {"input": 0.0, "output": 0.0, "cache_read": 0.0, "cost_usd": 0.0, "turns": 0.0}
        state.context_limit = state.context_limit or DEFAULT_CONTEXT
        limit_hit: dict[str, Any] = {}
        texts: list[str] = []
        errors: list[str] = []

        def result(outcome: str, error: str | None = None) -> CycleResult:
            return CycleResult(outcome, cycle.summary, cycle.next_task, error, usage, limit_hit.get("resets_at"))

        def on_event(ev: dict[str, Any]) -> None:
            t = ev.get("type")
            if t == "thread.started" and ev.get("thread_id"):
                state.session_id = str(ev["thread_id"])
                memory.save_state()
            elif t == "turn.completed":
                u = ev.get("usage") or {}
                inp, cached, out = float(u.get("input_tokens") or 0), float(u.get("cached_input_tokens") or 0), float(u.get("output_tokens") or 0)
                usage["input"] += inp
                usage["cache_read"] += cached
                usage["output"] += out
                usage["turns"] += 1
                state.context_tokens = int(inp + cached + out)
                memory.save_state()
            elif t == "item.completed":
                item = ev.get("item") or {}
                kind = item.get("type")
                if kind == "agent_message":
                    texts.append(str(item.get("text") or ""))
                    memory.activity("text", str(item.get("text") or ""))
                elif kind == "reasoning":
                    memory.activity("thinking", str(item.get("text") or ""))
                elif kind == "command_execution":
                    memory.activity("tool", f"shell {item.get('command')}")
                    if item.get("aggregated_output") or item.get("output"):
                        memory.activity("result", f"shell: {str(item.get('aggregated_output') or item.get('output'))[:600]}")
                elif kind == "file_change":
                    changes = item.get("changes") or []
                    paths = [str(c.get("path")) for c in changes if isinstance(c, dict) and c.get("path")]
                    memory.activity("tool", f"edit {', '.join(paths) or item.get('path') or ''}")
                    for p in paths:
                        try:
                            rel = Path(p)
                            rel = rel.relative_to(ctx.workspace.resolve()) if rel.is_absolute() else rel
                            memory.touch(rel.as_posix())
                        except ValueError:
                            pass
                elif kind == "mcp_tool_call":
                    memory.activity("tool", f"{str(item.get('tool') or item.get('name') or '').removeprefix('huntun__')} {json.dumps(item.get('arguments') or {})[:300]}")
                elif kind == "web_search":
                    memory.activity("tool", f"web_search {item.get('query')}")
                elif kind == "error":
                    msg = str(item.get("message") or "")
                    errors.append(msg)
                    if looks_like_limit(msg):
                        limit_hit["reason"] = msg[:300]
                    memory.activity("error", msg)
            elif t in ("error", "stderr"):
                msg = str(ev.get("message") or ev.get("text") or "")
                errors.append(msg)
                if looks_like_limit(msg):
                    limit_hit["reason"] = msg[:300]

        specs = available_tools(ctx, "codex")
        sandbox = "read-only" if ctx.agent.role == "master" else "workspace-write"
        resuming = bool(state.resume_pending and state.session_id)                       # paused mid-cycle: pick up where it stopped
        continuing = continue_session(state, self.config.session_max_cycles)             # otherwise keep the same thread going across cycles
        if not continuing and state.session_id:
            log(f"starting a fresh thread after {state.session_cycles} cycles")
            state.session_id, state.session_cycles = None, 0
        thread_before = state.session_id
        try:
            async with McpBridge(specs, ctx) as bridge:
                base = self._base_args(ctx.workspace, model, effort, sandbox, bridge.url)
                if resuming:
                    args = [base[0], "exec", "resume", state.session_id, *base[2:]]
                    text = RESUME_PROMPT
                elif continuing:
                    # the system prompt was given when the thread started; repeat it so roster, goal, or brief changes reach the agent
                    args = [base[0], "exec", "resume", state.session_id, *base[2:]]
                    text = f"{CONTINUE_NOTE}{system}\n\n---\n\n{prompt}"
                else:
                    args = base
                    text = f"{system}\n\n---\n\n{prompt}"
                code, interrupted = await self._run(args, text, ctx.workspace, on_event, should_stop=should_stop)
        except Exception as e:
            return result("error", f"{type(e).__name__}: {e}")
        if state.session_id:
            state.session_cycles = state.session_cycles + 1 if state.session_id == thread_before else 1
        if continuing and not resuming and code not in (0, None) and not cycle.finished and any("thread" in e.lower() and ("not found" in e.lower() or "no such" in e.lower()) for e in errors):
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
