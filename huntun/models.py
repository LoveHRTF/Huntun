"""Model catalog the master chooses from, with list prices (USD per million tokens) and context windows."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


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
    ModelInfo("claude-opus-5-5", "Claude Opus 5.5", "frontier", 4.0, 20.0, 1_000_000,
              "the default frontier pick: architecture, hard debugging, long agentic coding runs, leadership reviews; cheaper and faster than Opus 5"),
    ModelInfo("claude-opus-5", "Claude Opus 5", "frontier", 5.0, 25.0, 1_000_000,
              "previous-generation frontier model (legacy); prefer Opus 5.5 unless a project needs it specifically"),
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
DEEPSEEK_CATALOG: list[ModelInfo] = [
    ModelInfo("deepseek-v4-pro", "DeepSeek V4 Pro", "strong", 1.32, 3.96, 1_000_000,
              "DeepSeek's flagship: strong at coding and reasoning at a fraction of frontier prices (peak-hour list price; off-peak is half)", "deepseek"),
    ModelInfo("deepseek-flash", "DeepSeek V4.1 Flash", "fast", 0.30, 1.20, 1_000_000,
              "very cheap and fast: routine implementation, tests, docs, scrum bookkeeping (peak-hour list price; off-peak is half)", "deepseek"),
]
OLLAMA_CATALOG: list[ModelInfo] = []                                             # the Ollama servers' models, filled by refresh_local()
VLLM_CATALOG: list[ModelInfo] = []                                               # the vLLM / OpenAI-compatible servers' models
LLAMACPP_CATALOG: list[ModelInfo] = []                                           # the llama.cpp servers' models
MODEL_BY_ID = {m.id: m for m in MODEL_CATALOG + CODEX_CATALOG + KIMI_CATALOG + DEEPSEEK_CATALOG}
MODEL_IDS = [m.id for m in MODEL_CATALOG + CODEX_CATALOG + KIMI_CATALOG + DEEPSEEK_CATALOG]


def ollama_host() -> str:
    import os

    return (os.environ.get("OLLAMA_HOST") or "http://127.0.0.1:11434").rstrip("/")


def vllm_base_url() -> str:
    """The OpenAI-compatible endpoint root, ending in /v1 (VLLM_BASE_URL may be given with or without it)."""
    import os

    base = (os.environ.get("VLLM_BASE_URL") or "http://127.0.0.1:8000/v1").rstrip("/")
    return base if base.endswith("/v1") else base + "/v1"


def vllm_api_key() -> str:
    """VLLM_API_KEY: the key the server was started with (`--api-key`), sent as a Bearer token; empty when it checks none."""
    import os

    return os.environ.get("VLLM_API_KEY", "")


# ---- local model servers: llama.cpp, Ollama and vLLM (or any OpenAI-compatible) servers on this machine or the network ----
# The Model providers menu saves any number of them (~/.huntun/providers.json, private to the owner: it holds API keys);
# the environment adds one of each type (HUNTUN_LLAMACPP_URL, OLLAMA_HOST, VLLM_BASE_URL), and Ollama and vLLM are also
# looked for at their default local addresses. Every model they serve joins the catalog, and a call for a model goes to
# the server that serves it. When two servers serve the same name, each copy gets the id "name@server".
SERVER_TYPES = {"llamacpp": "llama.cpp server", "ollama": "Ollama server", "vllm": "vLLM / OpenAI-compatible server"}
_DEFAULT_URL = {"llamacpp": "http://127.0.0.1:8080", "ollama": "http://127.0.0.1:11434", "vllm": "http://127.0.0.1:8000"}
_CONTEXT_ENV = {"llamacpp": "HUNTUN_LLAMACPP_CONTEXT", "ollama": "HUNTUN_OLLAMA_CONTEXT", "vllm": "HUNTUN_VLLM_CONTEXT"}
LOCAL_CATALOGS = {"llamacpp": LLAMACPP_CATALOG, "ollama": OLLAMA_CATALOG, "vllm": VLLM_CATALOG}
ROUTES: dict[str, dict[str, str]] = {}                                           # model id -> {"server", "type", "url", "key", "model" (the name the server knows)}
SERVER_STATUS: dict[str, dict[str, Any]] = {}                                    # server id -> what it answered on the last refresh
_local_checked = 0.0
_saved_cache: tuple[Any, dict] = (None, {})


def normalize_server_url(url: str) -> str:
    """ "10.0.0.72:8080" -> "http://10.0.0.72:8080"; trailing slashes and a trailing /v1 are dropped (the clients add their paths)."""
    url = url.strip().rstrip("/")
    if url and "://" not in url:
        url = "http://" + url
    if url.endswith("/v1"):
        url = url[: -len("/v1")]
    return url


def _providers_file():
    import os
    from pathlib import Path

    return Path(os.environ.get("HUNTUN_HOME") or Path.home() / ".huntun") / "providers.json"


def _read_providers() -> dict:
    """The saved file, re-read whenever it changes; {} when there is none."""
    import json

    global _saved_cache
    f = _providers_file()
    try:
        mtime = f.stat().st_mtime
    except OSError:
        return {}
    if (str(f), mtime) != _saved_cache[0]:
        try:
            data = json.loads(f.read_text())
        except (OSError, ValueError):
            data = {}
        _saved_cache = ((str(f), mtime), data if isinstance(data, dict) else {})
    return _saved_cache[1]


def saved_servers() -> list[dict[str, Any]]:
    """The servers saved from the Model providers menu, in the order they were added, keys included (never sent to the page)."""
    data = _read_providers()
    raw: list[Any] = []
    legacy = data.get("llamacpp")                                                   # the single llama.cpp server earlier versions saved
    if isinstance(legacy, dict) and legacy.get("url"):
        raw.append({"id": "llamacpp", "type": "llamacpp", **legacy})
    raw += data.get("servers") or []
    out: list[dict[str, Any]] = []
    for s in raw:
        if not (isinstance(s, dict) and s.get("type") in SERVER_TYPES and s.get("url") and s.get("id")):
            continue
        try:
            context = max(0, int(s.get("context") or 0))
        except (TypeError, ValueError):
            context = 0
        out.append({"id": str(s["id"]), "type": s["type"], "name": str(s.get("name") or "").strip(), "url": normalize_server_url(str(s["url"])),
                    "key": str(s.get("key") or ""), "note": str(s.get("note") or "").strip(), "context": context, "pinned": "", "source": "saved"})
    return out


def env_servers() -> list[dict[str, Any]]:
    """The servers the environment names, plus Ollama's and vLLM's default local addresses (listed only once they answer)."""
    import os

    out: list[dict[str, Any]] = []
    llama, pinned = normalize_server_url(os.environ.get("HUNTUN_LLAMACPP_URL") or ""), os.environ.get("HUNTUN_LLAMACPP_MODELS", "")
    if llama or pinned.strip():
        out.append({"id": "env-llamacpp", "type": "llamacpp", "name": "", "url": llama or _DEFAULT_URL["llamacpp"], "key": os.environ.get("HUNTUN_LLAMACPP_KEY") or "",
                    "note": os.environ.get("HUNTUN_LLAMACPP_NOTE", "").strip(), "context": 0, "pinned": pinned, "source": "environment"})
    out.append({"id": "env-ollama", "type": "ollama", "name": "", "url": normalize_server_url(ollama_host()), "key": "", "note": "", "context": 0,
                "pinned": os.environ.get("HUNTUN_OLLAMA_MODELS", ""), "source": "environment" if os.environ.get("OLLAMA_HOST") else "default"})
    out.append({"id": "env-vllm", "type": "vllm", "name": "", "url": normalize_server_url(vllm_base_url()), "key": vllm_api_key(), "note": "", "context": 0,
                "pinned": os.environ.get("HUNTUN_VLLM_MODELS", ""), "source": "environment" if os.environ.get("VLLM_BASE_URL") else "default"})
    return out


def servers() -> list[dict[str, Any]]:
    """Every local server: the saved ones first, then the environment's (unless a saved entry already names that address)."""
    saved = saved_servers()
    taken = {(s["type"], s["url"]) for s in saved}
    return saved + [e for e in env_servers() if (e["type"], e["url"]) not in taken]


