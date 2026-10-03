"""Unrestricted Codex tools apply to every role and saved conversation."""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest.mock import patch

from huntun.backends.codex import CodexBackend
from huntun.config import default_config


class CodexPermissionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.env = patch.dict(os.environ, {"CODEX_HOME": str(self.root), "HUNTUN_CODEX_BIN": "fake-codex"})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.backend = CodexBackend(default_config("goal"))

    def test_all_roles_and_resumed_threads_have_unrestricted_native_tools(self) -> None:
        for old_mode in ("read-only", "workspace-write", "danger-full-access"):
            base = self.backend._base_args(self.root, "gpt-6.1-sol", "high", old_mode, "http://localhost:1234/mcp")
            resumed = self.backend._resume_args(base, "old-restricted-thread")
            for args in (base, resumed):
                cfg = tomllib.loads("\n".join(args[i + 1] for i, arg in enumerate(args) if arg == "-c"))
                self.assertIn("--dangerously-bypass-approvals-and-sandbox", args)
                self.assertIn("--ignore-rules", args)
                self.assertEqual(cfg["approval_policy"], "never")
                self.assertEqual(cfg["shell_environment_policy"]["inherit"], "all")
                self.assertTrue(cfg["shell_environment_policy"]["ignore_default_excludes"])
                self.assertEqual(cfg["shell_environment_policy"]["exclude"], [])
                self.assertNotIn("permissions", cfg)
                self.assertNotIn("default_permissions", cfg)
                self.assertEqual(cfg["web_search"], "live")
                self.assertEqual(cfg["mcp_servers"]["huntun"]["default_tools_approval_mode"], "approve")
            self.assertNotIn("-C", resumed)

    def test_legacy_network_toggle_cannot_restrict_new_policy(self) -> None:
        with patch.dict(os.environ, {"HUNTUN_CODEX_NETWORK": "0"}):
            self.assertIn("--dangerously-bypass-approvals-and-sandbox",
                          self.backend._base_args(self.root, "", "low", "read-only", None))

    @unittest.skipUnless(os.environ.get("HUNTUN_TEST_CODEX_SANDBOX") == "1", "opt-in native Codex CLI compatibility")
    def test_native_cli_accepts_fresh_and_resume_flags_without_a_model_call(self) -> None:
        binary = shutil.which("codex")
        self.assertIsNotNone(binary)
        base = self.backend._base_args(self.root, "gpt-6.1-sol", "high", "read-only", None)
        base[0] = binary
        for args in (base, self.backend._resume_args(base, "00000000-0000-0000-0000-000000000000")):
            result = subprocess.run([*args, "--help"], capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
