"""Pi's native JSONL harness with CLM and Huntun's per-cycle MCP tools."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any
from uuid import uuid4

from .. import pi
from ..models import context_limit
from ..tools import available_tools
from ..types import CycleResult, HuntunConfig
from . import continue_session, looks_like_limit
from .codex import McpBridge, _event_lines
from .telemetry import LiveTelemetry


def message_text(message: dict[str, Any]) -> str:
    content = message.get("content") or []
    if isinstance(content, str):
        return content
    return "\n".join(
        str(p.get("text") or "")
        for p in content
        if isinstance(p, dict) and p.get("type") == "text"
    )


class PiClmBackend:
    name = "pi-clm"

    def __init__(self, config: HuntunConfig) -> None:
        self.config = config
        self.bin = pi.binary()
        self.extension = pi.clm_extension()
        if not self.bin or not self.extension:
            raise RuntimeError("Pi + CLM is not installed. Run: " + pi.INSTALL)

    def _args(self, model: str, effort: str) -> list[str]:
        args = [
            self.bin,
            "--mode",
            "json",
            "--print",
            "--approve",
            "--extension",
            str(self.extension),
            "--extension",
            str(Path(__file__).parent.parent / "pi_tools.js"),
        ]
        if extension := pi.claude_extension():
            args += ["--extension", str(extension)]
        if model and model != "pi-clm:default":
            # Namespacing keeps GPT/Claude on the selected Pi harness.
            native = model.removeprefix("pi-clm:")
            provider, separator, mid = native.partition("/")
            if not separator:
                raise ValueError(
                    "Pi model must be pi-clm:<provider>/<model> or pi-clm:default"
                )
            if provider == "pi-claude-code-provider" and not pi.claude_extension():
                raise RuntimeError(
                    "Pi Claude CLI adapter is not installed. Run: " + pi.CLAUDE_INSTALL
                )
            args += ["--provider", provider, "--model", mid]
        args += [
            "--thinking",
            "off"
            if effort == "none"
            else "max"
            if effort == "ultra"
            else effort or "high",
        ]
        return args

    async def _run(
        self,
        args: list[str],
        prompt: str,
        cwd: Path,
        on_event: Callable[[dict[str, Any]], None],
        should_stop: Callable[[], bool] | None = None,
        timeout: float | None = None,
        bridge_url: str = "",
    ) -> tuple[int | None, bool]:
        # Prompt through stdin avoids argv-size limits and exposes no prompt in ps.
        launcher_dir = tempfile.TemporaryDirectory(prefix="huntun-pi-cli-")
        launcher = pi.claude_launcher(Path(launcher_dir.name))
        entry = pi.sdk_entry()
        environment = {
            **os.environ,
            "NO_COLOR": "1",
            "HUNTUN_PI_MCP_URL": bridge_url,
            "HUNTUN_PI_SDK": entry.as_uri() if entry else "",
        }
        if launcher:
            environment["PI_CLAUDE_CODE_PROVIDER_PATH"] = str(launcher)
        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                cwd=str(cwd),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=environment,
            )
        except BaseException:
            launcher_dir.cleanup()
            raise
        interrupted = False
        errors = bytearray()

        async def feed() -> None:
            assert proc.stdin
            try:
                proc.stdin.write(prompt.encode())
                await proc.stdin.drain()
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                proc.stdin.close()

        async def drain() -> None:
            assert proc.stderr
            async for raw in _event_lines(proc.stderr):
                errors.extend(raw)
                del errors[:-8000]
                text = raw.decode(errors="replace").strip()
                if text:
                    try:
                        event = json.loads(text)
                    except ValueError:
                        event = None
                    if isinstance(event, dict) and event.get("type") == "huntun_ready":
                        on_event(event)
                    else:
                        on_event({"type": "stderr", "text": text})

        async def watch() -> None:
            nonlocal interrupted
            started = time.monotonic()
            while proc.returncode is None:
                await asyncio.sleep(0.25)
                if (should_stop and should_stop()) or (
                    timeout and time.monotonic() - started > timeout
                ):
                    interrupted = True
                    with contextlib.suppress(ProcessLookupError):
                        proc.send_signal(signal.SIGINT)
                    await asyncio.sleep(3)
                    if proc.returncode is None:
                        with contextlib.suppress(ProcessLookupError):
                            proc.kill()
                    return

        tasks = [asyncio.create_task(f()) for f in (feed, drain, watch)]
        try:
            assert proc.stdout
            async for raw in _event_lines(proc.stdout):
                try:
                    event = json.loads(raw)
                except (ValueError, UnicodeDecodeError):
                    if raw.strip():
                        on_event(
                            {
                                "type": "stderr",
                                "text": raw.decode(errors="replace").strip(),
                            }
                        )
                    continue
                if isinstance(event, dict):
                    on_event(event)
            await proc.wait()
            await tasks[1]
        finally:
            if proc.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
                await proc.wait()
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            launcher_dir.cleanup()
        if proc.returncode and errors:
            on_event({"type": "process_error", "text": errors.decode(errors="replace")})
        return proc.returncode, interrupted

    async def structured(
        self,
        *,
        prompt,
        tool_name,
        description,
        schema,
        model,
        effort,
        log=None,
        cwd=None,
    ) -> dict[str, Any]:
        texts, errors = [], []
        ready = False

        def event(ev: dict[str, Any]) -> None:
            nonlocal ready
            if ev.get("type") == "huntun_ready":
                ready = True
            message = ev.get("message") or {}
            if ev.get("type") == "message_end" and message.get("role") == "assistant":
                text = message_text(message)
                if text:
                    texts.append(text)
                    if log:
                        log(text[:400])
                if message.get("errorMessage"):
                    errors.append(str(message["errorMessage"]))
            if ev.get("type") in ("stderr", "process_error"):
                errors.append(str(ev.get("text") or ""))

        request = (
            prompt
            + f"\n\nReturn a single JSON object for {tool_name} ({description}), no prose or fences. Schema:\n"
            + json.dumps(schema)
        )
        with tempfile.TemporaryDirectory() as directory:
            # Planning can inspect the repository; CLM retains read/edit/write for its own mirror.
            code, interrupted = await self._run(
                self._args(model, effort) + ["--no-session"],
                request,
                cwd or Path(directory),
                event,
                timeout=600,
            )
        if interrupted or code or not texts or not ready:
            raise RuntimeError(
                "Pi + CLM planning failed: "
                + ("; ".join(errors)[-1000:] or f"exit {code}")
            )
        text = texts[-1].strip()
        try:
            data = json.loads(text[text.find("{") : text.rfind("}") + 1])
        except ValueError as exc:
            raise RuntimeError(
                "Pi did not return a JSON object: " + text[:300]
            ) from exc
        if not isinstance(data, dict):
            raise RuntimeError("Pi did not return a JSON object")
        return data

    async def probe(self) -> bool:
        answered, ready = False, False

        def event(ev: dict[str, Any]) -> None:
            nonlocal answered, ready
            if ev.get("type") == "huntun_ready":
                ready = True
            message = ev.get("message") or {}
            if (
                ev.get("type") == "message_end"
                and message.get("role") == "assistant"
                and message_text(message)
                and message.get("stopReason") != "error"
            ):
                answered = True

        with tempfile.TemporaryDirectory() as directory:
            try:
                model = getattr(self, "limited_model", "") or (
                    self.config.model if self.config.model.startswith("pi-clm:") else ""
                )
                code, interrupted = await self._run(
                    self._args(model, "none") + ["--no-session"],
                    "Reply with OK. Do not use tools.",
                    Path(directory),
                    event,
                    timeout=120,
                )
            except Exception:
                return False
        return answered and ready and not code and not interrupted

    async def run_cycle(
        self, *, ctx, system, prompt, model, effort, should_stop, log
    ) -> CycleResult:
        memory, state, cycle = ctx.memory, ctx.memory.state, ctx.cycle
        usage = {
            "input": 0.0,
            "output": 0.0,
            "cache_read": 0.0,
            "cache_write": 0.0,
            "cost_usd": 0.0,
            "turns": 0.0,
        }
        texts: list[str] = []
        errors: list[str] = []
        final_error = ""
        ready = False
        outcomes: set[str] = set()
        reported_cost = missing_cost = 0
        provider = ""
        live = LiveTelemetry(memory, "Pi + CLM")
        sessions = memory.dir / "pi-sessions"
        sessions.mkdir(exist_ok=True)
        continuing = continue_session(state, self.config.session_max_cycles)
        # Never pass a different provider's native session id to Pi.
        saved = Path(state.session_id) if state.session_id else None
        valid = (
            saved
            and saved.is_file()
            and saved.resolve().is_relative_to(sessions.resolve())
        )
        if not continuing or not valid:
            state.session_id, state.session_cycles, state.context_tokens = (
                "huntun-" + uuid4().hex,
                0,
                0,
            )
            state.resume_pending = False
        state.context_limit = context_limit(model)
        memory.save_state()
        args = self._args(model, effort) + ["--session-dir", str(sessions)]
        args += (
            ["--session", state.session_id]
            if continuing and valid
            else ["--session-id", state.session_id]
        )
        instructions = memory.dir / "pi-instructions.md"
        instructions.write_text(system)
        args += ["--append-system-prompt", str(instructions)]
        request = prompt
        if state.resume_pending:
            request = (
                "You were paused mid-cycle. Check git status and continue the unfinished task.\n\n"
                + prompt
            )
        elif continuing and valid:
            request = (
                "New cycle: earlier turns are context only; the current board, repository and notes are authoritative.\n\n"
                + prompt
            )

        def account(value: dict[str, Any]) -> None:
            nonlocal reported_cost, missing_cost
            if not value:
                return
            for native, key in (
                ("input", "input"),
                ("output", "output"),
                ("cacheRead", "cache_read"),
                ("cacheWrite", "cache_write"),
            ):
                usage[key] += float(value.get(native) or 0)
            amount = float((value.get("cost") or {}).get("total") or 0)
            if provider == "openai-codex":
                missing_cost += 1  # subscription adapters may supply zero/nominal API rates
            elif amount > 0:
                reported_cost += 1
                usage["cost_usd"] += amount
            else:
                missing_cost += 1

        def event(ev: dict[str, Any]) -> None:
            nonlocal ready, final_error, provider
            kind = ev.get("type")
            if kind == "huntun_ready":
                ready = True
                if ev.get("sessionFile"):
                    state.session_id = str(ev["sessionFile"])
                actual = ev.get("model") or {}
                provider = str(actual.get("provider") or "")
                state.context_limit = int(
                    actual.get("contextWindow") or state.context_limit
                )
                memory.save_state()
            elif kind == "message_update":
                update = ev.get("assistantMessageEvent") or {}
                if update.get("type") in ("thinking_delta", "text_delta"):
                    live.pulse(
                        "thinking" if update["type"] == "thinking_delta" else "text",
                        str(update.get("delta") or ""),
                    )
            elif kind == "message_end":
                message = ev.get("message") or {}
                if message.get("role") == "assistant":
                    value = message.get("usage") or {}
                    account(value)
                    usage["turns"] += 1
                    # Last request occupancy, not cumulative billing across turns.
                    state.context_tokens = int(
                        sum(
                            float(value.get(k) or 0)
                            for k in ("input", "output", "cacheRead", "cacheWrite")
                        )
                    )
                    text = message_text(message)
                    if text:
                        texts.append(text)
                        memory.activity("text", text)
                    for part in message.get("content") or []:
                        if isinstance(part, dict) and part.get("type") == "thinking":
                            memory.activity("thinking", str(part.get("thinking") or ""))
                    final_error = (
                        str(message.get("errorMessage") or "")
                        if message.get("stopReason") == "error"
                        else ""
                    )
                elif message.get("role") == "toolResult":
                    account(message.get("usage") or {})
            elif kind == "tool_execution_start":
                name, arguments = str(ev.get("toolName") or ""), ev.get("args") or {}
                live.pulse(
                    "tool",
                    name.removeprefix("mcp__huntun__")
                    + " "
                    + json.dumps(arguments)[:300],
                )
                if name in ("write", "edit") and isinstance(arguments.get("path"), str):
                    if Path(arguments["path"]).name == "LIVE_CONTEXT.md":
                        live.begin_compaction()
                    try:
                        path = Path(arguments["path"])
                        memory.touch(
                            (
                                path.relative_to(ctx.workspace.resolve())
                                if path.is_absolute()
                                else path
                            ).as_posix()
                        )
                    except ValueError:
                        pass  # CLM's private context mirror is outside the worktree.
            elif kind == "tool_execution_end":
                memory.activity("result", message_text(ev.get("result") or {})[:600])
            elif kind == "compaction_start":
                live.begin_compaction()
            elif kind == "compaction_end":
                value = ev.get("result") or {}
                account(value.get("usage") or {})
                if value:
                    live.complete_compaction()
                    state.context_tokens = int(value.get("estimatedTokensAfter") or 0)
                else:
                    state.compacting = False
            elif kind == "auto_retry_start":
                live.pulse(
                    "waiting", "Pi retrying: " + str(ev.get("errorMessage") or "")
                )
            elif kind == "entry_appended":
                entry = ev.get("entry") or {}
                if entry.get("customType") == "live-context-state":
                    outcome = (entry.get("data") or {}).get("lastOutcome") or {}
                    at = str(outcome.get("at") or "")
                    if at and at not in outcomes:
                        outcomes.add(at)
                        if outcome.get("kind") == "applied" and float(
                            outcome.get("afterEstimate") or 0
                        ) < float(outcome.get("beforeEstimate") or 0):
                            live.complete_compaction()
                        else:
                            state.compacting = False
            elif kind in ("stderr", "process_error", "extension_error"):
                errors.append(str(ev.get("text") or ev.get("error") or ""))

        live.pulse("waiting", "Waiting for Pi + CLM response")
        def cost_status() -> str:
            if provider == "ollama":
                return "local"
            return "partial" if reported_cost and missing_cost else "reported" if reported_cost else "untracked"

        try:
            async with McpBridge(available_tools(ctx, self.name), ctx) as bridge:
                code, interrupted = await self._run(
                    args,
                    request,
                    ctx.workspace,
                    event,
                    should_stop=should_stop,
                    bridge_url=bridge.url,
                )
        except Exception as exc:
            return CycleResult(
                "error", cycle.summary, cycle.next_task, f"Pi + CLM: {exc}", usage, cost_status=cost_status()
            )
        finally:
            live.close()
        state.session_cycles += 1
        error = final_error or ("; ".join(errors)[-1000:] if code or not ready else "")
        if error and looks_like_limit(error):
            outcome = "limit"
            self.limited_model = model
        elif interrupted and not cycle.finished:
            outcome = "paused"
        elif code or final_error or not ready:
            outcome = "error"
            error = (
                error
                or "Pi + CLM did not initialize; check /login inside Pi and the installed extension."
            )
        else:
            outcome = "finished"
        state.resume_pending = outcome in ("paused", "limit")
        memory.save_state()
        if outcome == "finished" and not cycle.finished:
            cycle.finished = True
            cycle.summary = (texts[-1] if texts else "(cycle ended without a summary)")[
                :2000
            ]
            cycle.next_task = cycle.next_task or state.current_task
        return CycleResult(
            outcome, cycle.summary, cycle.next_task, error or None, usage, cost_status=cost_status()
        )
