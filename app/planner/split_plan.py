"""AA 拆分那一路：按收款人个数动态生成查询节点，再一次性拆分转账。

为什么单独成文件：app/planner/dag.py 顶到 400 行硬上限（docs/08 编码规范）。
为什么不能用固定模板：dag._nodes() 用 {工具: 节点id} 建索引，同一个工具出现两次
会互相覆盖参数，多人 AA 必须自己造 Dag。

已知局限：同名收款人（种子里有两个「张三」）会取第一个候选。AA 场景按名字
指定多人，出现重名时用户应改用不同称呼；这一点写在 docs/06 里。
"""

from __future__ import annotations

from typing import Any, Final

from app.clock import ValidationError
from app.ids import new_id
from app.models import Dag, DAGNode

SPLIT_INTENT: Final[str] = "split_bill"
SPLIT_NAMES_SLOT: Final[str] = "payee_names"
MIN_PARTS: Final[int] = 2
MAX_PARTS: Final[int] = 10


def _names(slots: dict[str, Any]) -> list[str]:
    """收款人名单：模型给数组；给成逗号分隔的一串也认。"""
    raw = slots.get(SPLIT_NAMES_SLOT)
    if isinstance(raw, str):
        raw = raw.replace("，", ",").split(",")
    if not isinstance(raw, (list, tuple)):
        return []
    names: list[str] = []
    for item in raw:
        text = str(item).replace("，", ",").strip()
        for part in text.split(","):
            name = part.strip()
            if name:
                names.append(name)
    return names


def build_split(slots: dict[str, Any]) -> Dag:
    """一个人一个查询节点，最后一个节点一次性拆分转账。"""
    names = _names(slots)
    if len(names) < MIN_PARTS:
        raise ValidationError("AA 拆分至少要两个人", reason="DAG_SPLIT_TOO_FEW")
    if len(names) > MAX_PARTS:
        raise ValidationError("AA 拆分最多十个人", reason="DAG_SPLIT_TOO_MANY")
    amount = slots.get("amount_fen")
    if isinstance(amount, bool) or not isinstance(amount, int) or amount <= 0:
        raise ValidationError("缺金额", reason="DAG_MISSING_SLOT", field="amount_fen")
    nodes: list[DAGNode] = []
    payee_ids: list[str] = []
    previous = ""
    for index, name in enumerate(names, start=1):
        node_id = "n" + str(index)
        nodes.append(
            DAGNode(
                id=node_id,
                tool="resolve_payee",
                params={"name": name},
                depends_on=[previous] if previous else [],
            )
        )
        payee_ids.append("@" + node_id + ".candidates.0.payee_id")
        previous = node_id
    params: dict[str, Any] = {"payee_ids": payee_ids, "amount_fen": amount}
    note = str(slots.get("note") or "").strip()
    if note:
        params["note"] = note
    nodes.append(
        DAGNode(
            id="n" + str(len(names) + 1),
            tool="split_transfer",
            params=params,
            depends_on=[previous],
        )
    )
    return Dag(id=new_id("r"), nodes=nodes)

