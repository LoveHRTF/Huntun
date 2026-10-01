"""Anthropic Messages API backend: a manual streaming tool-use loop whose message history is written
to disk after every step so a cycle can be paused and resumed.

The same loop drives Anthropic-compatible providers: DeepSeek (https://api.deepseek.com/anthropic), Ollama servers
and llama.cpp servers (llama-server), set up in the Model providers menu or the environment; a call goes to the server
that serves its model. In that "compat" mode the Anthropic-only extras (betas, server-side
fallbacks, effort, adaptive thinking, server tools, eager input streaming) are left out, and any field a provider
still rejects with a 400 is dropped and the call retried.
"""
from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Callable
from contextvars import ContextVar
from pathlib import Path
from typing import Any

import anthropic

from ..models import (
    DEFAULT_API_MODEL,
    catalog_for,
    context_limit,
    cost_usd,
    note_shared_pool,
)
from ..tools import ToolContext, available_tools, execute
from ..types import CycleResult, HuntunConfig
from . import POOL_FULL_RE, looks_like_limit, looks_like_overflow
from .telemetry import LiveTelemetry

RETRYABLE_ATTEMPTS = 6
COMPACT_AT = 0.6  # compact the working context when a call reports more than this fraction of the window
OVERFLOW_RECOVERIES = 4  # context overflows one cycle recovers from (compacting, or waiting out a full shared pool) before it gives up


_stream_observer: ContextVar[Callable[[Any], None] | None] = ContextVar("huntun_stream_observer", default=None)


async def _stream_reply(stream: Any) -> Any:
    observer = _stream_observer.get()
    if observer and hasattr(stream, "__aiter__"):
        async for event in stream:
            observer(event)
    return await stream.get_final_message()


