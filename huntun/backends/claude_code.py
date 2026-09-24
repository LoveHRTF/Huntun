"""Claude Code backend: runs each work cycle as a Claude Code session through the Claude Agent SDK.

Authentication is whatever `claude` itself uses (your Claude login), so no API key is needed.
Claude Code's built-in tools cover files, shell, and web; Huntun's team tools (board, git commits,
notes, cycle control) are served to the session as an in-process MCP server. A paused cycle keeps
its Claude Code session id and is resumed with `resume=` later, so no progress is lost.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
from collections.abc import Callable
from functools import lru_cache
from pathlib import Path
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    HookMatcher,
    RateLimitEvent,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ThinkingBlock,
    ToolUseBlock,
    create_sdk_mcp_server,
    query,
    tool,
)

from ..models import context_limit
from ..tools import ToolContext, ToolSpec, available_tools, execute, rel_path
from ..types import CycleResult, HuntunConfig
from . import continue_session, looks_like_limit


@lru_cache(maxsize=8)
def _cli_version(path: str) -> tuple[int, ...]:
    try:
        out = subprocess.run([path, "--version"], capture_output=True, text=True, timeout=30).stdout
    except Exception:
        return ()
    m = re.search(r"(\d+)\.(\d+)\.(\d+)", out)
    return tuple(int(x) for x in m.groups()) if m else ()


def claude_cli_path(bundled: Path | None = None, system: str | None = None) -> str | None:
    """The Claude Code binary to run, or None to let the SDK use its own.

    The SDK ships a copy of Claude Code and always prefers it, so `claude update` never reaches the binary Huntun runs:
    a newly released model can be refused by the bundled copy while the installed CLI already supports it. Huntun runs
    `HUNTUN_CLAUDE_BIN` when set, otherwise whichever of the bundled copy and `claude` on PATH is newer.
    """
    override = os.environ.get("HUNTUN_CLAUDE_BIN")
    if override:
        return override
    if bundled is None:
        import claude_agent_sdk

        bundled = Path(claude_agent_sdk.__file__).parent / "_bundled" / ("claude.exe" if os.name == "nt" else "claude")
    system = system or shutil.which("claude")
    if not system:
        return None
    if not bundled.exists():
        return system
    return system if _cli_version(system) > _cli_version(str(bundled)) else None


BUILTIN_TOOLS = ["Read", "Write", "Edit", "MultiEdit", "NotebookEdit", "Bash", "Grep", "Glob", "WebSearch", "WebFetch", "TodoWrite"]
RESUME_PROMPT = (
    "You were paused mid-cycle by the orchestrator and are now resumed. Re-check the repository state (git status), "
    "continue exactly where you left off, and finish the cycle with finish_cycle."
)
CONTINUE_NOTE = "(New cycle in the same conversation. Your earlier cycles above are context only; the board, the repository and your notes are the truth now.)\n\n"


def _session_gone(text: str) -> bool:
    t = text.lower()
    return "no conversation found" in t or ("session" in t and ("not found" in t or "does not exist" in t or "unknown" in t))


def _sdk_tool(spec: ToolSpec, ctx: ToolContext):
    async def handler(args: dict[str, Any]) -> dict[str, Any]:
        out, is_err = await execute(spec, args or {}, ctx)
        res: dict[str, Any] = {"content": [{"type": "text", "text": out}]}
        if is_err:
            res["is_error"] = True
        return res

    return tool(spec.name, spec.description, spec.input_schema)(handler)


def _inside(workspace: Path, p: str) -> tuple[bool, str]:
    try:
        target = Path(p) if Path(p).is_absolute() else workspace / p
        rel = target.resolve().relative_to(workspace.resolve())
    except ValueError:
        return False, ""
    return True, rel.parts[0] if rel.parts else ""


class ClaudeCodeBackend:
    name = "claude-code"

    def __init__(self, config: HuntunConfig) -> None:
        self.config = config

    def _options(self, *, workspace: Path, system: str | None, mcp_tools: list[Any], allowed: list[str], model: str, effort: str,
                 max_turns: int, resume: str | None, on_touch: Callable[[str], None] | None,
                 on_activity: Callable[[str, str], None] | None = None) -> ClaudeAgentOptions:
        async def pre_tool(input_data: Any, _tool_use_id: Any, _context: Any) -> dict[str, Any]:
            """Sandbox: file writes must stay inside the workspace and out of .huntun/ and .git/."""
            tool_input = input_data.get("tool_input") or {}
            path = tool_input.get("file_path") or tool_input.get("notebook_path")
            if path:
                ok, first = _inside(workspace, str(path))
                reason = None
                if not ok:
                    reason = f"{path} is outside the workspace {workspace}; write inside the repository instead"
                elif first in (".huntun", ".git"):
                    reason = f"{first}/ is managed by the orchestrator and is off limits"
                if reason:
                    if on_activity:
                        on_activity("error", f"{input_data.get('tool_name')} denied: {reason}")
                    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": reason}}
            return {}

        async def post_any_tool(input_data: Any, _tool_use_id: Any, _context: Any) -> dict[str, Any]:
            """Records built-in tool results (Bash output, file reads, web results) in the activity log."""
            try:
                name = str(input_data.get("tool_name") or "")
                if not name.startswith("mcp__huntun__") and on_activity:
                    resp = input_data.get("tool_response")
                    text = resp if isinstance(resp, str) else json.dumps(resp, default=str)
                    on_activity("result", f"{name}: {text[:600]}")
            except Exception:
                pass
            return {}

        async def post_tool(input_data: Any, _tool_use_id: Any, _context: Any) -> dict[str, Any]:
            try:
                path = (input_data.get("tool_input") or {}).get("file_path") or (input_data.get("tool_input") or {}).get("notebook_path")
                if path and on_touch:
                    ok, first = _inside(workspace, str(path))
                    if ok and first not in (".huntun", ".git"):
                        on_touch(rel_path(workspace, Path(path) if Path(path).is_absolute() else workspace / path))
            except Exception:
                pass
            return {}

        model_id = model if model else None
        return ClaudeAgentOptions(
            system_prompt={"type": "preset", "preset": "claude_code", "append": system} if system else None,
            mcp_servers={"huntun": create_sdk_mcp_server("huntun", version="1.0.0", tools=mcp_tools)},
            allowed_tools=allowed,
            permission_mode="acceptEdits",
            hooks={
                "PreToolUse": [HookMatcher(matcher="Write|Edit|MultiEdit|NotebookEdit", hooks=[pre_tool])],
                "PostToolUse": [HookMatcher(matcher="Write|Edit|MultiEdit|NotebookEdit", hooks=[post_tool]), HookMatcher(matcher=None, hooks=[post_any_tool])],
            },
            cwd=str(workspace),
            model=model_id,
            effort=effort if effort in ("low", "medium", "high", "xhigh", "max") else None,  # type: ignore[arg-type]
            max_turns=max_turns,
            setting_sources=["project"],
            resume=resume,
            cli_path=claude_cli_path(),
        )

    async def probe(self) -> bool:
        """One tiny turn on the cheapest model; False when the CLI reports the limit is still in force."""
        try:
            options = ClaudeAgentOptions(allowed_tools=[], disallowed_tools=BUILTIN_TOOLS + ["Task", "Agent"], max_turns=1, model="haiku",
                                         permission_mode="default", setting_sources=[], system_prompt="Reply with the single word OK.", cli_path=claude_cli_path())
            async for msg in query(prompt="OK?", options=options):
                if isinstance(msg, RateLimitEvent) and msg.rate_limit_info.status == "rejected":
                    return False
                if isinstance(msg, ResultMessage):
                    return not (msg.is_error and looks_like_limit(msg.result))
            return True
        except Exception:
            return False

    async def structured(self, *, prompt: str, tool_name: str, description: str, schema: dict[str, Any], model: str, effort: str,
                         log: Callable[[str], None] | None = None, cwd: Path | None = None) -> dict[str, Any]:
        captured: dict[str, Any] = {}
        say = log or (lambda _t: None)
        say(f"Starting a Claude Code session ({model or 'default model'}, effort {effort})…")

        async def handler(args: dict[str, Any]) -> dict[str, Any]:
            captured.update(args or {})
            return {"content": [{"type": "text", "text": "Received. You are done; reply with a one-line confirmation."}]}

        t = tool(tool_name, description, schema)(handler)
        options = ClaudeAgentOptions(
            mcp_servers={"huntun": create_sdk_mcp_server("huntun", tools=[t])},
            tools=[],  # no built-in tools: this is a one-shot decision from the material in the prompt, not an exploration
            allowed_tools=[f"mcp__huntun__{tool_name}"],
            disallowed_tools=BUILTIN_TOOLS + ["Task", "Agent", "ToolSearch", "Skill", "LS"],
            permission_mode="acceptEdits",
            model=model or None,
            effort=effort if effort in ("low", "medium", "high", "xhigh", "max") else None,  # type: ignore[arg-type]
            max_turns=10,
            cli_path=claude_cli_path(),
            setting_sources=[],
            cwd=str(cwd) if cwd else None,                                     # the project the question is about, never Huntun's own directory
            system_prompt="You answer by calling the single tool you are given, using only the information in the message. You have no file, shell, or web access here; do not try to explore.",
        )
        last_text = ""
        async with ClaudeSDKClient(options=options) as client:
            await client.query(prompt + f"\n\nEverything you need is above; you cannot explore files or run commands in this step. Submit your answer now by calling the {tool_name} tool exactly once.")
            async for msg in client.receive_response():
                if isinstance(msg, AssistantMessage):
                    for b in msg.content:
                        if isinstance(b, TextBlock):
                            last_text = b.text
                            if b.text.strip():
                                say(b.text.strip()[:400])
                        elif isinstance(b, ToolUseBlock):
                            say(f"→ {b.name.removeprefix('mcp__huntun__')}" + (": submitting the answer" if b.name.endswith(tool_name) else ""))
                        elif isinstance(b, ThinkingBlock) and (b.thinking or "").strip():
                            say("thinking: " + b.thinking.strip()[:300])
                elif isinstance(msg, ResultMessage) and not msg.is_error:
                    say(f"Session finished ({msg.num_turns or 0} turns, ${msg.total_cost_usd or 0:.3f})")
                elif isinstance(msg, ResultMessage) and msg.is_error:
                    detail = msg.result or getattr(msg, "subtype", "") or ""
                    extra = getattr(msg, "permission_denials", None)
                    raise RuntimeError(f"Claude Code error: {detail}" + (f" (denials: {extra})" if extra else "") + (f" | last text: {last_text[:300]}" if last_text else ""))
        if not captured:
            raise RuntimeError(f"Model did not call {tool_name}. It said: {last_text[:500]}")
        return captured

    async def run_cycle(self, *, ctx, system, prompt, model, effort, should_stop, log) -> CycleResult:  # type: ignore[override]
        memory, cycle, state = ctx.memory, ctx.cycle, ctx.memory.state
        usage: dict[str, float] = {"cost_usd": 0.0, "turns": 0.0, "input": 0.0, "output": 0.0, "cache_read": 0.0}
        state.context_limit = state.context_limit or context_limit(model)

        limit_hit: dict[str, Any] = {}

        def result(outcome: str, error: str | None = None) -> CycleResult:
            return CycleResult(outcome, cycle.summary, cycle.next_task, error, usage, limit_hit.get("resets_at"))

        specs = available_tools(ctx, "claude-code")
        mcp_tools = [_sdk_tool(s, ctx) for s in specs]
        builtin = [t for t in BUILTIN_TOOLS if not (ctx.agent.role == "master" and t in ("Write", "Edit", "MultiEdit", "NotebookEdit"))]
        allowed = builtin + [f"mcp__huntun__{s.name}" for s in specs]
        resuming = bool(state.resume_pending and state.session_id)                       # paused mid-cycle: pick up where it stopped
        continuing = continue_session(state, self.config.session_max_cycles)             # otherwise keep the same conversation going across cycles
        if not continuing and state.session_id:
            log(f"starting a fresh session after {state.session_cycles} cycles")
            state.session_id, state.session_cycles = None, 0
        options = self._options(
            workspace=ctx.workspace, system=system, mcp_tools=mcp_tools, allowed=allowed, model=model, effort=effort,
            max_turns=self.config.max_tool_calls_per_cycle + 10, resume=state.session_id if continuing else None,
            on_touch=memory.touch, on_activity=memory.activity,
        )
        text_parts: list[str] = []
        interrupted = False
        session_id: str | None = None

        async def watch_stop(client: ClaudeSDKClient) -> None:
            nonlocal interrupted
            ticks = 0
            while True:
                await asyncio.sleep(2)
                ticks += 1
                if ticks % 5 == 0:  # every 10s: refresh the context-window gauge the way /context reports it
                    try:
                        cu = await asyncio.wait_for(client.get_context_usage(), timeout=5)
                        state.context_tokens, state.context_limit = int(cu.get("totalTokens") or 0), int(cu.get("maxTokens") or state.context_limit)
                        memory.save_state()
                    except Exception:
                        pass
                if should_stop():
                    interrupted = True
                    try:
                        await client.interrupt()
                    except Exception as e:  # session may already be finishing
                        log(f"interrupt failed: {e}")
                    return

        try:
            async with ClaudeSDKClient(options=options) as client:
                await client.query(RESUME_PROMPT if resuming else (CONTINUE_NOTE + prompt if continuing else prompt))
                watcher = asyncio.create_task(watch_stop(client))
                try:
                    async for msg in client.receive_response():
                        if isinstance(msg, RateLimitEvent):
                            info = msg.rate_limit_info
                            if info.status == "rejected":
                                limit_hit.update({"reason": f"{info.rate_limit_type} limit reached", "resets_at": info.resets_at})
                                memory.activity("error", f"Usage limit reached ({info.rate_limit_type}); pausing until it resets")
                                interrupted = True
                                try:
                                    await client.interrupt()
                                except Exception:
                                    pass
                            elif info.status == "allowed_warning":
                                memory.activity("cycle", f"Approaching the {info.rate_limit_type} usage limit ({round((info.utilization or 0) * 100)}%)")
                            continue
                        if isinstance(msg, SystemMessage) and msg.subtype == "compact_boundary":
                            state.compactions += 1
                            state.context_tokens = 0
                            memory.save_state()
                            memory.activity("cycle", f"Context compacted by Claude Code (#{state.compactions})")
                            continue
                        if isinstance(msg, AssistantMessage):
                            if msg.usage:
                                u = msg.usage
                                state.context_tokens = int((u.get("input_tokens") or 0) + (u.get("cache_read_input_tokens") or 0) + (u.get("cache_creation_input_tokens") or 0) + (u.get("output_tokens") or 0))
                            for b in msg.content:
                                if isinstance(b, ToolUseBlock):
                                    log(f"{b.name.removeprefix('mcp__huntun__')} {_short(b.input)}")
                                    memory.activity("tool", f"{b.name.removeprefix('mcp__huntun__')} {_short(b.input, 300)}")
                                elif isinstance(b, TextBlock) and b.text.strip():
                                    text_parts.append(b.text.strip())
                                    memory.activity("text", b.text.strip())
                                elif isinstance(b, ThinkingBlock) and (b.thinking or "").strip():
                                    memory.activity("thinking", b.thinking.strip())
                        elif isinstance(msg, ResultMessage):
                            session_id = msg.session_id
                            usage["cost_usd"] += msg.total_cost_usd or 0.0
                            usage["turns"] += msg.num_turns or 0
                            if msg.usage:
                                usage["input"] += msg.usage.get("input_tokens") or 0
                                usage["output"] += msg.usage.get("output_tokens") or 0
                                usage["cache_read"] += msg.usage.get("cache_read_input_tokens") or 0
                            if msg.is_error and (looks_like_limit(msg.result) or limit_hit):
                                limit_hit.setdefault("reason", (msg.result or "usage limit")[:300])
                                state.session_id = session_id
                                state.resume_pending = True
                                memory.save_state()
                                return result("limit", limit_hit["reason"])
                            if msg.is_error and not interrupted:
                                if continuing and not resuming and _session_gone(msg.result or msg.subtype or ""):
                                    log("the saved session is gone; starting fresh next cycle")
                                    state.session_id, state.session_cycles = None, 0
                                else:
                                    state.session_id = session_id
                                state.resume_pending = False
                                memory.save_state()
                                return result("error", f"Claude Code session error: {msg.result or msg.subtype}")
                finally:
                    watcher.cancel()
        except Exception as e:
            return result("error", f"{type(e).__name__}: {e}")

        if session_id:
            state.session_cycles = state.session_cycles + 1 if session_id == state.session_id else 1
            state.session_id = session_id
        if limit_hit and not cycle.finished:
            state.resume_pending = bool(session_id)
            memory.save_state()
            return result("limit", limit_hit.get("reason", "usage limit reached"))
        if interrupted and not cycle.finished:
            state.resume_pending = True
            memory.save_state()
            return result("paused")

        state.resume_pending = False
        memory.save_state()
        if not cycle.finished:
            cycle.finished = True
            cycle.summary = (text_parts[-1] if text_parts else "(cycle ended without a summary)")[:2000]
            cycle.next_task = cycle.next_task or state.current_task
        return result("finished")


def _short(data: Any, n: int = 100) -> str:
    try:
        s = json.dumps(data)
    except Exception:
        return ""
    return s if len(s) <= n else s[:n] + "…"
