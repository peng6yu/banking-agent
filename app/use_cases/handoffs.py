"""人工接管的写动作。

cancel / resume 会顺带续跑执行器，所以这两条保持异步、事务仍交给 app/handoffs.py
自己管：在用例层再包一层事务，会把执行器里「跑一半要落 failed」的写入一起回滚。
"""

from __future__ import annotations

from typing import Any

import app.handoffs as handoff_store
from app import db
from app.clock import BankError
from app.use_cases.base import Outcome, fail, ok, write_case


@write_case
def handoff_create(
    conn: Any,
    *,
    run_id: str,
    reason: str,
    operator: str = "",
    note: str = "",
    request_text: str = "",
    account_id: str = "",
) -> dict[str, Any]:
    """手动建单（幂等）：同一轮已有活跃单时返回原单，不会重复挂单。"""
    return {
        "handoff": handoff_store.create(
            conn,
            run_id=run_id,
            reason=reason,
            note=note,
            request_text=request_text,
            account_id=account_id,
        )
    }


@write_case
def handoff_claim(conn: Any, handoff_id: str, *, operator: str) -> dict[str, Any]:
    """领取一张 pending 单；并发领取只有一个成功。"""
    return {"handoff": handoff_store.claim(conn, handoff_id, operator)}


@write_case
def handoff_reject(conn: Any, handoff_id: str, *, operator: str, note: str = "") -> dict[str, Any]:
    """拒绝这张单：不执行、不动钱，只关闭队列项。"""
    return {"handoff": handoff_store.reject(conn, handoff_id, operator, note)}


@write_case
def handoff_need_info(conn: Any, handoff_id: str, *, operator: str, note: str = "") -> dict[str, Any]:
    """要求补充信息：必须写清楚缺什么（note 空会被领域层拒）。"""
    return {"handoff": handoff_store.need_info(conn, handoff_id, operator, note)}


async def handoff_cancel(handoff_id: str, *, operator: str, reason: str = "") -> Outcome:
    """取消工单，并顺带取消还卡着的那一轮执行（释放 hold）。"""
    try:
        with db.open_conn() as conn:
            row = await handoff_store.cancel(conn, handoff_id, operator, reason)
    except BankError as err:
        return fail(err)
    return ok({"handoff": row})


async def handoff_resume(handoff_id: str, *, operator: str, note: str = "") -> Outcome:
    """恢复：先复核实时业务状态，再按原 DAG 续跑；失败会落 failed + 审计。"""
    try:
        with db.open_conn() as conn:
            data = await handoff_store.resume(conn, handoff_id, operator=operator, note=note)
    except BankError as err:
        return fail(err)
    return ok(data)


__all__ = ["handoff_cancel", "handoff_claim", "handoff_create", "handoff_need_info", "handoff_reject", "handoff_resume"]
