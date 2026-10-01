from __future__ import annotations

import asyncio
import os
import re
import weakref
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
    elif Path(await git(workspace, "rev-parse", "--show-toplevel")).resolve() != workspace.resolve():
        raise GitError("Select the Git repository root as the project directory; nested directories cannot own isolated agent worktrees.")
    try:
        count = await git(workspace, "rev-list", "--count", "HEAD")
    except GitError:
        count = "0"
    if count == "0":
        ensure_gitignore(workspace)
        # Baseline commit: existing files become the starting point so agent commits are clean diffs.
        await git(workspace, "add", "-A")
        await _commit_as(workspace, author, "chore: initialize repository (huntun)", allow_empty=True)
    else:
        # Existing projects need local ignores without dirtying their tracked files.
        exclude = Path(await git(workspace, "rev-parse", "--git-path", "info/exclude"))
        if not exclude.is_absolute():
            exclude = workspace / exclude
        exclude.parent.mkdir(parents=True, exist_ok=True)
        text = exclude.read_text() if exclude.exists() else ""
        present = {line.strip().rstrip("/") for line in text.splitlines()}
        missing = [entry for entry in DEFAULT_IGNORES if entry.rstrip("/") not in present]
        if missing:
            exclude.write_text(text.rstrip("\n") + "\n" + "\n".join(missing) + "\n")


DEFAULT_IGNORES = [
    ".huntun/", ".claude/settings.local.json", ".kimi-code/", ".DS_Store", ".env", "__pycache__/", "*.pyc", ".venv/", "venv/", ".pytest_cache/",
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


_operation_locks: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()


def _operation_lock() -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    if loop not in _operation_locks:
        _operation_locks[loop] = asyncio.Lock()
    return _operation_locks[loop]


@dataclass
class CommitResult:
    sha: str
    files: list[str]
    stat: str


async def commit(workspace: Path, author: str, message: str, files: list[str]) -> CommitResult:
    """Stages `files` (or everything if empty) and commits as `author`. One commit at a time per process."""
    async with _operation_lock():
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


def agent_worktree(workspace: Path, name: str) -> Path:
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", name):
        raise GitError(f"Invalid agent name: {name}")
    return workspace.resolve() / ".huntun" / "worktrees" / name


async def ensure_worktree(workspace: Path, name: str) -> Path:
    """Create once; keep unfinished work and branches across restarts and retirement."""
    tree = agent_worktree(workspace, name)
    async with _operation_lock():
        if tree.exists():
            if await git(tree, "rev-parse", "--show-toplevel") != str(tree):
                raise GitError(f"Not an agent worktree: {tree}")
            return tree
        branch = f"huntun/agents/{name}"
        tree.parent.mkdir(parents=True, exist_ok=True)
        branches = (await git(workspace, "for-each-ref", "--format=%(refname:short)", f"refs/heads/{branch}")).splitlines()
        args = ["worktree", "add"]
        args += [str(tree), branch] if branch in branches else ["-b", branch, str(tree), "HEAD"]
        await git(workspace, *args)
    return tree


async def sync_worktree(workspace: Path, tree: Path, name: str) -> None:
    """Bring integrated work into a clean agent tree; preserve pending edits/conflicts."""
    async with _operation_lock():
        if await git(tree, "status", "--porcelain"):
            return
        head = await git(workspace, "rev-parse", "HEAD")
        await git(tree, "-c", f"user.name={name}", "-c", f"user.email={name}@huntun.local", "merge", "--no-edit", head)


async def merge_task(workspace: Path, tree: Path, name: str) -> str:
    """Resolve integration in the agent's tree, then fast-forward a clean main checkout.

    Conflicts remain in the agent tree for its owner to resolve. Never stash, reset,
    or modify the human's uncommitted project files.
    """
    async with _operation_lock():
        if await git(tree, "status", "--porcelain"):
            raise GitError("Commit all task changes and resolve conflicts in your worktree before completing the task.")
        main_head = await git(workspace, "rev-parse", "HEAD")
        tree_head = await git(tree, "rev-parse", "HEAD")
        if main_head == tree_head:
            return "No changes to merge."
        try:
            await git(tree, "merge-base", "--is-ancestor", tree_head, main_head)
            return "Task commits are already integrated."
        except GitError:
            pass
        # Untracked files also matter: git may otherwise overwrite them on merge.
        if await git(workspace, "status", "--porcelain", "--untracked-files=all"):
            raise GitError("The project checkout has uncommitted changes. Task commits remain safe in your branch; ask @human to commit their changes, then retry completion.")
        try:
            await git(tree, "-c", f"user.name={name}", "-c", f"user.email={name}@huntun.local", "merge", "--no-edit", main_head)
        except GitError as e:
            raise GitError(f"Integration conflict in your worktree. Resolve it, test, commit, and retry task completion: {e}") from e
        integrated = await git(tree, "rev-parse", "HEAD")
        # Re-check after the merge: a human may have committed while it ran.
        if await git(workspace, "rev-parse", "HEAD") != main_head or await git(workspace, "status", "--porcelain", "--untracked-files=all"):
            raise GitError("Project checkout changed during integration; retry completion. Your branch is preserved.")
        await git(workspace, "merge", "--ff-only", integrated)
        return f"Task merged into the project checkout at {integrated[:12]}."
