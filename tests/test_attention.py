"""Full human requests, scoped replies and reference enforcement, without live models."""
from __future__ import annotations

import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from huntun.communication import HUMAN_REQUEST_RULE, validate_human_request
from huntun.config import default_config
from huntun.roles import build_system_prompt
from huntun.store import Store
from huntun.types import AgentSpec
from huntun.watchdog import SYSTEM


class AttentionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "board.sqlite"
        self.store = Store(self.path)
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.store.close)

    def test_full_source_restores_legacy_truncated_requests(self) -> None:
        body = "@human " + "完整的背景和决策范围。" * 200 + "最终的问题：是否招聘一名 QA？"
        thread = self.store.create_thread("master", "Staffing", body)
        comment = self.store.add_comment(thread["id"], "qa-1", body)
        self.store._exec("UPDATE attention SET text=substr(text,1,600)")
        items = self.store.open_attention()
        self.assertEqual([it["text"] for it in items], [body, body])
        self.assertEqual(items[0]["comment_id"], comment["id"])
        self.assertLessEqual(len(self.store.open_attention(full_text=False)[0]["text"]), 600)
        self.store.close()
        self.store = Store(self.path)
        self.addCleanup(self.store.close)
        self.assertEqual(self.store.open_attention()[0]["text"], body)

    def test_inline_reply_wakes_comment_requester_and_preserves_other_asks(self) -> None:
        thread = self.store.create_thread("human", "Project decision", "Opening")
        proposal = self.store.add_comment(thread["id"], "qa-1", "@human Shall I test Safari?", requires_confirmation=True)
        other = self.store.add_comment(thread["id"], "dev-1", "@human What is the deployment hostname?")
        items = self.store.open_attention()
        qa_item = next(it for it in items if it["comment_id"] == proposal["id"])
        self.assertEqual(qa_item["requires_confirmation"], 1)
        other_item = next(it for it in items if it["comment_id"] == other["id"])
        self.store.take_inbox("qa-1")
        durable = []
        def notified(target):
            with_read = Store(self.path)
            try:
                durable.append((target, len(with_read.get_comments(thread["id"]))))
            finally:
                with_read.close()
        self.store.on("mention", notified)
        reply = self.store.reply_attention(qa_item["id"], "Yes, test Safari 18.")
        self.assertEqual(reply["author"], "human")
        self.assertEqual(reply["body"], "@qa-1 Yes, test Safari 18.")
        self.assertEqual(reply["thread_id"], thread["id"])
        self.assertEqual(self.store.pending_attention_ids(), [other_item["id"]])
        self.assertEqual(self.store.get_attention(qa_item["id"])["resolved_by"], "reply")
        self.assertEqual(self.store.confirmation_status(thread["id"], "qa-1"), (True, True))
        self.assertEqual(self.store.peek_inbox_count("qa-1"), 1)
        self.assertIn(("qa-1", 3), durable, "notification fires after the reply is readable from another connection")
        with self.assertRaisesRegex(ValueError, "already been resolved"):
            self.store.reply_attention(qa_item["id"], "Duplicate")
        self.assertEqual(len(self.store.get_comments(thread["id"])), 3)

    def test_concurrent_replies_are_atomic_across_connections(self) -> None:
        thread = self.store.create_thread("master", "Confirm hire", "@human Hire one QA using the project model?")
        item_id = self.store.open_attention()[0]["id"]
        def reply():
            connection = Store(self.path)
            try:
                try:
                    return connection.reply_attention(item_id, "Approved")["id"]
                except ValueError:
                    return None
            finally:
                connection.close()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: reply(), range(2)))
        self.assertEqual(sum(r is not None for r in results), 1)
        self.assertEqual(len(self.store.get_comments(thread["id"])), 1)
        self.assertEqual(self.store.attention_count(), 0)

    def test_invalid_reply_leaves_request_open_and_does_not_post(self) -> None:
        thread = self.store.create_thread("master", "Decision", "@human Which hostname should we use?")
        item_id = self.store.open_attention()[0]["id"]
        for item, body in ((item_id, " "), (item_id + 1, "answer")):
            with self.assertRaises(ValueError):
                self.store.reply_attention(item, body)
        self.assertEqual(self.store.attention_count(), 1)
        self.assertEqual(self.store.get_comments(thread["id"]), [])
        reply = self.store.reply_attention(item_id, "@master Use localhost")
        self.assertEqual(reply["body"], "@master Use localhost", "requester is not tagged twice")

    def test_request_pages_have_no_duplicates_or_missing_items(self) -> None:
        thread = self.store.create_thread("human", "Many requests", "opening")
        for n in range(75):
            self.store.add_comment(thread["id"], "qa-1", f"@human Please choose the hostname for environment {n}.")
        ids = self.store.pending_attention_ids()
        seen, before = [], 0
        while page := self.store.open_attention(30, before=before):
            seen.extend(it["id"] for it in page)
            before = page[-1]["id"]
        self.assertEqual(seen, ids)

    def test_reference_requests_are_rejected_before_any_write(self) -> None:
        thread = self.store.create_thread("human", "Existing conversation", "opening")
        bad = (
            "@human 请确认原605的提案。", "@human 请看线程 #605，批准招聘。",
            "@human 看 comment 6974，然后回复。", "@human see #605",
            "@human approve the earlier proposal", "@human 请查看之前的对话。",
            "@human 在 /huntun/#/w/abcd/t/605 确认。", "@human 关联512/5626562，请审批。",
            "@human Please reply in thread_id=605.", "@human 详见原帖，帮我处理。",
            "@human 请确认评论编号6974。", "@human 看 C6974。",
            "@human Please approve 605.", "@human 请在605回同意。",
            "@human 请确认6056972。", "@human 按human5626553批准执行。",
            "@human 請確認對話編號6974。", "@human コメント番号6974を見て承認してください。",
        )
        for body in bad:
            with self.subTest(body=body):
                with self.assertRaisesRegex(ValueError, "self-contained"):
                    self.store.create_thread("master", "Decision", body)
                with self.assertRaisesRegex(ValueError, "self-contained"):
                    self.store.add_comment(thread["id"], "master", body)
        self.assertEqual(len(self.store.list_threads()), 1)
        self.assertEqual(self.store.get_comments(thread["id"]), [])
        self.assertEqual(self.store.attention_count(), 0)
        # Ordinary coordination and genuine human posts may still reference history.
        self.store.add_comment(thread["id"], "qa-1", "@master Please read thread #605.")
        self.store.add_comment(thread["id"], "human", "@master Read comment #6974.")
        for body in (
            "@human Please approve one QA hire, gpt-6.1-sol medium, to test Safari before delivery.",
            "@human Task #335 needs a hostname. Use localhost or example.org? I recommend localhost for local testing.",
            "@human Issue #605 needs your decision: should we retain Safari 18 support?",
            "@human Please approve a $605 budget for 500 test runs.",
            "@human Please approve 500 test runs. I recommend Safari as the target browser.",
        ):
            validate_human_request(body)

    def test_all_roles_backends_and_watchdog_receive_the_shared_rule(self) -> None:
        cfg = default_config("Complete the project")
        for role in ("master", "team-lead", "backend", "qa"):
            agent = AgentSpec(role, role, role, "Brief")
            for backend in ("codex", "claude-code", "pi-clm", "kimi", "api", "vllm"):
                self.assertIn(HUMAN_REQUEST_RULE, build_system_prompt(agent, cfg, [agent], backend))
        self.assertIn(HUMAN_REQUEST_RULE, SYSTEM)
