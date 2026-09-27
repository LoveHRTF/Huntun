#!/usr/bin/env python3
"""Checks and measures a running llama-server for Qwen3.8-Flash-Next, then compares against the reference plan.

1. Anthropic-compatible /v1/messages with a tool: the path Huntun uses (must return a tool_use block).
2. Prefill: prompt processing speed at a few prompt lengths (prompt cache off).
3. Decode: generation speed with 1..N concurrent requests, and at a long context depth.

Standard library only. Usage: ./bench.py [--url http://127.0.0.1:8080] [--parallel 2] [--quick] [--sizes 4096,65536,250000]
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sys
import threading
import time
import urllib.error
import urllib.request

WORDS = ("alpha bravo charlie delta echo foxtrot golf hotel india juliet kilo lima mike november oscar papa quebec romeo "
         "sierra tango uniform victor whiskey xray yankee zulu river stone cloud maple ember harbor lantern meadow orbit "
         "pixel quartz saddle timber velvet willow zephyr anchor beacon canyon dune falcon glacier").split()

# The reference plan (RTX 5090 + 128 GB) and what this box (RTX 4090 + 64 GB DDR4) reaches: measured on Windows 11 with
# the N-gram table on NVMe (16.4-16.9 / 20.2-20.6 tok/s decode, 629-632 t/s prefill); a few percent either way is noise.
PLAN = {"decode_total_2": 60.0, "prefill": 4000.0}
EXPECT = {"decode_1": (15, 20), "decode_total_2": (18, 25), "prefill": (550, 800)}


AUTH: dict[str, str] = {}   # Authorization header for a server started with --api-key


def post(url: str, body: dict, headers: dict | None = None, timeout: float = 1800) -> dict:
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json", **AUTH, **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def wait_ready(base: str, limit: float = 900) -> None:
    t0 = time.time()
    while True:
        try:
            with urllib.request.urlopen(base + "/health", timeout=5) as r:
                if r.status == 200:
                    return
        except urllib.error.HTTPError as e:
            if e.code != 503:
                raise
        except (urllib.error.URLError, ConnectionError, TimeoutError):
            pass
        if time.time() - t0 > limit:
            raise SystemExit(f"server at {base} not ready after {limit:.0f}s")
        time.sleep(3)


def filler(base: str, n_tokens: int, seed: int) -> str:
    """Random text of about n_tokens tokens (measured with /tokenize), different per seed so no cache can help."""
    rng = random.Random(seed)
    text = " ".join(rng.choice(WORDS) for _ in range(n_tokens))
    counted = len(post(base + "/tokenize", {"content": text})["tokens"])
    scale = n_tokens / max(counted, 1)
    words = text.split()
    if scale < 1:
        text = " ".join(words[: int(len(words) * scale)])
    else:
        text = " ".join(words + [rng.choice(WORDS) for _ in range(int(len(words) * (scale - 1)))])
    return f"Seed {seed}. " + text


def complete(base: str, prompt: str, n_predict: int, ignore_eos: bool = True) -> dict:
    r = post(base + "/completion", {"prompt": prompt, "n_predict": n_predict, "cache_prompt": False,
                                    "ignore_eos": ignore_eos, "temperature": 0.7})
    return r.get("timings", {})


def check_messages(base: str, model: str, key: str) -> bool:
    body = {
        "model": model,
        "max_tokens": 4096,
        "messages": [{"role": "user", "content": "What time is it in UTC? Use the get_time tool; do not guess."}],
        "tools": [{"name": "get_time", "description": "Returns the current time in a timezone.",
                   "input_schema": {"type": "object", "properties": {"timezone": {"type": "string"}}, "required": ["timezone"]}}],
    }
    t0 = time.time()
    try:
        r = post(base + "/v1/messages", body, {"x-api-key": key or "none", "anthropic-version": "2023-06-01"})
    except urllib.error.HTTPError as e:
        print(f"  /v1/messages failed: HTTP {e.code} {e.read().decode()[:300]}")
        return False
    kinds = [b.get("type") for b in r.get("content", [])]
    tool = next((b for b in r.get("content", []) if b.get("type") == "tool_use"), None)
    print(f"  {time.time() - t0:.1f}s, stop_reason={r.get('stop_reason')}, blocks={kinds}")
    if tool:
        print(f"  tool_use: {tool.get('name')}({json.dumps(tool.get('input'))})  -> Huntun's tool loop will work")
    else:
        print("  no tool_use block: check that the server runs with --jinja and the model card's chat template")
    return tool is not None


def decode_round(base: str, n: int, n_predict: int, seed: int) -> tuple[float, list[float]]:
    prompts = [f"Seed {seed + i}. Write a long story about {random.Random(seed + i).choice(WORDS)}." for i in range(n)]
    results: list[dict] = [{} for _ in range(n)]

    def run(i: int) -> None:
        results[i] = complete(base, prompts[i], n_predict)

    threads = [threading.Thread(target=run, args=(i,)) for i in range(n)]
    t0 = time.time()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    wall = time.time() - t0
    per = [r.get("predicted_per_second", 0.0) for r in results]
    total = sum(r.get("predicted_n", 0) for r in results) / wall
    return total, per


def verdict(value: float, lo: float, hi: float) -> str:
    return "within expectation" if lo <= value <= hi else ("above expectation" if value > hi else "BELOW expectation")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8080")
    ap.add_argument("--model", default="qwen3.8-flash-next-uncensored")
    ap.add_argument("--api-key", default=os.environ.get("FLASHNEXT_API_KEY", ""), help="the server's API key (API_KEY in the kit's config), if it has one")
    ap.add_argument("--parallel", type=int, default=2, help="the server's -np")
    ap.add_argument("--quick", action="store_true", help="shorter prompts and generations")
    ap.add_argument("--sizes", default="", help="comma-separated prompt lengths for the prefill test; the last one is also "
                    "the depth of the long-context decode test and must fit in one slot (CTX_PER_SLOT)")
    ap.add_argument("--skip-messages", action="store_true")
    a = ap.parse_args()
    base = a.url.rstrip("/")
    if a.api_key:
        AUTH["Authorization"] = f"Bearer {a.api_key}"

    print(f"Waiting for {base} ...")
    wait_ready(base)
    seed = int(time.time())

    ok_messages = True
    if not a.skip_messages:
        print("\n1) Anthropic /v1/messages with a tool (Huntun's path)")
        ok_messages = check_messages(base, a.model, a.api_key)

    print("\n2) Prefill (prompt cache off)")
    if a.sizes:
        sizes = [int(x) for x in a.sizes.split(",") if x.strip()]
    else:
        sizes = [4096, 16384] if a.quick else [4096, 16384, 32768]
    prefill: dict[int, float] = {}
    for i, n in enumerate(sizes):
        t = complete(base, filler(base, n, seed + 100 + i), 1)
        prefill[n] = t.get("prompt_per_second", 0.0)
        print(f"  {t.get('prompt_n', n):>6} tokens: {prefill[n]:7.0f} t/s  ({t.get('prompt_ms', 0) / 1000:.1f}s)")

    print("\n3) Decode")
    n_predict = 128 if a.quick else 256
    totals: dict[int, float] = {}
    for n in range(1, a.parallel + 1):
        total, per = decode_round(base, n, n_predict, seed + 1000 * n)
        totals[n] = total
        print(f"  {n} concurrent: {total:5.1f} tok/s total, per request {', '.join(f'{p:.1f}' for p in per)}")
    depth = sizes[-1]
    t = complete(base, filler(base, depth, seed + 7), n_predict // 2)
    deep = t.get("predicted_per_second", 0.0)
    print(f"  1 request at {depth // 1024}K context depth: {deep:5.1f} tok/s")

    best_prefill = max(prefill.values())
    print("\nSummary vs the reference plan (5090 + 128 GB) and this box's expectation (4090 + 64 GB DDR4)")
    print(f"  prefill           {best_prefill:7.0f} t/s   plan >{PLAN['prefill']:.0f}   expected {EXPECT['prefill'][0]}-{EXPECT['prefill'][1]}  "
          f"-> {verdict(best_prefill, *EXPECT['prefill'])}")
    print(f"  decode, 1 stream  {totals[1]:7.1f} tok/s  plan  n/a    expected {EXPECT['decode_1'][0]}-{EXPECT['decode_1'][1]}  "
          f"-> {verdict(totals[1], *EXPECT['decode_1'])}")
    if 2 in totals:
        print(f"  decode, 2 streams {totals[2]:7.1f} tok/s  plan {PLAN['decode_total_2']:.0f}     expected {EXPECT['decode_total_2'][0]}-{EXPECT['decode_total_2'][1]}  "
              f"-> {verdict(totals[2], *EXPECT['decode_total_2'])}")
    if deep and totals[1]:
        print(f"  long-context decode keeps {deep / totals[1] * 100:.0f}% of short-context speed")
    if not ok_messages:
        print("\n/v1/messages tool check failed: Huntun cannot drive this server until it passes.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
