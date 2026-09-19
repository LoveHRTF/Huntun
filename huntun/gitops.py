from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from pathlib import Path


class GitError(RuntimeError):
    pass


async def git(workspace: Path, *args: str, env: dict[str, str] | None = None) -> str:
    proc = await asyncio.create_subprocess_exec(
        "git", *args, cwd=str(workspace), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0", **(env or {})},
    )
    out, err = await proc.communicate()
    if proc.returncode != 0:
        raise GitError((err or out).decode(errors="replace").strip() or f"git {' '.join(args)} failed")
    return out.decode(errors="replace").strip()


async def is_repo(workspace: Path) -> bool:
    try:
        return (await git(workspace, "rev-parse", "--is-inside-work-tree")) == "true"
    except GitError:
        return False


async def ensure_repo(workspace: Path, author: str = "master") -> None:
    """Creates the repo if needed, ignores .huntun/, and makes an initial commit."""
    if not await is_repo(workspace):
        await git(workspace, "init", "-b", "main")
    ensure_gitignore(workspace)
    try:
        count = await git(workspace, "rev-list", "--count", "HEAD")
    except GitError:
        count = "0"
    if count == "0":
        # Baseline commit: existing files become the starting point so agent commits are clean diffs.
        await git(workspace, "add", "-A")
        await _commit_as(workspace, author, "chore: initialize repository (huntun)", allow_empty=True)


DEFAULT_IGNORES = [
    ".huntun/", ".claude/settings.local.json", ".DS_Store", ".env", "__pycache__/", "*.pyc", ".venv/", "venv/", ".pytest_cache/",
    ".mypy_cache/", ".ruff_cache/", "*.egg-info/", "node_modules/", "dist/", "build/", ".next/", "coverage/", ".coverage", "*.log",
]


def ensure_gitignore(workspace: Path) -> None:
    """Adds Huntun's state directory and the usual local caches to .gitignore (keeps existing entries)."""
    ignore = workspace / ".gitignore"
    existing = ignore.read_text() if ignore.exists() else ""
    present = {line.strip().rstrip("/") for line in existing.splitlines()}
    missing = [e for e in DEFAULT_IGNORES if e.rstrip("/") not in present]
    if missing:
        block = "\n".join(missing)
        ignore.write_text(existing.rstrip("\n") + ("\n\n" if existing else "") + "# huntun: local state and caches\n" + block + "\n")


async def _commit_as(workspace: Path, author: str, message: str, allow_empty: bool = False) -> str:
    ident = f"{author} <{author}@huntun.local>"
    args = ["-c", f"user.name={author}", "-c", f"user.email={author}@huntun.local", "commit", "--author", ident, "-m", message]
    if allow_empty:
        args.append("--allow-empty")
    await git(workspace, *args)
    return await git(workspace, "rev-parse", "--short", "HEAD")


_commit_lock = asyncio.Lock()


@dataclass
class CommitResult:
    sha: str
    files: list[str]
    stat: str


async def commit(workspace: Path, author: str, message: str, files: list[str]) -> CommitResult:
    """Stages `files` (or everything if empty) and commits as `author`. One commit at a time per process."""
    async with _commit_lock:
        if files:
            existing = [f for f in files if (workspace / f).exists()]
            removed = [f for f in files if not (workspace / f).exists()]
            if existing:
                await git(workspace, "add", "--", *existing)
            if removed:
                try:
                    await git(workspace, "add", "--all", "--", *removed)
                except GitError:
                    pass
        else:
            await git(workspace, "add", "-A")
        staged = await git(workspace, "diff", "--cached", "--name-only")
        if not staged:
            raise GitError("Nothing to commit: no staged changes. Write files first, or pass explicit `files`.")
        sha = await _commit_as(workspace, author, message)
        stat = await git(workspace, "show", "--stat", "--format=", "HEAD")
        return CommitResult(sha, staged.splitlines(), stat)


async def recent_log(workspace: Path, n: int = 12) -> str:
    try:
        return await git(workspace, "log", f"-n{n}", "--date=relative", "--format=%h %an (%ad): %s")
    except GitError:
        return "(no commits yet)"


async def status(workspace: Path) -> str:
    try:
        return await git(workspace, "status", "--short")
    except GitError:
        return ""