def server_label(entry: dict[str, Any]) -> str:
    """What the page and the master call a server: its name, else its address without the scheme."""
    return entry.get("name") or entry.get("url", "").split("//", 1)[-1]


def _slugs(entries: list[dict[str, Any]]) -> dict[str, str]:
    """Server id -> the short, unique name that "model@server" ids use."""
    import re

    out: dict[str, str] = {}
    for e in entries:
        base = re.sub(r"[^a-z0-9]+", "-", server_label(e).lower()).strip("-") or e["id"]
        slug, n = base, 2
        while slug in out.values():
            slug, n = f"{base}-{n}", n + 1
        out[e["id"]] = slug
    return out


def _write_servers(entries: list[dict[str, Any]]) -> None:
    import json
    import os
    import tempfile

    global _saved_cache
    f = _providers_file()
    f.parent.mkdir(parents=True, exist_ok=True)
    data = {k: v for k, v in _read_providers().items() if k != "llamacpp"}
    data["servers"] = [{k: e[k] for k in ("id", "type", "name", "url", "key", "note", "context")} for e in entries]
    fd, tmp = tempfile.mkstemp(dir=f.parent, prefix=".providers-", suffix=".tmp")      # created owner-only
    with os.fdopen(fd, "w") as fh:
        fh.write(json.dumps(data, indent=2))
    os.replace(tmp, f)
    _saved_cache = (None, {})
    refresh_local(force=True)


