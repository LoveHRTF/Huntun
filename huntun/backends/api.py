"""Anthropic Messages API backend: a manual streaming tool-use loop whose message history is written
to disk after every step so a cycle can be paused and resumed."""
from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import Any

import anthropic

from ..models import DEFAULT_API_MODEL, context_limit, cost_usd
from ..tools import ToolContext, available_tools, execute
from ..types import CycleResult, HuntunConfig
from . import looks_like_limit

RETRYABLE_ATTEMPTS = 6
COMPACT_AT = 0.6  # compact the working context when a call reports more than this fraction of the window


def _tool_defs(ctx: ToolContext) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    specs = available_tools(ctx, "api")
    defs: list[dict[str, Any]] = [
        {"name": t.name, "description": t.description, "input_schema": t.input_schema, "eager_input_streaming": True} for t in specs
    ]
    defs.append({"type": "web_search_20260209", "name": "web_search", "max_uses": 8})
    defs.append({"type": "web_fetch_20260209", "name": "web_fetch", "max_uses": 8})
    return defs, {t.name: t for t in specs}


def _serializable(content: Any) -> list[dict[str, Any]]:
    """Turns response content blocks into plain dicts so the transcript can be saved and replayed unchanged."""
    return [b.model_dump(exclude_none=True) if hasattr(b, "model_dump") else b for b in content]


