"""订阅取消那一路：先把订阅列出来，再按名字唯一匹配，取消那一条。

为什么单独成文件：app/planner/dag.py 顶到 400 行硬上限（docs/08 编码规范），
订阅这条路由的模板、引用与匹配拆到这里，dag.py 只留调用点。

两条硬规矩：
1. 退订是写操作，只有用户点名到**唯一**一条订阅才动手——匹配不到或匹配到多条一律反问，
   绝不替用户挑一条退掉；
2. 反问的话术写在这里，因为只有这里知道用户名下到底有哪些订阅。
"""

from __future__ import annotations

from typing import Any, Final
from urllib.parse import quote, unquote

from app.clock import ValidationError
from app.planner.intent_schema import (
    CANCEL_SLOT,
    GENERIC_SUB_NAMES,
    SUBSCRIPTION_NAME_QUESTION,
)

# 订阅意图分两路：查（只列）与取消（先列、再按名字唯一匹配后取消）。
# 走取消那一路的信号是槽位里有 sub_name——意图层只在用户真说了「取消/关掉」时才留它。
# 没有 sub_name 的查询请求绝不会长出取消节点。
SUBSCRIPTION_TEMPLATES: Final[dict[str, tuple[tuple[str, str], ...]]] = {
    "query": (("n1", "list_subs"),),
    "cancel": (("n1", "list_subs"), ("n2", "cancel_subscription")),
}
# list_subs 返回结果里，订阅列表字段、订阅名字段与主键字段
SUB_LIST_FIELD: Final[str] = "subs"
SUB_NAME_FIELD: Final[str] = "merchant"
SUB_ID_FIELD: Final[str] = "sub_id"


def subscription_template(slots: dict[str, Any]) -> tuple[tuple[str, str], ...]:
    """订阅走哪一路：用户说了要取消就取消，否则只列。

    判据是意图层算出来的 cancel_subs 槽位——它只根据用户原话里的取消动作词产生，
    模型自己填的订阅名不算数。
    """
    return SUBSCRIPTION_TEMPLATES["cancel"] if slots.get(CANCEL_SLOT) else SUBSCRIPTION_TEMPLATES["query"]


def cancel_params(slots: dict[str, Any], ids: dict[str, str]) -> dict[str, Any]:
    """取消订阅节点的参数：sub_id 从上一步列出的订阅里按名字唯一匹配。

    名字为空也不在这里报错：建图期抛错会被 E8 兜底成单步图（用户只看到一张订阅列表，
    问不出「要取消哪一条」）。空名照样造引用，等引用解析时再问。

    匹配不上会在引用解析时抛 ValidationError（话术是反问），到不了工具层——
    cancel_subscription 根本不会被调用。
    """
    name = str(slots.get("sub_name") or "").strip()
    node_id = ids.get("list_subs")
    if node_id is None:
        raise ValidationError("模板里缺少必需的上游节点", reason="DAG_MISSING_REF", tool="list_subs")
    return {"sub_id": match_ref(node_id, SUB_LIST_FIELD, SUB_NAME_FIELD, name, SUB_ID_FIELD)}


def match_ref(node_id: str, list_field: str, key: str, value: str, out_field: str) -> str:
    """造一个带过滤的引用：@节点.列表字段[键=值].取值字段。

    值做 percent-encoding：中文订阅名里带点、带方括号，都不会把引用串拆坏。
    """
    quoted = quote(str(value), safe="").replace(".", "%2E")
    return f"@{node_id}.{list_field}[{key}={quoted}].{out_field}"


def resolve_match(current: Any, part: str) -> Any:
    """解析引用串里带过滤的那一段（列表字段[键=值]）：返回唯一匹配的那一条。

    匹配不到或匹配到多条都抛 ValidationError，话术是反问——宁可多问一句，
    也不能替用户在好几条订阅里挑一条退掉。
    """
    field, _, filt = part.partition("[")
    key, _, raw = filt.rstrip("]").partition("=")
    items = current.get(field) if isinstance(current, dict) else None
    return match_one(items, key, unquote(raw))


def match_one(items: Any, key: str, wanted: str) -> dict[str, Any]:
    """在列表里按列表项的 key 唯一匹配一条；0 条或多条都不猜，抛出去反问。"""
    rows = [row for row in (items or []) if isinstance(row, dict)]
    names = "、".join(f"「{row.get(key)}」" for row in rows) or "（一条都没有）"
    if not wanted.strip() or _strip(wanted) in GENERIC_SUB_NAMES:
        # 用户说了要取消，却没说是哪一条（空名或泛称）——列出你名下的订阅让他点。
        # 泛称也不能放行：「会员」会命中「爱奇艺会员」，那就变成替用户挑了一条退掉。
        raise ValidationError(
            f"要取消哪一条订阅？你名下现在是：{names}。请说订阅名称。",
            reason="DAG_MISSING_SLOT",
            field="sub_name",
        )
    hits = [row for row in rows if same_name(str(row.get(key) or ""), wanted)]
    if len(hits) == 1:
        return hits[0]
    if not hits:
        raise ValidationError(
            f"我没找到叫「{wanted}」的订阅。你名下现在是：{names}。要取消哪一条？",
            reason="DAG_SUB_NOT_FOUND",
            wanted=wanted,
        )
    raise ValidationError(
        f"有 {len(hits)} 条订阅跟「{wanted}」对得上：{names}。你要取消哪一条？",
        reason="DAG_SUB_AMBIGUOUS",
        wanted=wanted,
    )


def same_name(left: str, right: str) -> bool:
    """订阅名匹配：完全一样，或者一边是另一边的子串（「爱奇艺」对「爱奇艺会员」）。

    归一化只去掉空格与常见分隔符，不做模糊匹配——退错一条比多问一句糟得多。
    """
    a = _strip(left)
    b = _strip(right)
    return bool(a) and bool(b) and (a == b or a in b or b in a)


def _strip(text: str) -> str:
    return "".join(ch for ch in text if not ch.isspace() and ch not in "（）()【】[]、,，。.·-_/\\")


__all__ = [
    "SUB_ID_FIELD",
    "SUB_LIST_FIELD",
    "SUB_NAME_FIELD",
    "SUBSCRIPTION_TEMPLATES",
    "cancel_params",
    "match_one",
    "match_ref",
    "resolve_match",
    "same_name",
    "subscription_template",
]
