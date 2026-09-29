"""Sign-in for the web app: one account, set from Settings on the Projects page.

With no password set (the default) the web app opens without a sign-in page. The account lives in ~/.huntun/auth.json,
readable by its owner only: the user name, the password as a salted PBKDF2-HMAC-SHA256 hash (never the password), and a
random secret that signs the session cookies. Saving or removing the password replaces the secret, which signs every
browser out. Anyone with access to the server's files can remove the password: `huntun auth reset`.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

ALGORITHM = "pbkdf2_sha256"
ITERATIONS = 600_000                                                              # OWASP's figure for PBKDF2-HMAC-SHA256
SESSION_SEC = 30 * 86400                                                          # how long a sign-in lasts in one browser
MAX_FAILURES = 5                                                                  # wrong passwords in a row from one address...
LOCKOUT_SEC = 60                                                                  # ...lock sign-in there for this long
_cache: tuple[Any, dict[str, str] | None] = (None, None)
_failures: dict[str, tuple[int, float]] = {}                                      # client address -> (wrong passwords in a row, locked until)
_lock = threading.Lock()


def auth_file() -> Path:
    return Path(os.environ.get("HUNTUN_HOME") or Path.home() / ".huntun") / "auth.json"


def hash_password(password: str, iterations: int = 0) -> str:
    """ "pbkdf2_sha256$<iterations>$<salt hex>$<hash hex>"."""
    n = iterations or ITERATIONS
    salt = secrets.token_bytes(16)
    return f"{ALGORITHM}${n}${salt.hex()}${hashlib.pbkdf2_hmac('sha256', password.encode(), salt, n).hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algorithm, n, salt, digest = stored.split("$")
        if algorithm != ALGORITHM:
            return False
        got = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), int(n)).hex()
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(got, digest)


def account() -> dict[str, str] | None:
    """The saved account ({"username", "password" (the hash), "secret"}), or None when no password is set.

    A file that cannot be read still counts as set, with nothing matching it ({}): a damaged file locks the web app
    rather than opening it, until `huntun auth reset` removes it. Re-read whenever the file changes, so a reset takes
    effect in a running server.
    """
    global _cache
    f = auth_file()
    try:
        st = f.stat()
    except FileNotFoundError:
        return None
    except OSError:
        return {}
    key = (str(f), st.st_mtime_ns, st.st_size)
    if key != _cache[0]:
        try:
            data = json.loads(f.read_text())
        except (OSError, ValueError):
            data = None
        ok = isinstance(data, dict) and all(isinstance(data.get(k), str) and data[k] for k in ("username", "password", "secret"))
        _cache = (key, {k: data[k] for k in ("username", "password", "secret")} if ok and isinstance(data, dict) else {})
    return _cache[1]


def enabled() -> bool:
    return account() is not None


def clean_username(username: str) -> str:
    name = username.strip()
    if not name:
        raise ValueError("a user name is required")
    if len(name) > 64 or any(ord(c) < 32 or c == "\x7f" for c in name):
        raise ValueError("the user name must be at most 64 characters, with no control characters")
    return name


def save(username: str, password: str | None) -> dict[str, str]:
    """Sets the account. password None keeps the saved one (a new user name only). A new secret signs everyone out."""
    global _cache
    name = clean_username(username)
    current = account()
    if password is None:
        if not current:
            raise ValueError("a password is required")
        hashed = current["password"]
    else:
        if not password:
            raise ValueError("the password cannot be empty")
        hashed = hash_password(password)
    data = {"username": name, "password": hashed, "secret": secrets.token_hex(32)}
    f = auth_file()
    f.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=f.parent, prefix=".auth-", suffix=".tmp")    # created owner-only
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(json.dumps(data, indent=2))
        os.replace(tmp, f)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    _cache = (None, None)
    return data


def reset() -> bool:
    """Removes the password: the web app opens without signing in again. False when none was set."""
    global _cache
    _cache = (None, None)
    try:
        auth_file().unlink()
    except FileNotFoundError:
        return False
    return True


def check_login(username: str, password: str) -> bool:
    acct = account()
    if not acct:
        return False
    name_ok = hmac.compare_digest(username.strip().encode(), acct["username"].encode())
    return verify_password(password, acct["password"]) and name_ok       # the hash runs either way: no timing hint about the name


def _signature(acct: dict[str, str], payload: str) -> str:
    return hmac.new(acct["secret"].encode(), f"{acct['username']}\n{payload}".encode(), hashlib.sha256).hexdigest()


def new_session(acct: dict[str, str], now: float | None = None) -> str:
    """A cookie value: "<expiry>.<nonce>.<signature>". Nothing is kept on the server; the secret checks it."""
    payload = f"{int((now or time.time()) + SESSION_SEC)}.{secrets.token_hex(8)}"
    return f"{payload}.{_signature(acct, payload)}"


def valid_session(acct: dict[str, str] | None, token: str, now: float | None = None) -> bool:
    if not acct or not token:
        return False
    try:
        expiry, nonce, sig = token.split(".")
        if int(expiry) <= (now or time.time()):
            return False
    except ValueError:
        return False
    return hmac.compare_digest(sig, _signature(acct, f"{expiry}.{nonce}"))


def locked_for(client: str) -> int:
    """Seconds until this address may try a password again (0: now)."""
    with _lock:
        _, until = _failures.get(client, (0, 0.0))
    return max(0, int(until - time.time() + 0.999))


def note_attempt(client: str, ok: bool) -> None:
    with _lock:
        if ok:
            _failures.pop(client, None)
            return
        count, _ = _failures.get(client, (0, 0.0))
        count += 1
        _failures[client] = (0, time.time() + LOCKOUT_SEC) if count >= MAX_FAILURES else (count, 0.0)
