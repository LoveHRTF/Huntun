"""DeepSeek, Ollama and llama.cpp as Anthropic-compatible providers of the API backend: client wiring, request shaping,
discovery, and the llama.cpp settings saved from the Projects page."""
from __future__ import annotations

import asyncio
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
            self._send(200, {"object": "list", "data": [{"id": "qwen3.8-flash-next-uncensored", "object": "model"}]})
        elif self.path == "/props":
            self._send(200, {"default_generation_settings": {"n_ctx": 131072}, "total_slots": 2})
        else:
            self._send(404, {})

    def do_POST(self) -> None:  # noqa: N802
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if not self._authorized():
            return
        self._send(200, {"id": "msg_1", "type": "message", "role": "assistant", "model": body["model"],
                         "content": [{"type": "text", "text": "pong"}], "stop_reason": "end_turn", "stop_sequence": None,
                         "usage": {"input_tokens": 3, "output_tokens": 1}})


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


class LlamacppSettingsTests(unittest.TestCase):
    """Settings saved from the Projects page: stored privately, preferred over the environment, applied without a restart."""

    def setUp(self) -> None:
        for k in LLAMACPP_ENV:
            os.environ.pop(k, None)
        self.addCleanup(lambda: [os.environ.pop(k, None) for k in LLAMACPP_ENV])
        self.addCleanup(lambda: models.refresh_llamacpp(force=True))
        self.home = isolate_huntun_home(self)

    def _llama_server(self, key: str = "secret") -> str:
        _FakeLlamaServer.key, _FakeLlamaServer.seen_keys = key, []
        srv = HTTPServer(("127.0.0.1", 0), _FakeLlamaServer)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.shutdown)
        return f"127.0.0.1:{srv.server_port}"                                          # typed without http://, as people do

    def test_saved_settings_win_over_the_environment_and_stay_private(self) -> None:
        os.environ["HUNTUN_LLAMACPP_URL"] = "http://env-box:8080"
        self.assertEqual(models.llamacpp_settings()["source"], "environment")
        models.save_provider("llamacpp", {"url": "http://saved-box:8080", "key": "k", "note": "fast"})
        cfg = models.llamacpp_settings()
        self.assertEqual((cfg["url"], cfg["key"], cfg["note"], cfg["source"]), ("http://saved-box:8080", "k", "fast", "saved"))
        mode = stat.S_IMODE((self.home / "providers.json").stat().st_mode)
        self.assertEqual(mode & 0o077, 0, oct(mode))                                   # holds an API key: owner only
        models.save_provider("llamacpp", None)
        self.assertEqual(models.llamacpp_settings()["url"], "http://env-box:8080")

    def test_addresses_are_normalized(self) -> None:
        self.assertEqual(models.normalize_server_url(" 10.0.0.72:8080/v1/ "), "http://10.0.0.72:8080")
        self.assertEqual(models.normalize_server_url("https://gpu.lan"), "https://gpu.lan")
        self.assertEqual(models.normalize_server_url(""), "")

    def test_saving_makes_the_model_available_at_once(self) -> None:
        addr = self._llama_server()
        self.assertNotIn("llamacpp", models.available_backends())
        models.save_provider("llamacpp", {"url": addr, "key": "secret", "note": "Flash-Next"})
        avail = models.available_backends()
        self.assertIn("llamacpp", avail)
        m = models.model_info("qwen3.8-flash-next-uncensored")
        self.assertIsNotNone(m)
        self.assertTrue(m.use_for.endswith("Flash-Next"))

    def test_a_running_backend_follows_a_changed_server(self) -> None:
        models.save_provider("llamacpp", {"url": "http://old-box:8080", "key": "one", "note": ""})
        b = make_backend("llamacpp", default_config("Mock goal"))
        self.assertEqual((str(b.client.base_url).rstrip("/"), b.client.api_key), ("http://old-box:8080", "one"))
        models.save_provider("llamacpp", {"url": "10.0.0.72:8080", "key": "two", "note": ""})
        b._sync_client()
        self.assertEqual((str(b.client.base_url).rstrip("/"), b.client.api_key), ("http://10.0.0.72:8080", "two"))

    def test_probe_explains_what_is_wrong(self) -> None:
        addr = self._llama_server()
        self.assertIn("rejected the API key", models.probe_llamacpp(addr, "wrong")["error"])
        self.assertIn("cannot reach", models.probe_llamacpp("127.0.0.1:1", "")["error"])
        ok = models.probe_llamacpp(addr, "secret")
        self.assertEqual((ok["ok"], ok["models"], ok["context"], ok["slots"]), (True, ["qwen3.8-flash-next-uncensored"], 131072, 2))

    def test_projects_page_endpoints(self) -> None:
        from huntun.hub import Hub
        from huntun.server import start_server

        addr = self._llama_server()
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

        self.assertEqual(call("/api/providers")[1]["llamacpp"]["url"], "")
        code, res, _ = call("/api/providers/llamacpp/test", {"url": addr, "key": "wrong"})
        self.assertIn("rejected the API key", res["error"])
        code, res, _ = call("/api/providers/llamacpp/test", {"url": addr, "key": "secret"})
        self.assertTrue(res["ok"])
        self.assertEqual(models.llamacpp_settings()["url"], "")                       # testing does not save

        code, res, raw = call("/api/providers/llamacpp", {"url": addr, "key": "secret", "note": "fast"})
        self.assertEqual(code, 200)
        self.assertEqual((res["llamacpp"]["url"], res["llamacpp"]["has_key"], res["llamacpp"]["status"]["ok"]), (f"http://{addr}", True, True))
        self.assertIn("llamacpp", res["backends"])
        self.assertNotIn("secret", raw)                                                # the key never goes back to the page
        self.assertNotIn("secret", call("/api/providers")[2])

        call("/api/providers/llamacpp", {"url": addr, "key": "", "note": "faster"})     # blank key field keeps the key
        self.assertEqual((models.llamacpp_settings()["key"], models.llamacpp_settings()["note"]), ("secret", "faster"))
        call("/api/providers/llamacpp", {"url": addr, "clear_key": True, "note": ""})
        self.assertEqual(models.llamacpp_settings()["key"], "")

        self.assertEqual(call("/api/providers/llamacpp", {"url": "http://evil:1"}, {"content-type": "text/plain"})[0], 415)
        self.assertEqual(call("/api/providers/llamacpp", {"url": "http://evil:1"}, {"origin": "https://evil.example"})[0], 403)
        self.assertEqual(models.llamacpp_settings()["url"], f"http://{addr}")

        call("/api/providers/llamacpp", {"url": ""})                                    # an empty address removes the settings
        self.assertEqual(models.llamacpp_settings()["source"], "")


if __name__ == "__main__":
    unittest.main()