def save_server(values: dict[str, Any]) -> dict[str, Any]:
    """Adds a server (no "id") or changes a saved one. A blank "key" keeps the saved key; "clear_key" removes it."""
    import secrets

    entries = saved_servers()
    sid = str(values.get("id") or "")
    current = next((e for e in entries if e["id"] == sid), None)
    if sid and current is None:
        raise ValueError("unknown server (servers set in the environment cannot be edited here)")
    kind = str(values.get("type") or (current or {}).get("type") or "")
    if kind not in SERVER_TYPES:
        raise ValueError("type must be one of: " + ", ".join(SERVER_TYPES))
    url = normalize_server_url(str(values.get("url") or ""))
    if not url:
        raise ValueError("the server address is required")
    if any(e["type"] == kind and e["url"] == url and e is not current for e in entries):
        raise ValueError(f"{url} is already in the list")
    try:
        context = max(0, int(values.get("context") or 0))
    except (TypeError, ValueError):
        raise ValueError("context must be a number of tokens") from None
    key = "" if values.get("clear_key") else (str(values.get("key") or "").strip() or (current or {}).get("key", ""))
    entry = {"id": sid or secrets.token_hex(4), "type": kind, "name": str(values.get("name") or "").strip()[:60], "url": url, "key": key,
             "note": str(values.get("note") or "").strip()[:500], "context": context}
    if current is None:
        entries.append(entry)
    else:
        entries[entries.index(current)] = entry
    _write_servers(entries)
    return entry


def delete_server(sid: str) -> bool:
    entries = saved_servers()
    keep = [e for e in entries if e["id"] != sid]
    if len(keep) == len(entries):
        return False
    _write_servers(keep)
    return True


