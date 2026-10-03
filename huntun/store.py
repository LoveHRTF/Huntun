from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .config import now_iso
from .communication import validate_human_request
from .types import InboxItem

MENTION_RE = re.compile(r"(^|[^\w@])@([a-z0-9][a-z0-9-]*)", re.IGNORECASE)

SCHEMA = """
CREATE TABLE IF NOT EXISTS performance_usage (
  key TEXT PRIMARY KEY, agent TEXT NOT NULL, at TEXT NOT NULL,
  input REAL NOT NULL, output REAL NOT NULL, cache_read REAL NOT NULL, cache_write REAL NOT NULL);
CREATE INDEX IF NOT EXISTS performance_usage_time ON performance_usage(unixepoch(at),agent);
CREATE TABLE IF NOT EXISTS performance_commits (sha TEXT PRIMARY KEY,agent TEXT NOT NULL,at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS performance_commits_time ON performance_commits(unixepoch(at),agent);

CREATE TABLE IF NOT EXISTS tasks (
  id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL, description TEXT NOT NULL DEFAULT '',
  acceptance TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'backlog'
    CHECK(status IN ('backlog', 'in_process', 'done')),
  owner TEXT NOT NULL DEFAULT '', parent_id INTEGER REFERENCES tasks(id),
  thread_id INTEGER REFERENCES threads(id), evidence TEXT NOT NULL DEFAULT '',
  source_key TEXT UNIQUE, created_by TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS tasks_status_owner ON tasks(status, owner, id);
CREATE TABLE IF NOT EXISTS watchdog_messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT, role TEXT NOT NULL, body TEXT NOT NULL,
  target TEXT NOT NULL, model TEXT NOT NULL, effort TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS watchdog_performance_time ON watchdog_messages(unixepoch(created_at),role);
CREATE TABLE IF NOT EXISTS threads (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  author TEXT NOT NULL, title TEXT NOT NULL, body TEXT NOT NULL,
  commit_sha TEXT, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS comments (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  thread_id INTEGER NOT NULL REFERENCES threads(id),
  author TEXT NOT NULL, body TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS comments_thread_id ON comments(thread_id, id);
CREATE INDEX IF NOT EXISTS comments_thread_author ON comments(thread_id, author, id);
CREATE INDEX IF NOT EXISTS threads_performance_time ON threads(unixepoch(created_at),author);
CREATE INDEX IF NOT EXISTS comments_performance_time ON comments(unixepoch(created_at),author);
CREATE TABLE IF NOT EXISTS mentions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  target TEXT NOT NULL, kind TEXT NOT NULL, ref_id INTEGER NOT NULL,
  thread_id INTEGER NOT NULL, author TEXT NOT NULL,
  seen INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS mentions_target ON mentions(target, seen);
CREATE INDEX IF NOT EXISTS mentions_performance_time ON mentions(unixepoch(created_at),target);
CREATE TABLE IF NOT EXISTS control (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  agent TEXT NOT NULL, kind TEXT NOT NULL, detail TEXT NOT NULL, created_at TEXT NOT NULL, ref_id INTEGER);
CREATE TABLE IF NOT EXISTS agent_status (
  name TEXT PRIMARY KEY, status TEXT NOT NULL, task TEXT NOT NULL DEFAULT '', last_active TEXT);
CREATE TABLE IF NOT EXISTS attention (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  agent TEXT NOT NULL, text TEXT NOT NULL, thread_id INTEGER NOT NULL, comment_id INTEGER,
  created_at TEXT NOT NULL, resolved_at TEXT, resolved_by TEXT);
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
        self._deferred_events: list[tuple[str, tuple[Any, ...]]] | None = None
        with self._lock:
            self._db.execute("PRAGMA journal_mode = WAL")
            self._db.execute("PRAGMA busy_timeout = 5000")
            self._db.executescript(SCHEMA)
            if "ref_id" not in {r["name"] for r in self._db.execute("PRAGMA table_info(events)")}:
                self._db.execute("ALTER TABLE events ADD COLUMN ref_id INTEGER")
            if "requires_confirmation" not in {r["name"] for r in self._db.execute("PRAGMA table_info(comments)")}:
                self._db.execute("ALTER TABLE comments ADD COLUMN requires_confirmation INTEGER NOT NULL DEFAULT 0")
            self._db.execute("CREATE INDEX IF NOT EXISTS comments_confirmation ON comments(thread_id,author,requires_confirmation,id)")
            usage_columns = {r["name"] for r in self._db.execute("PRAGMA table_info(performance_usage)")}
            for name, definition in (("backend", "TEXT NOT NULL DEFAULT ''"), ("model", "TEXT NOT NULL DEFAULT ''"),
                                     ("accounting_version", "INTEGER NOT NULL DEFAULT 1"),
                                     ("cost_usd", "REAL NOT NULL DEFAULT 0"), ("cost_status", "TEXT NOT NULL DEFAULT 'legacy'")):
                if name not in usage_columns:
                    self._db.execute(f"ALTER TABLE performance_usage ADD COLUMN {name} {definition}")

    def close(self) -> None:
        with self._lock:
            self._db.close()

    # ---- events ----------------------------------------------------------------

    def on(self, event: str, fn: Callable[..., None]) -> None:
        self._listeners.setdefault(event, []).append(fn)

    def _emit(self, event: str, *args: Any) -> None:
        with self._lock:
            if self._deferred_events is not None:
                self._deferred_events.append((event, args))
                return
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

    def watchdog_message(self, role: str, body: str, target: str, model: str, effort: str) -> int:
        return self._exec("INSERT INTO watchdog_messages(role, body, target, model, effort, created_at) VALUES(?,?,?,?,?,?)",
                          role, body, target, model, effort, now_iso())

    def watchdog_history(self, before: int = 0, limit: int = 100) -> list[dict[str, Any]]:
        rows = self._q("SELECT * FROM watchdog_messages WHERE (? = 0 OR id < ?) ORDER BY id DESC LIMIT ?",
                       before, before, max(1, min(limit, 100)))
        return [dict(row) for row in reversed(rows)]

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
        if not running:
            self._exec("UPDATE control SET value = '' WHERE key LIKE 'recovery:%'")
        self.log_event(by, "start" if running else "pause", "All agents started" if running else "All agents paused")
        self._emit("control", running)

    # ---- delivery tasks (separate from discussion threads) ----------------------

    def seed_tasks(self, definition_of_done: str) -> None:
        """Idempotent migration for old projects and confirmed goals. Preserve progress."""
        now = now_iso()
        with self._lock:
            for line in definition_of_done.splitlines():
                title = re.sub(r"^\s*(?:[-*+]\s+(?:\[[ xX]\]\s*)?|\d+[.)]\s+)", "", line).strip()
                if not title:
                    continue
                key = "dod:" + hashlib.sha256(title.encode()).hexdigest()
                self._db.execute("INSERT OR IGNORE INTO tasks(title, acceptance, source_key, created_by, created_at, updated_at) VALUES(?,?,?,?,?,?)",
                                 (title, title, key, "master", now, now))

    def list_tasks(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self._q("SELECT * FROM tasks ORDER BY id")]

    def get_task(self, task_id: int) -> dict[str, Any] | None:
        row = self._one("SELECT * FROM tasks WHERE id = ?", task_id)
        return dict(row) if row else None

    def create_task(self, author: str, title: str, *, description: str = "", acceptance: str = "", owner: str = "",
                    parent_id: int | None = None, thread_id: int | None = None) -> dict[str, Any]:
        title = title.strip()
        if not title or len(title) > 500:
            raise ValueError("Task title must contain 1–500 characters")
        if not all(isinstance(v, str) for v in (description, acceptance, owner)) or not acceptance.strip():
            raise ValueError("Description and owner must be text; acceptance criteria are required")
        for ref in (parent_id, thread_id):
            if ref is not None and (not isinstance(ref, int) or isinstance(ref, bool) or ref <= 0):
                raise ValueError("Parent and thread references must be positive integers")
        with self._lock:
            if parent_id and not self.get_task(parent_id):
                raise ValueError("Parent task does not exist")
            if parent_id and self.get_task(parent_id)["status"] == "done":
                raise ValueError("Reopen the parent before adding work")
            if thread_id and not self.get_thread(thread_id):
                raise ValueError("Discussion thread does not exist")
            now = now_iso()
            task_id = self._exec("INSERT INTO tasks(title, description, acceptance, owner, parent_id, thread_id, created_by, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                                 title, description, acceptance, owner, parent_id or None, thread_id or None, author, now, now)
        self.log_event(author, "task", f"Task #{task_id} created in Backlog: {title}")
        self._emit("task", task_id)
        return self.get_task(task_id) or {}

    def update_task(self, task_id: int, author: str, changes: dict[str, Any]) -> dict[str, Any]:
        allowed = {"title", "description", "acceptance", "owner", "status", "evidence", "thread_id"}
        if not changes or set(changes) - allowed:
            raise ValueError("Invalid task fields")
        with self._lock:
            task = self.get_task(task_id)
            if not task:
                raise ValueError("Task does not exist")
            merged = {**task, **changes}
            if not all(isinstance(merged[k], str) for k in ("description", "acceptance", "owner", "evidence")) or not merged["acceptance"].strip():
                raise ValueError("Task text fields must be text; acceptance criteria are required")
            if merged["status"] not in ("backlog", "in_process", "done"):
                raise ValueError("Status must be backlog, in_process or done")
            if not isinstance(merged["title"], str) or not merged["title"].strip() or len(merged["title"]) > 500:
                raise ValueError("Task title must contain 1–500 characters")
            if merged["status"] == "in_process" and not merged["owner"]:
                raise ValueError("Assign an owner before starting work")
            if merged["thread_id"] and not self.get_thread(merged["thread_id"]):
                raise ValueError("Discussion thread does not exist")
            if merged["status"] == "done":
                if not merged["evidence"].strip():
                    raise ValueError("Completion evidence is required")
                if self._one("SELECT id FROM tasks WHERE parent_id = ? AND status != 'done' LIMIT 1", task_id):
                    raise ValueError("Finish all subtasks before completing their parent")
            elif task["status"] == "done" and task["parent_id"]:
                parent = self.get_task(task["parent_id"])
                if parent and parent["status"] == "done":
                    raise ValueError("Reopen the parent before reopening its subtask")
            updates = {**changes, "updated_at": now_iso()}
            self._exec("UPDATE tasks SET " + ", ".join(f"{k} = ?" for k in updates) + " WHERE id = ?", *updates.values(), task_id)
        self.log_event(author, "task", f"Task #{task_id}: {merged['status']} — {merged['title']}")
        self._emit("task", task_id)
        return self.get_task(task_id) or {}

    # ---- threads / comments ----------------------------------------------------

    def create_thread(self, author: str, title: str, body: str, commit_sha: str | None = None) -> dict[str, Any]:
        if author != "human" and "human" in parse_mentions(f"{title}\n{body}"):
            validate_human_request(f"{title}\n{body}")
        now = now_iso()
        tid = self._exec("INSERT INTO threads(author, title, body, commit_sha, created_at) VALUES(?, ?, ?, ?, ?)", author, title, body, commit_sha, now)
        self._record_mentions(author, "thread", tid, tid, f"{title}\n{body}")
        self.log_event(author, "thread", f"#{tid} {title}", ref_id=tid)
        self._emit("thread", tid)
        return self.get_thread(tid) or {}

    def add_comment(self, thread_id: int, author: str, body: str, *, requires_confirmation: bool = False,
                    attention_id: int | None = None) -> dict[str, Any]:
        if author != "human" and (requires_confirmation or "human" in parse_mentions(body)):
            validate_human_request(body)
        thread = self.get_thread(thread_id)
        if not thread:
            raise ValueError(f"Thread #{thread_id} does not exist")
        now = now_iso()
        cid = self._exec("INSERT INTO comments(thread_id, author, body, created_at, requires_confirmation) VALUES(?, ?, ?, ?, ?)",
                         thread_id, author, body, now, int(requires_confirmation))
        self._record_mentions(author, "comment", cid, thread_id, body)
        if thread["author"] not in (author, "human"):
            self._insert_mention(thread["author"], "comment", cid, thread_id, author, now)
        if author == "human":
            if attention_id is None:
                self.resolve_attention_for_thread(thread_id, "reply")
            else:
                self.resolve_attention(attention_id, "reply")
        self.log_event(author, "comment", f"#{thread_id}: {body[:120]}", ref_id=cid)
        if thread["author"] == "local-watchdog" and author != "human":
            target = next((n for n in parse_mentions(thread["body"]) if n != "local-watchdog"), "")
            if target and author in ("local-watchdog", target):
                self.log_event("watchdog", "dialogue", json.dumps({"target": target, "speaker": "watchdog" if author == "local-watchdog" else author,
                                                                   "text": body, "thread_id": thread_id}, ensure_ascii=False))
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
        if target == "human" and author != "human":
            # Tagging the human means an action is needed from them: track it until they respond.
            text = ""
            if kind == "thread":
                t = self.get_thread(thread_id)
                text = f"{t['title']}: {t['body']}" if t else ""
            else:
                c = self._one("SELECT body FROM comments WHERE id = ?", ref_id)
                text = c["body"] if c else ""
            # Cache only a preview; open_attention joins the original full message.
            self._exec("INSERT INTO attention(agent, text, thread_id, comment_id, created_at) VALUES(?, ?, ?, ?, ?)",
                       author, text[:600], thread_id, ref_id if kind == "comment" else None, now)
            self._emit("attention", thread_id)
        self._emit("mention", target)

    def get_thread(self, tid: int, include_body: bool = True) -> dict[str, Any] | None:
        columns = "*" if include_body else "id, author, title, commit_sha, created_at"
        row = self._one(f"SELECT {columns} FROM threads WHERE id = ?", tid)
        return dict(row) if row else None

    def confirmation_status(self, tid: int, proposer: str, authorization_comment_id: int | None = None) -> tuple[bool, bool]:
        """Check the proposal anchor, not the proposer's last progress reply.

        An explicit new proposal starts a new confirmation boundary. Legacy
        threads anchor at the original post/first proposer reply so an existing
        human confirmation survives acknowledgements, logs and model restarts.
        Bodies and @mentions cannot distinguish proposals from status updates.
        """
        thread = self.get_thread(tid, include_body=False)
        if not thread:
            return False, False
        proposal = self._one("SELECT id FROM comments WHERE thread_id=? AND author=? AND requires_confirmation=1 ORDER BY id DESC LIMIT 1", tid, proposer)
        if authorization_comment_id is not None:
            # The human can initiate an authorized hire before the master's
            # acknowledgement. Require its exact durable source, and keep an
            # explicit changed-scope proposal as a new approval boundary.
            source = ({"id": 0} if thread["author"] == "human" else None) if authorization_comment_id == 0 else self._one(
                "SELECT id FROM comments WHERE id=? AND thread_id=? AND author='human'", authorization_comment_id, tid)
            authorized = source is not None and (proposal is None or source["id"] > proposal["id"])
            return source is not None, authorized
        if proposal is None and thread["author"] != proposer:
            proposal = self._one("SELECT id FROM comments WHERE thread_id=? AND author=? ORDER BY id LIMIT 1", tid, proposer)
        proposed = proposal is not None or thread["author"] == proposer
        replied = self._one("SELECT id FROM comments WHERE thread_id = ? AND author = 'human' AND id > ? LIMIT 1", tid, proposal["id"] if proposal else 0)
        return proposed, proposed and replied is not None

    def get_comments(self, tid: int) -> list[dict[str, Any]]:
        return [dict(r) for r in self._q("SELECT id,thread_id,author,body,created_at FROM comments WHERE thread_id = ? ORDER BY id ASC", tid)]

    def comment_page(self, tid: int, *, before: int = 0, after: int = 0, limit: int = 100) -> dict[str, Any]:
        """Keyset pages: latest on open, older history, or forward-only updates."""
        limit = max(1, min(200, limit))
        where, params = "thread_id = ?", [tid]
        if before:
            where += " AND id < ?"
            params.append(before)
        elif after:
            where += " AND id > ?"
            params.append(after)
        direction = "ASC" if after else "DESC"
        rows = self._q(f"SELECT id,thread_id,author,body,created_at FROM comments WHERE {where} ORDER BY id {direction} LIMIT ?", *params, limit + 1)
        has_more = len(rows) > limit
        rows = rows[:limit]
        if not after:
            rows = list(reversed(rows))
        comments = [dict(r) for r in rows]
        return {"comments": comments, "has_more": has_more,
                "first_id": comments[0]["id"] if comments else 0,
                "last_id": comments[-1]["id"] if comments else after}

    def list_threads(self, limit: int = 50, preview: bool = False) -> list[dict[str, Any]]:
        return [
            dict(r)
            for r in self._q(
                f"""SELECT t.id, t.author, t.title, {'substr(t.body, 1, 400)' if preview else 't.body'} AS body, t.commit_sha, t.created_at,
                   (SELECT COUNT(*) FROM comments c WHERE c.thread_id = t.id) AS comment_count,
                   COALESCE((SELECT c.created_at FROM comments c WHERE c.thread_id = t.id ORDER BY c.id DESC LIMIT 1), t.created_at) AS last_activity
                   FROM threads t ORDER BY last_activity DESC, t.id DESC LIMIT ?""",
                limit,
            )
        ]

    def last_comments(self, thread_id: int, n: int = 2, preview: bool = False) -> list[dict[str, Any]]:
        columns = "id, thread_id, author, created_at, substr(body, 1, 240) AS body" if preview else "id, thread_id, author, created_at, body"
        rows = self._q(f"SELECT {columns} FROM comments WHERE thread_id = ? ORDER BY id DESC LIMIT ?", thread_id, n)
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

    # ---- attention: what the human needs to act on -----------------------------

    def open_attention(self, limit: int = 50, *, before: int = 0, full_text: bool = True) -> list[dict[str, Any]]:
        # Resolve the original message, including old items whose cached text was truncated.
        text = "COALESCE(c.body, CASE WHEN a.comment_id IS NULL THEN t.body END, a.text)" if full_text else "substr(a.text,1,600)"
        rows = self._q(f"""SELECT a.id, a.agent, {text} AS text, a.thread_id, a.comment_id,
                              a.created_at, a.resolved_at, a.resolved_by, t.title,
                              COALESCE(c.requires_confirmation,0) AS requires_confirmation
                           FROM attention a JOIN threads t ON t.id=a.thread_id
                           LEFT JOIN comments c ON c.id=a.comment_id AND c.thread_id=a.thread_id
                           WHERE a.resolved_at IS NULL AND (?=0 OR a.id<?)
                           ORDER BY a.id DESC LIMIT ?""", before, before, min(200, max(1, limit)))
        return [dict(r) for r in rows]

    def pending_attention_ids(self) -> list[int]:
        return [r["id"] for r in self._q("SELECT id FROM attention WHERE resolved_at IS NULL ORDER BY id DESC")]

    def get_attention(self, item_id: int) -> dict[str, Any] | None:
        row = self._one("SELECT * FROM attention WHERE id=?", item_id)
        return dict(row) if row else None

    def reply_attention(self, item_id: int, body: str) -> dict[str, Any]:
        """Persist a real human reply and wake its requester, resolving only this ask.

        Claim and reply commit together across SQLite connections. Notifications are emitted
        after commit so a newly awakened agent can already read its human authorization.
        """
        body = body.strip()
        if not body:
            raise ValueError("Enter a reply before sending.")
        events: list[tuple[str, tuple[Any, ...]]] = []
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            self._deferred_events = events
            try:
                item = self._one("SELECT * FROM attention WHERE id=?", item_id)
                if not item:
                    raise ValueError("This request no longer exists.")
                if item["resolved_at"]:
                    raise ValueError("This request has already been resolved. Your reply was not posted.")
                if item["agent"] not in parse_mentions(body):
                    body = f"@{item['agent']} {body}"
                comment = self.add_comment(item["thread_id"], "human", body, attention_id=item_id)
                self._db.execute("COMMIT")
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
            finally:
                self._deferred_events = None
        for event, args in events:
            self._emit(event, *args)
        return comment

    def attention_count(self) -> int:
        row = self._one("SELECT COUNT(*) AS n FROM attention WHERE resolved_at IS NULL")
        return int(row["n"]) if row else 0

    def resolve_attention(self, item_id: int, by: str = "human") -> bool:
        with self._lock:
            cur = self._db.execute("UPDATE attention SET resolved_at = ?, resolved_by = ? WHERE id = ? AND resolved_at IS NULL", (now_iso(), by, item_id))
            return cur.rowcount > 0

    def resolve_attention_for_thread(self, thread_id: int, by: str = "reply") -> int:
        with self._lock:
            cur = self._db.execute("UPDATE attention SET resolved_at = ?, resolved_by = ? WHERE thread_id = ? AND resolved_at IS NULL", (now_iso(), by, thread_id))
            return cur.rowcount

    # ---- events / status -------------------------------------------------------

    def log_event(self, agent: str, kind: str, detail: str, *, ref_id: int | None = None) -> None:
        # Keep structured dialogue intact; ordinary activity previews stay small.
        self._exec("INSERT INTO events(agent, kind, detail, created_at, ref_id) VALUES(?, ?, ?, ?, ?)",
                   agent, kind, detail if kind == "dialogue" else detail[:2000], now_iso(), ref_id)

    def list_events(self, since_id: int = 0, limit: int = 100) -> list[dict[str, Any]]:
        return [dict(r) for r in self._q("SELECT * FROM events WHERE id > ? ORDER BY id DESC LIMIT ?", since_id, limit)]

    def office_events(self, limit: int = 40) -> list[dict[str, Any]]:
        """Resolve full utterances by message ID without expanding board previews."""
        return [dict(r) for r in self._q("""SELECT e.*,
            CASE WHEN e.kind = 'thread' THEN t.title || '\n' || t.body
                 WHEN e.kind = 'comment' THEN c.body END AS speech
            FROM (SELECT * FROM events ORDER BY id DESC LIMIT ?) e
            LEFT JOIN threads t ON e.kind = 'thread' AND t.id = e.ref_id
            LEFT JOIN comments c ON e.kind = 'comment' AND c.id = e.ref_id
            ORDER BY e.id DESC""", limit)]

    def set_agent_status(self, name: str, status: str, task: str = "") -> None:
        self._exec(
            """INSERT INTO agent_status(name, status, task, last_active) VALUES(?, ?, ?, ?)
               ON CONFLICT(name) DO UPDATE SET status = excluded.status, task = excluded.task, last_active = excluded.last_active""",
            name, status, task[:500], now_iso(),
        )

    def agent_statuses(self) -> dict[str, dict[str, Any]]:
        return {r["name"]: {"status": r["status"], "task": r["task"], "last_active": r["last_active"]} for r in self._q("SELECT * FROM agent_status")}

    def agent_status(self, name: str) -> str | None:
        row = self._one("SELECT status FROM agent_status WHERE name=?", name)
        return row["status"] if row else None
