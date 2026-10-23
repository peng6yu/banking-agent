"""知识层接线：把银行领域知识真正用到意图识别与任务规划上。

对应文档：docs/11_知识层设计.md 2.5

为什么不让模型直接读知识原文：
1. `SYSTEM_PROMPT` 与 messages 结构被 tests/test_planner.py 逐字钉住，往上下文里塞知识
   等于换掉「模型看到的输入」，原有回归测试立刻失去意义；
2. 数据信封单段上限 500 字符（app/security/envelope.py），整份规则塞进去会被从中间截断，
   截断点由字符串长度决定，等于把「用哪条规则」交给巧合；
3. 最关键的是：知识一旦进了模型上下文，模型就有机会拿它当理由放松校验。

所以这里只做三件**确定性**的事（与 app/knowledge/__init__.py 的三条硬约束一致）：
- 问不问：追加必填槽位（跨场景缺产品代码时先追问，不建图）；
- 填什么：口语说法命中标准术语时补槽位（赎回 → action=redeem）、纠正明显走偏的意图；
- 怎么解释：把引用到的知识条目与来源 id 交给审计与前端。

放不放行永远由 app/policy/ 决定；本模块不 import 策略层，也不产出任何执行授权。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Final

from app.knowledge import (
    KnowledgeBase,
    KnowledgeWarning,
    Source,
    Term,
    get_base,
    normalize,
    retrieve,
    scenarios_of,
)
from app.knowledge.models import Retrieved

#: 知识层开关：默认开启。设成 0/false/off/no 即整层旁路（对照实验的 A 组用它）。
ENV_KNOWLEDGE_ENABLED: Final[str] = "KNOWLEDGE_ENABLED"
_DISABLED_VALUES: Final[frozenset[str]] = frozenset({"", "0", "false", "off", "no"})

#: 标准术语 → 意图。只在「模型没给出可用结构化结果」时兜底，绝不覆盖模型的高置信输出。
TERM_INTENT: Final[dict[str, str]] = {
    "转账": "transfer",
    "预约转账": "schedule_transfer",
    "代扣协议": "subscription",
    "解约": "subscription",
    "申购": "invest",
    "赎回": "invest",
    "账单": "bill_analysis",
    "账户余额": "query_balance",
    "挂失": "card_manage",
}

#: 「先赎回再转账」这条跨场景链路的名字（dag.py 的 CROSS_TEMPLATES 用同一个串）。
CROSS_REDEEM_TRANSFER: Final[str] = "redeem_then_transfer"
#: 跨场景下必须先问清的产品槽位：没有它无法建出 redeem_fund 节点。
CROSS_REQUIRED_SLOTS: Final[tuple[str, ...]] = ("product_code",)
CROSS_SLOT_QUESTION: Final[str] = (
    "赎回要先知道是哪只产品：请说产品代码或产品名，例如「F00001 现金添利货币A」。"
)
#: 「预约转账」说成 transfer 时用来纠正的术语。
SCHEDULE_TERM: Final[str] = "预约转账"
#: 未来/周期性时间标记。用户说「下周三转 500」时，意图层若只判出 transfer，
#: 钱会当场划走——这与用户的话不符，所以按时间标记纠正成预约转账，再由槽位缺失去问清日期。
FUTURE_MARKERS: Final[tuple[str, ...]] = (
    "下周",
    "下个月",
    "下月",
    "明天",
    "后天",
    "月底",
    "每月",
    "每周",
    "每年",
    "到期日",
)
#: 理财动作术语。
REDEEM_TERM: Final[str] = "赎回"
BUY_TERM: Final[str] = "申购"

#: 赎回的口语说法。「卖了/卖掉/变现」不进术语别名（那是词表的活），
#: 但跨场景判定不能只认术语：用户说「把货币基金卖了转 3000」时，少判一步就等于
#: 把赎回这一步丢掉、直接按转账执行。
REDEEM_WORDS: Final[tuple[str, ...]] = ("赎回", "卖出", "卖了", "卖掉", "变现", "取出来", "赎回来")

#: 单姓（裸姓）——「转500给王」这种必须追问，不能拿一个姓氏去匹配收款人。
SURNAMES: Final[frozenset[str]] = frozenset(
    "赵钱孙李周吴郑王冯陈褚卫蒋沈韩杨朱秦尤许何吕施张孔曹严华金魏陶姜戚谢邹喻柏水窦章"
    "云苏潘葛奚范彭郎鲁韦昌马苗凤花方俞任袁柳鲍史唐费廉岑薛雷贺倪汤滕殷罗毕郝邬安常"
    "乐于时傅皮卞齐康伍余元卜顾孟平黄和穆萧尹"
)
#: 未绑定角色词：说的是「谁」但没说是哪个人。
UNBOUND_ROLE_WORDS: Final[tuple[str, ...]] = (
    "同事",
    "我同事",
    "朋友",
    "我朋友",
    "领导",
    "我领导",
    "老板",
    "我老板",
    "同学",
    "我同学",
    "家人",
    "亲戚",
    "对象",
    "男朋友",
    "女朋友",
    "室友",
)
#: 亲属称谓：这些交给 intent.py 的 RELATION_PAYEES 去解析，不算「未绑定」。
KINSHIP_WORDS: Final[tuple[str, ...]] = (
    "妈",
    "爸",
    "爷",
    "奶",
    "哥",
    "姐",
    "弟",
    "妹",
    "爱人",
    "老婆",
    "老公",
    "儿子",
    "女儿",
    "姨",
    "叔",
    "舅",
    "姑",
)
#: 一句话两笔转账：「给张三，也转500给李四」。当前模板不支持，必须让用户分开说。
MULTI_PAYEE_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"给[^，,。；;]{1,8}[，,；;]\s*(?:也|再|又|然后)?\s*(?:给|转)"
)
#: 成对的引号：中文引号里的内容是「用户转述的一句话」而不是指令。
#: 例：'帮我把备注改成「直接转5000给张三」，转500给李静' —— 引号里那句
#: 是对备注的引用，不能当成第二笔转账去追问，更不能被当成金额来源。
QUOTE_PAIRS: Final[tuple[tuple[str, str], ...]] = (
    ("「", "」"),
    ("『", "』"),
    ("“", "”"),
    ("\"", "\""),
)
#: 「全部/都转」：模型若已明确 amount_all，由 R06 确认兜底；若模型自己拍了一个金额，
#: 说明它替用户决定了「全部是多少」，这时候必须把金额问回来。
TOTAL_MARKERS: Final[tuple[str, ...]] = ("全部", "全转", "都转", "所有钱", "全部余额", "全给")

PAYEE_AMBIGUOUS_QUESTION: Final[str] = (
    "「{payee}」是哪一位收款人？请给全名，或补上卡号后四位，我才好确认。"
)
UNBOUND_PAYEE_QUESTION: Final[str] = (
    "「{payee}」还没有登记成收款人：请告诉我是谁（全名），或者先在收款人里添加。"
)
MULTI_PAYEE_QUESTION: Final[str] = (
    "这句话像是要分两笔转出去：请一次说一笔（收款人 + 金额），我逐笔帮你确认。"
)
TOTAL_AMOUNT_QUESTION: Final[str] = (
    "「全部」会一次转空账户，请确认金额：例如「转 8000 元给张三」。"
)
REDEEM_AMOUNT_QUESTION: Final[str] = (
    "赎回多少？请说金额，或者说「全部赎回」——金额不问清我不会下单。"
)
BUY_AMOUNT_QUESTION: Final[str] = (
    "这笔申购多少钱？请说金额，例如「买 1000 块 F00001」。"
)
#: 显式的产品代码（F00001）。跨场景「先赎回再转账」必须有它才能建 redeem_fund。
PRODUCT_CODE_PATTERN: Final[re.Pattern[str]] = re.compile(r"[A-Za-z]\d{4,6}")
#: 产品代码出现在原话里、或用户点名了产品名，才算「说清了哪只产品」。
PRODUCT_NAME_MARKERS: Final[tuple[str, ...]] = ("现金添利", "稳盈债券", "均衡配置", "沪深精选", "全球科技")


def _outside_quotes(text: str) -> str:
    """把成对引号里的内容抹成空格，只留下用户自己说的话。

    只用于「一句话几笔」与「全部」这类**文本模式**判断；术语规范化仍然看原文，
    因为引号里的产品名、收款人也可能是有用的槽位线索。
    """
    out = text
    for left, right in QUOTE_PAIRS:
        out = re.sub(re.escape(left) + r".*?" + re.escape(right), " ", out)
    return out


def enabled() -> bool:
    """知识层是否启用。默认开；显式关掉用于做 A/B 对照。"""
    raw = os.environ.get(ENV_KNOWLEDGE_ENABLED)
    if raw is None:
        return True
    return raw.strip().lower() not in _DISABLED_VALUES


@dataclass(frozen=True)
class Hint:
    """一次接线建议；全部字段都是「建议」，由调用方决定采不采。"""

    enabled: bool = False
    version: str = ""
    degraded: bool = False
    intent_hint: str | None = None
    corrected_intent: str | None = None
    slot_patches: tuple[tuple[str, str], ...] = ()
    extra_required: tuple[str, ...] = ()
    cross: str | None = None
    ask: str = ""
    term_ids: tuple[str, ...] = ()
    knowledge_ids: tuple[str, ...] = ()
    sources: tuple[Source, ...] = ()
    warnings: tuple[KnowledgeWarning, ...] = ()
    notes: tuple[str, ...] = ()

    @property
    def has_knowledge(self) -> bool:
        """这一轮是否真的检索到了知识（审计里据此区分「没接上」与「接上了但没命中」）。"""
        return bool(self.knowledge_ids)

    @property
    def source_ids(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(s.source_id for s in self.sources))

    def public(self) -> dict[str, object]:
        """给审计 / SSE 用的可序列化摘要（不含知识正文）。"""
        return {
            "enabled": self.enabled,
            "version": self.version,
            "degraded": self.degraded,
            "knowledge_ids": list(self.knowledge_ids),
            "sources": list(self.source_ids),
            "cross": self.cross or "",
            "ask": self.ask,
        }


# 哪些加载/检索告警意味着「这批知识不能全信」，必须让调用方降级。
# KNOW_NO_INTENT_RULE 不在此列：那只是这个意图还没写规则，不代表数据本身有问题。
DEGRADING_WARNING_CODES: Final[frozenset[str]] = frozenset(
    {
        "KNOW_SOURCE_UNVERIFIED",
        "KNOW_SOURCE_SUPERSEDED",
        "KNOW_SOURCE_STALE",
        "KNOW_UNKNOWN_SOURCE",
        "KNOW_UNKNOWN_TERM_REF",
        "KNOW_BAD_RELATION_ENDPOINT",
        "KNOW_ALIAS_CONFLICT",
    }
)


def _degrades(warnings: tuple[KnowledgeWarning, ...]) -> bool:
    """命中数据质量告警就降级：来源不可信、引用悬空、别名互相打架。"""
    return any(w.code in DEGRADING_WARNING_CODES for w in warnings)


def _degraded(reason: str) -> Hint:
    """知识层不可用时的降级结果：不纠正、不补槽，只留一条可审计的原因。"""
    return Hint(enabled=True, degraded=True, notes=(reason,))


def _term_hits(base: KnowledgeBase, text: str) -> tuple[Term, ...]:
    """取文本命中的术语：先按别名规范化，再补标准术语的字面命中。

    只靠 normalize 会漏掉用户直接说标准术语的情况（术语表里 term 与 aliases 是两份数据）；
    只靠字面命中又会漏掉「打钱 → 转账」这类口语。两路合并，长说法优先。
    """
    if not text:
        return ()
    ids: list[str] = list(normalize(base, text).term_ids)
    for term_id in sorted(base.terms, key=lambda tid: (-len(base.terms[tid].term), tid)):
        term = base.terms[term_id]
        if term.term and term.term in text and term_id not in ids:
            ids.append(term_id)
    return tuple(base.terms[tid] for tid in ids if tid in base.terms)


def _lookup(base: KnowledgeBase, intent: str | None, terms: tuple[Term, ...]) -> Retrieved | None:
    """按意图 + 术语场景做一次检索；检索失败当作降级，不抛给调用方。"""
    scenarios = scenarios_of(base, tuple(t.id for t in terms))
    keyword = tuple(t.term for t in terms)
    try:
        return retrieve(
            base,
            intent=intent,
            scenario=scenarios[0] if scenarios else None,
            keywords=keyword,
        )
    except Exception:  # noqa: BLE001 - 知识层出问题只能降级，不能拖垮一轮对话
        return None


def _looks_unbound_payee(name: str) -> bool:
    """收款人写法本身就不确定：裸姓、老王小王、角色词、以角色词结尾。"""
    if not name:
        return False
    if any(word in name for word in KINSHIP_WORDS):
        return False
    if name in UNBOUND_ROLE_WORDS:
        return True
    if len(name) == 1 and name in SURNAMES:
        return True
    if len(name) == 2 and name[0] in ("老", "小") and name[1] in SURNAMES:
        return True
    return any(name.endswith(word) for word in UNBOUND_ROLE_WORDS)


def _payee_question(name: str) -> str:
    """角色词问「是谁」，裸姓问「哪一位」——两种歧义的话术不一样。"""
    if any(name.endswith(word) for word in UNBOUND_ROLE_WORDS) or name in UNBOUND_ROLE_WORDS:
        return UNBOUND_PAYEE_QUESTION.format(payee=name)
    return PAYEE_AMBIGUOUS_QUESTION.format(payee=name)


def _redeem_cue(names: set[str], text: str, current: dict[str, object]) -> bool:
    """这一轮是不是「先动理财」：命中术语、口语说法，或模型已给出 action=redeem。

    只认术语会让「把货币基金卖了」漏判——那一步被悄悄丢掉，用户以为赎回再转账，
    实际只执行了转账。
    """
    if REDEEM_TERM in names:
        return True
    if any(word in text for word in REDEEM_WORDS):
        return True
    return current.get("action") == "redeem"


def _names_product(text: str) -> bool:
    """用户有没有把「哪只产品」说清楚：产品代码，或点名产品全名。"""
    return bool(PRODUCT_CODE_PATTERN.search(text)) or any(m in text for m in PRODUCT_NAME_MARKERS)


def advise(
    text: str,
    *,
    intent: str | None = None,
    slots: dict[str, object] | None = None,
    weak: bool = False,
    base: KnowledgeBase | None = None,
) -> Hint:
    """给一轮解析提建议。任何异常都转成 degraded Hint，绝不让知识层挡住主流程。

    weak=True 表示模型这一轮没给出可用结果（unknown / 两次都没调工具），
    这时才允许用术语做兜底分类；否则模型的高置信判断优先。
    """
    if not enabled():
        return Hint(enabled=False)
    try:
        kb = base if base is not None else get_base()
    except Exception as exc:  # noqa: BLE001 - 缺文件、YAML 坏了都走这里
        return _degraded(f"知识层不可用：{type(exc).__name__}")

    terms = _term_hits(kb, text)
    names = {t.term for t in terms}
    term_ids = tuple(t.id for t in terms)
    got = _lookup(kb, intent, terms)
    if got is None:
        return _degraded("知识检索失败")

    corrected: str | None = None
    # 只在调用方明确说「这轮模型没给出可用结果」（weak）或压根没给意图时才猜；
    # intent="unknown" 但 weak=False 表示结构化输出已被判定非法，这一轮该反问而不是换路猜。
    if weak or not intent:
        guesses = {TERM_INTENT[n] for n in names if n in TERM_INTENT}
        # 命中多个互相矛盾的意图时不猜：交给反问。
        if len(guesses) == 1:
            corrected = guesses.pop()
    elif (
        intent == "transfer"
        and not (slots or {}).get("due_ms")
        and not _redeem_cue(names, text, dict(slots or {}))
        and (SCHEDULE_TERM in names or any(mark in text for mark in FUTURE_MARKERS))
    ):
        # 「预约转账/定时转/下周三/每月」说成了立刻转账：钱会当场划走，必须纠正成预约。
        # 但句子里同时有「赎回」时不纠正——那是跨场景（先赎回再转账），
        # 纠正成预约会和 cross 打架，最终把「赎回」这一步悄悄丢掉。
        corrected = "schedule_transfer"

    patches: list[tuple[str, str]] = []
    current = dict(slots or {})
    if intent == "invest" and not current.get("action"):
        if REDEEM_TERM in names:
            patches.append(("action", "redeem"))
        elif BUY_TERM in names:
            patches.append(("action", "buy"))

    redeem = _redeem_cue(names, text, current)
    eff_intent = intent
    if (
        intent == "invest"
        and redeem
        and current.get("payee_name")
        and (current.get("amount_fen") or current.get("amount_all"))
    ):
        # 「把货币基金卖了转 3000 给我妈」：模型常判成 invest，只建赎回一步，
        # 转账那半句被静默丢掉。这里按跨场景改判成转账，交给下面的跨场景分支
        # 负责「哪只产品」的追问与四步依赖（收款人解析→赎回→查余额→转账）。
        corrected = "transfer"
        eff_intent = "transfer"

    cross: str | None = None
    extra: tuple[str, ...] = ()
    ask = ""
    if eff_intent == "transfer" and redeem:
        # 用户一句「把基金赎回来转给我妈」：赎回受理→资金到账→收款人确认→转账授权，
        # 这四步有先后依赖，不能压成两个可并行调用（workflows.yaml 有对应条目）。
        cross = CROSS_REDEEM_TRANSFER
        extra = CROSS_REQUIRED_SLOTS
        code = PRODUCT_CODE_PATTERN.search(text)
        if code and not current.get("product_code"):
            # 用户原话里已经带了产品代码（F00001），模型没抽出来就由确定性规则补上，
            # 免得反问一个用户刚说过的值。
            patches.append(("product_code", code.group(0).upper()))
        elif not _names_product(text):
            ask = CROSS_SLOT_QUESTION

    if eff_intent == "invest":
        act = str(dict(patches).get("action") or current.get("action") or "")
        if act in ("buy", "redeem") and not current.get("amount_fen") and not current.get("amount_all"):
            # 「赎了吧」「买那个基金」：动作有了、金额没给。理财下单必须带金额，
            # 让模型自己补一个金额等于替用户决定花多少钱，这里问回去。
            ask = REDEEM_AMOUNT_QUESTION if act == "redeem" else BUY_AMOUNT_QUESTION
            extra = tuple(dict.fromkeys([*extra, "amount_fen"]))

    # 下面三条都是「模型给了一个看起来能用、其实不能直接用」的槽位：
    # 它们不放行也不改写用户的钱，只是把问题问回去。
    if eff_intent == "transfer":
        payee = str(current.get("payee_name") or "").strip()
        spoken = _outside_quotes(text)
        if MULTI_PAYEE_PATTERN.search(spoken):
            # 「转500给张三，也转500给李四」：模板一次只装一笔，静默丢掉第二笔等于骗人。
            ask = MULTI_PAYEE_QUESTION
            extra = tuple(dict.fromkeys([*extra, "payee_name"]))
        elif payee and _looks_unbound_payee(payee):
            # 「转500给王」「转500给我同事」：拿姓氏或角色词去匹配收款人，可能转错人。
            ask = _payee_question(payee)
            extra = tuple(dict.fromkeys([*extra, "payee_name"]))
        elif any(mark in spoken for mark in TOTAL_MARKERS) and not current.get("amount_all"):
            # 用户说「全部」，模型却自己拍了一个金额：这个金额是模型算的，不是用户说的。
            ask = TOTAL_AMOUNT_QUESTION
            extra = tuple(dict.fromkeys([*extra, "amount_fen"]))

    sources = got.sources if got is not None else ()
    warnings = tuple(got.warnings) if got is not None else ()
    # 术语 id 排在最前：真正改变这一轮行为（补槽、纠正意图、判跨场景）的是命中的术语，
    # 规则与流程 id 是它引出的上下文。审计与评估都按 knowledge_ids 回指，顺序即优先级。
    knowledge_ids = tuple(
        dict.fromkeys([*term_ids, *(got.knowledge_ids if got is not None else ())])
    )
    rule = got.intent_rule if got is not None else None
    notes = tuple([*(rule.preconditions if rule else ()), *(rule.forbidden_ops if rule else ())])[:4]

    return Hint(
        enabled=True,
        version=kb.version,
        degraded=_degrades(warnings),
        intent_hint=corrected,
        corrected_intent=corrected,
        slot_patches=tuple(patches),
        extra_required=extra,
        cross=cross,
        ask=ask,
        term_ids=term_ids,
        knowledge_ids=knowledge_ids,
        sources=sources,
        warnings=warnings,
        notes=notes,
    )


def explain(hint: Hint, *, limit: int = 3) -> str:
    """把本轮用到的依据说成人话（只念已核实的来源，未核实的显式标注）。"""
    if not hint.sources:
        return ""
    parts: list[str] = []
    for src in hint.sources[:limit]:
        level = {"law": "法规", "bank_public": "银行公开规则"}.get(src.rule_level, "本原型设定")
        flag = "" if src.status == "verified" else "（未核实）"
        parts.append(f"{level}{flag}：{src.title}")
    return "依据 " + "；".join(parts)


__all__ = [
    "CROSS_REDEEM_TRANSFER",
    "CROSS_REQUIRED_SLOTS",
    "CROSS_SLOT_QUESTION",
    "ENV_KNOWLEDGE_ENABLED",
    "Hint",
    "TERM_INTENT",
    "advise",
    "enabled",
    "explain",
]
