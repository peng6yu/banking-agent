"""反问候选的只读取数：SQL 收在视图层，接口层只负责把候选排成人话（第 5 层）。

为什么单独一个文件：以前 payees/cards/fund_holdings/subs 的 SELECT 直接写在
app/api/chat_hints.py 里，接口层既塑形又取数，读路径就永远收不干净。
这里只回原始行（或名字列表），怎么排成「张三（招商银行 尾号 4417）」由调用方决定。
"""

from __future__ import annotations

import sqlite3
from typing import Any, Final

#: 候选上限：反问时最多给 8 条，再多用户不会看。
CHOICE_LIMIT: Final[int] = 8
#: 没有持仓时退回公募产品列表，只给 6 条，避免把整张产品表倒出来。
PRODUCT_FALLBACK_LIMIT: Final[int] = 6


def payees(conn: sqlite3.Connection, limit: int = CHOICE_LIMIT) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT name, bank, card_tail FROM payees ORDER BY id LIMIT ?", (limit,)
    ).fetchall()
    return [dict(row) for row in rows]


def payee_names(conn: sqlite3.Connection, limit: int = CHOICE_LIMIT) -> list[str]:
    rows = conn.execute("SELECT name FROM payees ORDER BY id LIMIT ?", (limit,)).fetchall()
    return [str(row["name"]) for row in rows]


def cards(conn: sqlite3.Connection, user_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT c.tail, c.kind, c.status FROM cards c"
        " JOIN accounts a ON a.id = c.account_id WHERE a.user_id = ?"
        " ORDER BY CASE c.status WHEN 'active' THEN 0 ELSE 1 END, c.id",
        (user_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def products(conn: sqlite3.Connection, user_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT p.name, p.code FROM fund_holdings h"
        " JOIN fund_products p ON p.code = h.product_code WHERE h.user_id = ?",
        (user_id,),
    ).fetchall()
    if rows:
        return [dict(row) for row in rows]
    rows = conn.execute(
        "SELECT name, code FROM fund_products ORDER BY risk_level, code LIMIT ?",
        (PRODUCT_FALLBACK_LIMIT,),
    ).fetchall()
    return [dict(row) for row in rows]


def subscriptions(conn: sqlite3.Connection, account_id: str, limit: int = CHOICE_LIMIT) -> list[str]:
    rows = conn.execute(
        "SELECT merchant FROM subs WHERE account_id = ? AND status = 'active'"
        " ORDER BY merchant LIMIT ?",
        (account_id, limit),
    ).fetchall()
    return [str(row["merchant"]) for row in rows]


__all__ = [
    "CHOICE_LIMIT",
    "PRODUCT_FALLBACK_LIMIT",
    "cards",
    "payee_names",
    "payees",
    "products",
    "subscriptions",
]
