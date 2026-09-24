"""Which Claude Code binary the Claude Code backend runs: the newer of the SDK's bundled copy and `claude` on PATH, or HUNTUN_CLAUDE_BIN."""
from __future__ import annotations

import os
import stat
import tempfile
import unittest
from pathlib import Path

from huntun.backends.claude_code import _cli_version, claude_cli_path


def _fake(dir_: Path, name: str, version: str) -> Path:
    p = dir_ / name
    p.write_text(f"#!/bin/sh\necho '{version} (Claude Code)'\n")
    p.chmod(p.stat().st_mode | stat.S_IEXEC)
    return p


class ClaudeCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        os.environ.pop("HUNTUN_CLAUDE_BIN", None)

    def tearDown(self) -> None:
        os.environ.pop("HUNTUN_CLAUDE_BIN", None)
        self.tmp.cleanup()

    def test_newer_system_cli_wins_over_the_bundled_copy(self) -> None:
        bundled, system = _fake(self.dir, "bundled", "2.1.273"), _fake(self.dir, "system", "2.1.281")
        self.assertEqual(_cli_version(str(system)), (2, 1, 281))
        self.assertEqual(claude_cli_path(bundled, str(system)), str(system))

    def test_bundled_copy_is_kept_when_it_is_as_new(self) -> None:
        bundled, system = _fake(self.dir, "bundled", "2.1.281"), _fake(self.dir, "system", "2.1.281")
        self.assertIsNone(claude_cli_path(bundled, str(system)))
        older = _fake(self.dir, "older", "2.0.9")
        self.assertIsNone(claude_cli_path(bundled, str(older)))

    def test_missing_pieces(self) -> None:
        system = _fake(self.dir, "system", "2.1.281")
        self.assertEqual(claude_cli_path(self.dir / "absent", str(system)), str(system))   # no bundled copy: PATH it is
        self.assertIsNone(claude_cli_path(_fake(self.dir, "bundled", "2.1.281"), ""))      # nothing on PATH: SDK default
        self.assertEqual(_cli_version(str(self.dir / "absent")), ())

    def test_override(self) -> None:
        os.environ["HUNTUN_CLAUDE_BIN"] = "/opt/claude"
        self.assertEqual(claude_cli_path(self.dir / "absent", ""), "/opt/claude")


if __name__ == "__main__":
    unittest.main()
