"""DeepSeek, Ollama and llama.cpp as Anthropic-compatible providers of the API backend: client wiring, request shaping,
discovery, and the local servers saved from the Model providers menu."""
from __future__ import annotations

import asyncio
import io
import json
import os
import stat
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any
from unittest import mock

import anthropic

from huntun import models
from huntun.backends import make_backend
from huntun.backends.api import ApiBackend, _tool_defs, _without
from huntun.config import default_config


class _Block:
    def __init__(self, **kw: Any) -> None:
        self.__dict__.update(kw)


class _Response:
    def __init__(self, content: list[Any], stop_reason: str = "end_turn") -> None:
        self.content, self.stop_reason = content, stop_reason


class _Stream:
    """Mimics client.messages.stream(**params): an async context manager whose get_final_message() returns a scripted reply."""

    def __init__(self, handler: Any, params: dict[str, Any]) -> None:
        self.handler, self.params = handler, params

    async def __aenter__(self) -> "_Stream":
        return self

    async def __aexit__(self, *_: Any) -> None:
        return None

    async def get_final_message(self) -> Any:
        return self.handler(self.params)


LLAMACPP_ENV = ("HUNTUN_LLAMACPP_URL", "HUNTUN_LLAMACPP_KEY", "HUNTUN_LLAMACPP_MODELS", "HUNTUN_LLAMACPP_NOTE", "HUNTUN_LLAMACPP_CONTEXT")


class _FakeLlamaServer(BaseHTTPRequestHandler):
    """The parts of llama-server Huntun talks to: /v1/models and /props for discovery, /v1/messages for calls."""
    key = "secret"
    alias = "qwen3.8-flash-next-uncensored"
    seen_keys: list[str] = []

    def log_message(self, *a: Any) -> None:
        pass

    def _send(self, code: int, body: dict[str, Any]) -> None:
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _authorized(self) -> bool:
        got = self.headers.get("Authorization") or self.headers.get("X-Api-Key") or ""  # llama-server checks both, in this order
        got = got.removeprefix("Bearer ")
        type(self).seen_keys.append(got)
        if self.key and got != self.key:
            self._send(401, {"error": {"message": "Invalid API Key", "type": "authentication_error", "code": 401}})
            return False
        return True

    def do_GET(self) -> None:  # noqa: N802
        if not self._authorized():
            return
        if self.path == "/v1/models":
            self._send(200, {"object": "list", "data": [{"id": self.alias, "object": "model"}]})
        elif self.path == "/props":
            self._send(200, {"default_generation_settings": {"n_ctx": 131072}, "total_slots": 2})
        else:
            self._send(404, {})

    def do_POST(self) -> None:  # noqa: N802
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if not self._authorized():
            return
        message = {"id": "msg_1", "type": "message", "role": "assistant", "model": body["model"], "content": [{"type": "text", "text": "pong"}],
                   "stop_reason": "end_turn", "stop_sequence": None, "usage": {"input_tokens": 3, "output_tokens": 1}}
        if not body.get("stream"):
            self._send(200, message)
            return
        events = [("message_start", {"message": {**message, "content": [], "stop_reason": None}}),
                  ("content_block_start", {"index": 0, "content_block": {"type": "text", "text": ""}}),
                  ("content_block_delta", {"index": 0, "delta": {"type": "text_delta", "text": "pong"}}),
                  ("content_block_stop", {"index": 0}),
                  ("message_delta", {"delta": {"stop_reason": "end_turn", "stop_sequence": None}, "usage": {"output_tokens": 1}}),
                  ("message_stop", {})]
        data = "".join(f"event: {name}\ndata: {json.dumps({'type': name, **ev})}\n\n" for name, ev in events).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class _FakeLlamaRouter(_FakeLlamaServer):
    """llama-server as a router, as recorded from a real one: two models behind one address, one loaded. A GET that could
    load a model (a per-model /props without autoload=false) is recorded, since on one GPU it would evict the loaded one."""
    loads: list[str] = []
    posted_models: list[str] = []
    PRESET_27B = ("[qwen3.8-27b-uncensored]\njinja = true\nctx-size = 131072\ncache-type-k = q4_0\nkv-unified = true\n"
                  "model = C:\\flash-next\\models\\Qwen3.8-27B-Uncensored-Q4_K_M.gguf\nparallel = 2\n\n")
    PRESET_FN = ("[qwen3.8-flash-next-uncensored]\nctx-size = 131072\nkv-unified = true\nparallel = 2\nload-on-startup = true\n\n")

    def do_GET(self) -> None:  # noqa: N802
        if not self._authorized():
            return
        path, _, query = self.path.partition("?")
        params = dict(p.split("=", 1) for p in query.split("&") if "=" in p)
        if path == "/v1/models":
            self._send(200, {"object": "list", "data": [
                {"id": "qwen3.8-27b-uncensored", "object": "model", "status": {"value": "unloaded", "args": [], "preset": self.PRESET_27B}},
                {"id": "qwen3.8-flash-next-uncensored", "object": "model", "status": {"value": "loaded", "args": ["llama-server", "--ctx-size", "131072"],
                                                                                     "preset": self.PRESET_FN}}]})
        elif path == "/props" and not params.get("model"):
            self._send(200, {"role": "router", "max_instances": 1, "models_autoload": True, "default_generation_settings": {"params": None, "n_ctx": 0}})
        elif path == "/props":
            if params.get("autoload") != "false":
                type(self).loads.append(params["model"])
            if params["model"] == "qwen3.8-flash-next-uncensored":
                self._send(200, {"default_generation_settings": {"n_ctx": 262144}, "total_slots": 2})
            else:
                self._send(400, {"error": {"code": 400, "message": "model is not loaded", "type": "invalid_request_error"}})
        else:
            self._send(404, {})

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers["Content-Length"])
        body = self.rfile.read(length)
        type(self).posted_models.append(json.loads(body).get("model", ""))
        self.rfile = io.BytesIO(body)                                                  # let the parent read it again
        super().do_POST()


