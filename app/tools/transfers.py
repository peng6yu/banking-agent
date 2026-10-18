"""三个动钱的写工具：转账、预授权、商户支付。

对应文档：docs/06_工具层与场景实现规格.md 6.3、docs/05 资金两阶段协议。

三条硬规矩：
1. 每个 handler 的第一句都是 require_token，没有令牌当场 MissingCapability；
2. 余额只经 app/funds 的 hold/commit 动，工具层绝不直接 UPDATE accounts；
3. 工具不判断该不该做，额度/风控/二次因子是 policy 的活（docs/06 6.7）。
"""

from __future__ import annotations

import logging
import sqlite3
from typing import Any, Final

from app import funds
from app.clock import BankError, HoldExpired, InsufficientFunds
from app.models import ToolResult
from app.money import fen_to_yuan_str
from app.security import token as token_mod
from app.tools.base import (
    ToolContext,
    build_action,
    ensure_system_payee,
    fail_result,
    ok_result,
    payee_exists,
    payee_name,
    primary_account,
    require_token,
)

log = logging.getLogger(__name__)

MERCHANT_WHITELIST: Final[dict[str, str]] = {
    "flower_shop": "鲜花店",
    "cake_shop": "蛋糕店",
}

HELD_TEXT: Final[str] = "钱已经锁定，还没有扣款。"
COMMITTED_TEXT: Final[str] = "钱已经转出去了。"
UNSETTLED_TEXT: Final[str] = "钱已经锁定，但扣款没成功，稍后我会重试。"
NOT_OWNED_TEXT: Final[str] = "通讯录里没有属于你的这个收款人。"


def _payee_owned_by(conn: sqlite3.Connection, payee_id: str, account: sqlite3.Row) -> bool:
    """收款人必须在本次操作账户所属用户的通讯录里。

    纵深防御：工具层是能被直接调用的公开接口，不能假设调用方一定先跑过
    resolve_payee（红队 C03 就是绕过它直调 transfer，把他名下的收款人转走了）。
    """
    row = conn.execute("SELECT user_id FROM payees WHERE id = ?", (payee_id,)).fetchone()
    return row is not None and str(row["user_id"]) == str(account["user_id"])


def transfer(
    ctx: ToolContext, params: dict[str, Any], token: str | None = None
) -> ToolResult:
    """转账：一次调用把两阶段资金走完——建 hold 之后立刻 commit（docs/06 6.3）。"""
    require_token("transfer", params, token)
    account = primary_account(ctx.conn, ctx.account_id)
    payee_id = str(params.get("payee_id", ""))
    if not payee_exists(ctx.conn, payee_id):
        return fail_result("PAYEE_NOT_FOUND", f"通讯录里没有这个收款人（{payee_id}）。")
    if not _payee_owned_by(ctx.conn, payee_id, account):
        return fail_result("PAYEE_NOT_OWNED", NOT_OWNED_TEXT)
    amount_fen = int(params["amount_fen"])
    action = build_action("transfer", params, str(account["id"]), amount_fen)
    try:
        hold = funds.initiate(
            ctx.conn,
            action,
            session_id=ctx.session_key(),
            node_id=ctx.node_key(),
            token=token,
            secret=ctx.secret,
        )
    except InsufficientFunds as exc:
        return _insufficient(exc)
    return _settle_transfer(
        ctx,
        {
            "hold_id": hold.hold_id,
            "status": hold.status,
            "amount_fen": amount_fen,
            "amount_yuan": _yuan(amount_fen),
            "payee_id": payee_id,
            "payee_name": payee_name(ctx.conn, payee_id),
            "expires_ms": hold.expires_ms,
            "idempotent": bool(hold.reused),
            "message": HELD_TEXT,
        },
    )


def _settle_transfer(ctx: ToolContext, data: dict[str, Any]) -> ToolResult:
    """第二阶段：立刻 commit，让 transfer 一次调用就把两阶段资金走完。

    不可逆的 commit 必须留在工具层：planner 只消费返回值，不 import app.funds。
    commit 失败不往上抛，把 settle_error 塞进 data，钱仍锁在 hold 里等释放。
    """
    hold_id = str(data["hold_id"])
    try:
        outcome = funds.commit(ctx.conn, hold_id)
    except BankError as exc:
        log.warning("transfer.commit_failed hold=%s code=%s", hold_id, exc.code)
        data["settle_error"] = exc.code
        data["message"] = UNSETTLED_TEXT
        return ok_result(data)
    data["status"] = "committed"
    data["balance_after_fen"] = int(outcome.balance_after_fen)
    data["idempotent"] = bool(outcome.idempotent)
    data["message"] = COMMITTED_TEXT
    return ok_result(data)


