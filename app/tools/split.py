"""AA 拆分转账（场景 1 补齐）：一次授权、N 条台账。

为什么不是「一个工具转 N 次」：
1. 令牌只签发一次，绑的是用户确认过的**总额**；N 次独立转账会让用户点 N 次确认、
   也会在审计里留下 N 条互不相干的授权，事后对不上「这是一次 AA」；
2. 钱仍然只经 funds：funds.initiate_split 自己校验「份额之和 == 令牌授权的总额」，
   工具层不许自己算一遍再扣，否则校验就只是句注释；
3. 除不尽的零头给排在前面的人（一人多一分），总额必须严丝合缝对上。

金额语义（两个字段别混）：
- `amount_fen` / 结果里的 `total_amount_fen`：这次 AA 的**总额**，也是令牌绑定的金额、
  日累计与限额口径，账户实际出账就是这个数；
- `per_person_amount_fen`：**仅在校验时使用**的「每人应付」。除不尽（例如 100 元 3 人）
  时必须省略或给 null —— 硬塞一个 3333 会让三份加起来少 1 分；结果里只在能整除时回填，
  同时永远给出 `shares`（每人的真实份额，含零头），谁多一分一目了然。

收款人侧的第二道闸（第一道在 planner 的 resolve_payee 节点）：
数量、空 id、重复、归属、份额之和，这里全部再查一遍。工具是可以被单独调用的，
planner 的消歧不能当成工具层的前提。
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
    daily_cap_fen,
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


def _clean_payee_ids(raw: Any) -> tuple[list[str], str | None]:
    """把参数里的 payee_ids 归一；返回 (ids, 错误码)。空串/非字符串一律当无效。"""
    if not isinstance(raw, (list, tuple)):
        return [], "SPLIT_BAD_PAYEES"
    ids: list[str] = []
    for item in raw:
        text = str(item or "").strip()
        if not text:
            return [], "SPLIT_BAD_PAYEES"
        ids.append(text)
    return ids, None


def _check_per_person(total_fen: int, parts: int, raw: Any) -> str | None:
    """per_person_amount_fen 只用来校验；对不上就拒绝，绝不按它去改份额。"""
    if raw is None or raw == "":
        return None
    if isinstance(raw, bool) or not isinstance(raw, int) or raw <= 0:
        return "SPLIT_BAD_PER_PERSON"
    if total_fen % parts != 0:
        return "SPLIT_PER_PERSON_MISMATCH"
    if raw * parts != total_fen:
        return "SPLIT_PER_PERSON_MISMATCH"
    return None


def split_transfer(
    ctx: ToolContext, params: dict[str, Any], token: str | None = None
) -> ToolResult:
    """把一笔钱按人数平摊给通讯录里的收款人。"""
    require_token("split_transfer", params, token)
    account = primary_account(ctx.conn, ctx.account_id)
    raw_total = params.get("amount_fen")
    if isinstance(raw_total, bool) or not isinstance(raw_total, int) or raw_total <= 0:
        return fail_result("SPLIT_BAD_AMOUNT", "拆分的总额我没看懂，请给我一个大于零的金额。")
    total_fen = raw_total
    payee_ids, bad = _clean_payee_ids(params.get("payee_ids"))
    if bad is not None:
        return fail_result("SPLIT_BAD_PAYEES", "参与人名单里有空的或无效的收款人。")
    if len(payee_ids) < MIN_SPLIT_PARTS:
        return fail_result("SPLIT_TOO_FEW", "AA 至少要两个人。")
    if len(payee_ids) > MAX_SPLIT_PARTS:
        return fail_result("SPLIT_TOO_MANY", "AA 最多十个人。")
    if len(set(payee_ids)) != len(payee_ids):
        return fail_result("SPLIT_DUPLICATE_PAYEE", "同一个收款人不能出现两次。")
    for payee_id in payee_ids:
        if not _payee_owned_by(ctx.conn, payee_id, account):
            return fail_result("PAYEE_NOT_OWNED", "通讯录里没有属于你的这个收款人。")
    mismatch = _check_per_person(total_fen, len(payee_ids), params.get("per_person_amount_fen"))
    if mismatch == "SPLIT_BAD_PER_PERSON":
        return fail_result("SPLIT_BAD_PER_PERSON", "每人的金额我没看懂。")
    if mismatch == "SPLIT_PER_PERSON_MISMATCH":
        return fail_result(
            "SPLIT_PER_PERSON_MISMATCH",
            f"{fen_to_yuan_str(total_fen)} 元分给 {len(payee_ids)} 个人除不尽，别按每人金额算；我只按总额拆。",
        )
    shares = split_shares(total_fen, len(payee_ids))
    if sum(shares) != total_fen:
        # 理论上到不了这里；真到了说明 split_shares 坏了，宁可不动钱。
        return fail_result("SPLIT_SHARE_SUM", "分摊金额对不上总额，这笔我不做了。")
    # 令牌的指纹是按"调用方传来的参数"算的，这里重建 action 时必须保留可选字段：
    # 少带一个 per_person_amount_fen，params_hash 就对不上，任何带它的请求都会被判成 PARAMS_MISMATCH。
    action_params: dict[str, Any] = {"payee_ids": payee_ids, "amount_fen": total_fen}
    if params.get("per_person_amount_fen") is not None:
        action_params["per_person_amount_fen"] = params["per_person_amount_fen"]
    action = build_action(
        "split_transfer",
        action_params,
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
            run_id=ctx.task_id or "",
            token=token,
            secret=ctx.secret,
        )
    except InsufficientFunds as exc:
        return fail_result(
            "INSUFFICIENT_FUNDS",
            f"余额不够：要转 {fen_to_yuan_str(exc.amount_fen)} 元，卡里只有 {fen_to_yuan_str(exc.balance_fen)} 元。",
        )
    return _settle(ctx, payee_ids, shares, holds, token)


def _settle(
    ctx: ToolContext,
    payee_ids: list[str],
    shares: list[int],
    holds: list[Any],
    token: str | None = None,
) -> ToolResult:
    """逐份 commit；某一份失败就把剩下的 hold 释放，不留半截账。"""
    done: list[dict[str, Any]] = []
    problems: list[dict[str, Any]] = []
    committed: set[str] = set()
    for payee_id, amount, hold in zip(payee_ids, shares, holds, strict=True):
        try:
            outcome = funds.commit(
                ctx.conn, hold.hold_id, daily_limit_fen=daily_cap_fen(token, ctx.secret)
            )
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
    per_person = total // len(shares) if len(shares) and total % len(shares) == 0 else None
    return ok_result(
        {
            "shares": done,
            "total_amount_fen": total,
            "total_fen": total,
            "total_yuan": fen_to_yuan_str(total),
            "per_person_amount_fen": per_person,
            "per_person_amount_yuan": fen_to_yuan_str(per_person) if per_person else None,
            "indivisible": per_person is None,
            "parts": len(done),
            "amount_note": "总额以 total_amount_fen 为准；除不尽时 per_person_amount_fen 为 null，每人的真实份额看 shares。",
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
