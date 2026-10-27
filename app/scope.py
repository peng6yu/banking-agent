"""业务边界：这个助手只处理与「钱」有关的请求。

一条不变量贯穿全系统：**agent 的每一个动作都必须落在一个资金对象上**
（账户、卡、收款人、流水、账单、订阅与代扣、预约转账、计划与预算、理财与持仓、
额度、风控确认与人工接管）。对不上任何资金对象的请求不属于它该承接的事：
不建 DAG、不建计划、不写业务表，只说明边界并给出最接近的资金动作。

本模块是全系统唯一的边界词汇出处（第 0 层，只依赖标准库）：

* `classify` —— 一句话属于「钱的事 / 与钱无关 / 看不出来」；
* `reminder_anchor` —— 提醒必须挂在一个资金时间点（还款/扣费/预约/预算/储蓄/额度），
  没有锚点的提醒会被拒（「提醒我喝水」不记）；
* `is_money_text` —— 长期记忆只收与资金相关的偏好与事实；
* `OUT_OF_SCOPE_REPLY` / `reminder_boundary` —— 统一的边界话术。

只做词面判定，不读库、不看钟，因此可被任何层引用。
"""

from __future__ import annotations

from typing import Final

#: 资金对象相关词。命中任意一个就认为这句话在说钱的事。
MONEY_MARKERS: Final[tuple[str, ...]] = (
    "钱",
    "余额",
    "账户",
    "账号",
    "卡",
    "流水",
    "账",
    "转账",
    "收款",
    "汇款",
    "付款",
    "支付",
    "还款",
    "还信用卡",
    "信用卡",
    "借记卡",
    "储蓄",
    "存款",
    "存钱",
    "存",
    "攒",
    "预算",
    "订阅",
    "代扣",
    "扣费",
    "续费",
    "理财",
    "基金",
    "股票",
    "持仓",
    "赎回",
    "申购",
    "收益",
    "利息",
    "手续费",
    "额度",
    "限额",
    "工资",
    "支出",
    "收入",
    "消费",
    "贷款",
    "房贷",
    "车贷",
    "账单",
    "收款人",
    "商户",
    "商家",
    "银行",
    "推销",
    "利率",
    "分期",
    "预约转账",
    "计划",
    "审计",
    "授权",
    "确认",
    "风险",
    "测评",
    "积分",
    "报销",
    "税",
    "币",
)

#: 明显与钱无关的请求。只有「不含任何资金词」时才算越界，
#: 所以「还款日是哪天」不会被误判（含「还款」）。
NON_MONEY_MARKERS: Final[tuple[str, ...]] = (
    "天气",
    "气温",
    "下雨",
    "温度",
    "几点了",
    "几点",
    "几号",
    "星期几",
    "笑话",
    "唱歌",
    "唱首",
    "讲故事",
    "电影",
    "音乐",
    "菜谱",
    "做饭",
    "吃什么",
    "翻译",
    "作业",
    "数学题",
    "写代码",
    "编程",
    "星座",
    "新闻",
    "心情",
    "无聊",
    "陪我聊",
    "你好",
    "您好",
    "谢谢",
    "早上好",
    "晚安",
    "你是谁",
)

#: 提醒必须挂上的资金时间点：key → (中文名, 命中词)。
REMINDER_ANCHORS: Final[tuple[tuple[str, str, tuple[str, ...]], ...]] = (
    ("repayment", "还款日", ("还信用卡", "还房贷", "还车贷", "还花呗", "还款", "账单日", "最低还款")),
    ("deduction", "订阅扣费", ("扣费", "续费", "代扣", "到期", "会员")),
    ("scheduled", "预约转账", ("预约转账", "那笔转账", "定时转账", "转账到点")),
    ("budget", "预算", ("预算", "超支", "花到", "用到")),
    ("savings", "储蓄目标", ("存够", "存满", "攒够", "储蓄目标", "存到")),
    ("limit", "额度与大额", ("额度", "限额", "超过", "大额")),
)

ANCHOR_LABELS: Final[dict[str, str]] = {key: label for key, label, _ in REMINDER_ANCHORS}

#: 统一的边界话术：说明它能做什么，并给一个最接近的资金动作。
BOUNDARY_HEAD: Final[str] = "我只管你账上和计划里的事，这条我不接。"

OUT_OF_SCOPE_REPLY: Final[str] = (
    BOUNDARY_HEAD
    + "我能做的是：查余额与账单、转账与预约、订阅代扣、储蓄与预算、理财持仓、授权与审计。"
    "要不要我从这些里挑一件开始？"
)

REMINDER_BOUNDARY: Final[str] = (
    BOUNDARY_HEAD
    + "提醒只记和钱有关的时间点：还款日、订阅扣费、预约转账、预算超支、储蓄目标、额度与大额。"
    "比如「提醒我每月 25 号还信用卡」。"
)

MEMORY_BOUNDARY: Final[str] = (
    BOUNDARY_HEAD
    + "长期记忆只收和钱有关的：常用卡、记账习惯、预算尺度、常用收款人、风险偏好、还款习惯。"
)


def _hit(text: str, words: tuple[str, ...]) -> bool:
    return any(word in text for word in words)


def is_money_text(text: str) -> bool:
    """这句话里有没有资金对象；空串返回 False。"""
    clean = str(text or "").strip()
    return bool(clean) and _hit(clean, MONEY_MARKERS)


def classify(text: str) -> str:
    """'money' / 'off' / 'unknown'：先看资金词，再看无关词，其余算看不出来。"""
    clean = str(text or "").strip()
    if not clean:
        return "unknown"
    if _hit(clean, MONEY_MARKERS):
        return "money"
    if _hit(clean, NON_MONEY_MARKERS):
        return "off"
    return "unknown"


def out_of_scope_reply(text: str) -> str:
    """与钱无关时返回边界话术；否则返回空串（调用方据此决定是否继续原流程）。"""
    return OUT_OF_SCOPE_REPLY if classify(text) == "off" else ""


def reminder_anchor(text: str) -> str:
    """返回提醒锚点的 key（repayment/deduction/…）；没有锚点返回空串。"""
    clean = str(text or "").strip()
    if not clean:
        return ""
    for key, _label, words in REMINDER_ANCHORS:
        if _hit(clean, words):
            return key
    return ""


def anchor_label(anchor: str) -> str:
    return ANCHOR_LABELS.get(str(anchor or ""), "")


def anchor_choices() -> list[dict[str, str]]:
    """没有锚点时给用户可点的资金时间点选项（最多 6 条）。"""
    examples = {
        "repayment": "提醒我每月 25 号还信用卡",
        "deduction": "提醒我视频会员扣费前一天",
        "scheduled": "提醒我明天上午 9 点那笔预约转账",
        "budget": "提醒我生日预算花到 2000 就停",
        "savings": "提醒我存够 5 万的时候",
        "limit": "提醒我单笔超过 5000 的时候",
    }
    return [
        {"label": f"{label}提醒", "value": examples.get(key, f"提醒我{label}")}
        for key, label, _ in REMINDER_ANCHORS
    ]


__all__ = [
    "ANCHOR_LABELS",
    "BOUNDARY_HEAD",
    "MEMORY_BOUNDARY",
    "MONEY_MARKERS",
    "NON_MONEY_MARKERS",
    "OUT_OF_SCOPE_REPLY",
    "REMINDER_ANCHORS",
    "REMINDER_BOUNDARY",
    "anchor_choices",
    "anchor_label",
    "classify",
    "is_money_text",
    "out_of_scope_reply",
    "reminder_anchor",
]
