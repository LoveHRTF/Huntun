"""Model catalog the master chooses from, with list prices (USD per million tokens) and context windows."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ModelInfo:
    id: str
    label: str
    tier: str
    input_per_m: float
    output_per_m: float
    context: int
    use_for: str
    vendor: str = "anthropic"


MODEL_CATALOG: list[ModelInfo] = [
    ModelInfo("claude-opus-5", "Claude Opus 5", "frontier", 5.0, 25.0, 1_000_000,
              "architecture, hard debugging, leadership reviews, anything where a wrong decision is expensive"),
    ModelInfo("claude-sonnet-5", "Claude Sonnet 5", "strong", 2.0, 10.0, 1_000_000,
              "most engineering work: implementing well-specified features, tests, refactors, research, design docs"),
    ModelInfo("claude-haiku-4-5", "Claude Haiku 4.5", "fast", 1.0, 5.0, 200_000,
              "routine, well-bounded tasks: documentation, formatting, simple scripts, status summaries, scrum bookkeeping"),
]
CODEX_CATALOG: list[ModelInfo] = [
    ModelInfo(mid.strip(), mid.strip(), "codex", 0.0, 0.0, 400_000, "OpenAI Codex agent model, strong at end-to-end coding tasks (billed through your ChatGPT plan; Huntun cannot price it)", "openai")
    for mid in __import__("os").environ.get("HUNTUN_CODEX_MODELS", "gpt-5.3-codex").split(",") if mid.strip()
]
def kimi_models() -> list[str]:
    """Model aliases the local Kimi Code install knows, default first (HUNTUN_KIMI_MODELS overrides the list)."""
    import os
    import tomllib
    from pathlib import Path

    forced = [m.strip() for m in os.environ.get("HUNTUN_KIMI_MODELS", "").split(",") if m.strip()]
    if forced:
        return forced
    home = Path(os.environ.get("KIMI_CODE_HOME") or Path.home() / ".kimi-code")
    try:
        cfg = tomllib.loads((home / "config.toml").read_text())
    except (OSError, tomllib.TOMLDecodeError):
        return []
    aliases = list((cfg.get("models") or {}).keys())
    default = cfg.get("default_model")
    if default and default in aliases:
        aliases.remove(default)
        aliases.insert(0, default)
    return aliases


def _kimi_catalog() -> list[ModelInfo]:
    """Kimi Code model aliases from the local install (its config.toml), or HUNTUN_KIMI_MODELS; priced at 0 like Codex."""
    return [ModelInfo(a, a, "kimi", 0.0, 0.0, 262_144, "Kimi Code agent model alias from your Kimi Code config, strong at coding tasks (billed through your Kimi plan; Huntun cannot price it)", "moonshot")
            for a in kimi_models()]


KIMI_CATALOG: list[ModelInfo] = _kimi_catalog()
MODEL_BY_ID = {m.id: m for m in MODEL_CATALOG + CODEX_CATALOG + KIMI_CATALOG}
MODEL_IDS = [m.id for m in MODEL_CATALOG + CODEX_CATALOG + KIMI_CATALOG]


def catalog_for(backend: str) -> list[ModelInfo]:
    return CODEX_CATALOG if backend == "codex" else KIMI_CATALOG if backend == "kimi" else MODEL_CATALOG


def available_backends() -> dict[str, str]:
    """Backends usable on this machine -> a short reason (login / key found)."""
    import os
    import shutil

    out: dict[str, str] = {}
    if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"):
        out["api"] = "Anthropic API key"
    if shutil.which("claude"):
        out["claude-code"] = "Claude Code login"
    codex = os.environ.get("HUNTUN_CODEX_BIN") or shutil.which("codex")
    if codex and _codex_works(codex):
        out["codex"] = "OpenAI Codex login"
    kimi = os.environ.get("HUNTUN_KIMI_BIN") or shutil.which("kimi")
    if kimi and _codex_works(kimi) and KIMI_CATALOG:
        out["kimi"] = "Kimi Code login"
    return out


_codex_ok: dict[str, bool] = {}


def _codex_works(path: str) -> bool:
    """`codex --version` must succeed; a broken install (missing platform binary) otherwise looks available."""
    if path in _codex_ok:
        return _codex_ok[path]
    import subprocess

    try:
        ok = subprocess.run([path, "--version"], capture_output=True, text=True, timeout=20).returncode == 0
    except (OSError, subprocess.SubprocessError):
        ok = False
    _codex_ok[path] = ok
    return ok


def backend_for_model(model_id: str | None, available: dict[str, str] | None = None, default: str = "") -> str:
    """Which backend runs a model: Codex models on codex; Anthropic models on Claude Code when logged in, else the API."""
    avail = available if available is not None else available_backends()
    m = model_info(model_id)
    if m is None:
        return default
    if m.vendor == "openai":
        return "codex" if "codex" in avail else default
    if m.vendor == "moonshot":
        return "kimi" if "kimi" in avail else default
    if "claude-code" in avail:
        return "claude-code"
    if "api" in avail:
        return "api"
    return default


def catalog_available(available: dict[str, str] | None = None) -> list[tuple[ModelInfo, str]]:
    """Every model the team can actually run here, with the backend that would run it."""
    avail = available if available is not None else available_backends()
    out: list[tuple[ModelInfo, str]] = []
    if "claude-code" in avail or "api" in avail:
        out += [(m, "claude-code" if "claude-code" in avail else "api") for m in MODEL_CATALOG]
    if "codex" in avail:
        out += [(m, "codex") for m in CODEX_CATALOG]
    if "kimi" in avail:
        out += [(m, "kimi") for m in KIMI_CATALOG]
    return out
EFFORTS = ["low", "medium", "high", "xhigh", "max"]
DEFAULT_API_MODEL = "claude-opus-5"


def model_info(model_id: str | None) -> ModelInfo | None:
    if not model_id:
        return None
    if model_id in MODEL_BY_ID:
        return MODEL_BY_ID[model_id]
    for m in MODEL_CATALOG:  # tolerate aliases such as "opus", "sonnet", "haiku"
        if m.tier and model_id.lower() in m.id:
            return m
        if model_id.lower() in ("opus", "sonnet", "haiku") and model_id.lower() in m.id:
            return m
    return None


def context_limit(model_id: str | None) -> int:
    m = model_info(model_id)
    return m.context if m else 200_000


def cost_usd(model_id: str | None, input_tokens: float, output_tokens: float, cache_read: float = 0.0, cache_write: float = 0.0) -> float:
    """List-price cost of one call on the API backend (cache reads at 10%, cache writes at 125%)."""
    m = model_info(model_id) or MODEL_BY_ID[DEFAULT_API_MODEL]
    return round((input_tokens * m.input_per_m + cache_read * m.input_per_m * 0.1 + cache_write * m.input_per_m * 1.25 + output_tokens * m.output_per_m) / 1e6, 6)


def estimate_cost_usd(model_id: str | None, tokens: float) -> float:
    """Rough list-price cost for a token volume: assumes 85% input (half of it cached), 15% output. Codex models price at 0."""
    m = model_info(model_id)
    if not m or m.tier in ("codex", "kimi"):
        return 0.0
    inp, out = tokens * 0.85, tokens * 0.15
    return round((inp * 0.5 * m.input_per_m + inp * 0.5 * m.input_per_m * 0.1 + out * m.output_per_m) / 1e6, 2)


BACKEND_LABEL = {"api": "Anthropic API", "claude-code": "Claude Code", "codex": "OpenAI Codex", "kimi": "Kimi Code"}


def catalog_text(backend: str = "api", available: dict[str, str] | None = None) -> str:
    pairs = catalog_available(available)
    if not pairs:
        pairs = [(m, backend) for m in catalog_for(backend)]
    lines = []
    for m, b in pairs:
        price = f"${m.input_per_m:g}/M input, ${m.output_per_m:g}/M output" if m.input_per_m else "billed through the ChatGPT plan, no per-token price"
        lines.append(f"- {m.id} ({m.label}; vendor {m.vendor}; runs via {BACKEND_LABEL.get(b, b)}; {price}; {m.context // 1000}k context): {m.use_for}")
    return "\n".join(lines)
