"""储蓄计划：建、授权、划一笔、取消划回、暂停继续。真的动钱。

和 app/use_cases/proactive.py 里那套「只监测不动钱」的目标是同一张表的两面：
这边每一个写操作都可能改余额，所以每条都写审计，并且把 messages.review 带上——
划完钱之后「这个月花超了」之类的提醒要当场重新判一次，不能让用户等下次刷新。
"""

from __future__ import annotations

from typing import Any

import app.messages as messages
import app.proactive as proactive
import app.savings as savings
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
    account_id: str | None = None,
    cadence: str | None = None,
    auto_fen: int = 0,
) -> dict[str, Any]:
    """建计划：给了卡和节奏就当场授权自动划款，没给就只是"算个数"的计划。"""
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
    goal = created["goal"]
    authorized = False
    if account_id and cadence:
        out = savings.authorize(
            conn,
            goal_id=str(goal["goal_id"]),
            account_id=str(account_id),
            cadence=str(cadence),
            auto_fen=int(auto_fen or 0),
        )
        goal = out["goal"]
        authorized = True
    return {
        "goal": goal,
        "authorized": authorized,
        "rule_ids": created["rule_ids"],
        "audit_seq": created["audit_seq"],
        "review": messages.review(conn),
    }


@write_case
def goal_update(conn: Any, goal_id: str, **fields: Any) -> dict[str, Any]:
    """调整目标金额/日期等：只改传了的字段，按新参数重算计划并写审计。"""
    fields.pop("account_id", None)
    fields.pop("cadence", None)
    fields.pop("auto_fen", None)
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


@write_case
def goal_authorize(
    conn: Any,
    goal_id: str,
    *,
    account_id: str,
    cadence: str = "monthly",
    auto_fen: int = 0,
) -> dict[str, Any]:
    """授权每期自动划款：从哪张卡、什么节奏、每期多少（0 = 按缺口自动算）。"""
    out = savings.authorize(
        conn,
        goal_id=str(goal_id),
        account_id=str(account_id),
        cadence=str(cadence),
        auto_fen=int(auto_fen or 0),
        user_id=proactive.default_user_id(conn),
    )
    return {**out, "review": messages.review(conn)}


@write_case
def goal_deposit(conn: Any, goal_id: str, *, amount_fen: int | None = None) -> dict[str, Any]:
    """现在存一笔（手动）：不改节奏，只多划这一笔，流水能追到。"""
    out = savings.deposit(
        conn,
        goal_id=str(goal_id),
        user_id=proactive.default_user_id(conn),
        amount_fen=amount_fen,
        source="manual",
    )
    return {**out, "review": messages.review(conn)}


@write_case
def goal_refund(conn: Any, goal_id: str, *, reason: str = "") -> dict[str, Any]:
    """取消计划并把划过的钱原路划回储蓄卡。"""
    out = savings.refund_goal(
        conn,
        goal_id=str(goal_id),
        user_id=proactive.default_user_id(conn),
        reason=str(reason or ""),
    )
    return {**out, "review": messages.review(conn)}


@write_case
def goal_pause(conn: Any, goal_id: str) -> dict[str, Any]:
    """暂停：不再按周期划，已经划进去的钱留在目标里。"""
    out = savings.pause_goal(conn, goal_id=str(goal_id), user_id=proactive.default_user_id(conn))
    return {**out, "review": messages.review(conn)}


@write_case
def goal_resume(conn: Any, goal_id: str) -> dict[str, Any]:
    """继续：下一期照旧划，不补停掉的那几期。"""
    out = savings.resume_goal(conn, goal_id=str(goal_id), user_id=proactive.default_user_id(conn))
    return {**out, "review": messages.review(conn)}


@write_case
def run_due_now(conn: Any) -> dict[str, Any]:
    """手动把到期的计划跑一遍（页面上那颗「立即执行到期划款」用的就是它）。"""
    out = savings.run_due_goals(conn)
    return {**out, "review": messages.review(conn)}


__all__ = [
    "goal_authorize",
    "goal_create",
    "goal_deposit",
    "goal_pause",
    "goal_refund",
    "goal_resume",
    "goal_update",
    "run_due_now",
]
