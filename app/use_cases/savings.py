"""储蓄目标：建目标 / 改目标。钱一分不动，只调整计划与监测规则。"""

from __future__ import annotations

from typing import Any

import app.messages as messages
import app.proactive as proactive
from app.clock import NotFound
from app.use_cases.base import write_case


@write_case
def goal_create(
    conn: Any,
    *,
    name: str,
    target_fen: int,
    target_date: str,
    saved_fen: int = 0,
    budget_fen: int | None = None,
    progress_tolerance: int | None = None,
    period: str = "monthly",
) -> dict[str, Any]:
    """建储蓄目标：同时落一条进度监测规则，给了预算再加预算规则。写审计。"""
    created = proactive.create_goal(
        conn,
        user_id=proactive.default_user_id(conn),
        name=name,
        target_fen=target_fen,
        target_date=target_date,
        saved_fen=saved_fen,
        budget_fen=budget_fen,
        progress_tolerance=progress_tolerance,
        period=period,
    )
    return {
        "goal": created["goal"],
        "rule_ids": created["rule_ids"],
        "audit_seq": created["audit_seq"],
        # 新建目标会改变「预算/进度」口径，当场复核一遍洞察（频控照旧）。
        "review": messages.review(conn),
    }


@write_case
def goal_update(conn: Any, goal_id: str, **fields: Any) -> dict[str, Any]:
    """调整目标：只改传了的字段，按新参数重算计划并写审计。"""
    updated = proactive.update_goal(
        conn, goal_id, user_id=proactive.default_user_id(conn), **fields
    )
    if updated is None:
        raise NotFound()
    return {
        "goal": updated["goal"],
        "changes": updated["changes"],
        "rule_changes": updated["rule_changes"],
        "audit_seq": updated["audit_seq"],
    }


__all__ = ["goal_create", "goal_update"]
