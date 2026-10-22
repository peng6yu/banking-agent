"""只读查询工具：余额、流水、收款人。

对应文档：docs/06_工具层与场景实现规格.md 6.3 query_balance /
query_transactions / resolve_payee。账单分析在 app/tools/bill.py。
只读工具不需要能力令牌，policy 层可以直接调。
"""

from __future__ import annotations

import sqlite3
import time
from typing import Any, Final

from app.clock import now_ms
from app.models import ToolResult
from app.money import fen_to_yuan_str
from app.tools.base import (
    ToolContext,
    account_rows,
    fail_result,
    find_account_by_tail,
    ok_result,
    primary_account,
)

CATEGORIES: Final[tuple[str, ...]] = (
    "餐饮", "交通", "购物", "生活缴费", "娱乐", "医疗", "工资", "理财", "转账", "订阅",
)
DEFAULT_DAYS: Final[int] = 30
MAX_DAYS: Final[int] = 365
DEFAULT_LIMIT: Final[int] = 20
MAX_LIMIT: Final[int] = 100


def query_balance(ctx: ToolContext, params: dict[str, Any], token: str | None = None) -> ToolResult:
    """查余额：不传尾号回全部账户，主账户排第一；传了尾号查不到就是 ACCOUNT_NOT_FOUND。"""
    tail = params.get("account_tail")
    rows = account_rows(ctx.conn, ctx.account_id)
    if tail is not None:
        hit = find_account_by_tail(ctx.conn, str(tail), ctx.account_id)
        if hit is None:
            return fail_result(
                "ACCOUNT_NOT_FOUND", f"没找到尾号 {tail} 的账户，你名下只有这些卡。"
            )
        rows = [hit]
    primary = primary_account(ctx.conn, ctx.account_id)
    ordered = sorted(rows, key=lambda row: (str(row["id"]) != str(primary["id"]), str(row["id"])))
    return ok_result(
        {
            "accounts": [_account_view(row) for row in ordered],
            "primary_tail": str(primary["tail"]),
            "count": len(ordered),
        }
    )


def query_transactions(
    ctx: ToolContext, params: dict[str, Any], token: str | None = None
) -> ToolResult:
    """查流水：支持天数/分类/金额区间/条数；空结果照常返回，只是标 empty。"""
    category = params.get("category")
    if category is not None and str(category) not in CATEGORIES:
        return fail_result(
            "BAD_CATEGORY",
            f"没有「{category}」这个分类。",
            {"valid_categories": list(CATEGORIES)},
        )
    days = int(params.get("days", DEFAULT_DAYS))
    truncated = days > MAX_DAYS
    effective_days = min(days, MAX_DAYS)
    limit = min(int(params.get("limit", DEFAULT_LIMIT)), MAX_LIMIT)
    upper_ms = now_ms()
    since_ms = upper_ms - effective_days * 86_400_000
    account = primary_account(ctx.conn, ctx.account_id)
    rows = fetch_transactions(
        ctx.conn, account["id"], since_ms, upper_ms, category, params, limit
    )
    data: dict[str, Any] = {
        "account_tail": str(account["tail"]),
        "days": effective_days,
        "count": len(rows),
        "items": [_tx_view(row) for row in rows],
    }
    if truncated:
        data["truncated"] = True
    if not rows:
        data["empty"] = True
        data["summary"] = "这段时间没有查到符合条件的流水。"
    return ok_result(data)


def resolve_payee(ctx: ToolContext, params: dict[str, Any], token: str | None = None) -> ToolResult:
    """按姓名或备注找收款人；同名多个必须带出卡尾号让人能区分。

    用户说了卡尾号就按尾号收窄：同名两人只留尾号相同的那一个，
    「张三尾号4417」一次问清，不在同名人之间反复反问。尾号对不上就一个都不留——
    用户点名的尾号必须真的对上，不能悄悄换成另一个同名的人。
    """
    query_text = str(params.get("name", "")).strip()
    phone = str(params.get("phone") or "").strip()
    rows = _payee_candidates(ctx.conn, query_text, ctx.account_id)
    if phone:
        rows = _filter_phone(rows, phone)
    tail = str(params.get("card_tail") or "").strip()
    if tail:
        rows = [row for row in rows if str(row["card_tail"]) == tail]
    candidates = [
        {
            "payee_id": str(row["id"]),
            "name": str(row["name"]),
            "alias": row["alias"],
            "card_tail": str(row["card_tail"]),
            "bank": str(row["bank"]),
            "phone": row["phone"],
        }
        for row in rows
    ]
    return ok_result({"query": query_text, "candidates": candidates, "count": len(candidates)})


