"""Large board threads use indexed bounded pages rather than full history."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from huntun.store import Store


class ThreadPageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "board.sqlite")
        self.tid = self.store.create_thread("human", "Long thread", "opening " * 1000)["id"]
        self.ids = [self.store.add_comment(self.tid, "dev", "reply " * 1000)["id"] for _ in range(260)]

    def tearDown(self) -> None:
        self.store.close()
        self.tmp.cleanup()

    def test_latest_older_and_incremental_pages(self) -> None:
        latest = self.store.comment_page(self.tid)
        self.assertEqual([c["id"] for c in latest["comments"]], self.ids[-100:])
        self.assertTrue(latest["has_more"])
        older = self.store.comment_page(self.tid, before=latest["first_id"])
        self.assertEqual([c["id"] for c in older["comments"]], self.ids[-200:-100])
        oldest = self.store.comment_page(self.tid, before=older["first_id"])
        self.assertEqual([c["id"] for c in oldest["comments"]], self.ids[:60])
        self.assertFalse(oldest["has_more"])
        empty = self.store.comment_page(self.tid, after=latest["last_id"])
        self.assertEqual(empty["comments"], [])
        self.assertEqual(empty["last_id"], latest["last_id"])
        new = self.store.add_comment(self.tid, "human", "new")
        self.assertEqual(self.store.comment_page(self.tid, after=latest["last_id"])["comments"], [new])

    def test_forward_pages_never_skip_replies_and_limit_is_capped(self) -> None:
        page = self.store.comment_page(self.tid, after=self.ids[0], limit=100000)
        self.assertEqual([c["id"] for c in page["comments"]], self.ids[1:201])
        self.assertTrue(page["has_more"])
        tail = self.store.comment_page(self.tid, after=page["last_id"])
        self.assertEqual([c["id"] for c in tail["comments"]], self.ids[201:])
        self.assertFalse(tail["has_more"])

    def test_previews_bound_body_sizes_and_keep_counts(self) -> None:
        preview = self.store.list_threads(preview=True)[0]
        self.assertEqual(len(preview["body"]), 400)
        self.assertEqual(preview["comment_count"], 260)
        self.assertEqual(len(self.store.last_comments(self.tid, preview=True)[0]["body"]), 240)
        plan = self.store._q("EXPLAIN QUERY PLAN SELECT * FROM comments WHERE thread_id = ? AND id < ? ORDER BY id DESC LIMIT 100", self.tid, self.ids[-1])
        self.assertTrue(any("comments_thread_id" in row["detail"] for row in plan))

    def test_confirmation_checks_order_without_reading_bodies(self) -> None:
        self.assertEqual(self.store.confirmation_status(self.tid, "master"), (False, False))
        self.store.add_comment(self.tid, "master", "proposal @human")
        self.assertEqual(self.store.confirmation_status(self.tid, "master"), (True, False))
        self.store.add_comment(self.tid, "human", "approved")
        self.assertEqual(self.store.confirmation_status(self.tid, "master"), (True, True))
        self.store.add_comment(self.tid, "master", "Received; I will hire them now.")
        self.store.add_comment(self.tid, "master", 'Tool error: "@human has not replied since your proposal"')
        self.store.add_comment(self.tid, "dev", "I can prepare the handoff @human")
        self.assertEqual(self.store.confirmation_status(self.tid, "master"), (True, True))
        reopened = Store(Path(self.tmp.name) / 'board.sqlite')
        try:
            self.assertEqual(reopened.confirmation_status(self.tid, 'master'), (True,True))
        finally:
            reopened.close()
        self.store.add_comment(self.tid, "master", "new proposal @human", requires_confirmation=True)
        self.store.add_comment(self.tid, "master", "Awaiting your decision")
        self.assertEqual(self.store.confirmation_status(self.tid, "master"), (True, False))
        self.store.add_comment(self.tid, 'human', 'Confirmed the new proposal')
        self.store.add_comment(self.tid, 'master', 'Received')
        self.assertEqual(self.store.confirmation_status(self.tid, 'master'), (True,True))

    def test_human_reply_before_a_proposal_does_not_confirm_it(self) -> None:
        self.store.add_comment(self.tid, 'human', 'An earlier unrelated message')
        self.store.add_comment(self.tid, 'master', 'First proposal')
        self.assertEqual(self.store.confirmation_status(self.tid, 'master'), (True,False))
        other = self.store.create_thread('master', 'Proposal', '@human Hire a QA?')['id']
        self.store.add_comment(self.tid, 'human', 'Approved here only')
        self.assertEqual(self.store.confirmation_status(other, 'master'), (True,False))
        self.store.add_comment(other, 'human', 'Approved')
        self.store.add_comment(other, 'master', 'Received')
        self.assertEqual(self.store.confirmation_status(other, 'master'), (True,True))