def _get_json(url: str, key: str, timeout: float) -> Any:
    import json
    import urllib.request

    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {key}"} if key else {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def probe_server(kind: str, url: str, key: str = "", timeout: float = 1.5, context: int = 0) -> dict[str, Any]:
    """Asks a server what it serves: {"ok", "models", "contexts" (per model), "context", "slots", "error"}.

    llama.cpp: the alias from GET /v1/models, then from GET /props the context of one slot (the whole pool when the server
    shares its KV cache) and how many sessions it runs at once. Ollama: GET /api/tags; it does not say what context it
    serves, so that is the server's own setting here, else HUNTUN_OLLAMA_CONTEXT (32768). vLLM: GET /v1/models, where each
    model carries its max_model_len.
    """
    import os
    import urllib.error

    url = normalize_server_url(url)
    empty: dict[str, Any] = {"ok": False, "models": [], "contexts": {}, "context": 0, "slots": 0}
    if kind not in SERVER_TYPES:
        return {**empty, "error": f"unknown server type {kind!r}"}
    if not url:
        return {**empty, "error": "no server address set"}
    default_ctx = context or int(os.environ.get(_CONTEXT_ENV[kind]) or 32768)
    try:
        data = _get_json(url + ("/api/tags" if kind == "ollama" else "/v1/models"), key, timeout)
    except urllib.error.HTTPError as e:
        return {**empty, "error": "the server rejected the API key" if e.code in (401, 403) else f"the server answered HTTP {e.code}"}
    except Exception as e:
        return {**empty, "error": f"cannot reach {url}: {getattr(e, 'reason', None) or e}"}
    contexts: dict[str, int] = {}
    reported = slots = 0
    try:
        if kind == "ollama":
            for m in data.get("models") or []:
                if m.get("name") or m.get("model"):
                    contexts[str(m.get("name") or m.get("model"))] = default_ctx
            reported = context
        else:
            for m in data.get("data") or []:
                if isinstance(m, dict) and isinstance(m.get("id"), str) and m["id"]:
                    contexts[m["id"]] = int(m.get("max_model_len") or 0) or default_ctx
                    reported = reported or int(m.get("max_model_len") or 0)
    except (AttributeError, TypeError, ValueError):
        return {**empty, "error": "the answer is not a model list; is this the right type of server?"}
    if kind == "llamacpp":
        try:
            props = _get_json(url + "/props", key, timeout)
            reported = int((props.get("default_generation_settings") or {}).get("n_ctx") or 0)
            slots = int(props.get("total_slots") or 0)
        except Exception:
            pass
        if reported:
            contexts = {n: reported for n in contexts}
    if not contexts:
        return {**empty, "context": reported, "slots": slots, "error": "the server lists no model"}
    return {"ok": True, "models": list(contexts), "contexts": contexts, "context": reported, "slots": slots, "error": ""}


def probe_llamacpp(url: str, key: str = "", timeout: float = 1.5) -> dict[str, Any]:
    return probe_server("llamacpp", url, key, timeout)


def _pinned(spec: str, default_ctx: int) -> dict[str, int]:
    """HUNTUN_*_MODELS="name[@context],...": models named up front instead of asking the server."""
    out: dict[str, int] = {}
    for item in spec.split(","):
        if item.strip():
            n, _, c = item.strip().partition("@")
            out[n.strip()] = int(c) if c.strip().isdigit() else default_ctx
    return out


def _use_for(entry: dict[str, Any], status: dict[str, Any]) -> str:
    kind = {"llamacpp": "llama.cpp", "ollama": "Ollama", "vllm": "vLLM"}[entry["type"]]
    where = entry["url"].split("//", 1)[-1] + (f" ({entry['name']})" if entry.get("name") else "")
    text = (f"local model served by {kind} at {where} (no per-token cost; speed and quality depend on that machine"
            + (" and the model's tool calling)" if entry["type"] == "vllm" else ")"))
    slots = status.get("slots") or 0
    if slots:
        text += f"; it runs {slots} session{'s' if slots != 1 else ''} at once, so give it at most {slots} seat{'s' if slots != 1 else ''}"
    if entry.get("note"):
        text += f"; {entry['note']}"
    return text


