"""消息视图：把库里的行拼成页面能直接渲染、且用户能自己复核的卡片。

只往外说事实：文案与数字全部来自消息行（生成时已写死 facts），这里不做二次解释，
也不替用户判断「这条要不要采纳」。
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from app import messages, timefmt

#: 每条消息都带三种反馈动作；标签是前端契约，别让页面自己再写一套中文。
FEEDBACK_ORDER: tuple[str, ...] = ("adopted", "dismissed", "muted")


def _json(raw: Any, fallback: Any) -> Any:
    try:
        return json.loads(raw or "")
    except (TypeError, ValueError):
        return fallback


def message_view(row: sqlite3.Row) -> dict[str, Any]:
    facts = _json(row["facts_json"], {})
    actions = _json(row["actions_json"], [])
    created = int(row["created_ms"] or 0)
    status = str(row["status"] or "new")
    return {
        "message_id": str(row["id"]),
        "insight_id": str(row["insight_id"]),
        "severity": str(row["severity"] or "info"),
        "title": str(row["title"]),
        "body": str(row["body"]),
        "facts": facts if isinstance(facts, dict) else {},
        "actions": actions if isinstance(actions, list) else [],
        "status": status,
        "status_label": messages.STATUS_LABELS.get(status, status),
        "created_ms": created,
        "created_text": timefmt.stamp_text(created) if created else "",
        "feedback_actions": [
            {"action": key, "label": messages.FEEDBACK_LABELS[key], "primary": key == "adopted"}
            for key in FEEDBACK_ORDER
        ],
    }


def messages_view(conn: sqlite3.Connection, *, user_id: str | None = None, limit: int = 20) -> dict[str, Any]:
    uid = user_id or messages.first_user_id(conn)
    rows = messages.list_messages(conn, uid, limit=limit)
    items = [message_view(row) for row in rows]
    return {
        "items": items,
        "count": len(items),
        "counts": messages.counts(conn, uid),
        "daily": messages.daily_usage(conn, uid),
        "muted_insights": messages.muted_insights(conn, uid),
        "empty_reason": None if items else (
            "现在没有要提醒你的事。助手每天最多说三条，同一类事 7 天内不会重复；"
            "觉得吵可以点「这类别再提醒」。"
        ),
        "source": "messages 表（探测器只读账务表，不调模型）",
        "generated_text": items[0]["created_text"] if items else None,
    }


def insights_view() -> dict[str, Any]:
    """洞察目录：用户问「你凭什么想到这个」时，页面能摊开数据来源。"""
    return {
        "insights": [dict(item) for item in messages.CATALOG],
        "count": len(messages.CATALOG),
        "source": "app/messages.py 的 CATALOG（与探测器一一对应）",
    }


__all__ = ["message_view", "messages_view", "insights_view"]
