"""Real Pi auth resolution against synthetic native CLI credentials only."""

import shutil
import subprocess
import unittest
import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

from huntun import pi


@unittest.skipUnless(
    shutil.which("node") and pi.sdk_entry(),
    "install Pi to verify its native auth runtime",
)
class PiAuthTests(unittest.TestCase):
    def test_native_login_discovery_refresh_and_precedence(self):
        result = subprocess.run(
            [
                "node",
                str(Path(__file__).parent / "js/pi_auth_check.mjs"),
                pi.sdk_entry().as_uri(),
            ],
            capture_output=True,
            text=True,
            timeout=40,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Pi CLI authentication checks passed", result.stdout)

    @unittest.skipUnless(
        pi.claude_extension(), "install the Claude CLI adapter to verify discovery"
    )
    def test_claude_cli_catalog_launcher_and_logout(self):
        extension = pi.claude_extension()
        flags = [
            "--print",
            "--setting-sources",
            "--settings",
            "--disable-slash-commands",
            "--permission-mode",
            "--no-chrome",
            "--prompt-suggestions",
            "--output-format",
            "--input-format",
            "--include-partial-messages",
            "--verbose",
            "--no-session-persistence",
            "--strict-mcp-config",
            "--mcp-config",
            "--tools",
            "--allowedTools",
            "--model",
            "--effort",
            "--system-prompt",
        ]
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(os.environ, {"ANTHROPIC_API_KEY": "unrelated-test-key"}),
        ):
            root = Path(directory)
            home = root / "pi"
            home.mkdir()
            native = root / "claude CLI fixture"
            native.write_text(
                f"#!{sys.executable}\n"
                + "import json,sys\nfrom pathlib import Path\n"
                + f"root=Path({str(root)!r})\n"
                + "a=sys.argv[1:]\n"
                + "if a==['--version']: print('2.1.286')\n"
                + f"elif a==['--help']: print({chr(10).join(flags)!r})\n"
                + "elif a==['auth','status']: print(json.dumps({'loggedIn':not (root/'logout').exists(),'authMethod':'claude.ai','apiProvider':'firstParty','subscriptionType':'max'}))\n"
                + "elif '--settings' in a: print(json.dumps(json.loads(a[a.index('--settings')+1])))\n"
                + "else: raise SystemExit('Unexpected inference call in auth fixture')\n"
            )
            native.chmod(0o700)
            with patch.dict(
                os.environ,
                {
                    # Other suites and developer shells may provide API credentials.
                    # This fixture verifies only its synthetic Claude CLI login.
                    **{key: "" for key in os.environ
                       if key.endswith(("_API_KEY", "_OAUTH_TOKEN"))},
                    "PI_CODING_AGENT_DIR": str(home),
                    "CODEX_HOME": str(root / "no-codex-login"),
                    "PI_CLAUDE_CODE_PROVIDER_PATH": str(native),
                    "HUNTUN_PI_CLAUDE_EXTENSION": str(extension),
                    "HUNTUN_PI_MODELS": "",
                    "HUNTUN_PI_REUSE_CLI_AUTH": "1",
                    "PI_OFFLINE": "1",
                },
            ):
                rows = pi.model_rows()
                self.assertEqual(
                    {r["provider"] for r in rows}, {"pi-claude-code-provider"}
                )
                self.assertEqual(
                    {r["id"] for r in rows}, {"sonnet", "opus", "fable", "haiku"}
                )
                launcher = pi.claude_launcher(root)
                result = subprocess.run(
                    [str(launcher), "auth", "status"], capture_output=True, text=True
                )
                self.assertTrue(json.loads(result.stdout)["loggedIn"])
                settings = {
                    "disableAllHooks": True,
                    "enabledPlugins": {"original@fixture": False},
                }
                result = subprocess.run(
                    [str(launcher), "--print", "--settings", json.dumps(settings)],
                    capture_output=True,
                    text=True,
                )
                patched = json.loads(result.stdout)
                self.assertFalse(patched["enabledPlugins"]["plugin-authoring@builtin"])
                self.assertFalse(patched["enabledPlugins"]["original@fixture"])
                self.assertTrue(patched["disableAllHooks"])
                (root / "logout").touch()
                with patch.object(pi, "_checked_at", 0):
                    self.assertEqual(pi.model_rows(), [])
                with patch.dict(os.environ, {"HUNTUN_PI_REUSE_CLI_AUTH": "0"}):
                    self.assertEqual(pi.model_rows(), [])
                    self.assertIsNone(pi.claude_launcher(root))