def refresh_local(force: bool = False) -> dict[str, list[ModelInfo]]:
    """Asks every local server what it serves, all at once, and rebuilds the catalogs and routes; cached for 30 s.

    An empty or failed answer is cached too: a server on another machine that is switched off would otherwise cost a
    connection timeout on every page refresh. The note of a server is added to its models' descriptions for the master.
    """
    import os
    import time
    from collections import Counter
    from concurrent.futures import ThreadPoolExecutor

    global _local_checked
    if not force and time.time() - _local_checked < 30:
        return LOCAL_CATALOGS
    _local_checked = time.time()
    entries = servers()

    def ask(e: dict[str, Any]) -> dict[str, Any]:
        if e.get("pinned", "").strip():
            contexts = _pinned(e["pinned"], e.get("context") or int(os.environ.get(_CONTEXT_ENV[e["type"]]) or 32768))
            return {"ok": bool(contexts), "models": list(contexts), "contexts": contexts, "context": 0, "slots": 0, "error": "", "pinned": True}
        return probe_server(e["type"], e["url"], e["key"], context=e.get("context", 0))

    with ThreadPoolExecutor(max_workers=min(8, len(entries))) as pool:
        results = list(pool.map(ask, entries))
    served = Counter(n for r in results for n in dict.fromkeys(r["models"]))
    slugs = _slugs(entries)
    fresh: dict[str, list[ModelInfo]] = {t: [] for t in SERVER_TYPES}
    routes: dict[str, dict[str, str]] = {}
    for e, r in zip(entries, results):
        for n in r["models"]:
            mid = n if served[n] == 1 else f"{n}@{slugs[e['id']]}"
            if mid in routes:
                continue
            fresh[e["type"]].append(ModelInfo(mid, f"{n} · {server_label(e)}", e["type"], 0.0, 0.0, r["contexts"][n], _use_for(e, r), e["type"]))
            routes[mid] = {"server": e["id"], "type": e["type"], "url": e["url"], "key": e["key"], "model": n}
    for t, cat in fresh.items():
        for m in cat:
            MODEL_BY_ID[m.id] = m
        LOCAL_CATALOGS[t][:] = cat
    _replace(ROUTES, routes)                                                        # in place and never empty in between: calls route from other threads
    _replace(SERVER_STATUS, {e["id"]: r for e, r in zip(entries, results)})
    return LOCAL_CATALOGS


def _replace(target: dict, fresh: dict) -> None:
    target.update(fresh)
    for k in [k for k in list(target) if k not in fresh]:
        target.pop(k, None)


def refresh_ollama(force: bool = False) -> list[ModelInfo]:
    return refresh_local(force)["ollama"]


def refresh_vllm(force: bool = False) -> list[ModelInfo]:
    return refresh_local(force)["vllm"]


def refresh_llamacpp(force: bool = False) -> list[ModelInfo]:
    return refresh_local(force)["llamacpp"]


def local_route(model_id: str | None) -> dict[str, str] | None:
    """Which server serves a local model, and under what name: {"server", "type", "url", "key", "model"}, or None."""
    if not model_id:
        return None
    route = ROUTES.get(model_id)
    if route is None:
        refresh_local()
        route = ROUTES.get(model_id) or next((r for r in list(ROUTES.values()) if r["model"] == model_id), None)   # a plain name another server now shares
    if route is None and "@" in model_id:                                                               # "name@server" while that server is away
        name, _, slug = model_id.rpartition("@")
        entries = servers()
        slugs = _slugs(entries)
        e = next((x for x in entries if slugs[x["id"]] == slug), None)
        if e:
            route = {"server": e["id"], "type": e["type"], "url": e["url"], "key": e["key"], "model": name}
    return route


