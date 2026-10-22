
"""定时转账（场景 1 补齐）：预约不立刻动钱，到点由系统时钟执行。

三条边界：
1. 预约本身不动钱——只在 scheduled_transfers 里留一行，到点执行时才走 hold/commit；
2. 预约是资金指令，走黄灯确认（R05），且令牌里的 params_hash 覆盖到期时间和收款人；
3. 取消只对还没执行的预约生效，已执行的只能靠冲正（reverse_transfer）。

执行由 app/scheduler.py 负责，它是一个系统时钟驱动的受信组件，
不经模型、也不经意图层——令牌在这里由系统自己签发并留 token.issued 审计。
"""

from __future__ import annotations

import datetime
import logging
import sqlite3
from typing import Any, Final

import app.audit as audit  # noqa: PLR0402
import app.db as db  # noqa: PLR0402
import app.funds as funds  # noqa: PLR0402
import app.ids as ids  # noqa: PLR0402
from app.clock import ValidationError, now_ms
from app.ids import idempotency_key
from app.models import ToolResult
from app.money import fen_to_yuan_str
from app.security import token as token_mod
from app.tools.base import (
    ToolContext,
    build_action,
    fail_result,
    ok_result,
    payee_exists,
    payee_name,
    primary_account,
    require_token,
)

log = logging.getLogger(__name__)

RECURRENCES: Final[tuple[str, ...]] = ("once", "monthly")
MAX_SCHEDULE_AHEAD_MS: Final[int] = 366 * 24 * 3600 * 1000
SCHEDULED_TEXT: Final[str] = "已经帮你记下了，到时间会自动转。"


def _new_id(prefix: str) -> str:
    return ids.new_id(prefix)


def _read_due(params: dict[str, Any]) -> int:
    due = params.get("due_ms")
    if not isinstance(due, int):
        raise ValidationError("缺少执行时间", reason="MISSING_DUE_MS", field="due_ms")
    now = now_ms()
    if due <= now:
        raise ValidationError("执行时间必须晚于现在", reason="DUE_IN_PAST", field="due_ms")
    if due - now > MAX_SCHEDULE_AHEAD_MS:
        raise ValidationError("执行时间太远了", reason="DUE_TOO_FAR", field="due_ms")
    return due


def schedule_transfer(
    ctx: ToolContext, params: dict[str, Any], token: str | None = None
) -> ToolResult:
    """预约一笔转账：只写一行指令，不动钱。"""
    require_token("schedule_transfer", params, token)
    try:
        due = _read_due(params)
    except ValidationError as exc:
        return fail_result(str(exc.fields.get("reason") or exc.code), exc.user_text)
    account = primary_account(ctx.conn, ctx.account_id)
    payee_id = str(params.get("payee_id", ""))
    if not payee_exists(ctx.conn, payee_id):
        return fail_result("PAYEE_NOT_FOUND", "通讯录里没有这个收款人。")
    amount_fen = int(params["amount_fen"])
    due_ms = _read_due(params)
    recurrence = str(params.get("recurrence") or "once")
    if recurrence not in RECURRENCES:
        raise ValidationError("不认识的重复方式", reason="BAD_RECURRENCE", recurrence=recurrence)
    action = build_action(
        "schedule_transfer", {"payee_id": payee_id, "amount_fen": amount_fen, "due_ms": due_ms},
        str(account["id"]), amount_fen,
    )
    key = idempotency_key(ctx.session_key(), ctx.node_key(), str(params.get("note") or ""))
    row_id = _new_id("sch")
    with _tx(ctx.conn):
        ctx.conn.execute(
            "INSERT INTO scheduled_transfers (id, user_id, account_id, payee_id, amount_fen, note,"
            " due_ms, recurrence, status, created_ms, idempotency_key)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'scheduled', ?, ?)",
            (row_id, str(account["user_id"]), str(account["id"]), payee_id, amount_fen,
             str(params.get("note") or ""), due_ms, recurrence, now_ms(), key),
        )
        audit.append(
            ctx.conn,
            "scheduled.created",
            {"scheduled_id": row_id, "payee_id": payee_id, "amount_fen": amount_fen,
             "due_ms": due_ms, "recurrence": recurrence,
             "params_hash": token_mod.params_hash(action.params)},
            session_id=ctx.session_key(),
        )
    who = payee_name(ctx.conn, payee_id)
    when = _time_text(due_ms)
    extra = "，每月重复" if recurrence == "monthly" else ""
    return ok_result(
        {
            "scheduled_id": row_id,
            "payee_id": payee_id,
            "payee_name": who,
            "amount_fen": amount_fen,
            "amount_yuan": fen_to_yuan_str(amount_fen),
            "due_ms": due_ms,
            "recurrence": recurrence,
            "status": "scheduled",
            "summary": f"已预约 {when} 向 {who} 转出 {fen_to_yuan_str(amount_fen)} 元{extra}。{SCHEDULED_TEXT}",
        }
    )


