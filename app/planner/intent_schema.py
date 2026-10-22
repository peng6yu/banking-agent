"""emit_intent 的工具 schema 与词表：意图枚举、槽位白名单、槽位说明（docs/07 7.2）。

为什么单独成文件：
1. app/planner/intent.py 有 400 行硬上限（docs/08），schema 与词表一多就顶破；
2. 意图名、槽位名、schema 三者必须一致——模型只能填 schema 里声明过的键
   （additionalProperties=false），解析器又只放行 SLOT_KEYS 里的键，
   任何一处漏了，那个槽位就是死代码（merchant 与 card_tail 就这样丢过一次）。
"""

from __future__ import annotations

from typing import Any, Final

EMIT_INTENT_NAME: Final[str] = "emit_intent"

# 意图枚举：模型必须从这七项里选一个。
INTENT_NAMES: Final[tuple[str, ...]] = (
    "query_balance",
    "transfer",
    "bill_analysis",
    "subscription",
    "birthday_plan",
    # 理财：申购/赎回共用一个意图，动作靠 action 槽位区分（工具侧落 buy_fund / redeem_fund）
    "invest",
    "unknown",
)

# 每个意图什么时候用。缺了这段说明模型只能猜：实测 birthday_plan 没有说明时，
# 「下周三我妈生日，帮我提前留出300块给她订束花」会被解析成 unknown 再反问（场景 6 走不通）。
INTENT_GUIDE: Final[str] = (
    "用户这句话属于哪一类，从下面七项里选一个："
    "query_balance=查余额、流水、额度、卡里还剩多少钱；"
    "transfer=给某人转钱、付款、还款；"
    "bill_analysis=分析某月账单、找出异常消费；"
    "subscription=查订阅、看自动扣款；"
    "用户明确要取消或关掉某条订阅时也选它，并把订阅名填进 sub_name（例如「爱奇艺会员」）；"
    "birthday_plan=送礼安排：先预留一笔钱，等某个日子再付给商户，"
    "例如「下周三我妈生日，提前留出300块给她订束花送过去」；"
    "invest=基金理财的申购或赎回；"
    "unknown=以上都不是、实在判断不出来。"
)

# 槽位白名单：解析器只放行这些键，模型给的别的键会被 _slots 丢掉。
# merchant 是生日花束模板要用的槽位，白名单漏了它，模型给的商户名会被丢掉，注入扫描就扫不到。
SLOT_KEYS: Final[tuple[str, ...]] = (
    "payee_name",
    "amount_fen",
    "note",
    "period",
    "month",
    "card_tail",
    "merchant",
    "action",
    "product_code",
    # 生日送礼场景（birthday_plan）：这笔钱做什么用、送什么、什么时候花
    "purpose",
    "gift",
    "date_hint",
    # 订阅名（商户名）：用户说「爱奇艺会员」就填「爱奇艺会员」
    "sub_name",
)

# 模型偶尔会报一个我们根本没有的槽位名（真模型实测生日场景报的是 birthday_date，
# 我们只有 date_hint）。先按别名归一，归一不出来的一律丢掉：反问一个我们自己都填不了的
# 槽位没有意义——用户答了也进不了任何工具参数，反而把一次本来能跑完的请求卡在反问上。
MISSING_ALIASES: Final[dict[str, str]] = {
    "birthday_date": "date_hint",
    "birthday_time": "date_hint",
    "date": "date_hint",
    "time": "date_hint",
    "when": "date_hint",
    "birthday_gift": "gift",
    "gift_item": "gift",
    "gift_name": "gift",
    "money_reason": "purpose",
    "reason": "purpose",
}

# 槽位说明：模型不会照字段名猜语义，缺说明就填不对（product_code 就是这么填错过）。
SLOT_GUIDE: Final[dict[str, str]] = {
    "card_tail": "仅当用户明确说到某张卡尾号时才填；转账、申购、账单都不需要",
    "merchant": "商户代码：花店填 flower_shop、蛋糕店填 cake_shop",
    "action": "理财动作：buy=申购、redeem=赎回、query=看在售产品",
    "product_code": (
        "基金产品代码：F00001 现金添利货币A／F00002 稳盈债券A／F00003 均衡配置混合A／"
        "F00004 沪深精选指数A／F00005 全球科技QDII；用户报产品名时必须换成代码"
    ),
    "purpose": "这笔钱的用途，例如「生日礼物」",
    "gift": "要送的东西，例如「花束」「蛋糕」",
    "date_hint": "用户说的时间，原样保留，例如「下周三」「月底」",
    "sub_name": (
        "订阅名称：用户说「爱奇艺会员」就填「爱奇艺会员」；"
        "只在用户明确要取消某条订阅时填，查订阅时不要填"
    ),
}

