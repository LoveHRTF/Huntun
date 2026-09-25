"""Model backends. Both drive the same tools and produce the same CycleResult.

- api:         the Anthropic Messages API via the `anthropic` SDK (needs ANTHROPIC_API_KEY or an `ant auth login` profile)
- claude-code: Claude Code via the `claude-agent-sdk` (uses your Claude Code login; no API key needed)
- codex:       OpenAI Codex via `codex exec` (uses your ChatGPT / Codex login; no API key needed)
- kimi:        Kimi Code via `kimi -p` (uses your Kimi login or Moonshot key; no API key needed here)
- deepseek:    DeepSeek's Anthropic-compatible API (DEEPSEEK_API_KEY), driven by the api backend's loop
- ollama:      a local Ollama server's Anthropic-compatible API (OLLAMA_HOST, default http://127.0.0.1:11434), same loop
- vllm:        a local vLLM server's OpenAI-compatible Chat Completions API (VLLM_BASE_URL, default http://127.0.0.1:8000/v1)
"""
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

from ..tools import ToolContext
from ..types import CycleResult


class Backend(Protocol):
    name: str

    async def run_cycle(
        self,
        *,
        ctx: ToolContext,
        system: str,
        prompt: str,
        model: str,
        effort: str,
        should_stop: Callable[[], bool],
        log: Callable[[str], None],
    ) -> CycleResult: ...

    async def structured(self, *, prompt: str, tool_name: str, description: str, schema: dict[str, Any], model: str, effort: str,
                         log: Callable[[str], None] | None = None, cwd: Path | None = None) -> dict[str, Any]:
        """Asks the model one question and returns the arguments it passes to the given tool. `log` receives progress lines for the UI;
        `cwd` is the project directory the question is about (the session runs there, so the model never mistakes Huntun's own cwd for it)."""
        ...

    async def probe(self) -> bool:
        """Cheapest possible call: True when the provider accepts requests again after a usage-limit pause."""
        ...


LIMIT_RE = __import__("re").compile(r"usage limit|rate limit|rate_limit|too many requests|limit reached|limit exceeded|resets? at|\b429\b|out of (?:extra )?usage", __import__("re").IGNORECASE)


def continue_session(state: Any, max_cycles: int) -> bool:
    """Whether the next cycle should continue the agent's existing conversation (Claude Code session / Codex thread).

    Sessions carry over between cycles so the context and its compactions behave like one long conversation; after
    `max_cycles` cycles (0 = never) the agent starts fresh so a stale context does not build up forever.
    """
    if not state.session_id:
        return False
    if state.resume_pending:
        return True
    return max_cycles <= 0 or state.session_cycles < max_cycles


def looks_like_limit(text: str | None) -> bool:
    return bool(text) and bool(LIMIT_RE.search(text or ""))


def make_backend(name: str, config: Any) -> Backend:
    if name == "api":
        from .api import ApiBackend

        return ApiBackend(config)
    if name == "claude-code":
        from .claude_code import ClaudeCodeBackend

        return ClaudeCodeBackend(config)
    if name == "codex":
        from .codex import CodexBackend

        return CodexBackend(config)
    if name == "kimi":
        from .kimi import KimiBackend

        return KimiBackend(config)
    if name in ("deepseek", "ollama"):
        from .api import ApiBackend

        return ApiBackend(config, provider=name)
    if name == "vllm":
        from .vllm import VllmBackend

        return VllmBackend(config)
    raise ValueError(f"unknown backend {name!r}")


BACKENDS = ("api", "claude-code", "codex", "kimi", "deepseek", "ollama", "vllm")