def list_scheduled(
    ctx: ToolContext, params: dict[str, Any], token: str | None = None
) -> ToolResult:
    """列出还没执行的预约转账。"""
    rows = ctx.conn.execute(
        "SELECT s.id, s.amount_fen, s.due_ms, s.recurrence, s.status, p.name AS payee"
        " FROM scheduled_transfers s JOIN payees p ON p.id = s.payee_id"
        " WHERE s.status = 'scheduled' ORDER BY s.due_ms",
    ).fetchall()
    items = [
        {
            "scheduled_id": str(row["id"]),
            "payee_name": str(row["payee"]),
            "amount_fen": int(row["amount_fen"]),
            "amount_yuan": fen_to_yuan_str(int(row["amount_fen"])),
            "due_ms": int(row["due_ms"]),
            "due_text": _time_text(int(row["due_ms"])),
            "recurrence": str(row["recurrence"]),
        }
        for row in rows
    ]
    if not items:
        return ok_result({"count": 0, "items": [], "summary": "现在没有待执行的预约转账。"})
    return ok_result(
        {"count": len(items), "items": items, "summary": f"你有 {len(items)} 笔待执行的预约转账。"}
    )


def cancel_scheduled(
    ctx: ToolContext, params: dict[str, Any], token: str | None = None
) -> ToolResult:
    """取消一笔还没执行的预约；已经执行过的取消不了。"""
    require_token("cancel_scheduled", params, token)
    row_id = str(params.get("scheduled_id") or "").strip()
    row = ctx.conn.execute(
        "SELECT id, status FROM scheduled_transfers WHERE id = ?", (row_id,)
    ).fetchone()
    if row is None:
        return fail_result("SCHEDULED_NOT_FOUND", "没找到这条预约。")
    if str(row["status"]) == "cancelled":
        return ok_result({"scheduled_id": row_id, "status": "cancelled", "idempotent": True,
                          "summary": "这条预约已经取消过了。"})
    if str(row["status"]) != "scheduled":
        return fail_result("SCHEDULED_NOT_CANCELLABLE", "这笔预约已经执行过了，取消不了；需要的话我可以帮你冲正。")
    with _tx(ctx.conn):
        ctx.conn.execute(
            "UPDATE scheduled_transfers SET status = 'cancelled' WHERE id = ? AND status = 'scheduled'",
            (row_id,),
        )
        audit.append(ctx.conn, "scheduled.cancelled", {"scheduled_id": row_id},
                     session_id=ctx.session_key())
    return ok_result({"scheduled_id": row_id, "status": "cancelled", "summary": "这条预约已经取消了。"})


def execute_due(
    conn: sqlite3.Connection, now: int | None = None, secret: str | None = None
) -> list[dict[str, Any]]:
    """把到点的预约真正转出去。由系统时钟调用，不经模型。

    钱一律经 app/funds 的 hold/commit，不在这个函数里直接改余额——定时执行也不能例外。
    """
    stamp = now if now is not None else now_ms()
    rows = conn.execute(
        "SELECT * FROM scheduled_transfers WHERE status = 'scheduled' AND due_ms <= ?"
        " ORDER BY due_ms",
        (stamp,),
    ).fetchall()
    done: list[dict[str, Any]] = []
    for row in rows:
        result = _execute_one(conn, row, stamp, secret)
        done.append(result)
    return done


def _execute_one(
    conn: sqlite3.Connection, row: sqlite3.Row, stamp: int, secret: str | None = None
) -> dict[str, Any]:
    """执行单条预约：签系统令牌、建 hold、commit、写审计，失败置 failed 不吞异常。"""
    sched_id = str(row["id"])
    payee_id = str(row["payee_id"])
    amount_fen = int(row["amount_fen"])
    action = build_action(
        "scheduled_transfer",
        {"payee_id": payee_id, "amount_fen": amount_fen, "due_ms": int(row["due_ms"])},
        str(row["account_id"]),
        amount_fen,
    )
    session_id = f"sched:{sched_id}"
    try:
        tok = token_mod.issue_audited(conn, action, session_id=session_id, level="yellow", secret=secret)
        hold = funds.initiate(
            conn, action, session_id=session_id, node_id="due", token=tok, payee_id=payee_id, secret=secret
        )
        outcome = funds.commit(conn, hold.hold_id)
        with _tx(conn):
            conn.execute(
                "UPDATE scheduled_transfers SET status = ?, executed_ms = ? WHERE id = ?",
                ("executed" if str(row["recurrence"]) == "once" else "scheduled", stamp, sched_id),
            )
            if str(row["recurrence"]) == "monthly":
                conn.execute(
                    "UPDATE scheduled_transfers SET due_ms = ? WHERE id = ?",
                    (_next_month_ms(int(row["due_ms"])), sched_id),
                )
            audit.append(
                conn,
                "scheduled.executed",
                {"scheduled_id": sched_id, "hold_id": hold.hold_id,
                 "amount_fen": amount_fen, "balance_after_fen": int(outcome.balance_after_fen)},
                session_id=session_id,
            )
        return {"scheduled_id": sched_id, "status": "executed", "amount_fen": amount_fen}
    except Exception as exc:  # noqa: BLE001 - 一条失败不能拖垮整批，但要留痕
        log.warning("scheduled.execute_failed id=%s err=%s", sched_id, exc)
        with _tx(conn):
            conn.execute(
                "UPDATE scheduled_transfers SET status = 'failed', executed_ms = ? WHERE id = ?",
                (stamp, sched_id),
            )
        return {"scheduled_id": sched_id, "status": "failed", "error": str(exc)}


def _next_month_ms(due_ms: int) -> int:
    """按月重复的下一期：固定 30 天，演示场景够用，不做日历月推算。"""
    return due_ms + 30 * 24 * 3600 * 1000


def _time_text(ms: int) -> str:
    stamp = datetime.datetime.fromtimestamp(ms / 1000, tz=datetime.UTC).astimezone()
    return stamp.strftime("%Y-%m-%d %H:%M")


def _tx(conn: sqlite3.Connection) -> Any:
    return db.tx(conn)