# 订阅意图分两种用法：查订阅，和取消某一条。
# 判「是不是要取消」只看用户原话里有没有取消动作词——模型顺手填了名字不算数：
# 把一句「看看视频会员扣多少」变成替用户退订，是这个系统最不该犯的错（t47）。
CANCEL_WORDS: Final[tuple[str, ...]] = (
    "取消", "取消了", "关掉", "关闭", "关了", "关上", "停掉", "停了", "停用", "停收",
    "退订", "解绑", "解除", "别再", "不再", "不用续", "不要续", "别续", "不要续费",
    "别扣", "不要扣", "别自动扣款", "不要自动扣款",
)
# 泛称：用户说了要取消，但没说是哪一条（「把订阅都关了吧」）。
# 泛称不能拿去匹配订阅名——「会员」会命中「爱奇艺会员」，那就变成替用户挑了一条退掉。
GENERIC_SUB_NAMES: Final[frozenset[str]] = frozenset({
    "订阅", "订阅们", "订阅服务", "自动续费", "续费", "扣款",
    "包月", "包年", "会员", "会员服务", "所有订阅", "全部订阅",
})
# 意图层算出来的槽位：用户确实说了要取消订阅时为 True。
# 它不进 SLOT_KEYS——模型不填它，只有解析器根据用户原话算。
CANCEL_SLOT: Final[str] = "cancel_subs"
# 说了要取消、却没说是哪一条时问的话（一次只问一个问题，docs/07 7.1 第 4 条）
SUBSCRIPTION_NAME_QUESTION: Final[str] = "要取消哪一条订阅？请说订阅名称，比如「爱奇艺会员」。"


def wants_cancel(text: str) -> bool:
    """用户原话里有没有取消订阅的动作词。"""
    return any(word in text for word in CANCEL_WORDS)


def subscription_slots(slots: dict[str, Any], text: str) -> dict[str, Any]:
    """订阅槽位整形：查订阅不留名字，要取消才留。

    三条规则：
    1. 原话里没有取消动作词 → 丢掉 sub_name（查询请求绝不能变成取消请求）；
    2. 有取消动作词 → 记下 cancel_subs=True，订阅名留着去匹配；
    3. 订阅名是泛称（「订阅」「都」）→ 丢掉 sub_name，交给规划层反问要哪一条。
    订阅取消没有金额，金额兜底也别把「每月 2500」算成转账金额。
    """
    slots.pop("amount_fen", None)
    if not wants_cancel(text):
        slots.pop("sub_name", None)
        return slots
    slots[CANCEL_SLOT] = True
    if str(slots.get("sub_name") or "").strip() in GENERIC_SUB_NAMES:
        slots.pop("sub_name", None)
    return slots


def emit_intent_schema() -> dict[str, Any]:
    """emit_intent 的工具定义；键与类型对齐 docs/07 7.2（tests 断言键集合覆盖 SLOT_KEYS）。"""
    properties: dict[str, Any] = {
        "intent": {"type": "string", "enum": list(INTENT_NAMES), "description": INTENT_GUIDE},
        "payee_name": {"type": "string"},
        "amount_fen": {"type": "integer"},
        "note": {"type": "string"},
        "merchant": {"type": "string"},
        "card_tail": {"type": "string"},
        "sub_name": {"type": "string"},
        "period": {"type": "string", "enum": ["month", "year"]},
        "month": {"type": "integer"},
        "action": {"type": "string", "enum": ["buy", "redeem", "query"]},
        "product_code": {"type": "string"},
        "purpose": {"type": "string"},
        "gift": {"type": "string"},
        "date_hint": {"type": "string"},
        "missing": {"type": "array", "items": {"type": "string"}},
        "confidence": {"type": "number"},
    }
    for key, description in SLOT_GUIDE.items():
        properties[key]["description"] = description
    return {
        "name": EMIT_INTENT_NAME,
        "description": (
            "把用户这句话解析成结构化意图。必须调用我，不要直接回答。"
            "missing 只填真正缺失、必须问用户才能补的槽位；"
            "card_tail / note / merchant 都是可选槽位，不要填进 missing。"
            "用户说的是「先留出一笔钱、之后再买或订」这类跨场景安排时，intent 选 birthday_plan。"
        ),
        "parameters": {
            "type": "object",
            "properties": properties,
            "required": ["intent", "missing", "confidence"],
            "additionalProperties": False,
        },
    }


__all__ = [
    "CANCEL_SLOT",
    "CANCEL_WORDS",
    "EMIT_INTENT_NAME",
    "GENERIC_SUB_NAMES",
    "INTENT_GUIDE",
    "INTENT_NAMES",
    "MISSING_ALIASES",
    "SLOT_GUIDE",
    "SLOT_KEYS",
    "SUBSCRIPTION_NAME_QUESTION",
    "emit_intent_schema",
    "subscription_slots",
    "wants_cancel",
]
