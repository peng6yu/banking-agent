"""订阅工具：列订阅、取消订阅。

对应文档：docs/06_工具层与场景实现规格.md 6.3 list_subs / cancel_subscription。

cancel_subscription 是写工具，第一句就是 require_token：没有令牌直接
MissingCapability（docs/05 5.3）。已经取消过的再取消一次算幂等成功。
"""

from __future__ import annotations

import time
from typing import Any

from app import audit, db
from app.clock import now_ms
from app.models import ToolResult
from app.money import fen_to_yuan_str
from app.tools.base import (
    ToolContext,
    fail_result,
    ok_result,
    primary_account,
    require_token,
)

_SUBS_SQL = "SELECT * FROM subs WHERE account_id = ? ORDER BY next_charge_ms, id"


def list_subs(ctx: ToolContext, params: dict[str, Any], token: str | None = None) -> ToolResult:
    """列订阅：默认只列生效中的，include_cancelled=True 才带上已取消的。"""
    account = primary_account(ctx.conn, ctx.account_id)
    include_cancelled = bool(params.get("include_cancelled", False))
    rows = ctx.conn.execute(_SUBS_SQL, (account["id"],)).fetchall()
    subs = [
        _sub_view(row)
        for row in rows
        if include_cancelled or str(row["status"]) == "active"
    ]
    monthly = sum(
        int(row["amount_fen"])
        for row in subs
        if str(row["period"]) == "monthly" and str(row["status"]) == "active"
    )
    return ok_result(
        {
            "account_tail": str(account["tail"]),
            "subs": subs,
            "count": len(subs),
            "monthly_total_fen": monthly,
            "monthly_total_yuan": fen_to_yuan_str(monthly),
        }
    )


def cancel_subscription(
    ctx: ToolContext, params: dict[str, Any], token: str | None = None
) -> ToolResult:
    """取消订阅：写工具，没令牌当场拒绝；已取消过算幂等成功。"""
    require_token("cancel_subscription", params, token)
    sub_id = str(params.get("sub_id", ""))
    account = primary_account(ctx.conn, ctx.account_id)
    row = ctx.conn.execute(
        "SELECT * FROM subs WHERE id = ? AND account_id = ?", (sub_id, account["id"])
    ).fetchone()
    if row is None:
        return fail_result("SUB_NOT_FOUND", f"没找到这条订阅（{sub_id}）。")
    if str(row["status"]) == "cancelled":
        return ok_result(_cancel_payload(row, idempotent=True))
    with db.tx(ctx.conn):
        changed = ctx.conn.execute(
            "UPDATE subs SET status = 'cancelled' WHERE id = ? AND status = 'active'",
            (sub_id,),
        ).rowcount
    if changed != 1:
        return ok_result(_cancel_payload(row, idempotent=True))
    # audit_log 的 event_type 是闭集，这里只能用 tool.executed 记录状态变更
    seq = audit.append(
        ctx.conn,
        "tool.executed",
        {
            "tool": "cancel_subscription",
            "sub_id": sub_id,
            "merchant": str(row["merchant"]),
            "state_change": "active->cancelled",
        },
        session_id=ctx.session_id,
        task_id=ctx.task_id,
    )
    return ToolResult(ok=True, data=_cancel_payload(row, idempotent=False), audit_seq=seq)


def _sub_view(row) -> dict[str, Any]:
    amount_fen = int(row["amount_fen"])
    charge_ms = int(row["next_charge_ms"])
    return {
        "sub_id": str(row["id"]),
        "merchant": str(row["merchant"]),
        "amount_fen": amount_fen,
        "amount_yuan": fen_to_yuan_str(amount_fen),
        "period": str(row["period"]),
        "status": str(row["status"]),
        "next_charge_ms": charge_ms,
        "next_charge_text": time.strftime("%Y-%m-%d", time.localtime(charge_ms / 1000)),
    }


def _cancel_payload(row, idempotent: bool) -> dict[str, Any]:
    return {
        "sub_id": str(row["id"]),
        "merchant": str(row["merchant"]),
        "status": "cancelled",
        "next_charge_ms": int(row["next_charge_ms"]),
        "idempotent": idempotent,
        "message": "这条订阅已经是取消状态了。" if idempotent else "已经取消，下次扣费不会再发生。",
        "ts_ms": now_ms(),
    }