class ContextOverflow(Exception):
    """The server could not fit the conversation: the request was too long, or (pool_full) the KV pool the server's
    sessions share filled up and it aborted every request in flight, possibly someone else's fault."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.pool_full = bool(POOL_FULL_RE.search(message))


PROVIDERS: dict[str, dict[str, Any]] = {
    "api": {"label": "Anthropic API"},
    "deepseek": {"label": "DeepSeek", "base_url": "https://api.deepseek.com/anthropic", "key_env": "DEEPSEEK_API_KEY"},
    "ollama": {"label": "Ollama (local)", "base_url_env": "OLLAMA_HOST", "base_url": "http://127.0.0.1:11434", "key": "ollama"},
    # llama-server takes the key as X-Api-Key (what the SDK sends); without --api-key it ignores the placeholder
    "llamacpp": {"label": "llama.cpp server", "base_url_env": "HUNTUN_LLAMACPP_URL", "base_url": "http://127.0.0.1:8080",
                 "key_env": "HUNTUN_LLAMACPP_KEY", "key": "none"},
}
LOCAL_PROVIDERS = ("ollama", "llamacpp")      # servers set up in the Model providers menu or the environment, possibly several
OPTIONAL_FIELDS = ("fallbacks", "output_config", "thinking", "cache_control", "tool_choice", "eager_input_streaming")   # dropped one by one on a 400


def _tool_defs(ctx: ToolContext, provider: str = "api") -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """The tools for one cycle: Anthropic's API searches and fetches server-side; compatible providers get Huntun's own web tools."""
    specs = available_tools(ctx, provider)
    defs: list[dict[str, Any]] = [{"name": t.name, "description": t.description, "input_schema": t.input_schema} for t in specs]
    if provider == "api":
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
    compat = False                        # True for Anthropic-compatible providers (DeepSeek, Ollama, llama.cpp)
    dropped: set[str]

    def __init__(self, config: HuntunConfig, provider: str = "api") -> None:
        self.config = config
        self.provider = provider
        self.name = provider
        self.compat = provider != "api"
        self.dropped = set()
        spec = PROVIDERS[provider]
        self._client_for: tuple[str, str] | None = None
        self._clients: dict[tuple[str, str], Any] = {}
        if self.compat:
            base, key, _ = self._endpoint()
            if not key:
                raise RuntimeError(f"{spec['label']}: set {spec['key_env']}")
            self.client = anthropic.AsyncAnthropic(api_key=key, base_url=base)
            self._client_for = (base, key)
        else:
            self.client = anthropic.AsyncAnthropic()

    def _endpoint(self, model: str | None = None) -> tuple[str, str, str]:
        """(base URL, API key, model name to send) for a compatible provider. Ollama and llama.cpp servers can be several,
        from the Model providers menu and the environment: the model decides which one; without one, the first."""
        spec = PROVIDERS[self.provider]
        if self.provider in LOCAL_PROVIDERS:
            from ..models import default_route, local_route

            r = local_route(model) if model else None
            if r is None or r["type"] != self.provider:
                r = default_route(self.provider)
            return r["url"], r["key"] or spec["key"], r["model"] or model or ""
        base = os.environ.get(spec.get("base_url_env", ""), "") or spec["base_url"]
        key = (os.environ.get(spec["key_env"]) if spec.get("key_env") else None) or spec.get("key")
        return base.rstrip("/"), key or "", model or ""

    def _sync_client(self) -> None:
        """The first server of a type can change in the Model providers menu while a team runs: follow it."""
        if self.provider not in LOCAL_PROVIDERS or self._client_for is None:
            return
        base, key, _ = self._endpoint()
        if (base, key) != self._client_for:
            self.client = anthropic.AsyncAnthropic(api_key=key, base_url=base)
            self._client_for = (base, key)

    def _client(self, model: str | None) -> tuple[Any, str]:
        """The client for the server that serves this model, and the model name that server knows."""
        if self.provider not in LOCAL_PROVIDERS:
            return self.client, model or ""
        self.__dict__.setdefault("_clients", {})
        base, key, served = self._endpoint(model)
        if (base, key) == self._client_for:
            return self.client, served
        if (base, key) not in self._clients:
            self._clients[(base, key)] = anthropic.AsyncAnthropic(api_key=key, base_url=base)
        return self._clients[(base, key)], served

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
        self._sync_client()
        client, served = self._client(params.get("model"))
        if served:
            params = {**params, "model": served}
        while True:
            p = self._prepare(params)
            try:
                if self.compat:
                    async with client.messages.stream(**p) as stream:
                        return await _stream_reply(stream)
                async with client.beta.messages.stream(**p) as stream:
                    return await _stream_reply(stream)
            except anthropic.BadRequestError as e:
                msg = str(getattr(e, "message", e)).lower()
                if looks_like_overflow(msg):
                    raise ContextOverflow(str(getattr(e, "message", e))) from None
                culprit = next((f for f in OPTIONAL_FIELDS if f not in self.dropped and f.replace("_", "") in msg.replace("_", "")), None)
                if culprit is None:
                    raise
                self.dropped.add(culprit)
            except anthropic.APIError as e:                                         # llama.cpp reports a full pool inside the stream
                if looks_like_overflow(str(getattr(e, "message", e))):
                    raise ContextOverflow(str(getattr(e, "message", e))) from None
                raise

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
            "model": model or self._default_model(), "max_tokens": 16000, "output_config": {"effort": {"none": "low", "minimal": "low", "ultra": "max"}.get(effort, effort)},
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
        self._sync_client()
        try:
            client, served = self._client(self._default_model() if self.compat else None)
            await client.messages.create(model=served if self.compat else "claude-haiku-4-5", max_tokens=5, messages=[{"role": "user", "content": "ping"}])
            return True
        except anthropic.RateLimitError:
            return False
        except anthropic.APIStatusError as e:
            return not looks_like_limit(getattr(e, "message", str(e)))
        except Exception:
            return False

    async def _compact_safely(self, params: dict[str, Any], messages: list[dict[str, Any]], prompt: str, log: Callable[[str], None],
                              memory: Any, trimmed_first: bool = False) -> list[dict[str, Any]]:
        """Compacts the transcript even when the server cannot take it any more: the summary is asked of the whole
        conversation, then of copies with old tool output cut shorter and shorter; when not even those fit, a mechanical
        handoff (the task, the tools called, the last words) replaces it. Returns the transcript unchanged when the
        server is unreachable."""
        memory.state.compacting = True
        memory.save_state()
        try:
            attempts = ([] if trimmed_first else [messages]) + [_trimmed(messages, keep=2, limit=1500), _trimmed(messages, keep=0, limit=200)]
            new: list[dict[str, Any]] | None = None
            for candidate in attempts:
                try:
                    new = await self._compact(params, candidate, prompt, log)
                    break
                except ContextOverflow:
                    continue
            if new is None:
                log("even a trimmed summary request did not fit: compacting mechanically")
                new = _handoff(messages, prompt)
            memory.state.compactions += 1
            memory.state.context_tokens = 0
            memory.save_transcript(new)
            memory.activity("cycle", f"Context compacted (#{memory.state.compactions})")
            return new
        except anthropic.APIError as e:
            log(f"compaction failed: {e}")
            return messages
        finally:
            memory.state.compacting = False
            memory.save_state()

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
        live = LiveTelemetry(memory, self.provider)
        defs, by_name = _tool_defs(ctx, self.provider)
        usage = {"input": 0.0, "output": 0.0, "cache_read": 0.0, "cache_write": 0.0, "cost_usd": 0.0}
        effective_model = model or self._default_model()

        def result(outcome: str, error: str | None = None, resets_at: float | None = None) -> CycleResult:
            live.close()
            return CycleResult(outcome, cycle.summary, cycle.next_task, error, usage, resets_at)

        saved = memory.load_transcript()
        foreign = saved and any(m.get("role") not in ("user", "assistant") or "tool_calls" in m for m in saved)   # left by the vllm backend (OpenAI format)
        messages = saved if saved and not foreign else [{"role": "user", "content": prompt}]
        memory.save_transcript(messages)
        use_fallbacks = self.config.fallbacks
        pause_continuations = 0
        nudged = False
        attempts = 0
        overflows = 0

        while True:
            if should_stop():
                return result("paused")
            params = {
                "model": effective_model,
                "max_tokens": self.config.max_tokens,
                "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
                "tools": defs,
                "messages": messages,
                "output_config": {"effort": {"none": "low", "minimal": "low", "ultra": "max"}.get(effort, effort)},
                "thinking": {"type": "adaptive", "display": "summarized"},
            }
            try:
                memory.activity("waiting", "Waiting for provider response")
                observer_token = _stream_observer.set(live.anthropic_event)
                try:
                    message = await self._call(params, use_fallbacks)
                finally:
                    _stream_observer.reset(observer_token)
                attempts = 0
            except ContextOverflow as e:
                overflows += 1
                if overflows > OVERFLOW_RECOVERIES:
                    return result("error", f"the context kept overflowing: {e}")
                if e.pool_full and self.provider == "llamacpp":
                    url = self._endpoint(effective_model)[0]
                    if await asyncio.to_thread(note_shared_pool, url):
                        log(f"{url} shares one KV pool among its sessions: each now counts on {context_limit(effective_model)} tokens")
                    memory.state.context_limit = context_limit(effective_model)
                    memory.save_state()
                own_share_exceeded = memory.state.context_tokens > COMPACT_AT * context_limit(effective_model)
                if len(messages) > 1 and (not e.pool_full or own_share_exceeded or not memory.state.context_tokens):
                    log(f"context overflow ({e}); compacting")
                    messages = await self._compact_safely(params, messages, prompt, log, memory, trimmed_first=True)
                    continue
                if e.pool_full:                                                 # another session filled the shared pool; it compacts too
                    wait = 15 * overflows
                    log(f"the server's shared context pool was full ({e}); retrying in {wait}s")
                    await asyncio.sleep(wait)
                    continue
                return result("error", f"the task prompt alone does not fit the model's context: {e}")
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
                messages = await self._compact_safely(params, messages, prompt, log, memory)
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


def _cut(text: str, limit: int) -> str:
    return text if len(text) <= limit else f"{text[:limit]}\n[... {len(text) - limit} characters cut to save context]"


def _trimmed(messages: list[dict[str, Any]], keep: int, limit: int) -> list[dict[str, Any]]:
    """A copy of the transcript for a summary request that must fit: all but the last `keep` messages lose their
    thinking, and their tool outputs and long tool inputs are cut to `limit` characters."""
    out: list[dict[str, Any]] = []
    for i, m in enumerate(messages):
        content = m.get("content")
        if i == 0 or i >= len(messages) - keep or not isinstance(content, (str, list)):   # the task prompt stays whole
            out.append(m)
            continue
        if isinstance(content, str):
            out.append({**m, "content": _cut(content, limit * 2)})
            continue
        blocks: list[Any] = []
        for b in content:
            if not isinstance(b, dict):
                blocks.append(b)
            elif b.get("type") in ("thinking", "redacted_thinking"):
                continue
            elif b.get("type") == "tool_result":
                c = b.get("content")
                text = c if isinstance(c, str) else "\n".join(x.get("text", "") for x in c or [] if isinstance(x, dict))
                blocks.append({**b, "content": _cut(text, limit)})
            elif b.get("type") == "tool_use" and isinstance(b.get("input"), dict):
                blocks.append({**b, "input": {k: _cut(v, limit) if isinstance(v, str) else v for k, v in b["input"].items()}})
            elif b.get("type") == "text":
                blocks.append({**b, "text": _cut(b.get("text", ""), limit * 2)})
            else:
                blocks.append(b)
        out.append({**m, "content": blocks or [{"type": "text", "text": "(trimmed)"}]})
    return out


def _handoff(messages: list[dict[str, Any]], prompt: str) -> list[dict[str, Any]]:
    """A handoff written without the model, for when not even a trimmed summary request fits: the task, every tool
    called in this cycle, and the last thing the agent said."""
    first = messages[0]["content"] if messages and messages[0]["role"] == "user" and isinstance(messages[0]["content"], str) else prompt
    calls: list[str] = []
    last = ""
    for m in messages[1:]:
        if m.get("role") != "assistant" or not isinstance(m.get("content"), list):
            continue
        for b in m["content"]:
            if isinstance(b, dict) and b.get("type") == "tool_use":
                calls.append(f"- {b.get('name')} {_short(b.get('input'), 160)}")
            elif isinstance(b, dict) and b.get("type") == "text" and b.get("text", "").strip():
                last = b["text"].strip()
    done = "\n".join(calls[-60:]) or "(none)"
    return [{"role": "user", "content": f"{first}\n\n[Context compacted: the conversation of this cycle no longer fitted the model's context, so it was replaced "
                                        f"by this record. Tools you called, oldest first:]\n{done}\n\n[The last thing you said:]\n{_cut(last, 2000) or '(nothing)'}\n\n"
                                        "Check git_status, your notes and the board for what you already did, then continue from here."}]
