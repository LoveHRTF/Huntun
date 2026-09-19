from __future__ import annotations

import re
import sqlite3
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .config import now_iso
from .types import InboxItem

MENTION_RE = re.compile(r"(^|[^\w@])@([a-z0-9][a-z0-9-]*)", re.IGNORECASE)

SCHEMA = """
CREATE TABLE IF NOT EXISTS threads (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  author TEXT NOT NULL, title TEXT NOT NULL, body TEXT NOT NULL,
  commit_sha TEXT, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS comments (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  thread_id INTEGER NOT NULL REFERENCES threads(id),
  author TEXT NOT NULL, body TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS mentions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  target TEXT NOT NULL, kind TEXT NOT NULL, ref_id INTEGER NOT NULL,
  thread_id INTEGER NOT NULL, author TEXT NOT NULL,
  seen INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS mentions_target ON mentions(target, seen);
CREATE TABLE IF NOT EXISTS control (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  agent TEXT NOT NULL, kind TEXT NOT NULL, detail TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS agent_status (
  name TEXT PRIMARY KEY, status TEXT NOT NULL, task TEXT NOT NULL DEFAULT '', last_active TEXT);
"""


def parse_mentions(text: str) -> list[str]:
    out: list[str] = []
    for m in MENTION_RE.finditer(text):
        name = m.group(2).lower()
        if name not in out:
            out.append(name)
    return out


