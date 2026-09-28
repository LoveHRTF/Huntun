"""Web search and page reading that Huntun runs itself, for agents whose provider has no web tools of its own
(llama.cpp, Ollama, vLLM and DeepSeek). Anthropic's API runs its own server-side tools, and Claude Code, Codex and Kimi
bring theirs.

Search goes to a SearXNG server of your own (HUNTUN_SEARXNG_URL), Brave Search (BRAVE_API_KEY) or, by default,
DuckDuckGo's keyless HTML page. Pages are fetched over HTTP and turned into text with their links; a page that builds
its content with JavaScript is rendered in headless Chromium first when Playwright is installed.
HUNTUN_WEB_TOOLS=off takes both tools away.
"""
from __future__ import annotations

import asyncio
import html as htmllib
import io
import json
import os
import re
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from typing import Any

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
TIMEOUT = 30.0
MAX_BYTES = 5_000_000
DEFAULT_CHARS = 10_000                    # one page of a fetched document: about 3K tokens of English, more of Chinese
MAX_CHARS = 20_000
CACHE_TTL = 600                           # later pages of a document come from here instead of fetching it again
DDG_URL = "https://html.duckduckgo.com/html/"
BRAVE_URL = "https://api.search.brave.com/res/v1/web/search"
SEARCH_LABEL = {"duckduckgo": "DuckDuckGo", "brave": "Brave Search", "searxng": "SearXNG"}


def enabled() -> bool:
    return os.environ.get("HUNTUN_WEB_TOOLS", "on").strip().lower() not in ("off", "0", "false", "no")


def search_provider() -> str:
    choice = os.environ.get("HUNTUN_SEARCH", "auto").strip().lower()
    if choice in SEARCH_LABEL:
        return choice
    if os.environ.get("HUNTUN_SEARXNG_URL"):
        return "searxng"
    if os.environ.get("BRAVE_API_KEY"):
        return "brave"
    return "duckduckgo"


_ssl: ssl.SSLContext | None = None


def _ssl_context() -> ssl.SSLContext:
    """The operating system's trust store when truststore is there (a python.org build on macOS has no CA bundle of its own)."""
    global _ssl
    if _ssl is None:
        try:
            import truststore

            _ssl = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        except Exception:
            _ssl = ssl.create_default_context()
    return _ssl


def _request(url: str, data: dict[str, str] | None = None, headers: dict[str, str] | None = None) -> tuple[int, str, dict[str, str], bytes]:
    """(status, final URL, headers, body up to MAX_BYTES). Follows redirects; an HTTP error status is returned, not raised."""
    h = {"User-Agent": UA, "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,text/plain;q=0.8,*/*;q=0.5",
         "Accept-Language": "en-US,en;q=0.9,zh-CN;q=0.8", **(headers or {})}
    body = urllib.parse.urlencode(data).encode() if data is not None else None
    req = urllib.request.Request(url, data=body, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT, context=_ssl_context() if url.startswith("https:") else None) as r:
            return r.status, r.geturl(), {k.lower(): v for k, v in r.headers.items()}, r.read(MAX_BYTES)
    except urllib.error.HTTPError as e:
        return e.code, e.geturl() or url, {k.lower(): v for k, v in (e.headers or {}).items()}, e.read(64_000) if e.fp else b""
    except urllib.error.URLError as e:
        raise RuntimeError(f"cannot reach {url}: {e.reason}") from None
    except (TimeoutError, OSError) as e:
        raise RuntimeError(f"cannot reach {url}: {e}") from None


def _decode(body: bytes, headers: dict[str, str]) -> str:
    m = re.search(r"charset=([\w-]+)", headers.get("content-type", ""), re.I) or re.search(rb"<meta[^>]+charset=[\"']?([\w-]+)", body[:4096], re.I)
    enc = m.group(1) if m else "utf-8"
    enc = enc.decode() if isinstance(enc, bytes) else enc
    try:
        return body.decode(enc, errors="replace")
    except LookupError:
        return body.decode("utf-8", errors="replace")


