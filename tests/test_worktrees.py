"""Real git integration: isolation, restart, dirty human files and conflicts."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from huntun.gitops import (
    GitError,
    commit,
    ensure_repo,
    ensure_worktree,
    git,
    merge_task,
    sync_worktree,
)


class WorktreeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        (self.root / "shared.txt").write_text("baseline\n")
        await ensure_repo(self.root)

    async def asyncTearDown(self) -> None:
        self.tmp.cleanup()

    async def test_separate_checkouts_and_task_merge(self) -> None:
        a = await ensure_worktree(self.root, "dev-a")
        b = await ensure_worktree(self.root, "dev-b")
        self.assertNotEqual(a, b)
        (a / "a.txt").write_text("a")
        (b / "b.txt").write_text("b")
        await commit(a, "dev-a", "feat: a", ["a.txt"])
        await commit(b, "dev-b", "feat: b", ["b.txt"])
        self.assertFalse((self.root / "a.txt").exists())
        self.assertFalse((b / "a.txt").exists())
        await merge_task(self.root, a, "dev-a")
        await merge_task(self.root, b, "dev-b")
        self.assertEqual((self.root / "a.txt").read_text(), "a")
        self.assertEqual((self.root / "b.txt").read_text(), "b")
        await sync_worktree(self.root, a, "dev-a")
        self.assertEqual((a / "b.txt").read_text(), "b")

    async def test_restart_keeps_unfinished_work(self) -> None:
        a = await ensure_worktree(self.root, "dev-a")
        (a / "pending.txt").write_text("in progress")
        again = await ensure_worktree(self.root, "dev-a")
        await sync_worktree(self.root, again, "dev-a")
        self.assertEqual((again / "pending.txt").read_text(), "in progress")
        with self.assertRaisesRegex(GitError, "Commit all task changes"):
            await merge_task(self.root, again, "dev-a")

    async def test_human_changes_are_never_stashed_or_overwritten(self) -> None:
        a = await ensure_worktree(self.root, "dev-a")
        (a / "new.txt").write_text("agent")
        await commit(a, "dev-a", "feat: new", ["new.txt"])
        original_head = await git(self.root, "rev-parse", "HEAD")
        (self.root / "shared.txt").write_text("human pending\n")
        with self.assertRaisesRegex(GitError, "uncommitted changes"):
            await merge_task(self.root, a, "dev-a")
        self.assertEqual((self.root / "shared.txt").read_text(), "human pending\n")
        self.assertEqual(await git(self.root, "rev-parse", "HEAD"), original_head)
        self.assertFalse((self.root / "new.txt").exists())
        await commit(self.root, "human", "human: pending", ["shared.txt"])
        await merge_task(self.root, a, "dev-a")
        self.assertEqual((self.root / "shared.txt").read_text(), "human pending\n")

    async def test_conflicts_stay_with_owner_and_can_be_retried(self) -> None:
        a = await ensure_worktree(self.root, "dev-a")
        b = await ensure_worktree(self.root, "dev-b")
        (a / "shared.txt").write_text("a\n")
        (b / "shared.txt").write_text("b\n")
        await commit(a, "dev-a", "feat: a", ["shared.txt"])
        await commit(b, "dev-b", "feat: b", ["shared.txt"])
        await merge_task(self.root, a, "dev-a")
        with self.assertRaisesRegex(GitError, "Integration conflict"):
            await merge_task(self.root, b, "dev-b")
        self.assertEqual((self.root / "shared.txt").read_text(), "a\n")
        self.assertIn("<<<<<<<", (b / "shared.txt").read_text())
        (b / "shared.txt").write_text("a and b\n")
        await commit(b, "dev-b", "fix: resolve integration", ["shared.txt"])
        await merge_task(self.root, b, "dev-b")
        self.assertEqual((self.root / "shared.txt").read_text(), "a and b\n")

    async def test_existing_repo_is_not_dirtied_by_initialization(self) -> None:
        before = (self.root / ".gitignore").read_text()
        # Simulate an existing repo whose tracked ignore file has no Huntun entry.
        (self.root / ".gitignore").write_text("*.pyc\n")
        await commit(self.root, "human", "chore: existing ignore", [".gitignore"])
        await ensure_repo(self.root)
        self.assertEqual((self.root / ".gitignore").read_text(), "*.pyc\n")
        self.assertEqual(await git(self.root, "status", "--porcelain"), "")
        a = await ensure_worktree(self.root, "dev-a")
        (a / "new.txt").write_text("works")
        await commit(a, "dev-a", "feat: existing project", ["new.txt"])
        await merge_task(self.root, a, "dev-a")
        self.assertEqual((self.root / "new.txt").read_text(), "works")
        self.assertIn(".huntun/", before)

    async def test_concurrent_completions_serialize_integration(self) -> None:
        import asyncio
        trees = [await ensure_worktree(self.root, name) for name in ("dev-a", "dev-b")]
        for i, tree in enumerate(trees):
            (tree / f"{i}.txt").write_text(str(i))
            await commit(tree, f"dev-{i}", f"feat: {i}", [f"{i}.txt"])
        await asyncio.gather(*(merge_task(self.root, tree, f"dev-{i}") for i, tree in enumerate(trees)))
        self.assertTrue(all((self.root / f"{i}.txt").exists() for i in range(2)))

    async def test_legacy_agent_migrates_and_unfinished_tasks_stay_private(self) -> None:
        from unittest.mock import patch

        from huntun.config import default_config, save_config, save_team
        from huntun.orchestrator import AgentRuntime, Orchestrator
        from huntun.tools import TOOLS, execute
        from huntun.types import AgentSpec, CycleResult

        config = default_config("Existing team")
        config.backend = "api"
        agent = AgentSpec("dev-a", "backend", "Developer", "task")
        save_config(self.root, config)
        save_team(self.root, [agent])
        seen = []
        mode = "unfinished"

        class Fake:
            async def run_cycle(fake, *, ctx, **kwargs):
                seen.append(ctx.workspace)
                self.assertNotEqual(ctx.workspace, self.root)
                async def call(name, **args):
                    result, error = await execute(next(t for t in TOOLS if t.name == name), args, ctx)
                    self.assertFalse(error, result)
                if mode == "unfinished":
                    await call("write_file", path="pending.txt", content="private task")
                    await call("git_commit", message="feat: private task", summary_title="Task", summary_body="Work in progress")
                await call("finish_cycle", summary=mode, next_task="next", task_complete=mode == "complete")
                return CycleResult("finished", mode, "next")

        with patch("huntun.orchestrator.make_backend", return_value=Fake()):
            orch = Orchestrator(self.root)
            rt = AgentRuntime(orch, agent)
            rt.memory.state.session_id = "old-shared-checkout-session"
            rt.memory.state.resume_pending = True
            rt.memory.save_transcript([{"role": "user", "content": "old checkout"}])
            rt.memory.save_notes("Keep the existing task context")
            try:
                self.assertEqual(await rt._run_one("resume"), "finished")
                self.assertIsNone(rt.memory.state.session_id)
                self.assertIsNone(rt.memory.load_transcript())
                self.assertEqual(rt.memory.notes(), "Keep the existing task context")
                self.assertFalse((self.root / "pending.txt").exists())
                mode = "complete"
                self.assertEqual(await rt._run_one("work"), "finished")
                self.assertEqual(seen[0], seen[1], "worktree is reused across cycles")
                self.assertEqual((self.root / "pending.txt").read_text(), "private task")
            finally:
                orch.store.close()
