"""Huntun's own web tools (web_search, web_fetch) for providers without web tools of their own, against a local server."""
from __future__ import annotations

import asyncio
import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlparse

from huntun import web
from huntun.backends.api import _tool_defs
from huntun.config import agents_dir, db_path, default_config
from huntun.master import master_spec
from huntun.memory import AgentMemory
from huntun.roles import build_system_prompt
from huntun.store import Store
from huntun.tools import TOOLS, ToolContext, available_tools, execute
from huntun.types import AgentSpec, CycleState

PAGE = """<!doctype html><html><head><title>Widget guide</title><style>body{color:red}</style>
<script>tracking()</script></head><body>
<nav><a href="/">Home</a> <a href="/pricing">Pricing</a> Site nav</nav>
<main><h1>Getting started</h1>
<p>Install the <b>widget</b> package, then read <a href="/docs/api">the API</a> or <a href="#top">jump up</a>.</p>
<ul><li>First step</li><li>Second step</li></ul>
<pre>widget --init
  --verbose</pre>
<table><tr><th>Flag</th><th>Meaning</th></tr><tr><td>-v</td><td>verbose</td></tr></table>
<p>""" + "Plenty of words about widgets. " * 20 + """</p>
<a href="/card"><h3>A card</h3><p>with a paragraph</p></a>
</main>
<footer>Copyright footer</footer></body></html>"""

APP = """<!doctype html><html><head><title>App</title></head><body><div id="root"></div>
<script>document.getElementById('root').innerHTML = '<h1>Built by script</h1><p>' + 'hello world '.repeat(60) + '</p>';</script></body></html>"""

DDG = """<html><body>
<div class="result results_links results_links_deep result--ad "><h2 class="result__title"><a class="result__a" href="https://duckduckgo.com/y.js?ad_domain=x">Buy widgets</a></h2>
<a class="result__snippet" href="#">An ad</a></div>
<div class="result results_links results_links_deep web-result "><div class="links_main links_deep result__body">
<h2 class="result__title"><a rel="nofollow" class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fgithub.com%2Fggml-org%2Fllama.cpp&amp;rut=abc">ggml-org/<b>llama.cpp</b>: LLM inference</a></h2>
<a class="result__snippet" href="//duckduckgo.com/l/?uddg=x">LLM inference in <b>C/C++</b> &amp; more</a></div></div>
<div class="result results_links results_links_deep web-result "><h2 class="result__title"><a class="result__a" href="https://example.org/router">Router mode</a></h2>
<a class="result__snippet" href="https://example.org/router">Serve several models.</a></div>
</body></html>"""


class Handler(BaseHTTPRequestHandler):
    hits: dict[str, int] = {}
    seen_headers: dict[str, str] = {}

    def log_message(self, *a) -> None:  # quiet
        pass

    def _send(self, status: int, body: bytes | str, ctype: str = "text/html; charset=utf-8", extra: dict[str, str] | None = None) -> None:
        data = body.encode() if isinstance(body, str) else body
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:  # noqa: N802
        u = urlparse(self.path)
        Handler.hits[u.path] = Handler.hits.get(u.path, 0) + 1
        q = parse_qs(u.query)
        if u.path == "/page":
            return self._send(200, PAGE)
        if u.path == "/app":
            return self._send(200, APP)
        if u.path == "/long":
            return self._send(200, "".join(f"line {i:05d}\n" for i in range(3000)), "text/plain; charset=utf-8")
        if u.path == "/json":
            return self._send(200, '{"models":[{"id":"qwen","ctx":65536}]}', "application/json")
        if u.path == "/old":
            return self._send(302, "", extra={"Location": "/page"})
        if u.path == "/bin":
            return self._send(200, b"\x00\x01\x02", "application/octet-stream")
        if u.path == "/gbk":
            return self._send(200, "<html><head><title>中文</title></head><body><p>你好，世界</p></body></html>".encode("gbk"), "text/html; charset=gbk")
        if u.path == "/searx/search":
            if q.get("format") != ["json"]:
                return self._send(403, "forbidden")
            return self._send(200, json.dumps({"results": [{"title": "SearX hit", "url": "https://example.com/a", "content": "about <b>" + q["q"][0] + "</b>"}]}), "application/json")
        if u.path == "/brave":
            Handler.seen_headers["token"] = self.headers.get("X-Subscription-Token", "")
            return self._send(200, json.dumps({"web": {"results": [{"title": "Brave hit", "url": "https://example.com/b", "description": "found <strong>it</strong>"}]}}), "application/json")
        return self._send(404, "<html><body><p>No such page</p></body></html>")

    def do_POST(self) -> None:  # noqa: N802
        u = urlparse(self.path)
        n = int(self.headers.get("Content-Length") or 0)
        form = parse_qs(self.rfile.read(n).decode())
        Handler.hits[u.path] = Handler.hits.get(u.path, 0) + 1
        if u.path == "/ddg" and form.get("q"):
            return self._send(200, DDG)
        if u.path == "/ddg-blocked":
            return self._send(202, "<html><body>If this error persists, please let us know: anomaly</body></html>")
        return self._send(404, "")


class WebToolTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.srv.server_address[1]}"

    @classmethod
    def tearDownClass(cls) -> None:
        cls.srv.shutdown()

    def setUp(self) -> None:
        web._cache.clear()
        Handler.hits.clear()
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self.tmp.name)
        self.config = default_config("Build a widget")
        self.store = Store(db_path(self.ws))
        env = {k: v for k, v in os.environ.items() if k not in ("HUNTUN_WEB_TOOLS", "HUNTUN_SEARCH", "HUNTUN_SEARXNG_URL", "BRAVE_API_KEY")}
        patcher = mock.patch.dict(os.environ, env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self) -> None:
        self.store.close()
        self.tmp.cleanup()

    def ctx(self, agent: AgentSpec) -> ToolContext:
        return ToolContext(agent=agent, team=lambda: [master_spec(), agent], workspace=self.ws, store=self.store,
                           memory=AgentMemory(agents_dir(self.ws), agent.name), config=self.config, cycle=CycleState())

    def tool(self, name: str, **args) -> tuple[str, bool]:
        spec = next(t for t in TOOLS if t.name == name)
        return asyncio.run(execute(spec, args, self.ctx(AgentSpec("dev-1", "backend", "Dev", "dev"))))

    def test_html_becomes_readable_text_with_links(self) -> None:
        title, text = web.html_to_text(PAGE, "https://widgets.example/guide/")
        self.assertEqual(title, "Widget guide")
        self.assertIn("# Getting started", text)
        self.assertIn("Install the widget package, then read [the API](https://widgets.example/docs/api) or jump up.", text)
        self.assertIn("- First step\n- Second step", text)
        self.assertIn("widget --init\n  --verbose", text, "preformatted text keeps its lines")
        self.assertIn("Flag", text)
        self.assertIn("(link: https://widgets.example/card)", text, "a link around whole blocks keeps them and adds its address")
        for gone in ("tracking()", "color:red", "Site nav", "Pricing", "Copyright footer"):
            self.assertNotIn(gone, text)

    def test_a_page_without_main_keeps_everything_but_the_chrome(self) -> None:
        _, text = web.html_to_text("<body><header><h2>Blog</h2></header><div>Short post.</div><footer>f</footer></body>", "")
        self.assertEqual(text, "## Blog\n\nShort post.")

    def test_fetch_a_page(self) -> None:
        out, err = self.tool("web_fetch", url=f"{self.base}/page")
        self.assertFalse(err, out)
        self.assertIn(f"URL: {self.base}/page", out)
        self.assertIn("Title: Widget guide", out)
        self.assertIn(f"[the API]({self.base}/docs/api)", out)
        self.assertNotIn("more characters", out)

    def test_long_documents_come_in_parts_from_one_download(self) -> None:
        out, _ = self.tool("web_fetch", url=f"{self.base}/long", max_chars=1000)
        self.assertIn("line 00000", out)
        self.assertIn("Characters 0-1000 of 33000", out)
        self.assertIn("more characters: call web_fetch with the same url and start=1000]", out)
        out, _ = self.tool("web_fetch", url=f"{self.base}/long", start=1000, max_chars=1000)
        self.assertIn("line 00091", out)
        self.assertEqual(Handler.hits["/long"], 1, "the second part comes from the cache")
        out, _ = self.tool("web_fetch", url=f"{self.base}/long", start=40000)
        self.assertIn("(nothing past this point)", out)

    def test_redirects_errors_and_other_content(self) -> None:
        out, err = self.tool("web_fetch", url=f"{self.base}/old")
        self.assertFalse(err)
        self.assertIn(f"URL: {self.base}/page  (redirected from {self.base}/old)", out)
        out, err = self.tool("web_fetch", url=f"{self.base}/nope")
        self.assertTrue(err)
        self.assertIn("HTTP 404", out)
        self.assertIn("No such page", out)
        out, _ = self.tool("web_fetch", url=f"{self.base}/json")
        self.assertIn('"ctx": 65536', out, "JSON is shown indented")
        out, _ = self.tool("web_fetch", url=f"{self.base}/bin")
        self.assertIn("Binary content (application/octet-stream, 3 bytes)", out)
        out, _ = self.tool("web_fetch", url=f"{self.base}/gbk")
        self.assertIn("你好，世界", out)
        self.assertIn("Title: 中文", out)
        out, err = self.tool("web_fetch", url="file:///etc/passwd")
        self.assertTrue(err)
        self.assertIn("http:// and https:// addresses only", out)
        out, err = self.tool("web_fetch", url="http://127.0.0.1:9/")
        self.assertTrue(err)
        self.assertIn("cannot reach", out)

    def test_duckduckgo_results_without_ads(self) -> None:
        with mock.patch.object(web, "DDG_URL", f"{self.base}/ddg"):
            out, err = self.tool("web_search", query="llama.cpp router")
        self.assertFalse(err, out)
        self.assertIn("DuckDuckGo results for 'llama.cpp router':", out)
        self.assertIn("1. ggml-org/llama.cpp: LLM inference\n   https://github.com/ggml-org/llama.cpp\n   LLM inference in C/C++ & more", out)
        self.assertIn("2. Router mode\n   https://example.org/router", out)
        self.assertNotIn("Buy widgets", out)
        with mock.patch.object(web, "DDG_URL", f"{self.base}/ddg-blocked"):
            out, err = self.tool("web_search", query="x")
        self.assertTrue(err)
        self.assertIn("HUNTUN_SEARXNG_URL or BRAVE_API_KEY", out)

    def test_searxng_and_brave(self) -> None:
        os.environ["HUNTUN_SEARXNG_URL"] = f"{self.base}/searx/"
        out, err = self.tool("web_search", query="widgets")
        self.assertFalse(err, out)
        self.assertIn("SearXNG results", out)
        self.assertIn("about widgets", out)
        del os.environ["HUNTUN_SEARXNG_URL"]
        os.environ["BRAVE_API_KEY"] = "k-123"
        with mock.patch.object(web, "BRAVE_URL", f"{self.base}/brave"):
            out, err = self.tool("web_search", query="widgets")
        self.assertFalse(err, out)
        self.assertIn("Brave Search results", out)
        self.assertIn("found it", out)
        self.assertEqual(Handler.seen_headers["token"], "k-123")
        os.environ["HUNTUN_SEARCH"] = "searxng"
        out, err = self.tool("web_search", query="widgets")
        self.assertTrue(err)
        self.assertIn("needs HUNTUN_SEARXNG_URL", out)

    def test_only_providers_without_web_tools_get_them(self) -> None:
        dev, master = self.ctx(AgentSpec("dev-1", "backend", "Dev", "dev")), self.ctx(master_spec())
        for backend in ("llamacpp", "ollama", "vllm", "deepseek"):
            for c in (dev, master):
                self.assertTrue({"web_search", "web_fetch"} <= {t.name for t in available_tools(c, backend)}, backend)
        for backend in ("api", "claude-code", "codex", "kimi"):
            self.assertFalse({"web_search", "web_fetch"} & {t.name for t in available_tools(dev, backend)}, backend)
        defs, by_name = _tool_defs(dev, "llamacpp")
        self.assertIn("web_fetch", by_name)
        self.assertIn("input_schema", next(d for d in defs if d["name"] == "web_fetch"))
        defs, by_name = _tool_defs(dev, "api")
        self.assertNotIn("web_fetch", by_name, "Anthropic's API fetches server-side")
        self.assertEqual(next(d for d in defs if d["name"] == "web_fetch")["type"], "web_fetch_20260209")
        self.assertIn("web_search / web_fetch for research", build_system_prompt(dev.agent, self.config, [dev.agent], "llamacpp"))
        os.environ["HUNTUN_WEB_TOOLS"] = "off"
        self.assertFalse({"web_search", "web_fetch"} & {t.name for t in available_tools(dev, "llamacpp")})
        self.assertIn("no web search or fetch tools", build_system_prompt(dev.agent, self.config, [dev.agent], "llamacpp"))

    def test_a_page_built_by_javascript(self) -> None:
        with mock.patch.object(web, "can_render", return_value=False):
            out, _ = self.tool("web_fetch", url=f"{self.base}/app")
        self.assertIn("seems to build its content with JavaScript", out)
        self.assertIn("pip install playwright", out)
        if not web.can_render():
            self.skipTest("Playwright is not installed")
        web._cache.clear()
        out, _ = self.tool("web_fetch", url=f"{self.base}/app")
        if "Could not render" in out:
            self.skipTest(f"no browser for Playwright here: {out}")
        self.assertIn("Rendered in a headless browser", out)
        self.assertIn("# Built by script", out)
        self.assertIn("hello world hello world", out)


if __name__ == "__main__":
    unittest.main()
