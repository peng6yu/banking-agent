"""出账通道闸门：账户状态 + 卡状态 + 单笔限额。

为什么单独一层：卡的状态（冻结/挂失/销户）以前只影响卡自己的列表，
转账链路（app/funds.py）压根不读 cards 表——于是「冻结了这张卡」之后，
用同一个账户发起的转账照样能出账。那是语义错误，不是 UI 问题。

这一层只回答一个问题：这笔钱现在能不能从这个账户出去。
裁决仍然在 policy（该不该做、要不要授权），这里管的是「通道可不可用」。

模型边界（照实写在代码里，也写在 docs/audit/feature_matrix.md）：
- 账户状态取自 accounts.status（active / frozen / closed）；
- 卡状态取 cards.status，只有 active 能用；
- 单笔限额取该账户下所有可用卡里**最大**的那个 per_tx_limit_fen
  （演示环境把卡当作支付通道，一笔转账走哪张卡由持卡人决定，
   所以按最宽松的可用卡算，不虚构一个用户没设过的额度）；
- 账户一张卡都没有时不做卡校验（这类账户出现在部分单元测试里，
  真实数据里每个活账户都有卡）。
"""

from __future__ import annotations

import sqlite3
from typing import Any

from app.clock import PolicyDenied, ValidationError

#: 可出账的账户状态
ACCOUNT_STATUS_OK: frozenset[str] = frozenset({"active"})
#: 可出账的卡状态
CARD_STATUS_OK: frozenset[str] = frozenset({"active"})

ACCOUNT_STATE_TEXT: dict[str, str] = {
    "active": "正常",
    "frozen": "已冻结",
    "closed": "已销户",
}
CARD_STATE_TEXT: dict[str, str] = {
    "active": "正常",
    "frozen": "已冻结",
    "lost": "已挂失",
    "closed": "已销户",
}


def account_status(conn: sqlite3.Connection, account_id: str) -> str:
    """账户状态；查不到账户就报参数错（不是安全拒绝）。"""
    row = conn.execute(
        "SELECT status FROM accounts WHERE id = ?", (account_id,)
    ).fetchone()
    if row is None:
        raise ValidationError("账户不存在", account_id=account_id)
    # 老库刚补列时可能是 NULL，按正常处理；缺省语义是 active
    return str(row["status"] or "active")


def usable_cards(conn: sqlite3.Connection, account_id: str) -> list[sqlite3.Row]:
    """该账户下状态为 active 的卡。"""
    return list(
        conn.execute(
            "SELECT * FROM cards WHERE account_id = ? ORDER BY id", (account_id,)
        ).fetchall()
    )


def _amount_fen(value: Any) -> int | None:
    """金额转 int；没有就不校验限额。"""
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def assert_spendable(
    conn: sqlite3.Connection,
    account_id: str,
    amount_fen: int | None,
) -> dict[str, Any]:
    """出账前最后一道通道检查；不通过就抛 PolicyDenied（带 account/card 事实）。

    返回可用通道信息，供调用方（审计/事件）如实说明「这笔走的是哪张卡」。
    """
    status = account_status(conn, account_id)
    if status not in ACCOUNT_STATUS_OK:
        raise PolicyDenied(
            f"这个账户现在是{ACCOUNT_STATE_TEXT.get(status, status)}状态，不能出账。",
            reason="ACCOUNT_NOT_USABLE",
            account_id=account_id,
            account_status=status,
        )
    cards = usable_cards(conn, account_id)
    if not cards:
        # 该账户没有卡：不做卡级校验（见模块注释里的模型边界）
        return {"card_id": None, "card_tail": None, "per_tx_limit_fen": None, "account_status": status}
    active = [row for row in cards if str(row["status"]) in CARD_STATUS_OK]
    if not active:
        states = "、".join(
            f"{CARD_STATE_TEXT.get(str(row['status']), str(row['status']))}（尾号 {row['tail']}）"
            for row in cards
        )
        raise PolicyDenied(
            f"这个账户下的卡现在都不能用：{states}。换一张可用的卡，或者先解冻。",
            reason="CARD_NOT_USABLE",
            account_id=account_id,
            cards=[{"card_id": str(row["id"]), "tail": str(row["tail"]), "status": str(row["status"])} for row in cards],
        )
    limit_fen = max(int(row["per_tx_limit_fen"] or 0) for row in active)
    chosen = max(active, key=lambda row: int(row["per_tx_limit_fen"] or 0))
    amount = _amount_fen(amount_fen)
    if amount is not None and limit_fen > 0 and amount > limit_fen:
        raise PolicyDenied(
            f"这张卡的单笔限额是 {limit_fen / 100:.2f} 元，这笔 {amount / 100:.2f} 元超了。"
            "可以在「业务管理」里调高单笔限额，或者拆成两笔。",
            reason="CARD_LIMIT_EXCEEDED",
            account_id=account_id,
            card_id=str(chosen["id"]),
            per_tx_limit_fen=limit_fen,
            amount_fen=amount,
        )
    return {
        "card_id": str(chosen["id"]),
        "card_tail": str(chosen["tail"]),
        "per_tx_limit_fen": limit_fen,
        "account_status": status,
    }


__all__ = [
    "ACCOUNT_STATE_TEXT",
    "ACCOUNT_STATUS_OK",
    "CARD_STATE_TEXT",
    "CARD_STATUS_OK",
    "account_status",
    "assert_spendable",
    "usable_cards",
]
