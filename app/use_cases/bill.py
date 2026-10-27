"""账单异常的核实与撤销：只免打扰，不动钱、不改流水。

为什么单独一条用例而不塞进补录：核实是读后的一个决定，和有没有新流水无关；
它唯一的效果是让这条异常从首页收件箱、账单页、明细标红里消失，或者反过来回来。

幂等：同一组再点一次只算「又看了一遍」（审计照记）。结论可以改——先点「我自己刷的」，
后来想起来不对再点「不是我刷的」，应该覆盖成后者，所以工具层用 upsert。
"""

from __future__ import annotations

from typing import Any

from app.clock import ValidationError
from app.tools import bill_acks as acks_tool
from app.tools.base import primary_account
from app.tools.bill import ANOMALY_KINDS
from app.use_cases.base import write_case


def _account_id(conn: Any, account_id: str) -> str:
    """不指定账户就是主储蓄卡——页面上的入口本来就只长在那一张卡的账单上。"""
    return str(account_id or primary_account(conn)["id"])


def _check_kind(kind: str) -> str:
    cleaned = str(kind or "").strip()
    if cleaned not in ANOMALY_KINDS:
        raise ValidationError(
            "不认识的异常类型，刷新页面再来一次。",
            reason="ANOMALY_KIND_UNKNOWN",
            field="kind",
        )
    return cleaned


@write_case
def anomaly_ack(
    conn: Any,
    *,
    account_id: str,
    kind: str,
    tx_ids: list[str],
    note: str = "",
    verdict: str = "mine",
) -> dict[str, Any]:
    """记下用户对这一组流水的结论：自己刷的 / 不是我刷的。"""
    return acks_tool.ack_anomaly(
        conn,
        account_id=_account_id(conn, account_id),
        kind=_check_kind(kind),
        tx_ids=tx_ids,
        note=note,
        verdict=verdict,
    )


@write_case
def anomaly_unack(conn: Any, *, account_id: str, kind: str, tx_ids: list[str]) -> dict[str, Any]:
    """撤销核实：这一组重新挂回待核实。"""
    return acks_tool.unack_anomaly(
        conn,
        account_id=_account_id(conn, account_id),
        kind=_check_kind(kind),
        tx_ids=tx_ids,
    )


__all__ = ["anomaly_ack", "anomaly_unack"]