class Store:
    """The discussion board and control plane: one SQLite file shared by the orchestrator,
    the web server thread, and the CLI running in another process (pause / resume)."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), check_same_thread=False, timeout=10, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self._listeners: dict[str, list[Callable[..., None]]] = {}
        with self._lock:
            self._db.execute("PRAGMA journal_mode = WAL")
            self._db.execute("PRAGMA busy_timeout = 5000")
            self._db.executescript(SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._db.close()

    # ---- events ----------------------------------------------------------------

    def on(self, event: str, fn: Callable[..., None]) -> None:
        self._listeners.setdefault(event, []).append(fn)

    def _emit(self, event: str, *args: Any) -> None:
        for fn in self._listeners.get(event, []):
            try:
                fn(*args)
            except Exception:  # listeners must never break a write
                pass

    def _q(self, sql: str, *params: Any) -> list[sqlite3.Row]:
        with self._lock:
            return self._db.execute(sql, params).fetchall()

    def _one(self, sql: str, *params: Any) -> sqlite3.Row | None:
        with self._lock:
            return self._db.execute(sql, params).fetchone()

    def _exec(self, sql: str, *params: Any) -> int:
        with self._lock:
            cur = self._db.execute(sql, params)
            return int(cur.lastrowid or 0)

    # ---- control ---------------------------------------------------------------

    def get_control(self, key: str, default: str) -> str:
        row = self._one("SELECT value FROM control WHERE key = ?", key)
        return row["value"] if row else default

    def set_control(self, key: str, value: str) -> None:
        self._exec("INSERT INTO control(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value", key, value)

    def is_running(self) -> bool:
        return self.get_control("running", "0") == "1"

    def set_running(self, running: bool, by: str = "human") -> None:
        self.set_control("running", "1" if running else "0")
        self.log_event(by, "start" if running else "pause", "All agents started" if running else "All agents paused")
        self._emit("control", running)

    # ---- threads / comments ----------------------------------------------------

    def create_thread(self, author: str, title: str, body: str, commit_sha: str | None = None) -> dict[str, Any]:
        now = now_iso()
        tid = self._exec("INSERT INTO threads(author, title, body, commit_sha, created_at) VALUES(?, ?, ?, ?, ?)", author, title, body, commit_sha, now)
        self._record_mentions(author, "thread", tid, tid, f"{title}\n{body}")
        self.log_event(author, "thread", f"#{tid} {title}")
        self._emit("thread", tid)
        return self.get_thread(tid) or {}

    def add_comment(self, thread_id: int, author: str, body: str) -> dict[str, Any]:
        thread = self.get_thread(thread_id)
        if not thread:
            raise ValueError(f"Thread #{thread_id} does not exist")
        now = now_iso()
        cid = self._exec("INSERT INTO comments(thread_id, author, body, created_at) VALUES(?, ?, ?, ?)", thread_id, author, body, now)
        self._record_mentions(author, "comment", cid, thread_id, body)
        if thread["author"] not in (author, "human"):
            self._insert_mention(thread["author"], "comment", cid, thread_id, author, now)
        self.log_event(author, "comment", f"#{thread_id}: {body[:120]}")
        self._emit("comment", thread_id, cid)
        return {"id": cid, "thread_id": thread_id, "author": author, "body": body, "created_at": now}

    def _record_mentions(self, author: str, kind: str, ref_id: int, thread_id: int, text: str) -> None:
        now = now_iso()
        for target in parse_mentions(text):
            if target != author:
                self._insert_mention(target, kind, ref_id, thread_id, author, now)

    def _insert_mention(self, target: str, kind: str, ref_id: int, thread_id: int, author: str, now: str) -> None:
        if self._one("SELECT id FROM mentions WHERE target = ? AND kind = ? AND ref_id = ?", target, kind, ref_id):
            return
        self._exec("INSERT INTO mentions(target, kind, ref_id, thread_id, author, seen, created_at) VALUES(?, ?, ?, ?, ?, 0, ?)", target, kind, ref_id, thread_id, author, now)
        self._emit("mention", target)

    def get_thread(self, tid: int) -> dict[str, Any] | None:
        row = self._one("SELECT * FROM threads WHERE id = ?", tid)
        return dict(row) if row else None

    def get_comments(self, tid: int) -> list[dict[str, Any]]:
        return [dict(r) for r in self._q("SELECT * FROM comments WHERE thread_id = ? ORDER BY id ASC", tid)]

    def list_threads(self, limit: int = 50) -> list[dict[str, Any]]:
        return [
            dict(r)
            for r in self._q(
                """SELECT t.*, COUNT(c.id) AS comment_count, COALESCE(MAX(c.created_at), t.created_at) AS last_activity
                   FROM threads t LEFT JOIN comments c ON c.thread_id = t.id
                   GROUP BY t.id ORDER BY last_activity DESC, t.id DESC LIMIT ?""",
                limit,
            )
        ]

    def last_comments(self, thread_id: int, n: int = 2) -> list[dict[str, Any]]:
        rows = self._q("SELECT * FROM comments WHERE thread_id = ? ORDER BY id DESC LIMIT ?", thread_id, n)
        return [dict(r) for r in reversed(rows)]

    def take_inbox(self, agent: str) -> list[InboxItem]:
        """Unseen mentions of `agent` (or @all) not authored by the agent itself, plus replies on its threads. Marks them seen."""
        rows = self._q("SELECT * FROM mentions WHERE (target = ? OR target = 'all') AND author != ? AND seen = 0 ORDER BY id ASC", agent, agent)
        if not rows:
            return []
        items: list[InboxItem] = []
        for m in rows:
            thread = self.get_thread(m["thread_id"])
            if not thread:
                continue
            if m["kind"] == "thread":
                items.append(InboxItem("mention", thread["id"], thread["title"], None, m["author"], thread["body"], m["created_at"]))
            else:
                c = self._one("SELECT * FROM comments WHERE id = ?", m["ref_id"])
                if not c:
                    continue
                names = parse_mentions(c["body"])
                kind = "mention" if (agent in names or "all" in names) else "reply"
                items.append(InboxItem(kind, thread["id"], thread["title"], c["id"], c["author"], c["body"], c["created_at"]))
        ids = [r["id"] for r in rows]
        self._exec(f"UPDATE mentions SET seen = 1 WHERE id IN ({','.join('?' * len(ids))})", *ids)
        return items

    def peek_inbox_count(self, agent: str) -> int:
        row = self._one("SELECT COUNT(*) AS n FROM mentions WHERE (target = ? OR target = 'all') AND author != ? AND seen = 0", agent, agent)
        return int(row["n"]) if row else 0

    def new_comments_since(self, agent: str, since_id: int, limit: int = 30) -> list[dict[str, Any]]:
        """Comments newer than since_id on threads the agent started or commented on."""
        return [
            dict(r)
            for r in self._q(
                """SELECT c.*, t.title FROM comments c JOIN threads t ON t.id = c.thread_id
                   WHERE c.id > ? AND c.author != ?
                     AND (t.author = ? OR c.thread_id IN (SELECT thread_id FROM comments WHERE author = ?))
                   ORDER BY c.id ASC LIMIT ?""",
                since_id, agent, agent, agent, limit,
            )
        ]

    def max_comment_id(self) -> int:
        row = self._one("SELECT COALESCE(MAX(id), 0) AS m FROM comments")
        return int(row["m"]) if row else 0

    # ---- events / status -------------------------------------------------------

    def log_event(self, agent: str, kind: str, detail: str) -> None:
        self._exec("INSERT INTO events(agent, kind, detail, created_at) VALUES(?, ?, ?, ?)", agent, kind, detail[:2000], now_iso())

    def list_events(self, since_id: int = 0, limit: int = 100) -> list[dict[str, Any]]:
        return [dict(r) for r in self._q("SELECT * FROM events WHERE id > ? ORDER BY id DESC LIMIT ?", since_id, limit)]

    def set_agent_status(self, name: str, status: str, task: str = "") -> None:
        self._exec(
            """INSERT INTO agent_status(name, status, task, last_active) VALUES(?, ?, ?, ?)
               ON CONFLICT(name) DO UPDATE SET status = excluded.status, task = excluded.task, last_active = excluded.last_active""",
            name, status, task[:500], now_iso(),
        )

    def agent_statuses(self) -> dict[str, dict[str, Any]]:
        return {r["name"]: {"status": r["status"], "task": r["task"], "last_active": r["last_active"]} for r in self._q("SELECT * FROM agent_status")}