def reserve_funds(
    ctx: ToolContext, params: dict[str, Any], token: str | None = None
) -> ToolResult:
    """预授权：只占额度不产生转账流水，收款人是系统占位 p_system。"""
    require_token("reserve_funds", params, token)
    account = primary_account(ctx.conn, ctx.account_id)
    ensure_system_payee(ctx.conn, account)
    amount_fen = int(params["amount_fen"])
    action = build_action("reserve_funds", params, str(account["id"]), amount_fen)
    try:
        hold = funds.initiate(
            ctx.conn,
            action,
            session_id=ctx.session_key(),
            node_id=ctx.node_key(),
            token=token,
            secret=ctx.secret,
            payee_id=funds.SYSTEM_PAYEE_ID,
        )
    except InsufficientFunds as exc:
        return _insufficient(exc)
    return ok_result(
        {
            "hold_id": hold.hold_id,
            "status": hold.status,
            "amount_fen": amount_fen,
            "amount_yuan": _yuan(amount_fen),
            "purpose": str(params.get("purpose", "")),
            "expires_ms": hold.expires_ms,
            "idempotent": bool(hold.reused),
            "message": "这笔钱先给你留着，还没有扣款。",
        }
    )


def pay_merchant(
    ctx: ToolContext, params: dict[str, Any], token: str | None = None
) -> ToolResult:
    """商户支付：给了有效 hold 就直接 commit，没给就现建 hold 再 commit。"""
    require_token("pay_merchant", params, token)
    merchant = str(params.get("merchant", ""))
    if merchant not in MERCHANT_WHITELIST:
        return fail_result(
            "MERCHANT_NOT_ALLOWED",
            f"暂不支持商户 {merchant}。",
            {"allowed_merchants": sorted(MERCHANT_WHITELIST)},
        )
    account = primary_account(ctx.conn, ctx.account_id)
    ensure_system_payee(ctx.conn, account)
    amount_fen = int(params["amount_fen"])
    action = build_action("pay_merchant", params, str(account["id"]), amount_fen)
    hold_id = params.get("hold_id")
    if hold_id:
        return _commit_existing(ctx, str(hold_id), str(account["id"]), token, action, params)
    try:
        hold = funds.initiate(
            ctx.conn,
            action,
            session_id=ctx.session_key(),
            node_id=ctx.node_key(),
            token=token,
            secret=ctx.secret,
            payee_id=funds.SYSTEM_PAYEE_ID,
        )
    except InsufficientFunds as exc:
        return _insufficient(exc)
    return _commit(ctx, hold.hold_id, params)


def _commit_existing(
    ctx: ToolContext,
    hold_id: str,
    account_id: str,
    token: str | None,
    action: Any,
    params: dict[str, Any],
) -> ToolResult:
    """复用已有 hold：先按第 6 步重新校一次令牌，再落第二阶段。"""
    hold = funds.fetch_hold(ctx.conn, hold_id)
    if hold is None or str(hold.account_id) != account_id:
        return fail_result("HOLD_NOT_FOUND", f"找不到这笔预授权（{hold_id}）。")
    token_mod.verify(ctx.conn, str(token), action, secret=ctx.secret)
    return _commit(ctx, hold_id, params)


def _commit(ctx: ToolContext, hold_id: str, params: dict[str, Any]) -> ToolResult:
    try:
        outcome = funds.commit(ctx.conn, hold_id)
    except HoldExpired as exc:
        return fail_result("HOLD_EXPIRED", exc.user_text, {"hold_id": hold_id})
    amount_fen = int(outcome.amount_fen)
    return ok_result(
        {
            "hold_id": hold_id,
            "status": "committed",
            "amount_fen": amount_fen,
            "amount_yuan": _yuan(amount_fen),
            "merchant": str(params.get("merchant", "")),
            "item": str(params.get("item", "")),
            "balance_after_fen": int(outcome.balance_after_fen),
            "idempotent": bool(outcome.idempotent),
            "message": f"已经付了 {params.get('item', '')} {fen_to_yuan_str(amount_fen)} 元。",
        }
    )


def _insufficient(exc: InsufficientFunds) -> ToolResult:
    """余额不够：把还差多少一并说清楚，让人知道差在哪。"""
    balance_fen = int(exc.fields.get("balance_fen", 0))
    amount_fen = int(exc.fields.get("amount_fen", 0))
    short_fen = max(amount_fen - balance_fen, 0)
    return fail_result(
        "INSUFFICIENT_FUNDS",
        f"余额不够，还差 {short_fen / 100:.2f} 元。",
        {
            "balance_fen": balance_fen,
            "amount_fen": amount_fen,
            "short_fen": short_fen,
            "account_id": str(exc.fields.get("account_id", "")),
        },
    )


def _yuan(amount_fen: int) -> str:
    """分转成带千分位的中文金额串。"""
    return fen_to_yuan_str(amount_fen)


__all__ = ["MERCHANT_WHITELIST", "pay_merchant", "reserve_funds", "transfer"]