def _plain(fragment: str) -> str:
    return re.sub(r"\s+", " ", htmllib.unescape(re.sub(r"<[^>]+>", "", fragment or ""))).strip()


# ---- HTML to text -------------------------------------------------------------------------------------------------

VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}
SKIP = {"script", "style", "noscript", "template", "svg", "iframe", "canvas", "head", "nav", "footer", "button", "select", "dialog"}
BLOCK = {"p", "div", "section", "article", "main", "header", "aside", "ul", "ol", "table", "tr", "pre", "blockquote", "form",
         "dl", "dt", "dd", "figure", "figcaption", "details", "summary", "hr", "address"}
HEADINGS = {"h1", "h2", "h3", "h4", "h5", "h6"}


class _Text(HTMLParser):
    """Readable text from HTML: headings as #, list items as -, links as [text](url), no scripts, styles or navigation.
    Text inside <main> or <article> is also kept apart, so a page with one can drop its surroundings."""

    def __init__(self, base: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base = base
        self.title = ""
        self._in_title = False
        self.parts: list[tuple[str, bool]] = []                  # (text, inside main/article)
        self.stack: list[tuple[str, str]] = []                   # open elements and what they do: skip, main, pre or ""
        self.depth = {"skip": 0, "main": 0, "pre": 0}
        self.links: list[tuple[int, str]] = []                   # open <a>: where its text starts, and its URL

    def _put(self, s: str) -> None:
        if s:
            self.parts.append((s, self.depth["main"] > 0))

    def _tail(self) -> str:
        return "".join(p for p, _ in self.parts[-3:])

    def _newline(self, n: int = 1) -> None:
        have = len(self._tail()) - len(self._tail().rstrip("\n")) if self.parts else n
        if have < n:
            self._put("\n" * (n - have))

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = {k: v or "" for k, v in attrs}
        if tag == "title" and not self.title:
            self._in_title = True
        hidden = "hidden" in a or a.get("aria-hidden") == "true" or "display:none" in a.get("style", "").replace(" ", "")
        kind = "skip" if tag in SKIP or hidden else "main" if tag in ("main", "article") or a.get("role") == "main" else "pre" if tag == "pre" else ""
        if tag not in VOID:
            self.stack.append((tag, kind))
            if kind:
                self.depth[kind] += 1
        if self.depth["skip"]:
            return
        if tag in HEADINGS:
            self._newline(2)
            self._put("#" * int(tag[1]) + " ")
        elif tag == "li":
            self._newline()
            self._put("- ")
        elif tag in BLOCK:
            self._newline(2 if tag in ("p", "pre", "table", "blockquote") else 1)
        elif tag == "br":
            self._newline()
        elif tag in ("td", "th"):
            self._put("| " if not self.parts or self._tail().endswith("\n") else " | ")
        elif tag == "img" and a.get("alt", "").strip():
            self._put(f"[image: {a['alt'].strip()}]")
        elif tag == "a":
            self.links.append((len(self.parts), a.get("href", "")))

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False
        if not any(t == tag for t, _ in self.stack):
            return
        while self.stack:
            t, kind = self.stack.pop()
            skipping = self.depth["skip"] > 0
            if kind:
                self.depth[kind] -= 1
            if not skipping:
                self._close(t)
            if t == tag:
                break

    def _close(self, tag: str) -> None:
        if tag == "a" and self.links:
            start, href = self.links.pop()
            text = "".join(p for p, _ in self.parts[start:]).strip()
            url = urllib.parse.urljoin(self.base, href.strip()) if href.strip() else ""
            if not text or not url.startswith(("http://", "https://")) or href.strip().startswith("#") or text == url:
                return
            if "\n" in text:                                    # a link around whole blocks (a card): its address on a line of its own
                self._newline()
                self._put(f"(link: {url})")
                self._newline(2)
                return
            inside = self.parts[start][1]
            del self.parts[start:]
            self.parts.append((f"[{text}]({url})", inside))
        elif tag in HEADINGS or tag in BLOCK or tag == "li":
            self._newline(2 if tag in HEADINGS or tag in ("p", "pre", "table", "blockquote") else 1)

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title += data
            return
        if self.depth["skip"]:
            return
        if self.depth["pre"]:
            self._put(data)
            return
        text = re.sub(r"\s+", " ", data)
        if not text.strip():                                     # whitespace between inline elements: one space at most
            if self.parts and not self._tail().endswith((" ", "\n")):
                self._put(" ")
            return
        if not self.parts or self._tail().endswith((" ", "\n")):
            text = text.lstrip()
        self._put(text)

    def text(self) -> str:
        whole = "".join(p for p, _ in self.parts)
        main = "".join(p for p, inside in self.parts if inside)
        chosen = main if len(main.strip()) >= 400 else whole
        lines = [ln.rstrip() for ln in chosen.splitlines()]
        return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def html_to_text(html: str, base: str = "") -> tuple[str, str]:
    """(title, readable text) of an HTML page."""
    p = _Text(base)
    try:
        p.feed(html)
        p.close()
    except Exception:  # malformed markup: keep what was read
        pass
    return re.sub(r"\s+", " ", p.title).strip(), p.text()


# ---- search ---------------------------------------------------------------------------------------------------------


class _DuckDuckGo(HTMLParser):
    """Results from DuckDuckGo's HTML page: each is a div.result with a.result__a (title and link) and .result__snippet."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.results: list[dict[str, str]] = []
        self._ad = False
        self._field = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = {k: v or "" for k, v in attrs}
        cls = a.get("class", "").split()
        if tag == "div" and "result" in cls:
            self._ad = "result--ad" in cls
            self.results.append({"title": "", "url": "", "snippet": "", "ad": "1" if self._ad else ""})
        elif self.results and "result__a" in cls:
            self._field = "title"
            href = a.get("href", "")
            q = urllib.parse.parse_qs(urllib.parse.urlparse(href).query)
            self.results[-1]["url"] = q["uddg"][0] if "uddg" in q else urllib.parse.urljoin("https://duckduckgo.com/", href)
        elif self.results and "result__snippet" in cls:
            self._field = "snippet"

    def handle_endtag(self, tag: str) -> None:
        if tag in ("a", "td", "div") and self._field:
            self._field = ""

    def handle_data(self, data: str) -> None:
        if self._field and self.results:
            self.results[-1][self._field] += data


def parse_duckduckgo(html: str) -> list[dict[str, str]]:
    p = _DuckDuckGo()
    p.feed(html)
    out = []
    for r in p.results:
        url = r["url"]
        if r["ad"] or not url.startswith(("http://", "https://")) or "duckduckgo.com/y.js" in url:
            continue
        out.append({"title": _plain(r["title"]), "url": url, "snippet": _plain(r["snippet"])})
    return out


def _search(query: str, count: int) -> list[dict[str, str]]:
    provider = search_provider()
    if provider == "searxng":
        base = os.environ.get("HUNTUN_SEARXNG_URL", "").rstrip("/")
        if not base:
            raise RuntimeError("HUNTUN_SEARCH=searxng needs HUNTUN_SEARXNG_URL, the address of your SearXNG server")
        status, _, _, body = _request(f"{base}/search?" + urllib.parse.urlencode({"q": query, "format": "json"}), headers={"Accept": "application/json"})
        if status == 403:
            raise RuntimeError("SearXNG refused a JSON search: add json under search.formats in its settings.yml")
        if status >= 400:
            raise RuntimeError(f"SearXNG answered HTTP {status}")
        items = json.loads(body.decode("utf-8", errors="replace")).get("results") or []
        return [{"title": _plain(i.get("title", "")), "url": i.get("url", ""), "snippet": _plain(i.get("content", ""))} for i in items][:count]
    if provider == "brave":
        key = os.environ.get("BRAVE_API_KEY", "")
        if not key:
            raise RuntimeError("HUNTUN_SEARCH=brave needs BRAVE_API_KEY")
        status, _, _, body = _request(f"{BRAVE_URL}?" + urllib.parse.urlencode({"q": query, "count": min(count, 20)}),
                                      headers={"Accept": "application/json", "X-Subscription-Token": key})
        if status == 429:
            raise RuntimeError("Brave Search: rate limit or monthly quota reached")
        if status >= 400:
            raise RuntimeError(f"Brave Search answered HTTP {status}: {body[:300].decode(errors='replace')}")
        items = (json.loads(body.decode("utf-8", errors="replace")).get("web") or {}).get("results") or []
        return [{"title": _plain(i.get("title", "")), "url": i.get("url", ""), "snippet": _plain(i.get("description", ""))} for i in items][:count]
    status, _, headers, body = _request(DDG_URL, data={"q": query}, headers={"Referer": "https://html.duckduckgo.com/"})
    text = _decode(body, headers)
    results = parse_duckduckgo(text) if status == 200 else []
    if not results and (status in (202, 403, 429) or "anomaly" in text.lower()):
        raise RuntimeError("DuckDuckGo refused an automated search (it does this after bursts of queries). Wait a minute and try again, "
                           "or ask the human to set HUNTUN_SEARXNG_URL or BRAVE_API_KEY for Huntun")
    if status >= 400:
        raise RuntimeError(f"DuckDuckGo answered HTTP {status}")
    return results[:count]


async def search(query: str, count: int = 8) -> str:
    query = query.strip()
    if not query:
        raise ValueError("empty query")
    count = max(1, min(int(count or 8), 20))
    results = await asyncio.to_thread(_search, query, count)
    label = SEARCH_LABEL[search_provider()]
    if not results:
        return f"No results for {query!r} ({label}). Try other words."
    lines = [f"{label} results for {query!r}:", ""]
    for n, r in enumerate(results, 1):
        lines += [f"{n}. {r['title'] or r['url']}", f"   {r['url']}"] + ([f"   {r['snippet']}"] if r["snippet"] else []) + [""]
    lines.append("Read a result with web_fetch.")
    return "\n".join(lines)


# ---- fetch ----------------------------------------------------------------------------------------------------------

_cache: dict[tuple[str, bool], tuple[float, dict[str, Any]]] = {}


def can_render() -> bool:
    try:
        import playwright.async_api  # noqa: F401
    except ImportError:
        return False
    return True


async def _launch(pw: Any) -> Any:
    """Playwright's own Chromium, else the Google Chrome or Microsoft Edge installed on the machine (so `pip install playwright`
    is enough there), else HUNTUN_CHROMIUM when it names a Chromium executable."""
    if os.environ.get("HUNTUN_CHROMIUM"):
        return await pw.chromium.launch(executable_path=os.environ["HUNTUN_CHROMIUM"])
    for channel in (None, "chrome", "msedge"):
        try:
            return await (pw.chromium.launch(channel=channel) if channel else pw.chromium.launch())
        except Exception:
            continue
    raise RuntimeError("no browser for Playwright: install Google Chrome, or run `playwright install chromium`")


async def _render(url: str) -> tuple[str, str]:
    """(final URL, HTML after the page's scripts ran) from a headless browser."""
    from playwright.async_api import TimeoutError as PlaywrightTimeout
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        browser = await _launch(pw)
        try:
            page = await browser.new_page(user_agent=UA)
            try:
                await page.goto(url, wait_until="networkidle", timeout=TIMEOUT * 1000)
            except PlaywrightTimeout:
                pass                                                                   # still loading: take what is there
            return page.url, await page.content()
        finally:
            await browser.close()


def _pdf_text(body: bytes) -> str | None:
    try:
        from pypdf import PdfReader
    except ImportError:
        return None
    reader = PdfReader(io.BytesIO(body))
    return "\n\n".join((page.extract_text() or "").strip() for page in reader.pages).strip()


async def _load(url: str, render: bool) -> dict[str, Any]:
    status, final, headers, body = await asyncio.to_thread(_request, url)
    ctype = headers.get("content-type", "").split(";")[0].strip().lower()
    head = body[:512].lstrip().lower()
    is_html = "html" in ctype or (not ctype and head.startswith((b"<!doctype html", b"<html")))
    doc: dict[str, Any] = {"status": status, "url": final, "type": ctype or "unknown", "title": "", "note": ""}
    if len(body) >= MAX_BYTES:
        doc["note"] = f"Only the first {MAX_BYTES // 1_000_000} MB were read."
    if is_html:
        html = _decode(body, headers)
        title, text = html_to_text(html, final)
        scripted = len(text) < 300 and "<script" in html.lower()           # probably an app shell that fills itself in
        if status < 400 and (render or scripted):
            if can_render():
                try:
                    final, rendered = await _render(url)
                    title2, text2 = html_to_text(rendered, final)
                    if len(text2) >= len(text):
                        title, text = title2 or title, text2
                    doc["note"] = "Rendered in a headless browser (the page's JavaScript ran)."
                except Exception as e:
                    doc["note"] = f"Could not render the page in a headless browser ({str(e).splitlines()[0][:200]}); this is the HTML as served."
            else:
                doc["note"] = ("The page seems to build its content with JavaScript, which this fetch does not run. The machine running Huntun "
                               "can render such pages once Playwright is installed there (pip install playwright; it uses Google Chrome if present).")
        doc.update(title=title, text=text, url=final)
    elif ctype == "application/pdf" or head.startswith(b"%pdf"):
        text = _pdf_text(body)
        doc["text"] = text if text is not None else ""
        if text is None:
            doc["note"] = f"A PDF ({len(body)} bytes). Reading PDFs needs pypdf on the machine running Huntun (pip install pypdf)."
    elif ctype.startswith("text/") or any(k in ctype for k in ("json", "xml", "javascript", "yaml", "csv", "markdown")) or not ctype:
        text = _decode(body, headers)
        if "json" in ctype:
            try:
                text = json.dumps(json.loads(text), indent=1, ensure_ascii=False)
            except ValueError:
                pass
        doc["text"] = text
    else:
        doc["text"] = ""
        doc["note"] = f"Binary content ({ctype}, {len(body)} bytes): web_fetch shows web pages, text, JSON and PDF."
    return doc


async def fetch(url: str, start: int = 0, max_chars: int = DEFAULT_CHARS, render: bool = False) -> str:
    url = url.strip()
    parts = urllib.parse.urlparse(url)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise ValueError("web_fetch reads http:// and https:// addresses only")
    start, max_chars = max(0, int(start or 0)), max(1000, min(int(max_chars or DEFAULT_CHARS), MAX_CHARS))
    key = (url, bool(render))
    now = time.monotonic()
    hit = _cache.get(key)
    if hit and now - hit[0] < CACHE_TTL:
        doc = hit[1]
    else:
        doc = await _load(url, bool(render))
        _cache[key] = (now, doc)
        for k in sorted(_cache, key=lambda k: _cache[k][0])[:-32]:                  # keep the 32 most recent
            del _cache[k]
    text = doc.get("text", "")
    if doc["status"] >= 400:
        _cache.pop(key, None)
        return f"ERROR: HTTP {doc['status']} from {doc['url']}" + (f"\n\n{text[:1500]}" if text.strip() else "")
    lines = [f"URL: {doc['url']}" + (f"  (redirected from {url})" if doc["url"] != url else "")]
    if doc["title"]:
        lines.append(f"Title: {doc['title']}")
    if doc["note"]:
        lines.append(f"Note: {doc['note']}")
    piece = text[start:start + max_chars]
    if start or len(text) > max_chars:
        lines.append(f"Characters {start}-{start + len(piece)} of {len(text)}")
    out = "\n".join(lines) + "\n\n" + (piece if piece else "(no text)" if not start else "(nothing past this point)")
    rest = len(text) - start - len(piece)
    if rest > 0:
        out += f"\n\n[{rest} more characters: call web_fetch with the same url and start={start + len(piece)}]"
    return out
