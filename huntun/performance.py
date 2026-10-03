"""Project performance: durable usage samples and incremental historical imports."""
from __future__ import annotations

import json
import math
import re
import subprocess
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import agents_dir, now_iso
from .gitops import GitError, git
from .store import Store
from .usage import TOKEN_KEYS, journal_usage

BIN_SECONDS = {"1m": 60, "5m": 300, "15m": 900, "30m": 1800, "1h": 3600, "3h": 10800,
               "6h": 21600, "12h": 43200, "1d": 86400, "1w": 604800, "30d": 2592000}
RANGE_SECONDS = {"1h": 3600, "6h": 21600, "24h": 86400, "7d": 604800, "30d": 2592000}
MAX_BINS = 512


def _bin_bounds(first: int, until: int, width: int) -> tuple[int, int]:
    # Weekly bins start on Monday in UTC. Other intervals have stable epoch alignment.
    anchor = 4 * 86400 if width == BIN_SECONDS["1w"] else 0
    return (first - anchor) // width * width + anchor, (until - 1 - anchor) // width * width + anchor + width


def _iso(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat()


def record_usage(store: Store, agent: str, usage: dict[str, float], *, key: str = "", at: str = "",
                 backend: str = "", model: str = "", accounting_version: int = 2, cost_status: str = "untracked") -> str:
    key = key or str(uuid.uuid4())
    amounts = [max(0, float(usage.get(k, 0) or 0)) for k in TOKEN_KEYS]
    store._exec("INSERT OR IGNORE INTO performance_usage(key,agent,at,input,output,cache_read,cache_write,backend,model,accounting_version,cost_usd,cost_status) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                key, agent, at or now_iso(), *amounts, backend, model, accounting_version, usage.get("cost_usd", 0) or 0, cost_status)
    return key


def record_commit(store: Store, agent: str, sha: str, at: str) -> None:
    if not re.fullmatch(r"[0-9a-f]{7,64}", sha):
        return
    with store._lock:
        # Old git_commit events used abbreviated hashes. Do not count a SHA
        # again when its full hash appears in a native cycle or Git history.
        prefixes = [sha[:n] for n in range(7,len(sha)+1)]
        marks = ",".join("?" for _ in prefixes)
        old = store._one(f"SELECT sha,at FROM performance_commits WHERE sha IN ({marks}) OR (sha>=? AND sha<?) LIMIT 1",
                         *prefixes, sha, sha + "g")
        if old:
            if len(sha) > len(old["sha"]):
                store._exec("UPDATE performance_commits SET sha=?,at=? WHERE sha=?", sha, at, old["sha"])
            return
        store._exec("INSERT OR IGNORE INTO performance_commits(sha,agent,at) VALUES(?,?,?)", sha, agent, at)


async def record_cycle_commits(store: Store, tree: Path, agent: str, baseline: str) -> None:
    """Include native shell commits; exclude merges and inherited upstream work."""
    if not baseline:
        return
    try:
        rows = await git(tree, "log", "--first-parent", "--no-merges", "--format=%H %cI", baseline + "..HEAD")
    except GitError:
        return
    for row in rows.splitlines():
        sha, _, at = row.partition(" ")
        record_commit(store, agent, sha, at)


@contextmanager
def _import_batch(store: Store):
    # One durable flush for a historical batch, with its cursors committed
    # atomically. Avoid thousands of WAL fsyncs on the first review request.
    with store._lock:
        store._db.execute("BEGIN IMMEDIATE")
        try:
            yield
            store._db.execute("COMMIT")
        except BaseException:
            store._db.execute("ROLLBACK")
            raise


def _import_history(store: Store, workspace: Path) -> bool:
    """Read only appended complete journal lines; cursors survive server restarts."""
    pending = False
    root = agents_dir(workspace)
    budget = 2_000_000
    with _import_batch(store):
        for journal in sorted(root.glob("*/journal.jsonl")):
            agent = journal.parent.name
            cursor_key = "performance:journal:" + agent
            offset = int(store.get_control(cursor_key, "0"))
            if journal.stat().st_size < offset:
                offset = 0
            if budget <= 0:
                pending |= journal.stat().st_size > offset
                continue
            with journal.open("rb") as fh:
                fh.seek(offset)
                while budget > 0:
                    start = fh.tell()
                    line = fh.readline()
                    if not line or not line.endswith(b"\n"):
                        pending |= bool(line)
                        break
                    budget -= len(line)
                    try:
                        entry = json.loads(line)
                        if not isinstance(entry, dict) or not entry.get("at"):
                            continue
                        key = entry.get("usage_sample") or f"journal:{agent}:{start}"
                        if isinstance(entry.get("usage"), dict):
                            normalized = journal_usage(entry)
                            version = 2 if normalized != entry["usage"] else entry.get("usage_accounting_version", 1)
                            record_usage(store, agent, normalized, key=key, at=entry["at"],
                                         backend=entry.get("backend", ""), model=entry.get("model", ""),
                                         accounting_version=version, cost_status=entry.get("cost_status") or "legacy")
                            if not entry.get("usage_accounting_version"):
                                store.set_control("performance:legacy-usage", "1")
                        for sha in entry.get("commits", []):
                            record_commit(store, agent, str(sha), entry["at"])
                    except (ValueError, TypeError):
                        continue
                    finally:
                        offset = fh.tell()
                pending |= fh.tell() < journal.stat().st_size
            store.set_control(cursor_key, str(offset))
        event_id = int(store.get_control("performance:commit-events", "0"))
        through = store._one("SELECT COALESCE(MAX(id),0) AS id FROM events")["id"]
        events = store._q("SELECT id,agent,detail,created_at FROM events WHERE kind='commit' AND id>? AND id<=? ORDER BY id", event_id, through)
        for e in events:
            record_commit(store, e["agent"], e["detail"].split(" ")[0], e["created_at"])
        if through > event_id:
            store.set_control("performance:commit-events", str(through))
    # Older native commits with Huntun identities can also be recovered from Git.
    # Cache by refs so polling never rereads an unchanged repository history.
    if not (workspace / ".git").exists():
        return pending
    try:
        refs = subprocess.run(["git", "-C", str(workspace), "show-ref", "--head"], capture_output=True, text=True, timeout=5)
        fingerprint = refs.stdout
        if fingerprint and fingerprint != store.get_control("performance:git-refs", ""):
            log = subprocess.run(["git", "-C", str(workspace), "log", "--all", "--no-merges", "--author=@huntun[.]local", "--format=%H %cI %ae"],
                                 capture_output=True, text=True, timeout=10)
            if log.returncode == 0:
                for line in log.stdout.splitlines():
                    parts = line.split(" ", 2)
                    if len(parts) == 3 and parts[2].endswith("@huntun.local"):
                        record_commit(store, parts[2].split("@")[0], parts[0], parts[1])
                # Branch reflogs identify native CLI commits even when the CLI
                # used the human's default Git author. Ignore sync/merge entries.
                branches = {line.split(" ", 1)[1] for line in fingerprint.splitlines()
                            if " refs/heads/huntun/agents/" in line}
                complete = True
                for branch in sorted(branches):
                    agent = branch.rsplit("/", 1)[1]
                    reflog = subprocess.run(["git", "-C", str(workspace), "reflog", "show", branch, "--format=%H %cI %gs"],
                                            capture_output=True, text=True, timeout=10)
                    complete &= reflog.returncode == 0
                    for line in reflog.stdout.splitlines():
                        parts = line.split(" ", 2)
                        if len(parts) == 3 and re.match(r"commit(?: \(amend\)| \(initial\))?:", parts[2]):
                            record_commit(store, agent, parts[0], parts[1])
                if complete:
                    store.set_control("performance:git-refs", fingerprint)
    except (OSError, subprocess.TimeoutExpired):
        pass
    return pending


def review(store: Store, workspace: Path, team: list[Any], period: str = "7d", *,
           bin_size: str = "auto", now: datetime | None = None) -> dict[str, Any]:
    if period not in {*RANGE_SECONDS, "all"}:
        raise ValueError("range must be 1h, 6h, 24h, 7d, 30d, or all")
    if bin_size != "auto" and bin_size not in BIN_SECONDS:
        raise ValueError("bin must be auto, " + ", ".join(BIN_SECONDS))
    pending = _import_history(store, workspace)
    now = now or datetime.now(timezone.utc)
    # Include all observed events in the current second. Changing bin size never
    # changes the query window or totals; only the grouping boundaries change.
    window_end = int(now.timestamp()) + 1
    if period == "all":
        row = store._one("""SELECT MIN(t) AS first FROM (
          SELECT MIN(unixepoch(at)) AS t FROM performance_usage UNION ALL
          SELECT MIN(unixepoch(at)) FROM performance_commits UNION ALL
          SELECT MIN(unixepoch(created_at)) FROM threads UNION ALL
          SELECT MIN(unixepoch(created_at)) FROM comments UNION ALL
          SELECT MIN(unixepoch(created_at)) FROM watchdog_messages WHERE role='assistant')""")
        first = row["first"] if row and row["first"] is not None else window_end - 1
        window_start = min(int(first), window_end - 1)
        default_width = max(86400, math.ceil(max(1, window_end - window_start) / (60 * 86400)) * 86400)
    else:
        window_start = window_end - RANGE_SECONDS[period]
        default_width = {"1h": 60, "6h": 300, "24h": 3600, "7d": 21600, "30d": 86400}[period]
    width = default_width if bin_size == "auto" else BIN_SECONDS[bin_size]
    start, end = _bin_bounds(window_start, window_end, width)
    count = (end - start) // width
    options = [{"id": "auto", "seconds": default_width, "available": True}]
    for key, seconds in BIN_SECONDS.items():
        left, right = _bin_bounds(window_start, window_end, seconds)
        options.append({"id": key, "seconds": seconds, "available": (right - left) // seconds <= MAX_BINS})
    if count > MAX_BINS:
        available = next(o["id"] for o in options[1:] if o["available"]) if any(o["available"] for o in options[1:]) else "auto"
        raise ValueError(f"This interval would create {count} bins; maximum is {MAX_BINS}. Choose {available} or a shorter time range.")
    buckets = [_iso(start + i * width) for i in range(count)]
    coverage = [min(window_end, start + (i + 1) * width) - max(window_start, start + i * width) for i in range(count)]
    specs = {a.name: a for a in team}
    historical = {r["agent"] for r in store._q("""SELECT agent FROM performance_usage UNION SELECT agent FROM performance_commits
      UNION SELECT author AS agent FROM threads UNION SELECT author FROM comments
      UNION SELECT 'local-watchdog' FROM watchdog_messages WHERE role='assistant'""") if r["agent"] != "human"}
    names = list(specs) + sorted(historical - specs.keys())
    rows = {}
    for name in names:
        spec = specs.get(name)
        watchdog = name == "local-watchdog"
        rows[name] = {"name": name, "role": spec.role if spec else "watchdog" if watchdog else "historical",
                      "title": spec.title if spec else "Local watchdog" if watchdog else name,
                      "retired": spec.status == "retired" if spec else not watchdog,
                      "tokens": [0] * count, "messages": [0] * count, "commits": [0] * count,
                      "totals": {"tokens": 0, "messages": 0, "commits": 0, "threads": 0, "replies": 0, "received": 0}}
    # Aggregate in SQLite and return at most 512 bins, independent of thread
    # lengths and number of historical cycles. Never load message bodies.
    queries = [
        ("tokens", "SELECT agent, unixepoch(at) AS ts, input+output+cache_read+cache_write AS value FROM performance_usage"),
        ("commits", "SELECT agent, unixepoch(at) AS ts, 1 AS value FROM performance_commits"),
        ("messages", "SELECT author AS agent, unixepoch(created_at) AS ts, 1 AS value FROM threads UNION ALL SELECT author,unixepoch(created_at),1 FROM comments UNION ALL SELECT 'local-watchdog',unixepoch(created_at),1 FROM watchdog_messages WHERE role='assistant'"),
    ]
    for metric, source in queries:
        data = store._q(f"SELECT agent,CAST((ts-?)/? AS INTEGER) AS bucket,SUM(value) AS value FROM ({source}) WHERE ts>=? AND ts<? GROUP BY agent,bucket",
                        start, width, window_start, window_end)
        for d in data:
            if d["agent"] in rows:
                rows[d["agent"]][metric][d["bucket"]] = round(d["value"])
                rows[d["agent"]]["totals"][metric] += round(d["value"])
    for field, table, col in (("threads", "threads", "author"), ("replies", "comments", "author"), ("received", "mentions", "target")):
        for d in store._q(f"SELECT {col} AS agent,COUNT(*) AS n FROM {table} WHERE unixepoch(created_at)>=? AND unixepoch(created_at)<? GROUP BY {col}", window_start, window_end):
            if d["agent"] in rows:
                rows[d["agent"]]["totals"][field] = d["n"]
    if "local-watchdog" in rows:
        replies = store._one("SELECT COUNT(*) AS n FROM watchdog_messages WHERE role='assistant' AND unixepoch(created_at)>=? AND unixepoch(created_at)<?", window_start, window_end)
        rows["local-watchdog"]["totals"]["replies"] += replies["n"]
    return {"range": period, "bin": bin_size, "bucket_seconds": width, "buckets": buckets,
            "window_start": _iso(window_start), "window_end": _iso(window_end),
            "bucket_coverage_seconds": coverage, "partial_buckets": [seconds < width for seconds in coverage],
            "bin_options": options, "max_bins": MAX_BINS, "agents": list(rows.values()),
            "updated_at": now.isoformat(), "history_import_pending": pending,
            "usage_history_incomplete": store.get_control("performance:legacy-usage", "0") == "1",
            "definitions": {"tokens": "Uncached input + output + cache read + cache write, each token counted once, recorded at cycle end (including interrupted cycles). Cached tokens count toward consumption at their own billing rate; this is not context occupancy.",
                            "messages": "Actual discussion threads/replies and watchdog dialog replies per time bucket. Received = direct mentions/inbox deliveries.",
                            "commits": "Distinct non-merge Git commits, including private worktrees. SHA prefixes are deduplicated.",
                            "history": "Existing journals, board posts and Huntun Git identities are imported. Older usage without a timestamp and commits without an agent identity or retained worktree reflog cannot be attributed."}}
