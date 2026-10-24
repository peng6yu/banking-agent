"""AA 拆分那一路：按收款人个数动态生成查询节点，再一次性拆分转账。

为什么单独成文件：app/planner/dag.py 顶到 400 行硬上限（docs/08 编码规范）。
为什么不能用固定模板：dag._nodes() 用 {工具: 节点id} 建索引，同一个工具出现两次
会互相覆盖参数，多人 AA 必须自己造 Dag。

收款人消歧（2026-10-24 修）：
旧实现用 `@n1.candidates.0.payee_id`，等于「同名多人时默认挑第一个」。
现在的规则是：
1. 每个参与人一个 resolve_payee 节点，引用写成 `candidates.unique.payee_id`——
   dag._lookup 的 unique 段要求候选**恰好一个**，0 个或多个都抛
   DAG_PAYEE_AMBIGUOUS，绝不再有「默默取第 0 个」；
2. 参与人可以在名字后面直接写卡号尾号（「张三 尾号4417」「张三（4417）」），
   这会作为 card_tail 传给 resolve_payee，把同名的人一次定死；
3. 名单里出现「同名且都没尾号」的重复参与人，建图阶段就拒绝（DAG_SPLIT_DUPLICATE），
   不生成任何计划；
4. 任何一位参与人没解析出唯一收款人，执行器会在 resolve_payee 节点停下来问，
   后面的 split_transfer 根本不会拿到令牌（见 app/planner/runner.py 的 _payee_gate）。
"""

from __future__ import annotations

import re
from typing import Any, Final

from app.clock import ValidationError
from app.ids import new_id
from app.models import Dag, DAGNode

SPLIT_INTENT: Final[str] = "split_bill"
SPLIT_NAMES_SLOT: Final[str] = "payee_names"
MIN_PARTS: Final[int] = 2
MAX_PARTS: Final[int] = 10
# 「张三 尾号4417」「张三（4417）」「张三尾号4417」都认；只认明确的尾号写法，
# 不把「张三4417」这种黏在一起的当尾号，名字里带数字的人不该被误拆。
_TAIL_PATTERNS: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(r"^(?P<name>.+?)\s*(?:尾号|卡号|后四位|末四位)\s*(?P<tail>\d{4})$"),
    re.compile(r"^(?P<name>.+?)\s*[（(]\s*(?P<tail>\d{4})\s*[)）]$"),
    re.compile(r"^(?P<name>.+?)\s+(?P<tail>\d{4})$"),
)


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


def parse_participant(raw: str) -> tuple[str, str | None]:
    """把「张三 尾号4417」拆成 (张三, 4417)；没有尾号时第二项是 None。"""
    text = str(raw or "").strip()
    for pattern in _TAIL_PATTERNS:
        matched = pattern.match(text)
        if matched is None:
            continue
        name = matched.group("name").strip(" （()）、,，")
        tail = matched.group("tail")
        if name:
            return name, tail
    return text.strip(" （()）、,，"), None


def build_split(slots: dict[str, Any]) -> Dag:
    """一位参与人一个查询节点，最后一个节点一次性拆分转账。"""
    names = _names(slots)
    if len(names) < MIN_PARTS:
        raise ValidationError("AA 拆分至少要两个人", reason="DAG_SPLIT_TOO_FEW")
    if len(names) > MAX_PARTS:
        raise ValidationError("AA 拆分最多十个人", reason="DAG_SPLIT_TOO_MANY")
    amount = slots.get("amount_fen")
    if isinstance(amount, bool) or not isinstance(amount, int) or amount <= 0:
        raise ValidationError("缺金额", reason="DAG_MISSING_SLOT", field="amount_fen")
    participants: list[tuple[str, str | None]] = [parse_participant(name) for name in names]
    if any(not name for name, _tail in participants):
        raise ValidationError("AA 名单里有空名字", reason="DAG_SPLIT_EMPTY_NAME")
    # 同名、又都没写尾号：这不是「两个人」，是同一个人的两种写法，建图阶段就拒绝，
    # 免得执行器问「你要哪一个」时用户其实只想转给一个人两次（工具层也不允许重复收款人）。
    seen: set[tuple[str, str | None]] = set()
    for participant in participants:
        if participant in seen:
            raise ValidationError(
                "AA 名单里有重复的参与人", reason="DAG_SPLIT_DUPLICATE", payee=participant[0]
            )
        seen.add(participant)
    nodes: list[DAGNode] = []
    payee_ids: list[str] = []
    previous = ""
    for index, (name, tail) in enumerate(participants, start=1):
        node_id = "n" + str(index)
        params: dict[str, Any] = {"name": name}
        if tail:
            params["card_tail"] = tail
        nodes.append(
            DAGNode(
                id=node_id,
                tool="resolve_payee",
                params=params,
                depends_on=[previous] if previous else [],
            )
        )
        # unique：候选必须恰好一个。0 个或多个都抛 DAG_PAYEE_AMBIGUOUS，
        # 不许再退回「取第 0 个」——那正是同名候选被静默转账的入口。
        payee_ids.append("@" + node_id + ".candidates.unique.payee_id")
        previous = node_id
    params = {"payee_ids": payee_ids, "amount_fen": amount}
    note = str(slots.get("note") or "").strip()
    if note:
        params["note"] = note
    nodes.append(
        DAGNode(
            id="n" + str(len(participants) + 1),
            tool="split_transfer",
            params=params,
            depends_on=[previous],
        )
    )
    return Dag(id=new_id("r"), nodes=nodes)