def isolate_huntun_home(test: unittest.TestCase) -> Path:
    """Points HUNTUN_HOME at a temporary directory so saved provider settings on this machine cannot leak into a test."""
    tmp = tempfile.TemporaryDirectory()
    test.addCleanup(tmp.cleanup)
    old = os.environ.get("HUNTUN_HOME")
    os.environ["HUNTUN_HOME"] = tmp.name
    test.addCleanup(lambda: os.environ.__setitem__("HUNTUN_HOME", old) if old is not None else os.environ.pop("HUNTUN_HOME", None))
    models._saved_cache = (-1.0, {})
    return Path(tmp.name)


class ProviderTests(unittest.TestCase):
    def setUp(self) -> None:
        os.environ["DEEPSEEK_API_KEY"] = "sk-test"
        os.environ.pop("OLLAMA_HOST", None)
        os.environ.pop("HUNTUN_OLLAMA_MODELS", None)
        os.environ.pop("HUNTUN_COMPAT_THINKING", None)
        for k in LLAMACPP_ENV:
            os.environ.pop(k, None)
        isolate_huntun_home(self)
        self.config = default_config("Mock goal")

    def tearDown(self) -> None:
        for k in ("DEEPSEEK_API_KEY", "OLLAMA_HOST", "HUNTUN_OLLAMA_MODELS", *LLAMACPP_ENV):
            os.environ.pop(k, None)
        models.refresh_llamacpp(force=True)

    def test_deepseek_client_points_at_the_anthropic_compatible_endpoint(self) -> None:
        b = make_backend("deepseek", self.config)
        self.assertIsInstance(b, ApiBackend)
        self.assertEqual((b.name, b.provider, b.compat), ("deepseek", "deepseek", True))
        self.assertEqual(str(b.client.base_url).rstrip("/"), "https://api.deepseek.com/anthropic")
        self.assertEqual(b.client.api_key, "sk-test")
        self.assertEqual(b._default_model(), "deepseek-v4-pro")

    def test_deepseek_needs_a_key(self) -> None:
        os.environ.pop("DEEPSEEK_API_KEY")
        with self.assertRaises(RuntimeError):
            make_backend("deepseek", self.config)

    def test_ollama_client_uses_the_host_and_a_placeholder_key(self) -> None:
        os.environ["OLLAMA_HOST"] = "http://gpu-box:11434/"
        os.environ["HUNTUN_OLLAMA_MODELS"] = "qwen3:27b@65536,llama4"
        b = make_backend("ollama", self.config)
        self.assertEqual(str(b.client.base_url).rstrip("/"), "http://gpu-box:11434")
        self.assertEqual(b.client.api_key, "ollama")
        cat = models.refresh_ollama(force=True)
        self.assertEqual([(m.id, m.context) for m in cat], [("qwen3:27b", 65536), ("llama4", 32768)])
        self.assertEqual(b._default_model(), "qwen3:27b")
        self.assertEqual(models.backend_for_model("qwen3:27b", {"ollama": "x"}), "ollama")
        self.assertEqual(models.estimate_cost_usd("qwen3:27b", 1_000_000), 0.0)

    def test_ollama_discovery_reads_the_servers_tag_list(self) -> None:
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                body = json.dumps({"models": [{"name": "gemma4:26b"}, {"model": "qwen3:8b"}]}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a: Any) -> None:
                pass

        srv = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            os.environ["OLLAMA_HOST"] = f"http://127.0.0.1:{srv.server_port}"
            cat = models.refresh_ollama(force=True)
            self.assertEqual([m.id for m in cat], ["gemma4:26b", "qwen3:8b"])
            self.assertIn("ollama", models.available_backends())
        finally:
            srv.shutdown()
        os.environ["OLLAMA_HOST"] = "http://127.0.0.1:1"                                  # nothing listening: no models, no backend
        self.assertEqual(models.refresh_ollama(force=True), [])
        self.assertNotIn("ollama", models.available_backends())

    def _llama_server(self, key: str = "secret") -> HTTPServer:
        _FakeLlamaServer.key, _FakeLlamaServer.seen_keys = key, []
        srv = HTTPServer(("127.0.0.1", 0), _FakeLlamaServer)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.shutdown)
        os.environ["HUNTUN_LLAMACPP_URL"] = f"http://127.0.0.1:{srv.server_port}/"
        return srv

    def test_llamacpp_discovers_the_alias_slot_context_and_session_count(self) -> None:
        self._llama_server()
        os.environ["HUNTUN_LLAMACPP_KEY"] = "secret"
        os.environ["HUNTUN_LLAMACPP_NOTE"] = "Qwen3.8-Flash-Next, ~17 tok/s"
        cat = models.refresh_llamacpp(force=True)
        self.assertEqual([(m.id, m.context, m.vendor) for m in cat], [("qwen3.8-flash-next-uncensored", 131072, "llamacpp")])
        self.assertIn("give it at most 2 seats", cat[0].use_for)
        self.assertTrue(cat[0].use_for.endswith("Qwen3.8-Flash-Next, ~17 tok/s"))
        self.assertEqual(set(_FakeLlamaServer.seen_keys), {"secret"})
        avail = models.available_backends()
        self.assertIn("llamacpp", avail)
        self.assertEqual(models.backend_for_model("qwen3.8-flash-next-uncensored", avail), "llamacpp")
        self.assertIn(("qwen3.8-flash-next-uncensored", "llamacpp"), [(m.id, b) for m, b in models.catalog_available(avail)])
        self.assertEqual(models.estimate_cost_usd("qwen3.8-flash-next-uncensored", 1_000_000), 0.0)
        self.assertEqual(models.context_limit("qwen3.8-flash-next-uncensored"), 131072)
        self.assertIn("runs via llama.cpp server (local); runs locally, no per-token price; 131k context", models.catalog_text(available=avail))

    def test_llamacpp_router_offers_each_model_with_its_own_capacity_without_loading_any(self) -> None:
        _FakeLlamaRouter.key, _FakeLlamaRouter.seen_keys, _FakeLlamaRouter.loads, _FakeLlamaRouter.posted_models = "secret", [], [], []
        srv = HTTPServer(("127.0.0.1", 0), _FakeLlamaRouter)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.shutdown)
        os.environ["HUNTUN_LLAMACPP_URL"] = f"http://127.0.0.1:{srv.server_port}"
        os.environ["HUNTUN_LLAMACPP_KEY"] = "secret"
        cat = {m.id: m for m in models.refresh_llamacpp(force=True)}
        self.assertEqual({i: m.context for i, m in cat.items()},
                         {"qwen3.8-flash-next-uncensored": 131072,                               # loaded: its /props gives each session the whole shared pool
                          "qwen3.8-27b-uncensored": 65536})                                      # not loaded: from its preset; a shared pool is split
        self.assertEqual(_FakeLlamaRouter.loads, [], "discovery must never make the router load a model")
        for mid, other in (("qwen3.8-27b-uncensored", "qwen3.8-flash-next-uncensored"), ("qwen3.8-flash-next-uncensored", "qwen3.8-27b-uncensored")):
            self.assertIn("give it at most 2 seats", cat[mid].use_for)
            self.assertIn(f"also serves {other} but holds only one model at a time", cat[mid].use_for)
        st = models.probe_server("llamacpp", os.environ["HUNTUN_LLAMACPP_URL"], "secret")
        self.assertEqual((st["max_loaded"], st["details"]["qwen3.8-27b-uncensored"], st["details"]["qwen3.8-flash-next-uncensored"]["state"]),
                         (1, {"context": 65536, "slots": 2, "state": "unloaded", "pool": 131072}, "loaded"))
        b = make_backend("llamacpp", self.config)                                        # each call names its model, which is what the router routes on
        asyncio.run(b.client.messages.create(model=models.local_route("qwen3.8-27b-uncensored")["model"], max_tokens=5,
                                             messages=[{"role": "user", "content": "ping"}]))
        self.assertEqual(_FakeLlamaRouter.posted_models, ["qwen3.8-27b-uncensored"])

    def test_router_preset_capacity(self) -> None:
        cap = models._preset_capacity
        self.assertEqual(cap("[m]\nctx-size = 131072\nparallel = 2\n", None), (65536, 2))           # separate slots split the pool
        self.assertEqual(cap("[m]\nctx-size = 131072\nparallel = 2\nkv-unified = true\n", None), (65536, 2))   # a shared pool: each counts on its share
        self.assertEqual(cap("[m]\nc = 8192\n", None), (8192, 0))                                   # slots "auto": one shared pool
        self.assertEqual(cap("", ["llama-server", "--ctx-size", "32768", "--parallel", "1", "--jinja"]), (32768, 1))
        self.assertEqual(cap("[m]\njinja = true\n", ["llama-server", "--reasoning-budget", "-1"]), (0, 0))

    def test_llamacpp_with_a_wrong_key_or_no_server_offers_nothing(self) -> None:
        self._llama_server()
        os.environ["HUNTUN_LLAMACPP_KEY"] = "wrong"
        self.assertEqual(models.refresh_llamacpp(force=True), [])
        self.assertNotIn("llamacpp", models.available_backends())
        os.environ["HUNTUN_LLAMACPP_URL"] = "http://127.0.0.1:1"                         # nothing listening
        self.assertEqual(models.refresh_llamacpp(force=True), [])
        os.environ.pop("HUNTUN_LLAMACPP_URL")                                            # not configured: no request at all
        self.assertEqual(models.refresh_llamacpp(force=True), [])

    def test_llamacpp_caches_an_unreachable_server(self) -> None:
        os.environ["HUNTUN_LLAMACPP_URL"] = "http://127.0.0.1:1"
        self.assertEqual(models.refresh_llamacpp(force=True), [])
        with mock.patch("urllib.request.urlopen", side_effect=AssertionError("asked again within 30 s")):
            self.assertEqual(models.refresh_llamacpp(), [])
            self.assertNotIn("llamacpp", models.available_backends())

    def test_llamacpp_models_can_be_pinned_without_discovery(self) -> None:
        os.environ["HUNTUN_LLAMACPP_URL"] = "http://gpu-box:8080"
        os.environ["HUNTUN_LLAMACPP_MODELS"] = "flash-next@262144"
        self.assertEqual([(m.id, m.context) for m in models.refresh_llamacpp(force=True)], [("flash-next", 262144)])

    def test_llamacpp_backend_calls_the_server_with_its_key(self) -> None:
        self._llama_server()
        os.environ["HUNTUN_LLAMACPP_KEY"] = "secret"
        models.refresh_llamacpp(force=True)
        b = make_backend("llamacpp", self.config)
        self.assertEqual((b.name, b.provider, b.compat), ("llamacpp", "llamacpp", True))
        self.assertEqual(b._default_model(), "qwen3.8-flash-next-uncensored")
        reply = asyncio.run(b.client.messages.create(model=b._default_model(), max_tokens=5, messages=[{"role": "user", "content": "ping"}]))
        self.assertEqual(reply.content[0].text, "pong")
        self.assertEqual(_FakeLlamaServer.seen_keys[-1], "secret")                        # sent as X-Api-Key, which llama-server accepts

    def test_llamacpp_without_a_key_uses_a_placeholder(self) -> None:
        self._llama_server(key="")
        b = make_backend("llamacpp", self.config)
        self.assertEqual(b.client.api_key, "none")
        self.assertEqual([m.id for m in models.refresh_llamacpp(force=True)], ["qwen3.8-flash-next-uncensored"])

    def test_compat_requests_drop_anthropic_only_fields_and_survive_rejections(self) -> None:
        b = make_backend("deepseek", self.config)
        seen: list[dict[str, Any]] = []

        def handler(params: dict[str, Any]) -> Any:
            seen.append(params)
            if "cache_control" in json.dumps(params) and len(seen) == 1:
                raise anthropic.BadRequestError("cache_control is not supported", response=_FakeResp(), body=None)
            return _Response([_Block(type="text", text="hello")])

        b.client.messages.stream = lambda **params: _Stream(handler, params)        # type: ignore[method-assign]
        params = {"model": "deepseek-v4-pro", "max_tokens": 10, "system": [{"type": "text", "text": "S", "cache_control": {"type": "ephemeral"}}],
                  "tools": [{"name": "t", "description": "d", "input_schema": {"type": "object"}, "eager_input_streaming": True}],
                  "messages": [{"role": "user", "content": "hi"}], "output_config": {"effort": "high"}, "thinking": {"type": "adaptive"}}
        msg = asyncio.run(b._call(params, use_fallbacks=True))
        self.assertEqual(msg.content[0].text, "hello")
        first, second = seen
        for f in ("output_config", "thinking", "fallbacks", "betas"):
            self.assertNotIn(f, first)
        self.assertIn("cache_control", first["system"][0])
        self.assertNotIn("cache_control", second["system"][0])                          # dropped after the 400 and remembered
        self.assertIn("cache_control", b.dropped)

    def test_structured_falls_back_to_json_in_text(self) -> None:
        b = make_backend("deepseek", self.config)
        b.client.messages.stream = lambda **params: _Stream(lambda p: _Response([_Block(type="text", text='Sure: {"agents": [], "rationale": "x"}')]), params)  # type: ignore[method-assign]
        data = asyncio.run(b.structured(prompt="plan", tool_name="propose_team", description="d", schema={"type": "object"}, model="", effort="high"))
        self.assertEqual(data["rationale"], "x")

    def test_tool_defs_and_without(self) -> None:
        defs = [{"name": "a", "eager_input_streaming": True}]
        p = _without({"tools": defs, "system": [{"type": "text", "text": "s", "cache_control": {"type": "ephemeral"}}], "thinking": {}}, "eager_input_streaming")
        self.assertNotIn("eager_input_streaming", p["tools"][0])
        p = _without(p, "cache_control")
        self.assertNotIn("cache_control", p["system"][0])
        self.assertTrue(callable(_tool_defs))