def _account_view(row: sqlite3.Row) -> dict[str, Any]:
    balance_fen = int(row["balance_fen"])
    return {
        "account_id": str(row["id"]),
        "name": str(row["name"]),
        "tail": str(row["tail"]),
        "type": str(row["type"]),
        "balance_fen": balance_fen,
        "balance_yuan": fen_to_yuan_str(balance_fen),
        "credit_limit_fen": int(row["credit_limit_fen"]),
    }


def _tx_view(row: sqlite3.Row) -> dict[str, Any]:
    amount_fen = int(row["amount_fen"])
    return {
        "transaction_id": str(row["id"]),
        "direction": str(row["direction"]),
        "amount_fen": amount_fen,
        "amount_yuan": fen_to_yuan_str(amount_fen),
        "counterparty": str(row["counterparty"]),
        "category": str(row["category"]),
        "note": str(row["note"]),
        "channel": str(row["channel"]),
        "ts_ms": int(row["ts_ms"]),
        "time_text": time.strftime("%Y-%m-%d %H:%M", time.localtime(int(row["ts_ms"]) / 1000)),
    }


def fetch_transactions(
    conn: sqlite3.Connection,
    account_id: str,
    since_ms: int,
    until_ms: int,
    category: str | None,
    params: dict[str, Any] | None,
    limit: int | None,
) -> list[sqlite3.Row]:
    """按账户与时间窗取流水，分类/金额区间/条数都是可选过滤。"""
    sql = "SELECT * FROM transactions WHERE account_id = ? AND ts_ms >= ? AND ts_ms < ?"
    args: list[Any] = [account_id, since_ms, until_ms]
    if category:
        sql += " AND category = ?"
        args.append(str(category))
    if params:
        min_fen = params.get("min_fen")
        max_fen = params.get("max_fen")
        if min_fen is not None:
            sql += " AND amount_fen >= ?"
            args.append(int(min_fen))
        if max_fen is not None:
            sql += " AND amount_fen <= ?"
            args.append(int(max_fen))
    sql += " ORDER BY ts_ms DESC, id"
    if limit is not None:
        sql += " LIMIT ?"
        args.append(int(limit))
    return list(conn.execute(sql, args).fetchall())
def _escape_like(text: str) -> str:
    """LIKE 通配符转义：用户输入里的 % 和 _ 不能当通配符跑。"""
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _user_scope(conn: sqlite3.Connection, account_id: str | None) -> str | None:
    if not account_id:
        return None
    row = conn.execute("SELECT user_id FROM accounts WHERE id = ?", (account_id,)).fetchone()
    return str(row["user_id"]) if row else None


def _payee_candidates(
    conn: sqlite3.Connection, query_text: str, account_id: str | None
) -> list[sqlite3.Row]:
    """先按姓名或备注精确匹配，匹配不到再模糊包含；同名多个都带卡尾号返回。"""
    if not query_text:
        return []
    scope = _user_scope(conn, account_id)
    where = "user_id = ?" if scope else "1 = 1"
    args: list[Any] = [scope] if scope else []
    exact_sql = (
        f"SELECT * FROM payees WHERE {where} AND (name = ? OR alias = ? OR phone = ?)"
        " ORDER BY name, card_tail"
    )
    exact = list(
        conn.execute(exact_sql, [*args, query_text, query_text, query_text]).fetchall()
    )
    if exact:
        return exact
    pattern = f"%{_escape_like(query_text)}%"
    fuzzy_sql = (
        f"SELECT * FROM payees WHERE {where}"
        " AND (name LIKE ? ESCAPE '\\' OR alias LIKE ? ESCAPE '\\'"
        " OR phone LIKE ? ESCAPE '\\')"
        " ORDER BY name, card_tail"
    )
    hits = list(conn.execute(fuzzy_sql, [*args, pattern, pattern, pattern]).fetchall())
    if hits:
        return hits
    # 手机号允许只报后四位/中间几位：纯数字且够长时按尾号段再找一次
    digits = "".join(ch for ch in query_text if ch.isdigit())
    if digits and len(digits) >= 4 and digits == query_text:
        tail_sql = (
            f"SELECT * FROM payees WHERE {where} AND phone LIKE ? ESCAPE '\\'"
            " ORDER BY name, card_tail"
        )
        return list(conn.execute(tail_sql, [*args, f"%{digits}"]).fetchall())
    return []


def _filter_phone(rows: list[sqlite3.Row], phone: str) -> list[sqlite3.Row]:
    """显式给了手机号就只留对得上的；对不上一条都不留，不悄悄换人。"""
    digits = "".join(ch for ch in phone if ch.isdigit())
    if not digits:
        return rows
    return [
        row
        for row in rows
        if row["phone"] and digits in str(row["phone"]).replace(" ", "")
    ]
