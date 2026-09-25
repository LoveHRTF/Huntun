"""vLLM backend: a local vLLM server's OpenAI-compatible Chat Completions API (any server speaking the same protocol works:
SGLang, LM Studio, llama.cpp's llama-server).

Each work cycle is a manual tool-use loop over POST {VLLM_BASE_URL}/chat/completions that drives Huntun's own file,
shell and team tools, like the API backend; the message history (OpenAI format) is written to disk after every step so a
cycle can be paused and resumed. Requests use only the standard library and run on daemon threads, so a slow local
generation never holds up shutdown.

vLLM has to be started with tool calling on (`--enable-auto-tool-choice --tool-call-parser <parser for the model>`);
structured answers (goal check, team plan) use a named tool choice, which vLLM serves with guided decoding.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ..models import catalog_for, context_limit, vllm_api_key, vllm_base_url
from ..tools import ToolContext, available_tools, execute
from ..types import CycleResult, HuntunConfig
from . import looks_like_limit

RETRYABLE_ATTEMPTS = 6
COMPACT_AT = 0.6  # compact the working context when a call reports more than this fraction of the window
MAX_BAD_TURNS = 5  # consecutive turns whose tool calls were all unusable (unknown tool, broken JSON) before the cycle gives up
TOOL_PARSER_HINT = ("the vLLM server has tool calling off. Restart it with `--enable-auto-tool-choice --tool-call-parser <parser>` "
                    "(e.g. hermes for Qwen, llama3_json for Llama, mistral for Mistral).")


class ServerError(Exception):
    """An HTTP error status from the server (status 0: no usable answer: connection refused, timeout, bad JSON)."""

    def __init__(self, status: int, message: str, retry_after: float | None = None) -> None:
        super().__init__(f"{status}: {message}" if status else message)
        self.status, self.message, self.retry_after = status, message, retry_after

    @property
    def transient(self) -> bool:
        return self.status == 0 or self.status >= 500

    @property
    def context_overflow(self) -> bool:
        return self.status == 400 and "context length" in self.message.lower()


def _error_message(raw: str) -> str:
    """vLLM answers errors as {"message": ...} (older) or {"error": {"message": ...}} (OpenAI style)."""
    try:
        data = json.loads(raw)
    except ValueError:
        return raw.strip()[:500]
    if isinstance(data, dict):
        err = data.get("error")
        if isinstance(err, dict) and err.get("message"):
            return str(err["message"])
        if isinstance(err, str):
            return err
        if data.get("message"):
            return str(data["message"])
    return raw.strip()[:500]


def _http(url: str, key: str, body: dict[str, Any] | None, timeout: float) -> dict[str, Any]:
    """One blocking JSON request (GET without a body, POST with one)."""
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    req = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None, headers=headers,
                                 method="POST" if body is not None else "GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        ra = e.headers.get("retry-after") if e.headers else None
        try:
            retry_after = float(ra) if ra else None
        except ValueError:
            retry_after = None
        raise ServerError(e.code, _error_message(e.read().decode("utf-8", errors="replace")) or str(e.reason), retry_after) from None
    except (urllib.error.URLError, OSError) as e:                                         # refused, reset, timed out, DNS
        raise ServerError(0, f"cannot reach {url}: {getattr(e, 'reason', e)}") from None
    try:
        data = json.loads(raw)
    except ValueError:
        raise ServerError(0, f"{url} did not answer with JSON: {raw[:200]}") from None
    if not isinstance(data, dict):
        raise ServerError(0, f"{url} answered with unexpected JSON: {raw[:200]}")
    return data


async def _in_thread(fn: Callable[..., Any], *args: Any) -> Any:
    """Runs a blocking call on a daemon thread (unlike asyncio.to_thread, exit never waits for a generation to finish)."""
    loop = asyncio.get_running_loop()
    fut: asyncio.Future[Any] = loop.create_future()

    def settle(result: Any, exc: BaseException | None) -> None:
        if fut.done():
            return
        if exc is not None:
            fut.set_exception(exc)
        else:
            fut.set_result(result)

    def run() -> None:
        try:
            result, exc = fn(*args), None
        except BaseException as e:  # noqa: BLE001 - handed to the awaiting coroutine
            result, exc = None, e
        try:
            loop.call_soon_threadsafe(settle, result, exc)
        except RuntimeError:                                                               # the loop closed while we waited
            pass

    threading.Thread(target=run, name="huntun-vllm", daemon=True).start()
    return await fut


def _split_think(text: str) -> tuple[str, str]:
    """Separates <think>…</think> reasoning a server left in the content (no --reasoning-parser) from the reply."""
    m = re.search(r"<think>(.*?)</think>", text, re.DOTALL) or (re.search(r"^(.*?)</think>", text, re.DOTALL) if "</think>" in text else None)
    if not m:
        return "", text
    return m.group(1).strip(), (text[:m.start()] + text[m.end():]).strip()


def _reply(data: dict[str, Any]) -> tuple[str, str, list[dict[str, Any]], str]:
    """(text, reasoning, tool calls, finish reason) of the first choice; tool calls get ids when the server left them out."""
    choices = data.get("choices") or []
    if not choices:
        raise ServerError(0, f"no choices in the response: {json.dumps(data)[:200]}")
    msg = choices[0].get("message") or {}
    reasoning = msg.get("reasoning_content") or msg.get("reasoning") or ""
    think, text = _split_think(msg.get("content") or "")
    calls = []
    for tc in msg.get("tool_calls") or []:
        fn = tc.get("function") or {}
        args = fn.get("arguments")
        calls.append({"id": tc.get("id") or f"call_{uuid.uuid4().hex[:12]}", "type": "function",
                      "function": {"name": fn.get("name") or "", "arguments": args if isinstance(args, str) else json.dumps(args or {})}})
    return text, (reasoning or think).strip(), calls, choices[0].get("finish_reason") or ""


def _parse_args(raw: str) -> tuple[Any, str | None]:
    if not raw.strip():
        return {}, None
    try:
        return json.loads(raw), None
    except ValueError as e:
        return None, f"arguments are not valid JSON ({e}); call the tool again with a JSON object"


def is_openai_transcript(messages: list[dict[str, Any]]) -> bool:
    """True unless the saved transcript is another backend's (Anthropic content blocks), which this loop cannot replay."""
    return all(isinstance(m, dict) and m.get("role") in ("user", "assistant", "tool") and not isinstance(m.get("content"), list) for m in messages)


