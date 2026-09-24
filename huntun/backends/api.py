"""Anthropic Messages API backend: a manual streaming tool-use loop whose message history is written
to disk after every step so a cycle can be paused and resumed.

The same loop drives Anthropic-compatible providers: DeepSeek (https://api.deepseek.com/anthropic) and a local
Ollama server (http://127.0.0.1:11434). In that "compat" mode the Anthropic-only extras (betas, server-side
fallbacks, effort, adaptive thinking, server tools, eager input streaming) are left out, and any field a provider
still rejects with a 400 is dropped and the call retried.
"""
from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

import anthropic

from ..models import DEFAULT_API_MODEL, catalog_for, context_limit, cost_usd
from ..tools import ToolContext, available_tools, execute
from ..types import CycleResult, HuntunConfig
from . import looks_like_limit

RETRYABLE_ATTEMPTS = 6
COMPACT_AT = 0.6  # compact the working context when a call reports more than this fraction of the window


PROVIDERS: dict[str, dict[str, Any]] = {
    "api": {"label": "Anthropic API"},
    "deepseek": {"label": "DeepSeek", "base_url": "https://api.deepseek.com/anthropic", "key_env": "DEEPSEEK_API_KEY"},
    "ollama": {"label": "Ollama (local)", "base_url_env": "OLLAMA_HOST", "base_url": "http://127.0.0.1:11434", "key": "ollama"},
}
OPTIONAL_FIELDS = ("fallbacks", "output_config", "thinking", "cache_control", "tool_choice", "eager_input_streaming")   # dropped one by one on a 400


def _tool_defs(ctx: ToolContext, compat: bool = False) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    specs = available_tools(ctx, "api")
    defs: list[dict[str, Any]] = [{"name": t.name, "description": t.description, "input_schema": t.input_schema} for t in specs]
    if not compat:
        for d in defs:
            d["eager_input_streaming"] = True
        defs.append({"type": "web_search_20260209", "name": "web_search", "max_uses": 8})
        defs.append({"type": "web_fetch_20260209", "name": "web_fetch", "max_uses": 8})
    return defs, {t.name: t for t in specs}


def _without(params: dict[str, Any], field: str) -> dict[str, Any]:
    """A copy of the request without one optional field, wherever it lives."""
    p = {k: v for k, v in params.items() if k != field}
    if field == "cache_control" and isinstance(p.get("system"), list):
        p["system"] = [{k: v for k, v in b.items() if k != "cache_control"} for b in p["system"]]
    if field == "eager_input_streaming" and isinstance(p.get("tools"), list):
        p["tools"] = [{k: v for k, v in t.items() if k != "eager_input_streaming"} for t in p["tools"]]
    return p


def _serializable(content: Any) -> list[dict[str, Any]]:
    """Turns response content blocks into plain dicts so the transcript can be saved and replayed unchanged."""
    return [b.model_dump(exclude_none=True) if hasattr(b, "model_dump") else b for b in content]


