"""Codex catalog discovery without a provider call or access to real user config."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from huntun.backends.codex import CodexBackend, context_settings, reasoning_effort
from huntun.config import default_config
from huntun.models import (
    backend_for_model,
    catalog_available,
    model_info,
    refresh_codex,
)


class CodexModelTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.env = patch.dict(os.environ, {"CODEX_HOME": str(self.home), "HUNTUN_CODEX_MODELS": "", "HUNTUN_CODEX_BIN": "fake-codex", "HUNTUN_CODEX_CONTEXT": ""})
        self.env.start()

    def tearDown(self) -> None:
        self.env.stop()
        refresh_codex()
        self.tmp.cleanup()

    def cache(self, rows: list[dict]) -> None:
        (self.home / "models_cache.json").write_text(json.dumps({"models": rows}))

    def test_discovers_names_context_and_supported_efforts(self) -> None:
        self.cache([{"slug": "codex-new", "display_name": "New Codex", "context_window": 123456,
                     "supported_reasoning_levels": [{"effort": e} for e in ("medium", "high", "max", "ultra")]},
                    {"slug": "hidden", "visibility": "hide"}])
        self.assertEqual([m.id for m in refresh_codex()], ["codex-new"])
        self.assertEqual(model_info("codex-new").context, 123456)
        self.assertEqual(model_info("codex-new").label, "New Codex")
        self.assertEqual(backend_for_model("codex-new", {"codex": "ready"}), "codex")
        self.assertEqual([m.id for m, _ in catalog_available({"codex": "ready"})], ["codex-new"])
        self.assertEqual(reasoning_effort("codex-new", "ultra"), "ultra")
        self.assertEqual(reasoning_effort("codex-new", "max"), "max")
        self.assertEqual(reasoning_effort("codex-new", "xhigh"), "high")
        self.assertEqual(reasoning_effort("codex-new", "low"), "medium")

    def test_override_and_configured_model(self) -> None:
        self.cache([{"slug": "cached"}])
        (self.home / "config.toml").write_text('model = "configured"\n')
        self.assertEqual([m.id for m in refresh_codex()], ["configured", "cached"])
        with patch.dict(os.environ, {"HUNTUN_CODEX_MODELS": "one, two,one"}):
            self.assertEqual([m.id for m in refresh_codex()], ["one", "two"])

    def test_cache_updates_and_corrupt_cache_fallback(self) -> None:
        self.cache([{"slug": "first"}])
        self.assertEqual(refresh_codex()[0].id, "first")
        self.cache([{"slug": "replacement"}])
        self.assertEqual(refresh_codex()[0].id, "replacement")
        self.assertIsNone(model_info("first"))
        (self.home / "models_cache.json").write_text("broken json")
        self.assertEqual(refresh_codex()[0].id, "gpt-5.3-codex")

    def test_resume_flags_and_native_max(self) -> None:
        self.cache([{"slug": "codex-new", "supported_reasoning_levels": [{"effort": "max"}]}])
        backend = CodexBackend(default_config("goal"))
        base = backend._base_args(self.home, "codex-new", "max", "workspace-write", "http://127.0.0.1:999/mcp")
        self.assertIn('model_reasoning_effort="max"', base)
        args = backend._resume_args(base, "session")
        self.assertEqual(args[:4], ["fake-codex", "exec", "resume", "session"])
        self.assertNotIn("-C", args)
        self.assertNotIn("-s", args)
        self.assertIn('sandbox_mode="workspace-write"', args)
        self.assertIn("codex-new", args)

    def test_models_with_no_reasoning_and_malformed_metadata(self) -> None:
        self.cache([{"slug": "no-reasoning", "supported_reasoning_levels": [{"effort": "none"}]},
                    {"slug": "bad-metadata", "supported_reasoning_levels": None, "context_window": "invalid"}])
        self.assertEqual(reasoning_effort("no-reasoning", "high"), "none")
        self.assertEqual(model_info("bad-metadata").context, 400000)
        self.assertEqual(model_info("bad-metadata").reasoning_levels, ())

    def test_default_model_uses_the_active_profile_metadata(self) -> None:
        from huntun.models import codex_model_info
        self.cache([{"slug": "base"}, {"slug": "profile-model", "context_window": 123000,
                                       "supported_reasoning_levels": [{"effort": "max"}]}])
        (self.home / "config.toml").write_text('model = "base"\nprofile = "work"\n[profiles.work]\nmodel = "profile-model"\n')
        self.assertEqual(codex_model_info("").id, "profile-model")
        self.assertEqual(codex_model_info("").context, 123000)
        self.assertEqual(reasoning_effort("", "max"), "max")

    def test_maximum_windows_are_used_for_fresh_and_resumed_sessions(self) -> None:
        self.cache([{"slug": "gpt-6.1-sol", "context_window": 272000, "max_context_window": 872000},
                    {"slug": "gpt-5.5", "context_window": 272000, "max_context_window": 272000}])
        refresh_codex()
        backend = CodexBackend(default_config("goal"))
        for model, window in (("gpt-6.1-sol", 872000), ("gpt-5.5", 272000)):
            self.assertEqual(model_info(model).context, window)
            base = backend._base_args(self.home, model, "high", "read-only", None)
            for args in (base, backend._resume_args(base, "existing-session")):
                self.assertIn(f"model_context_window={window}", args)
                self.assertIn(f"model_auto_compact_token_limit={window * 9 // 10}", args)

    def test_invalid_maximum_falls_back_to_valid_default_window(self) -> None:
        self.cache([{"slug": "invalid-max", "context_window": 123456, "max_context_window": "invalid"},
                    {"slug": "negative-max", "context_window": 100000, "max_context_window": -1},
                    {"slug": "both-invalid", "context_window": -1, "max_context_window": None}])
        refresh_codex()
        self.assertEqual(model_info("invalid-max").context, 123456)
        self.assertEqual(model_info("negative-max").context, 100000)
        self.assertEqual(model_info("both-invalid").context, 400000)

    def test_context_cap_is_applied_to_the_cli_and_cannot_exceed_model_maximum(self) -> None:
        self.cache([{"slug": "gpt-6.1-sol", "context_window": 272000, "max_context_window": 872000}])
        refresh_codex()
        backend = CodexBackend(default_config("goal"))
        with patch.dict(os.environ, {"HUNTUN_CODEX_CONTEXT": "500000"}):
            self.assertEqual(context_settings("gpt-6.1-sol"), (500000, 450000))
            args = backend._base_args(self.home, "gpt-6.1-sol", "high", "read-only", None)
            self.assertIn("model_context_window=500000", args)
        with patch.dict(os.environ, {"HUNTUN_CODEX_CONTEXT": "2000000"}):
            self.assertEqual(context_settings("gpt-6.1-sol"), (872000, 784800))
        for cap in ("0", "-1", "invalid"):
            with patch.dict(os.environ, {"HUNTUN_CODEX_CONTEXT": cap}):
                with self.assertRaisesRegex(ValueError, "positive integer"):
                    context_settings("gpt-6.1-sol")

    def test_implicit_default_is_pinned_to_the_model_whose_maximum_is_requested(self) -> None:
        self.cache([{"slug": "older", "priority": 5, "context_window": 272000, "max_context_window": 400000},
                    {"slug": "gpt-6.1-sol", "priority": 1, "context_window": 272000, "max_context_window": 872000}])
        refresh_codex()
        backend = CodexBackend(default_config("goal"))
        args = backend._base_args(self.home, "", "high", "read-only", None)
        self.assertEqual(args[args.index("-m") + 1], "gpt-6.1-sol")
        self.assertIn("model_context_window=872000", args)
        self.assertEqual(context_settings(""), (872000, 784800))

    def test_existing_agent_info_ignores_stale_saved_default_window(self) -> None:
        from huntun.config import save_config, save_team
        from huntun.orchestrator import AgentRuntime, Orchestrator
        from huntun.types import AgentSpec
        self.cache([{"slug": "gpt-6.1-sol", "context_window": 272000, "max_context_window": 872000}])
        cfg = default_config("goal")
        cfg.backend = "codex"
        spec = AgentSpec("master", "master", "Master", "Lead", model="gpt-6.1-sol", backend="codex")
        save_config(self.home, cfg)
        save_team(self.home, [spec])
        orch = Orchestrator(self.home)
        try:
            rt = AgentRuntime(orch, spec)
            rt.memory.state.context_limit = 272000
            rt.memory.state.session_id = "existing-session"
            self.assertEqual(rt.info()["context_limit"], 872000)
            self.assertEqual(rt.memory.state.session_id, "existing-session")
        finally:
            orch.store.close()

    def test_existing_hidden_model_and_configured_default_keep_maximum_metadata(self) -> None:
        self.cache([{"slug": "existing-gpt", "visibility": "hide", "context_window": 272000, "max_context_window": 872000},
                    {"slug": "offered", "context_window": 123000}])
        (self.home / "config.toml").write_text('model = "existing-gpt"\n')
        with patch.dict(os.environ, {"HUNTUN_CODEX_MODELS": "offered"}):
            self.assertEqual([m.id for m in refresh_codex()], ["offered"])
            self.assertEqual(context_settings("existing-gpt"), (872000, 784800))
            args = CodexBackend(default_config("goal"))._base_args(self.home, "", "high", "read-only", None)
            self.assertEqual(args[args.index("-m") + 1], "existing-gpt")
            self.assertIn("model_context_window=872000", args)
