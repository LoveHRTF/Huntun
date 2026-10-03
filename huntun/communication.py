"""Shared rules for actionable, self-contained messages to the human."""
from __future__ import annotations

import re

HUMAN_REQUEST_RULE = (
    "Every request to the human must be understandable and answerable from that message alone. "
    "State exactly what you need them to do or decide, the relevant facts and blocker, the full proposed "
    "scope or options, your recommendation and the consequences when applicable. Repeat the necessary "
    "details even if they already appear elsewhere. Never send the human to a thread, comment, message "
    "or conversation number, a board URL, 'the earlier proposal' or 'see above' to understand or approve "
    "your ask. Read the history yourself and restate it. Numeric board IDs belong in tool arguments, "
    "not in requests to the human. Do not ask again for an unchanged action they already authorized."
)

_BOARD_REFERENCE = re.compile(
    r"(?:\b(?:thread|comment|message|post|discussion|conversation|reply)\s*"
    r"(?:(?:[_-]?id|no\.?|number)\s*[:=]?\s*|[#:]\s*)?\d+\b)"
    r"|(?:线程|線程|帖子?|对话|對話|会话|會話|评论|評論|回复|回覆|消息|讨论|討論|"
    r"スレッド|コメント|メッセージ|会話|投稿|返信)\s*"
    r"(?:(?:编号|編號|番号|号码?|號碼?|ID|第)\s*[:：=]?\s*|#\s*)?\d+"
    r"|(?:\b[tc]\d{2,}\b)"
    r"|(?:(?<![a-z0-9_-])human\d{3,}(?![a-z0-9_-]))"
    r"|(?:/(?:t|threads)/\d+)"
    r"|(?:(?:原帖?|参见|详见|关联)\s*#?\d{2,}(?:[/、,]\d+)*)"
    r"|(?:(?:\b(?:see|read|review|check|approve)|回复|确认|审批|查看|请看|看)\s*"
    r"#?\d{3,}(?:[/、,]\d+)*\s*(?:[.!?。！？，;；]|$))"
    r"|(?:(?:回复|在|到)\s*\d{3,}\s*(?:同意|确认|批准|回|帖|线程|评论|中|里))"
    r"|(?:\b(?:see|read|refer\s+to|look\s+at|approve|reply\s+(?:to|on|in))\s+"
    r"(?:the\s+)?(?:above|previous|earlier|original)\s+(?:thread|comment|message|post|proposal))"
    r"|(?:(?:详见|参见|请查看|请看|请回复|请确认|去看)\s*(?:之前|前面|上述|原|上[条帖])"
    r"(?:的)?(?:帖|对话|消息|评论|回复|方案|提案))",
    re.IGNORECASE,
)
_HASH_ID = re.compile(r"(?<![\w/#])#\d+\b")
_NON_BOARD_ID = re.compile(r"(?:task|issue|PR|ticket|commit|任务|卡片|工单)\s*$", re.IGNORECASE)


def validate_human_request(text: str) -> None:
    """Reject board references before a human request is persisted or delivered.

    This checks observable reference syntax, not whether prose is semantically complete;
    the shared prompt requires the latter. Task/issue numbers and ordinary quantities are valid.
    """
    if _BOARD_REFERENCE.search(text) or any(
        not _NON_BOARD_ID.search(text[max(0, m.start() - 24):m.start()])
        for m in _HASH_ID.finditer(text)
    ):
        raise ValueError("Human requests must be self-contained. Rewrite this message with the exact "
                         "ask, relevant facts, full scope/options and recommendation. Remove thread/comment "
                         "numbers and instructions to look up earlier messages. Read and summarize that "
                         "history yourself; keep board IDs only in tool arguments. Nothing was posted.")
