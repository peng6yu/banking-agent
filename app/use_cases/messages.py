"""助手主动提醒的写动作：刷新（生成一轮）与反馈（采纳/忽略/静音）。

为什么必须有这一层：仓库的硬规矩是「写操作只有一条路径」
（路由 -> 用例 -> 领域），路由自己 db.open_conn 会被 tests/test_use_cases.py 拦下。
这里不额外包事务：generate/feedback 内部已经按条/按动作开了事务，
再套一层只会把「生成到第三条失败」变成整轮回滚。
"""

from __future__ import annotations

from typing import Any

from app import db, messages
from app.use_cases.base import ok, write_case
from app.views.messages import message_view, messages_view


@write_case
def message_refresh(conn: Any) -> dict[str, Any]:
    """跑一轮探测器并按频控落库，返回这一轮结果 + 最新列表。"""
    uid = messages.first_user_id(conn)
    result = messages.generate(conn, uid)
    return {**result, "messages": messages_view(conn)}


@write_case
def message_feedback(conn: Any, message_id: str, *, action: str, note: str = "") -> dict[str, Any]:
    """采纳 / 忽略 / 不再提醒：状态翻转 + 记一条反馈 + 必要时静音这一类。"""
    uid = messages.first_user_id(conn)
    row = messages.feedback(conn, uid, message_id, action, note)
    return {"message": message_view(row), "messages": messages_view(conn)}


__all__ = ["message_feedback", "message_refresh"]
