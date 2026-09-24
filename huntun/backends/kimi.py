"""Kimi Code backend: runs each work cycle as a `kimi -p` print-mode session (your Kimi login or Moonshot key).

Kimi Code's own tools cover files, shell, and web. Huntun's team tools (board, git commits, notes, cycle
control) are served to the session over a local streamable-HTTP MCP server started for the cycle and
declared to Kimi through the project-level `.kimi-code/mcp.json`. A cycle keeps its Kimi session id and
is resumed with `kimi --session <id>` on the next cycle (or after a pause).

Print mode with `--output-format stream-json` writes one JSON object per line:
  {"role":"assistant","content":"...","tool_calls":[{"id":..,"function":{"name":..,"arguments":".."}}]}
  {"role":"tool","tool_call_id":..,"content":"..."}
  {"role":"meta","type":"session.resume_hint","session_id":"..."}   (and "system.version", "turn.step.retrying")
MCP tools are named `mcp__huntun__<tool>`. Thinking and tool progress go to stderr and are not streamed.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import shutil
import signal
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ..models import kimi_models
from ..tools import available_tools
from ..types import CycleResult, HuntunConfig
from . import continue_session, looks_like_limit
from .codex import McpBridge

CONTINUE_NOTE = "(New cycle in the same conversation. Your earlier cycles above are context only; the board, the repository and your notes are the truth now.)\n\n"
RESUME_PROMPT = (
    "You were paused mid-cycle by the orchestrator and are now resumed. Re-check the repository state (git status), "
    "continue exactly where you left off, and finish the cycle with the huntun finish_cycle tool."
)
DEFAULT_CONTEXT = int(os.environ.get("HUNTUN_KIMI_CONTEXT", "262144"))
EFFORT_MAP = {"low": "low", "medium": "medium", "high": "high", "xhigh": "high", "max": "max"}
SESSION_GONE = re.compile(r"session.*(not found|does not exist|no such)|unknown session", re.I)
JSON_BLOCK = re.compile(r"```(?:json)?\s*(\{[\s\S]*?\})\s*```")


def kimi_binary() -> str | None:
    return os.environ.get("HUNTUN_KIMI_BIN") or shutil.which("kimi")


class KimiBackend:
    name = "kimi"

    def __init__(self, config: HuntunConfig) -> None:
        self.config = config
        self.kimi = kimi_binary()
        if not self.kimi:
            raise RuntimeError("kimi CLI not found on PATH (install with `npm i -g @moonshot-ai/kimi-code`, then `kimi login`)")

    def _base_args(self, model: str, plan: bool) -> list[str]:
        args = [self.kimi, "--auto", "--output-format", "stream-json"]
        if model and model in kimi_models():
            args += ["-m", model]
        if plan:
            args.append("--plan")   # the master reviews and verifies; plan mode keeps Kimi to read-only tools
        return args

    @staticmethod
    def _write_mcp(workspace: Path, url: str | None) -> None:
        d = workspace / ".kimi-code"
        f = d / "mcp.json"
        if url is None:
            with contextlib.suppress(OSError):
                f.unlink()
            return
        d.mkdir(exist_ok=True)
        f.write_text(json.dumps({"mcpServers": {"huntun": {"url": url}}}, indent=2) + "\n")

    async def _run(self, args: list[str], prompt: str, cwd: Path, on_event: Callable[[dict[str, Any]], None], should_stop: Callable[[], bool] | None = None,
                   timeout: float | None = None) -> tuple[int | None, bool]:
        """Runs kimi in print mode, streaming JSONL events to on_event. Returns (exit code, interrupted)."""
        proc = await asyncio.create_subprocess_exec(
            *args, "-p", prompt, cwd=str(cwd), stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            env={**os.environ, "NO_COLOR": "1", "KIMI_DISABLE_TELEMETRY": "1"},
        )
        interrupted = False
        stderr_chunks: list[bytes] = []

        async def drain_err() -> None:
            assert proc.stderr
            async for line in proc.stderr:
                stderr_chunks.append(line)
                text = line.decode(errors="replace").strip()
                if text.lower().startswith("error") or looks_like_limit(text):
                    on_event({"role": "meta", "type": "stderr", "text": text})

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
                on_event({"role": "meta", "type": "stdout", "text": line})
        await proc.wait()
        watch_task.cancel()
        with contextlib.suppress(Exception):
            await err_task
        if proc.returncode not in (0, None) and not interrupted:
            on_event({"role": "meta", "type": "stderr", "text": b"".join(stderr_chunks).decode(errors="replace")[-2000:]})
        return proc.returncode, interrupted

    async def probe(self) -> bool:
        ok, answered = True, False

        def on_event(ev: dict[str, Any]) -> None:
            nonlocal ok, answered
            if ev.get("role") == "assistant" and ev.get("content"):
                answered = True
            if ev.get("type") == "stderr" and looks_like_limit(ev.get("text")):
                ok = False

        with tempfile.TemporaryDirectory() as d:
            try:
                code, _ = await self._run(self._base_args("", False), "Reply with the single word OK.", Path(d), on_event, timeout=120)
            except Exception:
                return False
        return ok and answered and code in (0, None)

    async def structured(self, *, prompt: str, tool_name: str, description: str, schema: dict[str, Any], model: str, effort: str,
                         log: Callable[[str], None] | None = None) -> dict[str, Any]:
        """Kimi has no output-schema flag: ask for JSON only and parse the last assistant message."""
        texts: list[str] = []
        errors: list[str] = []
        say = log or (lambda _t: None)
        say(f"Starting a Kimi Code session ({model or 'default model'})…")

        def on_event(ev: dict[str, Any]) -> None:
            if ev.get("role") == "assistant" and ev.get("content"):
                texts.append(str(ev["content"]))
                say(str(ev["content"]).strip()[:400])
            if ev.get("type") == "stderr":
                errors.append(str(ev.get("text")))

        full_prompt = (f"{prompt}\n\nAnswer with a single JSON object only, no prose and no code fence, matching this JSON schema for `{tool_name}` "
                       f"({description}):\n{json.dumps(schema)}")
        with tempfile.TemporaryDirectory() as d:
            code, _ = await self._run(self._base_args(model, True), full_prompt, Path(d), on_event, timeout=600)
        if any(looks_like_limit(e) for e in errors):
            raise RuntimeError("Kimi usage limit reached: " + "; ".join(errors)[:300])
        if not texts:
            raise RuntimeError(f"Kimi produced no answer (exit {code}): {'; '.join(errors)[:400]}")
        text = texts[-1].strip()
        m = JSON_BLOCK.search(text)
        candidate = m.group(1) if m else text[text.find("{"): text.rfind("}") + 1]
        try:
            data = json.loads(candidate)
        except json.JSONDecodeError as e:
            raise RuntimeError(f"Kimi output was not JSON: {text[:300]}") from e
        if not isinstance(data, dict):
            raise RuntimeError("Kimi output was not a JSON object")
        return data

    async def run_cycle(self, *, ctx, system, prompt, model, effort, should_stop, log) -> CycleResult:  # type: ignore[override]
        memory, cycle, state = ctx.memory, ctx.cycle, ctx.memory.state
        usage: dict[str, float] = {"input": 0.0, "output": 0.0, "cache_read": 0.0, "cost_usd": 0.0, "turns": 0.0}
        state.context_limit = state.context_limit or DEFAULT_CONTEXT
        limit_hit: dict[str, Any] = {}
        texts: list[str] = []
        errors: list[str] = []
        chars = 0

        def result(outcome: str, error: str | None = None) -> CycleResult:
            return CycleResult(outcome, cycle.summary, cycle.next_task, error, usage, limit_hit.get("resets_at"))

        def on_event(ev: dict[str, Any]) -> None:
            nonlocal chars
            role, t = ev.get("role"), ev.get("type")
            if role == "assistant":
                content = str(ev.get("content") or "")
                if content:
                    texts.append(content)
                    memory.activity("text", content)
                    chars += len(content)
                for call in ev.get("tool_calls") or []:
                    fn = call.get("function") or {}
                    name, raw = str(fn.get("name") or ""), str(fn.get("arguments") or "")
                    chars += len(raw)
                    if name.startswith("mcp__huntun__"):
                        memory.activity("tool", f"{name.removeprefix('mcp__huntun__')} {raw[:300]}")
                    else:
                        memory.activity("tool", f"{name} {raw[:300]}")
                        try:
                            args = json.loads(raw) if raw else {}
                        except json.JSONDecodeError:
                            args = {}
                        for key in ("path", "file_path", "filePath", "file"):
                            p = args.get(key) if isinstance(args, dict) else None
                            if isinstance(p, str) and p and re.search(r"write|edit|create|replace|append", name, re.I):
                                try:
                                    rel = Path(p)
                                    rel = rel.relative_to(ctx.workspace.resolve()) if rel.is_absolute() else rel
                                    memory.touch(rel.as_posix())
                                except ValueError:
                                    pass
                usage["turns"] += 1
            elif role == "tool":
                out = str(ev.get("content") or "")
                chars += len(out)
                memory.activity("result", out[:600])
            elif role == "meta":
                if t == "session.resume_hint" and ev.get("session_id"):
                    state.session_id = str(ev["session_id"])
                    memory.save_state()
                elif t == "turn.step.retrying":
                    msg = f"retrying after {ev.get('error_name')}: {ev.get('error_message')}"
                    memory.activity("error", msg)
                    if looks_like_limit(str(ev.get("error_message"))) or ev.get("status_code") == 429:
                        limit_hit["reason"] = str(ev.get("error_message"))[:300]
                elif t in ("stderr", "stdout"):
                    msg = str(ev.get("text") or "")
                    errors.append(msg)
                    if looks_like_limit(msg):
                        limit_hit["reason"] = msg[:300]

        specs = available_tools(ctx, "kimi")
        plan = ctx.agent.role == "master"
        resuming = bool(state.resume_pending and state.session_id)                       # paused mid-cycle: pick up where it stopped
        continuing = continue_session(state, self.config.session_max_cycles)             # otherwise keep the same session going across cycles
        if not continuing and state.session_id:
            log(f"starting a fresh session after {state.session_cycles} cycles")
            state.session_id, state.session_cycles = None, 0
        session_before = state.session_id
        try:
            async with McpBridge(specs, ctx) as bridge:
                self._write_mcp(ctx.workspace, bridge.url)
                base = self._base_args(model, plan)
                if resuming:
                    args, text = base + ["--session", state.session_id], RESUME_PROMPT
                elif continuing:
                    # the system prompt was given when the session started; repeat it so roster, goal, or brief changes reach the agent
                    args, text = base + ["--session", state.session_id], f"{CONTINUE_NOTE}{system}\n\n---\n\n{prompt}"
                else:
                    args, text = base, f"{system}\n\n---\n\n{prompt}"
                try:
                    code, interrupted = await self._run(args, text, ctx.workspace, on_event, should_stop=should_stop)
                finally:
                    self._write_mcp(ctx.workspace, None)
        except Exception as e:
            return result("error", f"{type(e).__name__}: {e}")
        usage["output"] += chars / 4                                                     # Kimi's print mode reports no token counts: a rough estimate
        state.context_tokens = min(state.context_limit, int(state.context_tokens + chars / 4))
        if state.session_id:
            state.session_cycles = state.session_cycles + 1 if state.session_id == session_before else 1
        if continuing and not resuming and code not in (0, None) and not cycle.finished and any(SESSION_GONE.search(e) for e in errors):
            log("the saved session is gone; starting fresh next cycle")
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
            return result("error", f"kimi exited with {code}: {'; '.join(errors)[-500:]}")
        if not cycle.finished and any(e.lower().startswith("error") for e in errors) and not texts:
            return result("error", f"kimi reported: {'; '.join(errors)[-500:]}")
        if not cycle.finished:
            cycle.finished = True
            cycle.summary = (texts[-1] if texts else "(cycle ended without a summary)")[:2000]
            cycle.next_task = cycle.next_task or state.current_task
        return result("finished")
