"""The web app's sign-in: the password hash, session cookies, the sign-in gate over HTTP, the Settings calls and `huntun auth reset`."""
from __future__ import annotations

import contextlib
import http.client
import io
import json
import os
import stat
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from huntun import auth, cli
from huntun.server import bind_address

try:
    from test_hub import HubServer  # when discovered with -s tests
except ImportError:  # pragma: no cover
    from tests.test_hub import HubServer


class AuthHome(unittest.TestCase):
    """Every test gets its own HUNTUN_HOME and a fast hash (the stored hash carries its own iteration count)."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = mock.patch.dict(os.environ, {"HUNTUN_HOME": self.tmp.name})
        env.start()
        self.addCleanup(env.stop)
        for name, value in (("ITERATIONS", 1000), ("_failures", {}), ("_cache", (None, None))):
            p = mock.patch.object(auth, name, value)
            p.start()
            self.addCleanup(p.stop)


class PasswordAndSessionTests(AuthHome):
    def test_the_password_is_stored_as_a_salted_sha256_hash(self) -> None:
        stored = auth.hash_password("hunter2")
        algorithm, n, salt, digest = stored.split("$")
        self.assertEqual((algorithm, n, len(salt), len(digest)), ("pbkdf2_sha256", "1000", 32, 64))
        self.assertNotEqual(stored, auth.hash_password("hunter2"))                  # a new salt every time
        self.assertTrue(auth.verify_password("hunter2", stored))
        self.assertFalse(auth.verify_password("hunter3", stored))
        self.assertFalse(auth.verify_password("hunter2", stored.replace("pbkdf2_sha256", "md5")))
        self.assertFalse(auth.verify_password("hunter2", "garbage"))

    def test_no_file_means_no_sign_in_and_a_damaged_one_locks_it(self) -> None:
        self.assertIsNone(auth.account())
        self.assertFalse(auth.enabled())
        auth.auth_file().write_text("{not json")
        self.assertEqual(auth.account(), {})                                       # set, with nothing matching it
        self.assertTrue(auth.enabled())
        self.assertFalse(auth.check_login("", ""))
        self.assertTrue(auth.reset())
        self.assertIsNone(auth.account())
        self.assertFalse(auth.reset())
        with mock.patch.object(Path, "stat", side_effect=PermissionError("denied")):
            self.assertEqual(auth.account(), {})                                   # cannot even look: locked too

    def test_save_keeps_only_the_hash_owner_only_and_renaming_keeps_the_password(self) -> None:
        with self.assertRaises(ValueError):
            auth.save("admin", None)                                               # the first save needs a password
        with self.assertRaises(ValueError):
            auth.save("  ", "pw")
        first = auth.save(" admin ", "s3cret-pw")
        raw = auth.auth_file().read_text()
        self.assertNotIn("s3cret-pw", raw)
        self.assertEqual(stat.S_IMODE(auth.auth_file().stat().st_mode), 0o600)
        self.assertEqual(auth.account(), {k: first[k] for k in ("username", "password", "secret")})
        self.assertEqual(first["username"], "admin")
        self.assertTrue(auth.check_login("admin", "s3cret-pw"))
        self.assertFalse(auth.check_login("Admin", "s3cret-pw"))
        self.assertFalse(auth.check_login("admin", "wrong"))
        renamed = auth.save("ziwei", None)
        self.assertEqual(renamed["password"], first["password"])
        self.assertNotEqual(renamed["secret"], first["secret"])
        self.assertTrue(auth.check_login("ziwei", "s3cret-pw"))

    def test_sessions_expire_and_die_with_the_secret(self) -> None:
        acct = auth.save("admin", "pw")
        token = auth.new_session(acct)
        self.assertTrue(auth.valid_session(acct, token))
        self.assertFalse(auth.valid_session(acct, token, now=time.time() + auth.SESSION_SEC + 5))
        expiry, nonce, sig = token.split(".")
        self.assertFalse(auth.valid_session(acct, f"{int(expiry) + 999}.{nonce}.{sig}"))   # a longer life is not signed
        self.assertFalse(auth.valid_session(acct, "junk"))
        self.assertFalse(auth.valid_session(None, token))
        self.assertFalse(auth.valid_session(auth.save("admin", "pw2"), token))     # a new password signs everyone out

    def test_wrong_passwords_lock_one_address_for_a_while(self) -> None:
        for _ in range(auth.MAX_FAILURES - 1):
            auth.note_attempt("10.0.0.9", False)
        self.assertEqual(auth.locked_for("10.0.0.9"), 0)
        auth.note_attempt("10.0.0.9", False)
        self.assertGreater(auth.locked_for("10.0.0.9"), 0)
        self.assertEqual(auth.locked_for("10.0.0.10"), 0)
        auth.note_attempt("10.0.0.10", False)
        auth.note_attempt("10.0.0.10", True)                                      # a right password clears the count
        self.assertNotIn("10.0.0.10", auth._failures)

    def test_bind_address(self) -> None:
        with mock.patch.dict(os.environ, {"HUNTUN_BIND": ""}):
            self.assertEqual(bind_address(), "127.0.0.1")
        with mock.patch.dict(os.environ, {"HUNTUN_BIND": " 0.0.0.0 "}):
            self.assertEqual(bind_address(), "0.0.0.0")


class SignInOverHttpTests(AuthHome):
    def setUp(self) -> None:
        super().setUp()
        self.srv = HubServer(Path(self.tmp.name))
        self.addCleanup(self.srv.server.server_close)
        self.addCleanup(self.srv.close)
        self.port = self.srv.server.server_address[1]
        self.cookie_name = f"huntun_session_{self.port}"

    def req(self, path: str, body: dict | None = None, cookie: str = "") -> tuple[int, Any, str]:
        """(status, JSON body or page text, the session cookie the response set, "" when it cleared it, None when it left it)."""
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        headers = {"content-type": "application/json"} if body is not None else {}
        if cookie:
            headers["cookie"] = f"{self.cookie_name}={cookie}"
        conn.request("POST" if body is not None else "GET", path, json.dumps(body) if body is not None else None, headers)
        r = conn.getresponse()
        text = r.read().decode()
        set_cookie = r.getheader("set-cookie")
        conn.close()
        got = None
        if set_cookie:
            self.assertIn("HttpOnly", set_cookie)
            self.assertIn("SameSite=Lax", set_cookie)
            got = set_cookie.split(";", 1)[0].split("=", 1)[1]
        data = json.loads(text) if r.getheader("content-type", "").startswith("application/json") else text
        return r.status, data, got

    def test_without_a_password_nothing_asks_to_sign_in(self) -> None:
        code, page, _ = self.req("/")
        self.assertEqual(code, 200)
        self.assertIn("🔒 Settings", page)                                        # the web app, not the sign-in page
        self.assertEqual(self.req("/api/auth")[1], {"enabled": False, "signed_in": True, "username": None, "reset_command": "huntun auth reset"})
        self.assertEqual(self.req("/api/workspaces")[0], 200)
        self.assertEqual(self.req("/api/auth/login", {"username": "x", "password": "y"})[0], 200)   # nothing to sign in to

    def test_the_whole_sign_in_flow(self) -> None:
        # the first password needs no current one; this browser stays signed in
        code, view, cookie = self.req("/api/auth/account", {"username": "admin", "password": "pw-1"})
        self.assertEqual(code, 200, view)
        self.assertEqual((view["enabled"], view["username"]), (True, "admin"))
        self.assertTrue(cookie)
        self.assertEqual(self.req("/api/workspaces", cookie=cookie)[0], 200)

        # another browser: the sign-in page, and every API call refused
        code, page, _ = self.req("/")
        self.assertEqual(code, 200)
        self.assertIn('id="login"', page)
        self.assertNotIn("🔒 Settings", page)
        self.assertEqual(self.req("/api/workspaces")[:2], (401, {"error": "sign in first", "login": True}))
        self.assertEqual(self.req("/api/providers/servers/delete", {"id": "x"})[0], 401)
        self.assertEqual(self.req("/api/auth/account", {"username": "evil", "password": "mine"})[0], 401)   # cannot take the account over
        self.assertEqual(self.req("/api/auth/remove", {})[0], 401)
        self.assertEqual(self.req("/api/auth", cookie="1.2.3")[1]["signed_in"], False)

        code, err, cookie2 = self.req("/api/auth/login", {"username": "admin", "password": "nope"})
        self.assertEqual((code, err["error"], cookie2), (401, "wrong user name or password", None))
        code, view, cookie2 = self.req("/api/auth/login", {"username": "admin", "password": "pw-1"})
        self.assertEqual((code, view["signed_in"], view["username"]), (200, True, "admin"))
        self.assertIn("🔒 Settings", self.req("/", cookie=cookie2)[1])
        self.assertEqual(self.req("/api/providers/servers/delete", {"id": "x"}, cookie=cookie2)[0], 404)   # through the gate

        # changing the password takes the current one; a wrong one is a 403, which does not sign the page out
        code, err, _ = self.req("/api/auth/account", {"username": "admin", "password": "pw-2", "current_password": "bad"}, cookie=cookie2)
        self.assertEqual((code, err), (403, {"error": "the current password is wrong"}))
        code, view, cookie3 = self.req("/api/auth/account", {"username": "admin", "password": "pw-2", "current_password": "pw-1"}, cookie=cookie2)
        self.assertEqual(code, 200, view)
        self.assertEqual(self.req("/api/workspaces", cookie=cookie2)[0], 401)       # every other browser is signed out
        self.assertEqual(self.req("/api/workspaces", cookie=cookie3)[0], 200)
        self.assertEqual(self.req("/api/auth/login", {"username": "admin", "password": "pw-1"})[0], 401)

        # a new user name alone keeps the password
        code, view, cookie4 = self.req("/api/auth/account", {"username": "ziwei", "password": "", "current_password": "pw-2"}, cookie=cookie3)
        self.assertEqual((code, view["username"]), (200, "ziwei"))
        self.assertEqual(self.req("/api/auth/login", {"username": "ziwei", "password": "pw-2"})[0], 200)
        self.assertEqual(self.req("/api/auth/account", {"username": "", "current_password": "pw-2"}, cookie=cookie4)[0], 400)

        # signing out clears the cookie
        code, view, cleared = self.req("/api/auth/logout", {}, cookie=cookie4)
        self.assertEqual((code, view["signed_in"], cleared), (200, False, ""))

        # removing the password takes the current one too; then the web app opens for anyone
        self.assertEqual(self.req("/api/auth/remove", {"current_password": "pw-1"}, cookie=cookie4)[0], 403)
        code, view, cleared = self.req("/api/auth/remove", {"current_password": "pw-2"}, cookie=cookie4)
        self.assertEqual((code, view["enabled"], cleared), (200, False, ""))
        self.assertFalse(auth.auth_file().exists())
        self.assertEqual(self.req("/api/workspaces")[0], 200)

    def test_too_many_wrong_passwords_lock_sign_in(self) -> None:
        auth.save("admin", "pw")
        codes = [self.req("/api/auth/login", {"username": "admin", "password": f"guess-{i}"})[0] for i in range(auth.MAX_FAILURES)]
        self.assertEqual(codes, [401] * (auth.MAX_FAILURES - 1) + [429])
        code, err, cookie = self.req("/api/auth/login", {"username": "admin", "password": "pw"})   # even the right one waits
        self.assertEqual((code, cookie), (429, None))
        self.assertIn("try again in", err["error"])

    def test_reset_from_the_command_line_opens_a_running_server(self) -> None:
        auth.save("admin", "pw")
        self.assertEqual(self.req("/api/workspaces")[0], 401)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli.main(["auth", "status"])
            cli.main(["auth", "reset"])
            cli.main(["auth", "reset"])
            cli.main(["auth", "status"])
        lines = out.getvalue().splitlines()
        self.assertIn("Sign-in is on for user 'admin'", lines[0])
        self.assertTrue(lines[1].startswith("Password removed"))
        self.assertTrue(lines[2].startswith("No password was set"))
        self.assertTrue(lines[3].startswith("No password set"))
        self.assertEqual(self.req("/api/workspaces")[0], 200)
        code, page, _ = self.req("/")
        self.assertEqual(code, 200)
        self.assertIn("🔒 Settings", page)

    def test_a_damaged_file_locks_the_web_app_until_reset(self) -> None:
        auth.auth_file().write_text("{}")
        self.assertEqual(self.req("/api/workspaces")[0], 401)
        self.assertIn('id="login"', self.req("/")[1])
        self.assertEqual(self.req("/api/auth/login", {"username": "", "password": ""})[0], 401)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli.main(["auth", "status"])
        self.assertIn("cannot be read", out.getvalue())


if __name__ == "__main__":
    unittest.main()