def _FakeResp() -> Any:  # noqa: N802
    import httpx2 as httpx  # the HTTP client the anthropic 1.x SDK is built on

    return httpx.Response(400, request=httpx.Request("POST", "http://test/v1/messages"), json={"error": {"message": "cache_control is not supported"}})


CLOSED = "127.0.0.1:9"                                                           # nothing listens there: refused at once


class ServerSettingsTests(unittest.TestCase):
    """Servers saved from the Model providers menu: stored privately, any number next to the environment's, used without a restart."""

    def setUp(self) -> None:
        env = (*LLAMACPP_ENV, "OLLAMA_HOST", "HUNTUN_OLLAMA_MODELS", "VLLM_BASE_URL", "VLLM_API_KEY", "HUNTUN_VLLM_MODELS")
        for k in env:
            os.environ.pop(k, None)
        self.addCleanup(lambda: [os.environ.pop(k, None) for k in env])
        self.addCleanup(lambda: models.refresh_local(force=True))
        self.home = isolate_huntun_home(self)
        # Real local model servers must not join the fake server catalog.
        for fn, replacement in (
            ("ollama_host", lambda: os.environ.get("OLLAMA_HOST") or f"http://{CLOSED}"),
            ("vllm_base_url", lambda: (os.environ.get("VLLM_BASE_URL") or f"http://{CLOSED}").rstrip("/").removesuffix("/v1") + "/v1"),
        ):
            patcher = mock.patch.object(models, fn, replacement)
            patcher.start()
            self.addCleanup(patcher.stop)
        models.refresh_local(force=True)

    def _llama_server(self, key: str = "secret", alias: str = "qwen3.8-flash-next-uncensored") -> tuple[str, type]:
        handler = type("Llama", (_FakeLlamaServer,), {"key": key, "alias": alias, "seen_keys": []})
        srv = HTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.shutdown)
        return f"127.0.0.1:{srv.server_port}", handler                                   # typed without http://, as people do

    def test_saved_servers_join_the_environment_ones_and_stay_private(self) -> None:
        os.environ["HUNTUN_LLAMACPP_URL"] = "http://127.0.0.1:10"
        models.save_server({"type": "llamacpp", "name": "4090 box", "url": CLOSED + "/v1/", "key": "k", "note": "fast"})
        models.save_server({"type": "ollama", "name": "M3 Max", "url": "http://127.0.0.1:11"})
        listed = [(e["type"], e["url"], e["source"]) for e in models.servers() if e["source"] != "default"]
        self.assertEqual(listed, [("llamacpp", "http://" + CLOSED, "saved"), ("ollama", "http://127.0.0.1:11", "saved"), ("llamacpp", "http://127.0.0.1:10", "environment")])
        mode = stat.S_IMODE((self.home / "providers.json").stat().st_mode)
        self.assertEqual(mode & 0o077, 0, oct(mode))                                   # holds API keys: owner only
        with self.assertRaises(ValueError):
            models.save_server({"type": "llamacpp", "url": "http://" + CLOSED})         # already in the list
        with self.assertRaises(ValueError):
            models.save_server({"type": "gpt", "url": CLOSED})
        with self.assertRaises(ValueError):
            models.save_server({"id": "env-llamacpp", "type": "llamacpp", "url": CLOSED})   # the environment's is not editable here

        box = models.saved_servers()[0]
        models.save_server({"id": box["id"], "name": "4090 box", "url": CLOSED, "key": "", "note": "faster"})   # a blank key keeps the saved one
        self.assertEqual({k: models.saved_servers()[0][k] for k in ("type", "key", "note")}, {"type": "llamacpp", "key": "k", "note": "faster"})
        models.save_server({"id": box["id"], "name": "4090 box", "url": CLOSED, "clear_key": True})
        self.assertEqual(models.saved_servers()[0]["key"], "")
        models.save_server({"type": "llamacpp", "url": "127.0.0.1:10"})                  # the environment's address, saved: listed once
        self.assertEqual([e["source"] for e in models.servers() if e["url"] == "http://127.0.0.1:10"], ["saved"])
        self.assertTrue(models.delete_server(box["id"]))
        self.assertFalse(models.delete_server(box["id"]))
        self.assertEqual([e["name"] for e in models.saved_servers()], ["M3 Max", ""])

    def test_the_single_server_earlier_versions_saved_is_kept(self) -> None:
        (self.home / "providers.json").write_text(json.dumps({"llamacpp": {"url": CLOSED, "key": "old", "note": "n"}}))
        self.assertEqual([(e["id"], e["url"], e["key"], e["note"]) for e in models.saved_servers()], [("llamacpp", "http://" + CLOSED, "old", "n")])
        models.save_server({"type": "ollama", "url": "127.0.0.1:11"})
        data = json.loads((self.home / "providers.json").read_text())
        self.assertNotIn("llamacpp", data)
        self.assertEqual([(s["type"], s["key"]) for s in data["servers"]], [("llamacpp", "old"), ("ollama", "")])

    def test_addresses_are_normalized(self) -> None:
        self.assertEqual(models.normalize_server_url(" 10.0.0.72:8080/v1/ "), "http://10.0.0.72:8080")
        self.assertEqual(models.normalize_server_url("https://gpu.lan"), "https://gpu.lan")
        self.assertEqual(models.normalize_server_url(""), "")

    def test_saving_makes_the_model_available_at_once(self) -> None:
        addr, _ = self._llama_server()
        self.assertNotIn("llamacpp", models.available_backends())
        models.save_server({"type": "llamacpp", "url": addr, "key": "secret", "note": "Flash-Next"})
        avail = models.available_backends()
        self.assertIn("llamacpp", avail)
        m = models.model_info("qwen3.8-flash-next-uncensored")
        self.assertIsNotNone(m)
        self.assertTrue(m.use_for.endswith("Flash-Next"))
        self.assertEqual(m.context, 131072)

    def test_servers_sharing_a_model_get_their_own_ids_and_calls(self) -> None:
        a, ha = self._llama_server(key="ka", alias="qwen")
        b, hb = self._llama_server(key="kb", alias="qwen")
        c, hc = self._llama_server(key="", alias="gemma")
        models.save_server({"type": "llamacpp", "name": "4090 box", "url": a, "key": "ka"})
        models.save_server({"type": "llamacpp", "name": "M3 Max", "url": b, "key": "kb"})
        models.save_server({"type": "llamacpp", "url": c})
        cat = models.refresh_llamacpp()
        self.assertEqual([(m.id, m.label) for m in cat], [("qwen@4090-box", "qwen · 4090 box"), ("qwen@m3-max", "qwen · M3 Max"), ("gemma", f"gemma · {c}")])
        self.assertIn("qwen@m3-max", models.model_ids())
        backend = make_backend("llamacpp", default_config("Mock goal"))
        ask = lambda model: asyncio.run(backend._send({"model": model, "max_tokens": 5, "messages": [{"role": "user", "content": "ping"}]}))   # noqa: E731
        before = len(ha.seen_keys)
        self.assertEqual(ask("qwen@m3-max").model, "qwen")                              # the server gets the name it knows
        self.assertEqual((hb.seen_keys[-1], len(ha.seen_keys)), ("kb", before))         # and the other one nothing
        ask("qwen@4090-box")
        self.assertEqual(ha.seen_keys[-1], "ka")
        ask("gemma")
        self.assertEqual(hc.seen_keys[-1], "none")                                      # no key set: the placeholder llama-server ignores
        self.assertEqual(models.local_route("qwen")["server"], models.saved_servers()[0]["id"])   # a plain name now shared: the first server

    def test_a_running_backend_follows_a_changed_server(self) -> None:
        box = models.save_server({"type": "llamacpp", "url": "http://127.0.0.1:12", "key": "one"})
        b = make_backend("llamacpp", default_config("Mock goal"))
        self.assertEqual((str(b.client.base_url).rstrip("/"), b.client.api_key), ("http://127.0.0.1:12", "one"))
        models.save_server({"id": box["id"], "url": "127.0.0.1:13", "key": "two"})
        b._sync_client()
        self.assertEqual((str(b.client.base_url).rstrip("/"), b.client.api_key), ("http://127.0.0.1:13", "two"))

    def test_probe_explains_what_is_wrong(self) -> None:
        addr, _ = self._llama_server()
        self.assertIn("rejected the API key", models.probe_llamacpp(addr, "wrong")["error"])
        self.assertIn("cannot reach", models.probe_llamacpp(CLOSED, "")["error"])
        ok = models.probe_llamacpp(addr, "secret")
        self.assertEqual((ok["ok"], ok["models"], ok["context"], ok["slots"]), (True, ["qwen3.8-flash-next-uncensored"], 131072, 2))
        self.assertIn("HTTP 404", models.probe_server("ollama", addr, "secret")["error"])   # the wrong type of server
        self.assertIn("unknown server type", models.probe_server("gpt", addr)["error"])

    def test_model_providers_endpoints(self) -> None:
        from huntun.hub import Hub
        from huntun.server import start_server

        addr, _ = self._llama_server()
        server = start_server(Hub(self.home), 0)
        self.addCleanup(server.shutdown)
        base = f"http://127.0.0.1:{server.server_address[1]}"

        def call(path: str, body: dict | None = None, headers: dict | None = None) -> tuple[int, dict, str]:
            req = urllib.request.Request(base + path, data=json.dumps(body).encode() if body is not None else None, method="POST" if body is not None else "GET",
                                         headers={"content-type": "application/json", **(headers or {})})
            try:
                with urllib.request.urlopen(req) as r:
                    raw = r.read().decode()
                    return r.status, json.loads(raw), raw
            except urllib.error.HTTPError as e:
                raw = e.read().decode()
                return e.code, json.loads(raw), raw

        code, res, _ = call("/api/providers")
        self.assertEqual((res["servers"], sorted(res["types"])), ([], ["llamacpp", "ollama", "vllm"]))
        code, res, _ = call("/api/providers/servers/test", {"type": "llamacpp", "url": addr, "key": "wrong"})
        self.assertIn("rejected the API key", res["error"])
        code, res, _ = call("/api/providers/servers/test", {"type": "llamacpp", "url": addr, "key": "secret"})
        self.assertTrue(res["ok"])
        self.assertEqual(models.saved_servers(), [])                                    # testing does not save

        code, res, raw = call("/api/providers/servers", {"type": "llamacpp", "name": "4090 box", "url": addr, "key": "secret", "note": "fast"})
        self.assertEqual(code, 200)
        entry = res["servers"][0]
        self.assertEqual((entry["label"], entry["url"], entry["has_key"], entry["status"]["ok"], entry["models"]),
                         ("4090 box", f"http://{addr}", True, True, ["qwen3.8-flash-next-uncensored"]))
        self.assertIn("llamacpp", res["backends"])
        self.assertNotIn("secret", raw)                                                # the key never goes back to the page
        self.assertNotIn("secret", call("/api/providers")[2])

        form = {"id": entry["id"], "type": "llamacpp", "name": "4090 box", "url": addr, "note": "faster"}
        self.assertTrue(call("/api/providers/servers/test", {**form, "key": ""})[1]["ok"])    # a blank key field tests with the saved key
        call("/api/providers/servers", {**form, "key": ""})                                   # and saving keeps it
        self.assertEqual((models.saved_servers()[0]["key"], models.saved_servers()[0]["note"]), ("secret", "faster"))
        call("/api/providers/servers", {**form, "clear_key": True})
        self.assertEqual(models.saved_servers()[0]["key"], "")

        self.assertEqual(call("/api/providers/servers", {"type": "llamacpp", "url": "http://evil:1"}, {"content-type": "text/plain"})[0], 415)
        self.assertEqual(call("/api/providers/servers", {"type": "llamacpp", "url": "http://evil:1"}, {"origin": "https://evil.example"})[0], 403)
        self.assertEqual(call("/api/providers/servers", {"type": "bogus", "url": CLOSED})[0], 400)
        self.assertEqual(len(models.saved_servers()), 1)

        self.assertEqual(call("/api/providers/servers/delete", {"id": entry["id"]})[1]["servers"], [])
        self.assertEqual(call("/api/providers/servers/delete", {"id": entry["id"]})[0], 404)


if __name__ == "__main__":
    unittest.main()
