"""账单异常的「我已核实」：只免打扰，不动钱、不改流水。

为什么单独一条用例而不塞进补录：核实是读后的一个决定，和有没有新流水无关；
它唯一的效果是让这条异常从首页收件箱、账单页、明细标红里消失。

幂等：同一组再点一次，只会新增 0 条（INSERT OR IGNORE），审计照记一条——
「用户又看了一遍」本身也是事实。
"""

from __future__ import annotations

from typing import Any

from app.tools import bill as bill_tool
from app.tools.base import primary_account
from app.use_cases.base import write_case


@write_case
def anomaly_ack(
    conn: Any, *, account_id: str, kind: str, tx_ids: list[str], note: str = ""
) -> dict[str, Any]:
    """把「我已核实，没问题」落到这几笔流水上。

    不指定账户就是主储蓄卡——页面上的入口本来就只长在那一张卡的账单上。
    """
    return bill_tool.ack_anomaly(
        conn,
        account_id=str(account_id or primary_account(conn)["id"]),
        kind=kind,
        tx_ids=tx_ids,
        note=note,
    )


__all__ = ["anomaly_ack"]
