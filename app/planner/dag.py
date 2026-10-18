"""把意图变成执行图：docs/07 7.3 的固定模板，不允许模型自由发挥。

三条硬规矩：
1. 模板是死的——每个意图走哪几个工具、什么顺序，规格里写死了，模型改不了；
2. 节点之间用 @node.field.path 引用传递结果，不把上一步的原始 JSON 硬抄进下一步；
3. 规划阶段出错（E8）不抛给用户，回退成只跑模板第一个工具的单步图。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Final

from app import clock
from app.ids import new_id
from app.models import Dag, DAGNode
from app.tools.base import ValidationError

REF_PREFIX: Final[str] = "@"
# docs/07 7.3 意图 → 模板，逐条照抄，不许增删改顺序
INTENT_TEMPLATES: Final[dict[str, tuple[tuple[str, str], ...]]] = {
    "query_balance": (("n1", "query_balance"),),
    "bill_analysis": (("n1", "analyze_bill"),),
    "subscription": (("n1", "list_subs"),),
    "transfer": (
        ("n1", "resolve_payee"),
        ("n2", "query_balance"),
        ("n3", "transfer"),
    ),
    "birthday_plan": (
        ("n1", "resolve_payee"),
        ("n2", "query_balance"),
        ("n3", "reserve_funds"),
        ("n4", "pay_merchant"),
    ),
}
DEFAULT_INTENT: Final[str] = "query_balance"
BILL_DEFAULT_PERIOD: Final[str] = "month"
BIRTHDAY_PURPOSE: Final[str] = "生日"
BIRTHDAY_MERCHANT: Final[str] = "flower_shop"
BIRTHDAY_ITEM: Final[str] = "生日鲜花"


def build(intent: dict[str, Any]) -> Dag:
    """按模板生成执行图；缺必填槽位抛 ValidationError，由调用方回退单步。"""
    name, slots = split(intent)
    template = INTENT_TEMPLATES.get(name)
    if template is None:
        raise ValidationError("没有这个意图的模板", intent=name)
    dag = Dag(id=new_id("r"), nodes=_nodes(template, slots))
    validate(dag)
    return dag


def build_or_single(intent: dict[str, Any]) -> Dag:
    """E8：规划阶段任何异常都回退成模板第一个工具的单步图，不中断整轮会话。"""
    try:
        return build(intent)
    except ValidationError:
        name, _ = split(intent)
        template = INTENT_TEMPLATES.get(name) or INTENT_TEMPLATES[DEFAULT_INTENT]
        node_id, tool = template[0]
        return Dag(id=new_id("r"), nodes=[DAGNode(id=node_id, tool=tool, params={})])


def validate(dag: Dag) -> None:
    """四道校验：ID 不重复、依赖不悬空、工具在册、没有环。"""
    seen: set[str] = set()
    for node in dag.nodes:
        if node.id in seen:
            raise ValidationError("DAG 节点 ID 重复", reason="DAG_DUP_ID", node_id=node.id)
        seen.add(node.id)
    for node in dag.nodes:
        for parent in node.depends_on:
            if parent not in seen:
                raise ValidationError("DAG 依赖悬空", reason="DAG_DANGLING", node_id=node.id, parent=parent)
    _check_tools(dag)
    if len(_topo(dag)) != len(dag.nodes):
        raise ValidationError("DAG 里有环", reason="DAG_CYCLE", nodes=[node.id for node in dag.nodes])


def topo(dag: Dag) -> list[DAGNode]:
    """拓扑序；同层按节点 ID 排序，保证每次跑出来的顺序一样。"""
    return _topo(dag)


def ref(node_id: str, *path: str) -> str:
    """造一个 @node.field.path 引用串。"""
    return REF_PREFIX + ".".join([node_id, *path])


def resolve_refs(params: dict[str, Any], results: dict[str, Any]) -> dict[str, Any]:
    """把 @开头的字符串换成上游节点结果里的真实值。"""
    resolved: dict[str, Any] = {}
    for key, value in params.items():
        if isinstance(value, str) and value.startswith(REF_PREFIX):
            resolved[key] = _lookup(value, results)
        else:
            resolved[key] = value
    return resolved


def split(intent: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """兼容 {'intent':..,'slots':{..}} 与扁平两种入参，返回 (意图名, 槽位)。"""
    slots = dict(intent.get("slots") or {})
    for key, value in intent.items():
        if key not in ("intent", "slots") and value is not None:
            slots.setdefault(key, value)
    return str(intent.get("intent") or DEFAULT_INTENT), slots


def _nodes(template: tuple[tuple[str, str], ...], slots: dict[str, Any]) -> list[DAGNode]:
    ids = {tool: node_id for node_id, tool in template}
    nodes: list[DAGNode] = []
    previous = ""
    for node_id, tool in template:
        nodes.append(
            DAGNode(
                id=node_id,
                tool=tool,
                params=_params(tool, slots, ids),
                depends_on=[previous] if previous else [],
            )
        )
        previous = node_id
    return nodes


def _params(tool: str, slots: dict[str, Any], ids: dict[str, str]) -> dict[str, Any]:
    """每个工具的参数；转账类一律用引用拿上游结果。"""
    if tool == "query_balance":
        return _card_tail(slots)
    if tool == "list_subs":
        return {}
    if tool == "analyze_bill":
        return _bill_params(slots)
    if tool == "resolve_payee":
        return {"name": _payee_name(slots)}
    if tool == "transfer":
        return _transfer_params(slots, ids)
    if tool == "reserve_funds":
        return {"amount_fen": _amount(slots), "purpose": str(slots.get("purpose") or BIRTHDAY_PURPOSE)}
    if tool == "pay_merchant":
        return _merchant_params(slots, ids)
    raise ValidationError("模板里出现未登记的工具", reason="DAG_UNKNOWN_TOOL", tool=tool)


def _transfer_params(slots: dict[str, Any], ids: dict[str, str]) -> dict[str, Any]:
    """payee_id 取自 resolve_payee；说「全部」时金额取自 query_balance 的余额。"""
    return {
        "payee_id": ref(_need(ids, "resolve_payee"), "candidates", "0", "payee_id"),
        "amount_fen": _amount_ref(slots, ids),
        "note": str(slots.get("note") or ""),
    }


def _merchant_params(slots: dict[str, Any], ids: dict[str, str]) -> dict[str, Any]:
    """hold_id 与金额都来自上一步的预授权。"""
    hold_node = _need(ids, "reserve_funds")
    params = {
        "merchant": str(slots.get("merchant") or BIRTHDAY_MERCHANT),
        "item": str(slots.get("item") or BIRTHDAY_ITEM),
        "hold_id": ref(hold_node, "hold_id"),
    }
    amount = _positive_int(slots.get("amount_fen"))
    params["amount_fen"] = amount if amount is not None else ref(hold_node, "amount_fen")
    return params


def _bill_params(slots: dict[str, Any]) -> dict[str, Any]:
    """账单默认看上个自然月，年份按当前时间推。"""
    year, month = _previous_month(clock.now_ms())
    return {
        "period": str(slots.get("period") or BILL_DEFAULT_PERIOD),
        "year": _positive_int(slots.get("year")) or year,
        "month": _positive_int(slots.get("month")) or month,
    }


def _card_tail(slots: dict[str, Any]) -> dict[str, Any]:
    tail = str(slots.get("card_tail") or slots.get("account_tail") or "").strip()
    return {"account_tail": tail} if tail else {}


def _payee_name(slots: dict[str, Any]) -> str:
    name = str(slots.get("payee_name") or "").strip()
    if not name:
        raise ValidationError("缺收款人", reason="DAG_MISSING_SLOT", field="payee_name")
    return name


def _amount(slots: dict[str, Any]) -> int:
    amount = _positive_int(slots.get("amount_fen"))
    if amount is None:
        raise ValidationError("缺金额", reason="DAG_MISSING_SLOT", field="amount_fen")
    return amount


def _amount_ref(slots: dict[str, Any], ids: dict[str, str]) -> Any:
    """有明确金额就用金额；说「全部」时引用上一步查到的余额。"""
    amount = _positive_int(slots.get("amount_fen"))
    if amount is not None:
        return amount
    if slots.get("amount_all"):
        return ref(_need(ids, "query_balance"), "accounts", "0", "balance_fen")
    raise ValidationError("缺金额", reason="DAG_MISSING_SLOT", field="amount_fen")


def _need(ids: dict[str, str], tool: str) -> str:
    node_id = ids.get(tool)
    if node_id is None:
        raise ValidationError("模板里缺少必需的上游节点", reason="DAG_MISSING_REF", tool=tool)
    return node_id


def _positive_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


def _previous_month(now_ms: int) -> tuple[int, int]:
    moment = datetime.fromtimestamp(now_ms // 1000)
    return (moment.year - 1, 12) if moment.month == 1 else (moment.year, moment.month - 1)


def _lookup(reference: str, results: dict[str, Any]) -> Any:
    parts = reference[len(REF_PREFIX):].split(".")
    current: Any = results.get(parts[0]) if parts else None
    for part in parts[1:]:
        if isinstance(current, dict):
            current = current.get(part)
        elif isinstance(current, (list, tuple)) and part.lstrip("-").isdigit():
            current = current[int(part)] if -len(current) <= int(part) < len(current) else None
        else:
            current = None
    if current is None:
        raise ValidationError("引用取不到值", reason="DAG_REF_MISSING", ref=reference)
    return current


def _check_tools(dag: Dag) -> None:
    from app.tools.registry import TOOL_NAMES

    for node in dag.nodes:
        if node.tool not in TOOL_NAMES:
            raise ValidationError("DAG 用了没登记的工具", reason="DAG_UNKNOWN_TOOL", tool=node.tool)


def _topo(dag: Dag) -> list[DAGNode]:
    index = {node.id: node for node in dag.nodes}
    pending = {node.id: list(node.depends_on) for node in dag.nodes}
    order: list[DAGNode] = []
    while pending:
        ready = sorted(node_id for node_id, deps in pending.items() if not deps)
        if not ready:
            break
        for node_id in ready:
            order.append(index[node_id])
            pending.pop(node_id)
            for deps in pending.values():
                if node_id in deps:
                    deps.remove(node_id)
    return order


__all__ = [
    "INTENT_TEMPLATES",
    "REF_PREFIX",
    "build",
    "build_or_single",
    "ref",
    "resolve_refs",
    "split",
    "topo",
    "validate",
]
