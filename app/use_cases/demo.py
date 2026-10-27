"""演示重置：收回智能体动过的钱、清 hold 与限额账，进行中的会话收掉。

调用方（路由）只负责清掉内存里的对话历史；会话语义在建表时就写死在这里，
不把「哪张表归谁」散回路由。
"""

from __future__ import annotations

import logging
from typing import Any

from app import db
from app.clock import BankError
from app.planner import executor
from app.use_cases.base import Outcome, fail, ok

log = logging.getLogger(__name__)


def demo_rewind(conn: Any) -> tuple[str | None, int | None]:
    """把 hold_id 非空的流水（只有智能体会产生）反向冲回，并清掉 hold 与限额。"""
    rows = conn.execute(
        "SELECT account_id, direction, amount_fen FROM transactions WHERE hold_id IS NOT NULL"
    ).fetchall()
    for row in rows:
        # 出去的钱是当时扣掉的，冲回就是加回来；进来的反过来减掉。
        delta = int(row["amount_fen"]) if row["direction"] == "out" else -int(row["amount_fen"])
        conn.execute(
            "UPDATE accounts SET balance_fen = balance_fen + ? WHERE id = ?",
            (delta, row["account_id"]),
        )
    conn.execute("DELETE FROM transactions WHERE hold_id IS NOT NULL")
    conn.execute("DELETE FROM holds")
    # 两本日累计账都要清：账户账是账单口径，用户账才是 R06/R07 的判级口径。
    # 只清账户账会让重演示的下一笔直接撞上上一轮留下的用户级累计。
    conn.execute("DELETE FROM daily_limits")
    conn.execute("DELETE FROM daily_user_limits")
    conn.execute("DELETE FROM confirmations")
    # 对话历史也算演示状态：留上一轮调试时刷进去的几十条重复气泡，答辩第一屏
    # 就是「这句话里有不止一个收款人」连排两屏，像坏了。chat_turns 不是审计表
    # （审计在 audit_log，触发器禁改禁删），清它不动证据链。
    conn.execute("DELETE FROM chat_turns")
    account = conn.execute(
        "SELECT id, balance_fen FROM accounts ORDER BY CASE type WHEN 'debit' THEN 0 ELSE 1 END, id LIMIT 1"
    ).fetchone()
    if account is None:
        return None, None
    return str(account["id"]), int(account["balance_fen"])


def demo_reset(*, reason: str = "演示重置") -> Outcome:
    """重置整个演示：种子数据重建 + 智能体动过的钱冲回 + 进行中的会话收掉。"""
    # 导入放函数里：scripts.seed 会反过来 import app 里的模块，模块级导入会成环。
    from scripts.seed import seed

    for session_id in executor.active_sessions():
        executor.close(session_id)
    try:
        with db.open_conn() as conn, db.tx(conn):
            seed(conn)
            account_id, balance_fen = demo_rewind(conn)
    except BankError as err:
        return fail(err)
    return ok({"reason": reason, "account_id": account_id, "balance_fen": balance_fen})


__all__ = ["demo_reset", "demo_rewind"]