class VllmBackend:
    name = "vllm"

    def __init__(self, config: HuntunConfig) -> None:
        self.config = config
        self.base_url = vllm_base_url()
        self.api_key = vllm_api_key()
        self.timeout = float(os.environ.get("HUNTUN_VLLM_TIMEOUT", "1800"))
        self.forced_choice_ok = True                                                      # False once the server rejected a named / "none" tool_choice

    def _default_model(self) -> str:
        cat = catalog_for("vllm")
        if not cat:
            raise RuntimeError(f"no model: nothing answers at {self.base_url}/models. Start vLLM, or set VLLM_BASE_URL / HUNTUN_VLLM_MODELS")
        return cat[0].id

    async def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        return await _in_thread(_http, self.base_url + path, self.api_key, body, self.timeout)

    async def _send(self, body: dict[str, Any]) -> dict[str, Any]:
        """One chat completion. A max_tokens the context cannot fit is retried without it (the server then fills what is
        left); a tool_choice other than "auto" that the server rejects is left out from then on."""
        while True:
            p = dict(body)
            forced = p.get("tool_choice") not in (None, "auto")
            if forced and not self.forced_choice_ok:
                p.pop("tool_choice")
            try:
                return await self._post("/chat/completions", p)
            except ServerError as e:
                msg = e.message.lower()
                if e.status == 400 and ("enable-auto-tool-choice" in msg or "tool-call-parser" in msg):
                    raise ServerError(400, TOOL_PARSER_HINT) from None
                if e.context_overflow and "max_tokens" in body:
                    body = {k: v for k, v in body.items() if k != "max_tokens"}
                    continue
                if e.status == 400 and "tool_choice" in msg and forced and self.forced_choice_ok:
                    self.forced_choice_ok = False
                    continue
                raise

    def _max_tokens(self, model: str) -> int:
        """Output cap: the configured one, but at most a quarter of the window so the prompt still fits beside it."""
        return max(1024, min(self.config.max_tokens, context_limit(model) // 4))

    async def structured(self, *, prompt: str, tool_name: str, description: str, schema: dict[str, Any], model: str, effort: str,
                         log: Callable[[str], None] | None = None, cwd: Path | None = None) -> dict[str, Any]:
        say = log or (lambda _t: None)
        model = model or self._default_model()
        say(f"Calling {model} (vLLM at {self.base_url}), waiting for the answer…")
        body = {
            "model": model, "max_tokens": min(16000, self._max_tokens(model)),
            "messages": [{"role": "user", "content": prompt + f"\n\nCall the `{tool_name}` tool with your answer; do not answer in prose."}],
            "tools": [{"type": "function", "function": {"name": tool_name, "description": description, "parameters": schema}}],
            "tool_choice": {"type": "function", "function": {"name": tool_name}},
        }
        text, reasoning, calls, _ = _reply(await self._send(body))
        if reasoning:
            say(reasoning[:400])
        if text:
            say(text[:400])
        for c in calls:
            if c["function"]["name"] == tool_name:
                data, _problem = _parse_args(c["function"]["arguments"])
                if isinstance(data, dict):
                    say(f"Answer received via {tool_name}")
                    return data
                text = c["function"]["arguments"] + "\n" + text
        start, end = text.find("{"), text.rfind("}")                                    # the answer came as JSON in text
        if start >= 0 and end > start:
            try:
                data = json.loads(text[start:end + 1])
                if isinstance(data, dict):
                    return data
            except ValueError:
                pass
        raise RuntimeError(f"Model did not call {tool_name}. It said: {text[:500]}")

    async def probe(self) -> bool:
        try:
            await self._post("/chat/completions", {"model": self._default_model(), "max_tokens": 1, "messages": [{"role": "user", "content": "ping"}]})
            return True
        except ServerError as e:
            return not (e.status in (0, 429) or looks_like_limit(e.message))
        except Exception:
            return False

    async def _compact(self, body: dict[str, Any], messages: list[dict[str, Any]], prompt: str, log: Callable[[str], None]) -> list[dict[str, Any]]:
        """Client-side compaction: ask the model for a handoff summary, then restart the transcript from it."""
        ask = {**body, "messages": body["messages"][:1] + messages + [{"role": "user", "content": (
            "Your context is nearly full. Write a compact handoff summary for yourself: what the task is, what you have done (files, commits, "
            "decisions), what remains, and any open questions or board threads to follow up. Do not call tools.")}], "tool_choice": "none", "max_tokens": 4000}
        text, _, _, _ = _reply(await self._send(ask))
        summary = text.strip() or "(no summary produced)"
        log(f"compacted context ({len(messages)} messages -> summary of {len(summary)} chars)")
        return [{"role": "user", "content": f"{_first_prompt(messages, prompt)}\n\n[Context compacted. Your handoff summary from before the compaction:]\n{summary}\n\nContinue from here."}]

    async def run_cycle(self, *, ctx: ToolContext, system: str, prompt: str, model: str, effort: str,
                        should_stop: Callable[[], bool], log: Callable[[str], None]) -> CycleResult:
        memory, cycle = ctx.memory, ctx.cycle
        specs = available_tools(ctx, "vllm")
        by_name = {t.name: t for t in specs}
        tools = [{"type": "function", "function": {"name": t.name, "description": t.description, "parameters": t.input_schema}} for t in specs]
        usage = {"input": 0.0, "output": 0.0, "cache_read": 0.0, "cache_write": 0.0, "cost_usd": 0.0}

        def result(outcome: str, error: str | None = None, resets_at: float | None = None) -> CycleResult:
            return CycleResult(outcome, cycle.summary, cycle.next_task, error, usage, resets_at)

        try:
            effective_model = model or self._default_model()
        except RuntimeError as e:
            return result("error", str(e))
        saved = memory.load_transcript()
        messages: list[dict[str, Any]] = saved if saved and is_openai_transcript(saved) else [{"role": "user", "content": prompt}]
        memory.save_transcript(messages)
        nudged = False
        attempts = 0
        overflow_resets = 0
        bad_turns = 0

        while True:
            if should_stop():
                return result("paused")
            body = {
                "model": effective_model,
                "max_tokens": self._max_tokens(effective_model),
                "messages": [{"role": "system", "content": system}] + messages,
                "tools": tools,
                "tool_choice": "auto",
            }
            try:
                data = await self._send(body)
                text, reasoning, calls, finish = _reply(data)
                attempts = 0
            except ServerError as e:
                if e.status == 429 or (not e.context_overflow and looks_like_limit(e.message)):
                    memory.save_transcript(messages)
                    return result("limit", f"rate limited: {e.message}", time.time() + e.retry_after if e.retry_after else None)
                if e.context_overflow and len(messages) > 1 and overflow_resets < 1:
                    # Too long to even ask for a summary: start over from the task. Work so far is on disk (files, commits, notes).
                    overflow_resets += 1
                    log(f"context window exceeded ({e.message[:120]}); restarting the cycle's conversation from the task")
                    messages = [{"role": "user", "content": f"{_first_prompt(messages, prompt)}\n\n[Your earlier conversation in this cycle overflowed the context window and "
                                                            "was dropped. What you did is on disk: check git status, your notes and the board before continuing.]"}]
                    memory.save_transcript(messages)
                    continue
                if e.transient:
                    attempts += 1
                    if attempts > RETRYABLE_ATTEMPTS:
                        return result("error", f"gave up after {attempts} attempts: {e}")
                    wait = min(120, 5 * 2**attempts)
                    log(f"vLLM server error ({e}); retrying in {wait}s")
                    await asyncio.sleep(wait)
                    continue
                return result("error", f"vLLM error {e}")

            u = data.get("usage") or {}
            prompt_tokens, completion_tokens = int(u.get("prompt_tokens") or 0), int(u.get("completion_tokens") or 0)
            cached = int((u.get("prompt_tokens_details") or {}).get("cached_tokens") or 0)
            usage["input"] += prompt_tokens - cached
            usage["cache_read"] += cached
            usage["output"] += completion_tokens
            memory.state.context_tokens = prompt_tokens + completion_tokens
            memory.state.context_limit = context_limit(effective_model)
            memory.save_state()
            needs_compaction = memory.state.context_tokens > COMPACT_AT * memory.state.context_limit and bool(calls)

            if reasoning:
                memory.activity("thinking", reasoning)
            if text:
                memory.activity("text", text)
            messages.append({"role": "assistant", "content": text or (None if calls else ""), **({"tool_calls": calls} if calls else {})})

            if not calls:
                if finish == "length":
                    messages.append({"role": "user", "content": "Your reply was cut off (max_tokens). Continue, more concisely."})
                    memory.save_transcript(messages)
                    continue
                if not cycle.finished:
                    cycle.finished = True
                    cycle.summary = text or "(cycle ended without a summary)"
                    cycle.next_task = cycle.next_task or memory.state.current_task
                memory.clear_transcript()
                return result("finished")

            results: list[dict[str, Any]] = []
            ran = 0
            for c in calls:
                name, raw = c["function"]["name"], c["function"]["arguments"]
                if finish == "length":
                    results.append({"role": "tool", "tool_call_id": c["id"], "content": "Tool input was truncated at max_tokens. Retry with a smaller input (e.g. write the file in parts)."})
                    continue
                if should_stop():
                    # Do not run tools while pausing. Roll the assistant turn back so the transcript stays valid.
                    messages.pop()
                    memory.save_transcript(messages)
                    return result("paused")
                spec = by_name.get(name)
                args, problem = _parse_args(raw)
                if spec is None or problem:
                    results.append({"role": "tool", "tool_call_id": c["id"], "content": f"ERROR: unknown tool {name}" if spec is None else f"ERROR: {problem}"})
                    continue
                ran += 1
                started = time.monotonic()
                memory.activity("tool", f"{name} {_short(args, 300)}")
                out, is_err = await execute(spec, args, ctx)
                log(f"{name} {_short(args)} -> {'ERROR ' if is_err else ''}{time.monotonic() - started:.1f}s")
                results.append({"role": "tool", "tool_call_id": c["id"], "content": out})
                if cycle.finished:
                    break
            answered = {r["tool_call_id"] for r in results}
            results += [{"role": "tool", "tool_call_id": c["id"], "content": "Skipped: the cycle already finished."} for c in calls if c["id"] not in answered]
            messages += results
            bad_turns = 0 if ran or finish == "length" else bad_turns + 1
            if bad_turns >= MAX_BAD_TURNS:
                memory.clear_transcript()                                                  # replaying the broken turns would only repeat them
                return result("error", f"the model made only unusable tool calls {bad_turns} turns in a row (last: {results[-1]['content'][:200]}); "
                                       "check that vLLM's --tool-call-parser matches the model")
            if not cycle.finished and cycle.tool_calls >= self.config.max_tool_calls_per_cycle and not nudged:
                nudged = True
                messages.append({"role": "user", "content": f"You have used {cycle.tool_calls} tool calls this cycle, which is the budget. Commit any finished work, update your notes with what remains, and call finish_cycle now."})
            memory.save_transcript(messages)

            if cycle.finished:
                memory.clear_transcript()
                return result("finished")
            if needs_compaction:
                memory.state.compacting = True
                memory.save_state()
                try:
                    messages = await self._compact(body, messages, prompt, log)
                    memory.state.compactions += 1
                    memory.state.context_tokens = 0
                    memory.save_transcript(messages)
                    memory.activity("cycle", f"Context compacted (#{memory.state.compactions})")
                except ServerError as e:
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


def _first_prompt(messages: list[dict[str, Any]], prompt: str) -> str:
    first = messages[0] if messages else {}
    return first["content"] if first.get("role") == "user" and isinstance(first.get("content"), str) else prompt


def _short(data: Any, n: int = 100) -> str:
    try:
        s = json.dumps(data)
    except Exception:
        return ""
    return s if len(s) <= n else s[:n] + "…"
