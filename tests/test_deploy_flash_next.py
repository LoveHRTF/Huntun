"""deploy/flash-next-4090: picking the GGUF set to download, and the benchmark against a stand-in llama-server."""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

KIT = Path(__file__).resolve().parent.parent / "deploy" / "flash-next-4090"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, KIT / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


fetch = _load("fetch_model")
bench = _load("bench")


class PickSet(unittest.TestCase):
    FILES = {
        "Qwen3.8-Flash-Next-Uncensored-AD-4.27-00001-of-00003.gguf": 30,
        "Qwen3.8-Flash-Next-Uncensored-AD-4.27-00002-of-00003.gguf": 40,
        "Qwen3.8-Flash-Next-Uncensored-AD-4.27-00003-of-00003.gguf": 15,
        "mtp/Qwen3.8-Flash-Next-Uncensored-AD-4.27-MTP-00001-of-00002.gguf": 50,
        "mtp/Qwen3.8-Flash-Next-Uncensored-AD-4.27-MTP-00002-of-00002.gguf": 50,
        "mmproj-F16.gguf": 1,
        "README.md": 1,
    }

    def test_groups_shards_and_leaves_out_projectors(self) -> None:
        sets = fetch.group_sets(self.FILES)
        self.assertEqual(sorted(sets), ["Qwen3.8-Flash-Next-Uncensored-AD-4.27", "mtp/Qwen3.8-Flash-Next-Uncensored-AD-4.27-MTP"])
        self.assertEqual(len(sets["Qwen3.8-Flash-Next-Uncensored-AD-4.27"]), 3)

    def test_prefers_the_non_mtp_set_unless_asked(self) -> None:
        sets = fetch.group_sets(self.FILES)
        self.assertEqual(fetch.choose(sets, "")[0], "Qwen3.8-Flash-Next-Uncensored-AD-4.27")
        self.assertEqual(fetch.choose(sets, "mtp")[0], "mtp/Qwen3.8-Flash-Next-Uncensored-AD-4.27-MTP")

    def test_ambiguous_or_incomplete_sets_are_not_picked(self) -> None:
        files = {"a-00001-of-00002.gguf": 1, "b.gguf": 1, "c-00001-of-00002.gguf": 1, "c-00002-of-00002.gguf": 1}
        key, candidates = fetch.choose(fetch.group_sets(files), "")
        self.assertIsNone(key)
        self.assertEqual(sorted(candidates), ["b", "c"])  # "a" is missing a shard

    def test_prefers_the_mainline_set_and_exact_names(self) -> None:
        # Navin-Models/...-AD-4.27-GGUF: a 34-file "-main" set (experimental attached MTP) and a 33-file "-mainline" set
        files = {f"Q-AD-4.27-main-{i:05d}-of-00034.gguf": 1 for i in range(1, 35)}
        files.update({f"Q-AD-4.27-mainline-{i:05d}-of-00033.gguf": 1 for i in range(1, 34)})
        sets = fetch.group_sets(files)
        self.assertEqual(fetch.choose(sets, "")[0], "Q-AD-4.27-mainline")
        self.assertEqual(fetch.choose(sets, "main")[0], "Q-AD-4.27-main")
        self.assertEqual(fetch.choose(sets, "mainline")[0], "Q-AD-4.27-mainline")
        self.assertIsNone(fetch.choose(sets, "AD-4.27")[0])

    def test_mmproj_prefers_f16_next_to_the_model(self) -> None:
        files = {"x/m.gguf": 1, "x/mmproj-BF16.gguf": 1, "x/mmproj-F16.gguf": 1, "mmproj-F32.gguf": 1}
        self.assertEqual(fetch.pick_mmproj(files, "x/m"), "x/mmproj-F16.gguf")


try:
    import huggingface_hub  # noqa: F401
    HAVE_HF = True
except ImportError:
    HAVE_HF = False


@unittest.skipUnless(HAVE_HF, "huggingface_hub not installed")
class Download(unittest.TestCase):
    def test_retries_network_errors_and_never_fails_on_the_model_card(self) -> None:
        from unittest import mock

        calls = {"snapshot": 0}

        def flaky(**kw):
            calls["snapshot"] += 1
            if calls["snapshot"] < 3:
                raise TimeoutError("The read operation timed out")

        def card(**kw):
            raise TimeoutError("The read operation timed out")

        err = io.StringIO()
        with mock.patch("huggingface_hub.snapshot_download", side_effect=flaky), \
                mock.patch("huggingface_hub.hf_hub_download", side_effect=card), \
                mock.patch.object(fetch.time, "sleep"), contextlib.redirect_stderr(err):
            fetch.download("o/r", Path("."), ["m-00001-of-00001.gguf"], ["README.md"])
        self.assertEqual(calls["snapshot"], 3)
        self.assertIn("retrying (2/4)", err.getvalue())
        self.assertIn("Skipped README.md", err.getvalue())

    def test_gives_up_after_the_last_attempt(self) -> None:
        from unittest import mock

        with mock.patch("huggingface_hub.snapshot_download", side_effect=TimeoutError("t")), \
                mock.patch.object(fetch.time, "sleep"), contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(TimeoutError):
                fetch.download("o/r", Path("."), ["m.gguf"])


class _FakeServer(BaseHTTPRequestHandler):
    tool_use = True

    def log_message(self, *a) -> None:
        pass

    def _send(self, body: dict, code: int = 200) -> None:
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        self._send({"status": "ok"})

    def do_POST(self) -> None:
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if self.path == "/tokenize":
            self._send({"tokens": list(range(len(body["content"].split())))})
        elif self.path == "/completion":
            n = body["n_predict"]
            self._send({"timings": {"prompt_n": len(body["prompt"].split()), "prompt_ms": 2000.0, "prompt_per_second": 1000.0,
                                    "predicted_n": n, "predicted_per_second": 18.0}})
        elif self.path == "/v1/messages":
            block = {"type": "tool_use", "id": "t1", "name": "get_time", "input": {"timezone": "UTC"}} if self.tool_use else {"type": "text", "text": "noon"}
            self._send({"content": [block], "stop_reason": "tool_use" if self.tool_use else "end_turn"})
        else:
            self._send({"error": "not found"}, 404)


class Bench(unittest.TestCase):
    def setUp(self) -> None:
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeServer)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self) -> None:
        self.server.shutdown()
        _FakeServer.tool_use = True

    def _run(self, *args: str) -> tuple[int, str]:
        import sys

        out = io.StringIO()
        argv, sys.argv = sys.argv, ["bench.py", "--url", self.url, "--quick", *args]
        try:
            with contextlib.redirect_stdout(out):
                code = bench.main()
        finally:
            sys.argv = argv
        return code, out.getvalue()

    def test_reports_prefill_decode_and_the_plan_comparison(self) -> None:
        code, out = self._run()
        self.assertEqual(code, 0, out)
        self.assertIn("tool_use: get_time", out)
        self.assertIn("2 concurrent", out)
        self.assertIn("plan >4000", out)
        self.assertIn("within expectation", out)  # 1000 t/s prefill

    def test_fails_when_messages_returns_no_tool_call(self) -> None:
        _FakeServer.tool_use = False
        code, out = self._run()
        self.assertEqual(code, 1)
        self.assertIn("Huntun cannot drive this server", out)


if __name__ == "__main__":
    unittest.main()
