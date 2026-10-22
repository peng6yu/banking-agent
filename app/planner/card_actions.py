"""场景四卡片管理那一路：按用户原话的动词选工具，卡号尾号原样带进参数。

为什么单独成文件：app/planner/dag.py 顶到 400 行硬上限（docs/08 编码规范）。

一条硬规矩：动作只从用户原话里读，不认模型给的 card_action。
冻结、挂失、改限额都是动账户安全的写操作，模型顺手填一个 loss 就把一次查询变成挂失，
那是这套系统最不该犯的错（与订阅取消的 sub_name 同一条原则）。
模型即使填了 card_action，这里也会用原话重新算一遍覆盖掉。
"""

from __future__ import annotations

from typing import Any, Final

from app.clock import ValidationError

CARD_INTENT: Final[str] = "card_manage"
# 意图层算出来的槽位：不是模型填的，也不进 SLOT_KEYS。
CARD_ACTION_SLOT: Final[str] = "card_action"
CARD_TAIL_SLOT: Final[str] = "card_tail"

CARD_DEFAULT_ACTION: Final[str] = "query"
CARD_QUERY_WORDS: Final[tuple[str, ...]] = ("查", "看", "有哪些", "几张", "列表", "额度")

# 顺序要紧：先判「挂失」再判「解冻」再判「冻结」，否则「解冻」会被「冻」抢走，
# 「挂失后能不能解冻」这种话会被听成解冻。
CARD_ACTION_WORDS: Final[tuple[tuple[str, tuple[str, ...]], ...]] = (
    ("loss", ("挂失", "丢了", "丢卡", "找不到了", "遗失")),
    ("unfreeze", ("解冻", "解封", "解锁", "恢复正常")),
    ("freeze", ("冻结", "冻", "锁卡", "锁住", "停用", "先别用")),
    ("set_limit", ("限额", "限到", "单笔上限", "调整额度", "提高额度", "降低额度")),
    ("apply", ("申请", "办一张", "开一张", "开卡", "新办", "虚拟卡")),
    ("change_pin", ("改密码", "换密码", "重置密码", "修改密码")),
    ("close", ("销户", "注销", "不用这张卡了", "消掉")),
)

# 每路一张单步模板；卡片管理没有多步链路，所有工具都不依赖上游结果。
CARD_TEMPLATES: Final[dict[str, tuple[tuple[str, str], ...]]] = {
    "query": (("n1", "list_cards"),),
    "apply": (("n1", "apply_virtual_card"),),
    "set_limit": (("n1", "set_card_limit"),),
    "freeze": (("n1", "freeze_card"),),
    "unfreeze": (("n1", "unfreeze_card"),),
    "loss": (("n1", "report_card_loss"),),
    "change_pin": (("n1", "change_card_pin"),),
    "close": (("n1", "close_card"),),
}
# 必须点名到具体某张卡的动作；查卡和开卡可以不带尾号（开卡默认挂主账户）。
CARD_TAIL_REQUIRED: Final[frozenset[str]] = frozenset({"set_limit", "freeze", "unfreeze", "loss", "change_pin", "close"})
CARD_TOOLS: Final[frozenset[str]] = frozenset(
    tool for template in CARD_TEMPLATES.values() for _, tool in template
)


def card_action(slots: dict[str, Any], text: str) -> dict[str, Any]:
    """按用户原话算出这张卡要做什么，写进 CARD_ACTION_SLOT。

    只认原话：模型填的 card_action 一律被覆盖。没命中任何动作词就是查询，
    写操作必须由用户自己说出口。
    """
    action = CARD_DEFAULT_ACTION
    for name, words in CARD_ACTION_WORDS:
        if any(word in text for word in words):
            action = name
            break
    slots[CARD_ACTION_SLOT] = action
    return slots


def is_query(text: str) -> bool:
    """这句话是不是在问卡的现状（列出卡片/看额度）。"""
    return any(word in text for word in CARD_QUERY_WORDS)


def card_template(slots: dict[str, Any]) -> tuple[tuple[str, str], ...]:
    """卡片意图走哪一张模板：看意图层算出来的 card_action。"""
    action = str(slots.get(CARD_ACTION_SLOT) or CARD_DEFAULT_ACTION)
    return CARD_TEMPLATES.get(action, CARD_TEMPLATES[CARD_DEFAULT_ACTION])


def card_params(tool: str, slots: dict[str, Any]) -> dict[str, Any]:
    """各卡片工具的参数。尾号用的是用户说的那张卡的卡号后四位。"""
    tail = str(slots.get(CARD_TAIL_SLOT) or "").strip()
    if tool in ("list_cards", "apply_virtual_card"):
        return {"account_tail": tail} if tail else {}
    if tool == "set_card_limit":
        return {"card_tail": _need_tail(tail), "per_tx_limit_fen": _need_amount(slots)}
    return {"card_tail": _need_tail(tail)}


def _need_tail(tail: str) -> str:
    """要动某一张卡却没说是哪张：反问一次，绝不按主账户随便挑一张。"""
    if tail:
        return tail
    raise ValidationError(
        "要操作哪张卡？请告诉我卡号后四位。",
        reason="DAG_MISSING_SLOT",
        field=CARD_TAIL_SLOT,
    )


def _need_amount(slots: dict[str, Any]) -> int:
    """调限额必须带新限额本身，缺了就反问，不默认沿用旧值。"""
    amount = slots.get("amount_fen")
    if isinstance(amount, int) and amount > 0:
        return amount
    raise ValidationError(
        "限额设成多少？",
        reason="DAG_MISSING_SLOT",
        field="amount_fen",
    )


__all__ = [
    "CARD_ACTION_SLOT",
    "CARD_ACTION_WORDS",
    "CARD_DEFAULT_ACTION",
    "CARD_INTENT",
    "CARD_TAIL_REQUIRED",
    "CARD_TAIL_SLOT",
    "CARD_TEMPLATES",
    "CARD_TOOLS",
    "card_action",
    "card_params",
    "card_template",
    "is_query",
]