def default_route(kind: str) -> dict[str, str]:
    """Where calls of this type go when they name no known model: the first server of the type that serves one, else the first listed."""
    entries = [e for e in servers() if e["type"] == kind]
    routes = list(ROUTES.values())
    for e in entries:
        for r in routes:
            if r["server"] == e["id"] and r["url"] == e["url"]:
                return r
    e = entries[0] if entries else {"id": "", "url": _DEFAULT_URL[kind], "key": ""}
    return {"server": e["id"], "type": kind, "url": e["url"], "key": e["key"], "model": ""}


def model_ids() -> list[str]:
    """Every model id an agent can be put on: the fixed catalogs plus what the local servers serve now."""
    local = [m.id for cat in refresh_local().values() for m in cat]
    return MODEL_IDS + [i for i in dict.fromkeys(local) if i not in MODEL_IDS]


def catalog_for(backend: str) -> list[ModelInfo]:
    if backend == "codex":
        return CODEX_CATALOG
    if backend == "kimi":
        return KIMI_CATALOG
    if backend == "deepseek":
        return DEEPSEEK_CATALOG
    if backend == "ollama":
        return refresh_ollama()
    if backend == "vllm":
        return refresh_vllm()
    if backend == "llamacpp":
        return refresh_llamacpp()
    return MODEL_CATALOG


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
    if os.environ.get("DEEPSEEK_API_KEY"):
        out["deepseek"] = "DeepSeek API key"
    if refresh_ollama():
        out["ollama"] = "Ollama server with models"
    if refresh_vllm():
        out["vllm"] = "vLLM server with models"
    if refresh_llamacpp():
        out["llamacpp"] = "llama.cpp server with a model"
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
    if m.vendor == "deepseek":
        return "deepseek" if "deepseek" in avail else default
    if m.vendor == "ollama":
        return "ollama" if "ollama" in avail else default
    if m.vendor == "vllm":
        return "vllm" if "vllm" in avail else default
    if m.vendor == "llamacpp":
        return "llamacpp" if "llamacpp" in avail else default
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
    if "deepseek" in avail:
        out += [(m, "deepseek") for m in DEEPSEEK_CATALOG]
    if "ollama" in avail:
        out += [(m, "ollama") for m in refresh_ollama()]
    if "vllm" in avail:
        out += [(m, "vllm") for m in refresh_vllm()]
    if "llamacpp" in avail:
        out += [(m, "llamacpp") for m in refresh_llamacpp()]
    return out
EFFORTS = ["low", "medium", "high", "xhigh", "max"]
DEFAULT_API_MODEL = "claude-opus-5-5"


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
    if not m or m.tier in ("codex", "kimi", "ollama", "vllm", "llamacpp"):
        return 0.0
    inp, out = tokens * 0.85, tokens * 0.15
    return round((inp * 0.5 * m.input_per_m + inp * 0.5 * m.input_per_m * 0.1 + out * m.output_per_m) / 1e6, 2)


BACKEND_LABEL = {"api": "Anthropic API", "claude-code": "Claude Code", "codex": "OpenAI Codex", "kimi": "Kimi Code", "deepseek": "DeepSeek", "ollama": "Ollama (local)",
                 "vllm": "vLLM (local)", "llamacpp": "llama.cpp server (local)"}


def catalog_text(backend: str = "api", available: dict[str, str] | None = None) -> str:
    pairs = catalog_available(available)
    if not pairs:
        pairs = [(m, backend) for m in catalog_for(backend)]
    lines = []
    for m, b in pairs:
        price = (f"${m.input_per_m:g}/M input, ${m.output_per_m:g}/M output" if m.input_per_m
                 else "runs locally, no per-token price" if m.vendor in ("ollama", "vllm", "llamacpp")
                 else "billed through the ChatGPT plan, no per-token price")
        lines.append(f"- {m.id} ({m.label}; vendor {m.vendor}; runs via {BACKEND_LABEL.get(b, b)}; {price}; {m.context // 1000}k context): {m.use_for}")
    return "\n".join(lines)
