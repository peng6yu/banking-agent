"""银行卡查询工具。

对应文档：docs/06_工具层与场景实现规格.md 6.3 list_cards。
只读，按账户后四位可选过滤。
"""

from __future__ import annotations

from typing import Any

from app.models import ToolResult
from app.money import fen_to_yuan_str
from app.tools.base import (
    ToolContext,
    fail_result,
    find_account_by_tail,
    ok_result,
    primary_account,
)


def list_cards(ctx: ToolContext, params: dict[str, Any], token: str | None = None) -> ToolResult:
    """列卡：传了尾号只回那一张对应的卡，查不到就是 ACCOUNT_NOT_FOUND。"""
    tail = params.get("account_tail")
    account = primary_account(ctx.conn, ctx.account_id)
    if tail is not None:
        account = find_account_by_tail(ctx.conn, str(tail), ctx.account_id)
        if account is None:
            return fail_result("ACCOUNT_NOT_FOUND", f"没找到尾号 {tail} 的账户。")
    rows = ctx.conn.execute(
        "SELECT c.*, a.tail AS account_tail FROM cards c"
        " JOIN accounts a ON a.id = c.account_id WHERE c.account_id = ? ORDER BY c.id",
        (account["id"],),
    ).fetchall()
    cards = [_card_view(row) for row in rows]
    return ok_result(
        {
            "cards": cards,
            "count": len(cards),
            "account_tail": str(account["tail"]),
            "summary": f"共 {len(cards)} 张卡，账户尾号 {account['tail']}。",
        }
    )


def _card_view(row) -> dict[str, Any]:
    limit_fen = int(row["per_tx_limit_fen"])
    return {
        "card_id": str(row["id"]),
        "account_tail": str(row["account_tail"]),
        "tail": str(row["tail"]),
        "kind": str(row["kind"]),
        "status": str(row["status"]),
        "per_tx_limit_fen": limit_fen,
        "per_tx_limit_yuan": fen_to_yuan_str(limit_fen),
    }
