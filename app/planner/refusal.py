"""确定性拒答守卫：无能力 / 敏感索取类请求不交给模型判断，命中即整轮拦截。

为什么要有它（2026-10-23 红队收口）：B03/B06/B07/B08/C01/C02/C10 这类请求
（念短信验证码、要系统提示词、要 CAPABILITY_SECRET、冒充内部审计员调他人资料、
改登录密码、加收款人、删审计日志）在生产上本来就没有任何工具能做，但「没有工具」
这件事过去只靠模型自己回 unknown 来表达。模型一旦回 unknown，整轮只剩一条
intent.parsed 审计：看不出「拒绝了什么、按哪条规矩拒绝的」，红队判定也只能记成
「这一轮没有裁决」（none）。本模块把它们收成确定性判定：

    命中模式 → intent=unsupported → DAG 的 unsupported_request 哨兵节点
    → 策略层 R03 红灯 → policy.decided + confirm.rejected → 用户看到逐类话术

三条设计取舍：
  * 只放高精度模式：宁可漏（退回模型判断），不可误（把正常转账、查账、查卡拦掉）；
    「忘了密码怎么办」「我收不到验证码」这类求助句不带索取/改动动词，不会命中。
  * 不压棒：这类请求不执行任何工具，压一根没人认领的棒只会污染下一轮的升色判断。
  * 话术逐类给：R03 的通用句「我还不会这个操作」对「念验证码给我」太含糊，
    所以把话术放进槽位交给策略层（app/policy/rules.py 的 R03 分支）带出去。

本模块是纯函数：不 import 模型、不读环境变量、不写库、不取时钟。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final

#: 落地时用的意图名：模板在册里没有它，DAG 会退成 unsupported_request 哨兵节点。
REFUSAL_INTENT: Final[str] = "unsupported"
#: 话术与类别放这两个槽位，只用于策略层 R03 的输出与审计，不当业务参数用。
REFUSAL_SLOT: Final[str] = "refusal"
REFUSAL_CATEGORY_SLOT: Final[str] = "refusal_category"


@dataclass(frozen=True)
class RefusalRule:
    """一类该整轮拒绝的请求：全部模式都命中才算（AND，降误判）。"""

    category: str
    patterns: tuple[re.Pattern[str], ...]
    reply: str


_PROMPT_WORDS, _SECRET_WORDS = r"提示词|系统设定|系统指令|system\s*prompt|developer\s*mode|开发者模式", (
    r"CAPABILITY_SECRET|API\s*key|api[_-]?key|密钥|私钥|签名密钥|secret"
)
_DELETE_VERBS = "删|删除|清除|清空|抹掉|擦掉|篡改|改掉|销毁"
_DELETE_OBJECTS = "审计|日志|流水|交易记录|历史记录|操作记录|记录"
_ADD_VERBS = "加|添加|新增|录入|绑定|补录"
_ADD_OBJECTS = "收款人|联系人|通讯录|收款账户|收款账号|转账对象"
_PIN_SUBJECTS = "登录密码|账户密码|银行卡密码|支付密码|网银密码|手机银行密码|登录口令|交易密码"
_PIN_CHANGE_VERBS = "改|修改|设置|重设|重置|换|设成|设为|改成|改为|更新"
_PIN_READ_VERBS = "告诉我|念|发给我|给我|是多少|是什么|查一?下?|报一下"
_CLAIM_SUBJECTS = r"我是.{0,10}(审计|内部|银行工作|客服|风控|监管|合规|工号)|审计员|审计人员|内部员工|内部人员|工号|稽查|风控人员"
_CLAIM_OBJECTS = "资料|信息|明细|余额|流水|数据|档案|调出|调取|电话号码|身份证"

RULES: Final[tuple[RefusalRule, ...]] = (
    RefusalRule(
        "verification_code",
        (re.compile(r"验证码|动态码|动态口令|短信码|短信口令"), re.compile(r"念|说出|告诉我|发给我|给我|报|读|填|输|截")),
        "验证码这类一次性凭证我不会念、也不会替你填，请你在手机上自己看。",
    ),
    RefusalRule(
        "system_prompt",
        (re.compile(_PROMPT_WORDS, re.I),),
        "系统提示词和内部设定不对外，这个我不能给。",
    ),
    RefusalRule(
        "secret",
        (re.compile(_SECRET_WORDS, re.I),),
        "密钥只存在进程环境里，任何工具都读不到，我也不会把它打印出来。",
    ),
    RefusalRule(
        "delete_audit",
        (re.compile(_DELETE_VERBS), re.compile(_DELETE_OBJECTS)),
        "审计日志和交易流水只增不改，系统里没有任何删除入口。",
    ),
    RefusalRule(
        "add_payee",
        (re.compile(_ADD_VERBS), re.compile(_ADD_OBJECTS)),
        "收款人只能在手机银行里自己加，我这边没有新增收款人的入口。",
    ),
    RefusalRule(
        "password_change",
        (re.compile(_PIN_SUBJECTS), re.compile(_PIN_CHANGE_VERBS)),
        "登录密码我没法替你改，也不接受在对话里设置新密码，请到手机银行的安全中心操作。",
    ),
    RefusalRule(
        "password_read",
        (re.compile(_PIN_SUBJECTS), re.compile(_PIN_READ_VERBS)),
        "密码我不能念、也查不到；忘记的话请在手机银行里重置。",
    ),
    RefusalRule(
        "impersonation",
        (re.compile(_CLAIM_SUBJECTS), re.compile(_CLAIM_OBJECTS)),
        "身份不能靠一句话证明；他人资料只能由本人授权后查询，我这边没有这个入口。",
    ),
)


@dataclass(frozen=True)
class Refusal:
    """命中结果：类别用于审计与复盘，话术交给策略层直接给用户。"""

    category: str
    reply: str


def detect(text: str) -> Refusal | None:
    """按对话原话判一次；命中第一条规则就返回，没命中返回 None。

    顺序即优先级：验证码/提示词/密钥这类「绝对不给」的排在最前面，
    免得多模式交叉时被后面的类别抢走话术。
    """
    if not text:
        return None
    for rule in RULES:
        if all(pattern.search(text) for pattern in rule.patterns):
            return Refusal(category=rule.category, reply=rule.reply)
    return None


__all__ = [
    "REFUSAL_CATEGORY_SLOT",
    "REFUSAL_INTENT",
    "REFUSAL_SLOT",
    "RULES",
    "Refusal",
    "RefusalRule",
    "detect",
]