class ApiBackend:
    name = "api"
    provider = "api"
    compat = False                        # True for Anthropic-compatible providers (DeepSeek, Ollama)
    dropped: set[str]

    def __init__(self, config: HuntunConfig, provider: str = "api") -> None:
        self.config = config
        self.provider = provider
        self.name = provider
        self.compat = provider != "api"
        self.dropped = set()
        spec = PROVIDERS[provider]
        if self.compat:
            base = os.environ.get(spec.get("base_url_env", ""), "") or spec["base_url"]
            key = os.environ.get(spec["key_env"]) if spec.get("key_env") else spec.get("key")
            if not key:
                raise RuntimeError(f"{spec['label']}: set {spec['key_env']}")
            self.client = anthropic.AsyncAnthropic(api_key=key, base_url=base.rstrip("/"))
        else:
            self.client = anthropic.AsyncAnthropic()

    def _default_model(self) -> str:
        if not self.compat:
            return DEFAULT_API_MODEL
        cat = catalog_for(self.provider)
        return cat[0].id if cat else ""

    def _prepare(self, params: dict[str, Any]) -> dict[str, Any]:
        """Applies the provider's capabilities: compat providers get no Anthropic-only extras, plus whatever this provider already rejected."""
        self.__dict__.setdefault("dropped", set())                                  # instances built without __init__ (tests) still work
        p = dict(params)
        if self.compat:
            for f in ("output_config", "thinking", "fallbacks", "betas"):
                p = _without(p, f)
            if os.environ.get("HUNTUN_COMPAT_THINKING", "off").lower() in ("on", "1", "true") and "thinking" not in self.dropped:
                p["thinking"] = {"type": "enabled", "budget_tokens": int(os.environ.get("HUNTUN_COMPAT_THINKING_BUDGET", "8192"))}
        for f in self.dropped:
            p = _without(p, f)
        return p

    async def _send(self, params: dict[str, Any]) -> Any:
        """One streamed call; on a 400 that names an optional field, drop that field for good and retry."""
        while True:
            p = self._prepare(params)
            try:
                if self.compat:
                    async with self.client.messages.stream(**p) as stream:
                        return await stream.get_final_message()
                async with self.client.beta.messages.stream(**p) as stream:
                    return await stream.get_final_message()
            except anthropic.BadRequestError as e:
                msg = str(getattr(e, "message", e)).lower()
                culprit = next((f for f in OPTIONAL_FIELDS if f not in self.dropped and f.replace("_", "") in msg.replace("_", "")), None)
                if culprit is None:
                    raise
                self.dropped.add(culprit)

    async def _call(self, params: dict[str, Any], use_fallbacks: bool) -> Any:
        p = dict(params)
        if use_fallbacks and not self.compat:
            p["betas"] = ["server-side-fallback-2026-07-01"]
            p["fallbacks"] = "default"
        return await self._send(p)

    async def structured(self, *, prompt: str, tool_name: str, description: str, schema: dict[str, Any], model: str, effort: str,
                         log: Callable[[str], None] | None = None, cwd: Path | None = None) -> dict[str, Any]:
        say = log or (lambda _t: None)
        say(f"Calling {model or self._default_model()} ({PROVIDERS[self.provider]['label']}), waiting for the answer…")
        params: dict[str, Any] = {
            "model": model or self._default_model(), "max_tokens": 16000, "output_config": {"effort": effort},
            "tools": [{"name": tool_name, "description": description, "input_schema": schema}],
            "tool_choice": {"type": "tool", "name": tool_name},
            "messages": [{"role": "user", "content": prompt + ("" if not self.compat else f"\n\nCall the `{tool_name}` tool with your answer; do not answer in prose.")}],
        }
        response = await self._send(params)
        if response.stop_reason == "refusal":
            raise RuntimeError("The model refused this request.")
        for block in response.content:
            if block.type == "text" and getattr(block, "text", "").strip():
                say(block.text.strip()[:400])
        for block in response.content:
            if block.type == "tool_use" and block.name == tool_name:
                say(f"Answer received via {tool_name}")
                return dict(block.input)
        text = "\n".join(b.text for b in response.content if b.type == "text")
        if self.compat:                                                          # some compatible models answer in text anyway: take the JSON out of it
            start, end = text.find("{"), text.rfind("}")
            if start >= 0 and end > start:
                try:
                    data = json.loads(text[start:end + 1])
                    if isinstance(data, dict):
                        return data
                except json.JSONDecodeError:
                    pass
        raise RuntimeError(f"Model did not call {tool_name}. It said: {text[:500]}")

    async def probe(self) -> bool:
        try:
            await self.client.messages.create(model="claude-haiku-4-5" if not self.compat else self._default_model(), max_tokens=5, messages=[{"role": "user", "content": "ping"}])
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
        m = await self._send(ask)
        summary = "\n".join(b.text for b in m.content if b.type == "text").strip() or "(no summary produced)"
        log(f"compacted context ({len(messages)} messages -> summary of {len(summary)} chars)")
        first = messages[0]["content"] if messages and messages[0]["role"] == "user" and isinstance(messages[0]["content"], str) else prompt
        return [{"role": "user", "content": f"{first}\n\n[Context compacted. Your handoff summary from before the compaction:]\n{summary}\n\nContinue from here."}]

    async def run_cycle(self, *, ctx, system, prompt, model, effort, should_stop, log) -> CycleResult:  # type: ignore[override]
        memory, cycle = ctx.memory, ctx.cycle
        defs, by_name = _tool_defs(ctx, self.compat)
        usage = {"input": 0.0, "output": 0.0, "cache_read": 0.0, "cache_write": 0.0, "cost_usd": 0.0}
        effective_model = model or self._default_model()

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
                "model": effective_model,
                "max_tokens": self.config.max_tokens,
                "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
                "tools": defs,
                "messages": messages,
                "output_config": {"effort": effort},
                "thinking": {"type": "adaptive", "display": "summarized"},
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
                elif b.type == "thinking" and getattr(b, "thinking", "").strip():
                    memory.activity("thinking", b.thinking.strip())
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
                memory.state.compacting = True
                memory.save_state()
                try:
                    messages = await self._compact(params, messages, prompt, log)
                    memory.state.compactions += 1
                    memory.state.context_tokens = 0
                    memory.save_transcript(messages)
                    memory.activity("cycle", f"Context compacted (#{memory.state.compactions})")
                except anthropic.APIError as e:
                    log(f"compaction failed: {e}")
                finally:
                    memory.state.compacting = False
                    memory.save_state()
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
