"""DeepSeek and Ollama as Anthropic-compatible providers of the API backend: client wiring, request shaping, discovery."""
from __future__ import annotations

import asyncio
import json
import os
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

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


class ProviderTests(unittest.TestCase):
    def setUp(self) -> None:
        os.environ["DEEPSEEK_API_KEY"] = "sk-test"
        os.environ.pop("OLLAMA_HOST", None)
        os.environ.pop("HUNTUN_OLLAMA_MODELS", None)
        os.environ.pop("HUNTUN_COMPAT_THINKING", None)
        self.config = default_config("Mock goal")

    def tearDown(self) -> None:
        for k in ("DEEPSEEK_API_KEY", "OLLAMA_HOST", "HUNTUN_OLLAMA_MODELS"):
            os.environ.pop(k, None)

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


if __name__ == "__main__":
    unittest.main()