class ApiBackend:
    name = "api"

    def __init__(self, config: HuntunConfig) -> None:
        self.config = config
        self.client = anthropic.AsyncAnthropic()

    async def _call(self, params: dict[str, Any], use_fallbacks: bool) -> Any:
        p = dict(params)
        if use_fallbacks:
            p["betas"] = ["server-side-fallback-2026-07-01"]
            p["fallbacks"] = "default"
        async with self.client.beta.messages.stream(**p) as stream:
            return await stream.get_final_message()

    async def structured(self, *, prompt: str, tool_name: str, description: str, schema: dict[str, Any], model: str, effort: str) -> dict[str, Any]:
        response = await self.client.messages.create(
            model=model or DEFAULT_API_MODEL,
            max_tokens=16000,
            output_config={"effort": effort},
            tools=[{"name": tool_name, "description": description, "input_schema": schema}],
            messages=[{"role": "user", "content": prompt}],
        )
        if response.stop_reason == "refusal":
            raise RuntimeError("The model refused this request.")
        for block in response.content:
            if block.type == "tool_use" and block.name == tool_name:
                return dict(block.input)
        text = "\n".join(b.text for b in response.content if b.type == "text")
        raise RuntimeError(f"Model did not call {tool_name}. It said: {text[:500]}")

    async def probe(self) -> bool:
        try:
            await self.client.messages.create(model="claude-haiku-4-5", max_tokens=5, messages=[{"role": "user", "content": "ping"}])
            return True
        except anthropic.RateLimitError:
            return False
        except anthropic.APIStatusError as e:
            return not looks_like_limit(getattr(e, "message", str(e)))
        except Exception:
            return False

    async def _compact(self, params: dict[str, Any], messages: list[dict[str, Any]], prompt: str, log: Callable[[str], None]) -> list[dict[str, Any]]:
        """Client-side compaction: ask the model for a handoff summary, then restart the transcript from it."""
        ask = {**params, "messages": messages + [{"role": "user", "content": "Your context is nearly full. Write a compact handoff summary for yourself: what the task is, what you have done (files, commits, decisions), what remains, and any open questions or board threads to follow up. Do not call tools."}],
               "tools": [], "max_tokens": 4000}
        async with self.client.beta.messages.stream(**ask) as stream:
            m = await stream.get_final_message()
        summary = "\n".join(b.text for b in m.content if b.type == "text").strip() or "(no summary produced)"
        log(f"compacted context ({len(messages)} messages -> summary of {len(summary)} chars)")
        first = messages[0]["content"] if messages and messages[0]["role"] == "user" and isinstance(messages[0]["content"], str) else prompt
        return [{"role": "user", "content": f"{first}\n\n[Context compacted. Your handoff summary from before the compaction:]\n{summary}\n\nContinue from here."}]

    async def run_cycle(self, *, ctx, system, prompt, model, effort, should_stop, log) -> CycleResult:  # type: ignore[override]
        memory, cycle = ctx.memory, ctx.cycle
        defs, by_name = _tool_defs(ctx)
        usage = {"input": 0.0, "output": 0.0, "cache_read": 0.0, "cache_write": 0.0, "cost_usd": 0.0}
        effective_model = model or DEFAULT_API_MODEL

        def result(outcome: str, error: str | None = None, resets_at: float | None = None) -> CycleResult:
            return CycleResult(outcome, cycle.summary, cycle.next_task, error, usage, resets_at)

        messages = memory.load_transcript() or [{"role": "user", "content": prompt}]
        memory.save_transcript(messages)
        use_fallbacks = self.config.fallbacks
        pause_continuations = 0
        nudged = False
        attempts = 0

        while True:
            if should_stop():
                return result("paused")
            params = {
                "model": model or DEFAULT_API_MODEL,
                "max_tokens": self.config.max_tokens,
                "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
                "tools": defs,
                "messages": messages,
                "output_config": {"effort": effort},
            }
            try:
                message = await self._call(params, use_fallbacks)
                attempts = 0
            except anthropic.BadRequestError as e:
                if use_fallbacks:
                    log(f"fallbacks rejected by API ({e.message}); retrying without")
                    use_fallbacks = False
                    continue
                return result("error", f"API error {e.status_code}: {e.message}")
            except anthropic.RateLimitError as e:
                # Usage limit: hand control to the orchestrator's watchdog; the transcript is on disk for resumption.
                retry_after = None
                try:
                    retry_after = float(e.response.headers.get("retry-after") or 0) or None
                except Exception:
                    pass
                memory.save_transcript(messages)
                return result("limit", f"rate limited: {e.message}", (asyncio.get_event_loop().time() * 0 + __import__("time").time() + retry_after) if retry_after else None)
            except (anthropic.InternalServerError, anthropic.APIConnectionError) as e:
                attempts += 1
                if attempts > RETRYABLE_ATTEMPTS:
                    return result("error", f"gave up after {attempts} attempts: {e}")
                wait = min(120, 5 * 2**attempts)
                log(f"transient API error ({type(e).__name__}); retrying in {wait}s")
                await asyncio.sleep(wait)
                continue
            except anthropic.APIError as e:
                return result("error", f"API error: {e}")
            except ValueError as e:
                # Eager tool input the SDK could not parse at all: re-issue the turn, bounded.
                attempts += 1
                if attempts > 2:
                    return result("error", f"unparseable tool input: {e}")
                log(f"stream error ({e}); re-issuing turn")
                continue

            u = message.usage
            usage["input"] += u.input_tokens or 0
            usage["output"] += u.output_tokens or 0
            usage["cache_read"] += getattr(u, "cache_read_input_tokens", 0) or 0
            usage["cache_write"] += getattr(u, "cache_creation_input_tokens", 0) or 0
            cr, cw = getattr(u, "cache_read_input_tokens", 0) or 0, getattr(u, "cache_creation_input_tokens", 0) or 0
            usage["cost_usd"] = round(usage["cost_usd"] + cost_usd(effective_model, u.input_tokens or 0, u.output_tokens or 0, cr, cw), 6)
            memory.state.context_tokens = int((u.input_tokens or 0) + cr + cw + (u.output_tokens or 0))
            memory.state.context_limit = context_limit(effective_model)
            memory.save_state()
            needs_compaction = memory.state.context_tokens > COMPACT_AT * memory.state.context_limit and message.stop_reason == "tool_use"

            if message.stop_reason == "refusal":
                details = getattr(message, "stop_details", None)
                why = getattr(details, "explanation", None) or "no explanation"
                cycle.summary = f"Cycle ended by a model refusal ({why})."
                cycle.next_task = memory.state.current_task
                memory.clear_transcript()
                return result("finished")

            for b in message.content:
                if b.type == "text" and b.text.strip():
                    memory.activity("text", b.text.strip())
            content = _serializable(message.content)
            if message.stop_reason == "pause_turn":
                messages.append({"role": "assistant", "content": content})
                pause_continuations += 1
                if pause_continuations > 8:
                    messages.append({"role": "user", "content": "Server tool use paused too many times. Stop researching and continue with what you have."})
                memory.save_transcript(messages)
                continue

            tool_uses = [b for b in message.content if b.type == "tool_use"]
            messages.append({"role": "assistant", "content": content})

            if not tool_uses:
                if message.stop_reason == "max_tokens":
                    messages.append({"role": "user", "content": "Your reply was cut off (max_tokens). Continue, more concisely."})
                    memory.save_transcript(messages)
                    continue
                text = "\n".join(b.text for b in message.content if b.type == "text").strip()
                if not cycle.finished:
                    cycle.finished = True
                    cycle.summary = text or "(cycle ended without a summary)"
                    cycle.next_task = cycle.next_task or memory.state.current_task
                memory.clear_transcript()
                return result("finished")

            results: list[dict[str, Any]] = []
            for tu in tool_uses:
                if message.stop_reason == "max_tokens":
                    results.append({"type": "tool_result", "tool_use_id": tu.id, "is_error": True,
                                    "content": "Tool input was truncated at max_tokens. Retry with a smaller input (e.g. write the file in parts)."})
                    continue
                if should_stop():
                    # Do not run tools while pausing. Roll the assistant turn back so the transcript stays valid.
                    messages.pop()
                    memory.save_transcript(messages)
                    return result("paused")
                spec = by_name.get(tu.name)
                if spec is None:
                    results.append({"type": "tool_result", "tool_use_id": tu.id, "is_error": True, "content": f"Unknown tool {tu.name}"})
                    continue
                started = asyncio.get_event_loop().time()
                memory.activity("tool", f"{tu.name} {_short(tu.input, 300)}")
                out, is_err = await execute(spec, tu.input, ctx)
                took = asyncio.get_event_loop().time() - started
                log(f"{tu.name} {_short(tu.input)} -> {'ERROR ' if is_err else ''}{took:.1f}s")
                r: dict[str, Any] = {"type": "tool_result", "tool_use_id": tu.id, "content": out}
                if is_err:
                    r["is_error"] = True
                results.append(r)
                if cycle.finished:
                    break
            answered = {r["tool_use_id"] for r in results}
            for tu in tool_uses:
                if tu.id not in answered:
                    results.append({"type": "tool_result", "tool_use_id": tu.id, "content": "Skipped: the cycle already finished."})
            user_content: list[dict[str, Any]] = list(results)
            if not cycle.finished and cycle.tool_calls >= self.config.max_tool_calls_per_cycle and not nudged:
                nudged = True
                user_content.append({"type": "text", "text": f"You have used {cycle.tool_calls} tool calls this cycle, which is the budget. Commit any finished work, update your notes with what remains, and call finish_cycle now."})
            messages.append({"role": "user", "content": user_content})
            memory.save_transcript(messages)

            if cycle.finished:
                memory.clear_transcript()
                return result("finished")
            if needs_compaction:
                try:
                    messages = await self._compact(params, messages, prompt, log)
                    memory.state.compactions += 1
                    memory.state.context_tokens = 0
                    memory.save_state()
                    memory.save_transcript(messages)
                    memory.activity("cycle", f"Context compacted (#{memory.state.compactions})")
                except anthropic.APIError as e:
                    log(f"compaction failed: {e}")
            if cycle.tool_calls >= self.config.max_tool_calls_per_cycle + 6:
                cycle.finished = True
                cycle.summary = "Cycle force-ended after exceeding the tool-call budget."
                cycle.next_task = memory.state.current_task
                memory.clear_transcript()
                return result("finished")


def _short(data: Any, n: int = 100) -> str:
    try:
        s = json.dumps(data)
    except Exception:
        return ""
    return s if len(s) <= n else s[:n] + "…"
