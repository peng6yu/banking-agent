"""AA 拆分转账（场景 1 补齐）：一次授权、N 条台账。

为什么不是「一个工具转 N 次」：
1. 令牌只签发一次，绑的是用户确认过的**总额**；N 次独立转账会让用户点 N 次确认、
   也会在审计里留下 N 条互不相干的授权，事后对不上「这是一次 AA」；
2. 钱仍然只经 funds：funds.initiate_split 自己校验「份额之和 == 令牌授权的总额」，
   工具层不许自己算一遍再扣，否则校验就只是句注释；
3. 除不尽的零头给排在前面的人（一人多一分），总额必须严丝合缝对上。
"""

from __future__ import annotations

import sqlite3
from typing import Any, Final

import app.funds as funds  # noqa: PLR0402
from app.clock import BankError, InsufficientFunds
from app.models import ToolResult
from app.money import fen_to_yuan_str
from app.tools.base import (
    ToolContext,
    build_action,
    fail_result,
    ok_result,
    primary_account,
    require_token,
)

MIN_SPLIT_PARTS: Final[int] = 2
MAX_SPLIT_PARTS: Final[int] = 10


def split_shares(total_fen: int, parts: int) -> list[int]:
    """均分总额，除不尽的零头一人一分给前面的人。总和不许多一分也不许少一分。"""
    if parts < MIN_SPLIT_PARTS or parts > MAX_SPLIT_PARTS:
        raise ValueError(f"分摊人数必须在 {MIN_SPLIT_PARTS}—{MAX_SPLIT_PARTS} 之间")
    if total_fen < parts:
        raise ValueError("总额太小，不够每人一分")
    base, remainder = divmod(total_fen, parts)
    return [base + (1 if index < remainder else 0) for index in range(parts)]


def _payee_owned_by(conn: sqlite3.Connection, payee_id: str, account: sqlite3.Row) -> bool:
    row = conn.execute("SELECT user_id FROM payees WHERE id = ?", (payee_id,)).fetchone()
    return row is not None and str(row["user_id"]) == str(account["user_id"])


def split_transfer(
    ctx: ToolContext, params: dict[str, Any], token: str | None = None
) -> ToolResult:
    """把一笔钱按人数平摊给通讯录里的收款人。"""
    require_token("split_transfer", params, token)
    account = primary_account(ctx.conn, ctx.account_id)
    total_fen = int(params["amount_fen"])
    payee_ids = [str(pid) for pid in params.get("payee_ids") or []]
    if len(payee_ids) < MIN_SPLIT_PARTS:
        return fail_result("SPLIT_TOO_FEW", "AA 至少要两个人。")
    if len(set(payee_ids)) != len(payee_ids):
        return fail_result("SPLIT_DUPLICATE_PAYEE", "同一个收款人不能出现两次。")
    for payee_id in payee_ids:
        if not _payee_owned_by(ctx.conn, payee_id, account):
            return fail_result("PAYEE_NOT_OWNED", "通讯录里没有属于你的这个收款人。")
    shares = split_shares(total_fen, len(payee_ids))
    action = build_action(
        "split_transfer",
        {"payee_ids": payee_ids, "amount_fen": total_fen},
        str(account["id"]),
        total_fen,
    )
    try:
        holds = funds.initiate_split(
            ctx.conn,
            action,
            session_id=ctx.session_key(),
            node_id=ctx.node_key(),
            shares=list(zip(payee_ids, shares, strict=True)),
            token=token,
            secret=ctx.secret,
        )
    except InsufficientFunds as exc:
        return fail_result(
            "INSUFFICIENT_FUNDS",
            f"余额不够：要转 {fen_to_yuan_str(exc.amount_fen)} 元，卡里只有 {fen_to_yuan_str(exc.balance_fen)} 元。",
        )
    return _settle(ctx, payee_ids, shares, holds)


def _settle(
    ctx: ToolContext,
    payee_ids: list[str],
    shares: list[int],
    holds: list[Any],
) -> ToolResult:
    """逐份 commit；某一份失败就把剩下的 hold 释放，不留半截账。"""
    done: list[dict[str, Any]] = []
    problems: list[dict[str, Any]] = []
    committed: set[str] = set()
    for payee_id, amount, hold in zip(payee_ids, shares, holds, strict=True):
        try:
            outcome = funds.commit(ctx.conn, hold.hold_id)
        except BankError as exc:
            problems.append({"payee_id": payee_id, "code": exc.code})
            continue
        committed.add(hold.hold_id)
        done.append(
            {
                "payee_id": payee_id,
                "payee_name": _payee_name(ctx.conn, payee_id),
                "amount_fen": amount,
                "amount_yuan": fen_to_yuan_str(amount),
                "hold_id": hold.hold_id,
                "balance_after_fen": int(outcome.balance_after_fen),
            }
        )
    if problems:
        _release_leftovers(ctx, [h for h in holds if h.hold_id not in committed])
        return fail_result(
            "SPLIT_PARTIAL", "有一份没转成功，剩下的已经不扣款了。",
            shares=done, problems=problems,
        )
    total = sum(shares)
    people = "、".join(f"{item['payee_name']} {item['amount_yuan']} 元" for item in done)
    return ok_result(
        {
            "shares": done,
            "total_fen": total,
            "total_yuan": fen_to_yuan_str(total),
            "parts": len(done),
            "summary": f"已经 AA 转出 {fen_to_yuan_str(total)} 元：{people}。",
        }
    )


def _release_leftovers(ctx: ToolContext, holds: list[Any]) -> None:
    for hold in holds:
        try:
            funds.release(ctx.conn, hold.hold_id)
        except BankError:
            continue


def _payee_name(conn: sqlite3.Connection, payee_id: str) -> str:
    row = conn.execute("SELECT name FROM payees WHERE id = ?", (payee_id,)).fetchone()
    return str(row["name"]) if row is not None else payee_id

