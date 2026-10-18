"""红队 runner 的离线脚本模型：只还原「模型输出意图与槽位」这一步。

不 import 任何真实 LLM 客户端、不读 API key，断网可跑；它下游的策略裁决、DAG、
令牌、两阶段资金、工具执行都是生产代码，脚本模型不碰。

槽位抽取口径（写在这里是为了让判定可复核）：
    意图   关键词表按序命中；卡片写操作（挂失/改密码等）刻意不落 query_balance，
           意图空间里本来就没有它们，让它走 unknown 反问才是真实行为
    金额   取动词附近最近的候选数字，并剔除 13—19 位卡号段
    收款人 优先 p_[0-9a-f]+ 形式的 id → 引号内容（引号前有 merchant/商户则算商户名）
           → 最后一个「转给/付给/汇给/划给/转到/给」之后的中文名或卡号串
    备注   取「备注」之后的文本；mixed_language 故障往每三个字里插一个零宽字符
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from app.llm import LLMError, LLMResponse, ToolCall
from app.money import parse_amount
from app.planner import intent




_NUM = r"(?:[0-9]+(?:\.[0-9]+)?\s*[万千百]?|[零一二两三四五六七八九十百千万壹贰叁肆伍陆柒捌玖拾]+)"
_AMOUNT_RE = re.compile(_NUM)
_CARD_RE = re.compile(r"(?<![0-9])[0-9](?:[0-9 ]{11,22}[0-9])(?![0-9])")
_VERB_RE = re.compile(r"转|付|汇|划|扣|到")
_GIVE_RE = re.compile(r"转给|付给|汇给|划给|转到|给")
_CJK_RE = re.compile(r"[\u4e00-\u9fa5]{2,20}")
_ID_RE = re.compile(r"p_[0-9a-f]+")
_QUOTE_RE = re.compile(r"[「『“]([^」』”]{2,40})[」』”]")
_ZW = "\u200b"

#: 意图关键词表。按顺序先命中先算；卡片写操作（挂失/改密码等）刻意不落到
#: query_balance——意图空间里本来就没有它们，让它走 unknown 反问才是真实行为。
INTENT_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("bill_analysis", ("账单", "交易", "流水", "花了", "消费", "对账", "明细", "异常")),
    ("subscription", ("订阅", "会员", "续费")),
    ("birthday_plan", ("生日", "下单", "买花", "鲜花", "买蛋糕", "花店")),
    ("query_balance", ("余额", "卡号", "账户", "额度", "身份证", "还剩", "剩多少", "多少钱")),
    ("transfer", ("转", "付", "汇", "划", "扣")),
)


@dataclass
class Understanding:
    """脚本模型对一句话的理解：意图 + 槽位 + 用于探针的「用户到底要求了什么」。"""

    text: str
    intent: str
    payee_name: str | None
    amount_fen: int | None
    note: str | None
    merchant: str | None
    requested_payee: str | None
    stated_amounts: frozenset[int]

    def to_args(self) -> dict[str, Any]:
        args: dict[str, Any] = {
            "intent": self.intent,
            "confidence": 0.9 if self.intent != "unknown" else 0.2,
            "missing": [],
        }
        for key, value in (
            ("payee_name", self.payee_name),
            ("amount_fen", self.amount_fen),
            ("note", self.note),
            ("merchant", self.merchant),
        ):
            if value is not None:
                args[key] = value
        return args


def _amount_candidates(text: str) -> list[tuple[int, int, int]]:
    """文本里所有金额候选 [(start, end, 分)]，卡号段整段剔除。"""
    spans = [(m.start(), m.end()) for m in _CARD_RE.finditer(text)]
    out: list[tuple[int, int, int]] = []
    for match in _AMOUNT_RE.finditer(text):
        if any(start <= match.start() and match.end() <= end for start, end in spans):
            continue
        value = parse_amount(match.group(0))
        if value and value > 0:
            out.append((match.start(), match.end(), value))
    return out


def _pick_amount(text: str) -> tuple[int | None, frozenset[int]]:
    """金额取「最后一个动词前最近的候选数字」——即用户最后一次下指令的位置。

    A09「全部转给李静，大概五百五」取不到数字 → 交给 intent.parse 的
    _guess_amount 兜底，命中「全部」落 amount_all，正是用例要考的歧义路径。
    """
    candidates = _amount_candidates(text)
    verbs = list(_VERB_RE.finditer(text))
    if not candidates:
        return None, frozenset()
    if verbs:
        last = verbs[-1].start()
        before = [item for item in candidates if item[1] <= last]
        if before:
            return before[-1][2], frozenset(item[2] for item in candidates)
        after = [item for item in candidates if item[0] >= last]
        if after:
            return after[0][2], frozenset(item[2] for item in candidates)
    return candidates[0][2], frozenset(item[2] for item in candidates)


def _pick_payee(text: str) -> tuple[str | None, str | None]:
    """收款人（或商户）名。返回 (payee_name, merchant)。"""
    found_id = _ID_RE.search(text)
    if found_id:
        return found_id.group(0), None
    quote = _QUOTE_RE.search(text)
    if quote:
        if re.search(r"merchant|商户", text[: quote.end()], re.IGNORECASE):
            return None, quote.group(1)
        return quote.group(1), None
    gives = list(_GIVE_RE.finditer(text))
    if not gives:
        return None, None
    tail = text[gives[-1].end() :].lstrip()
    if re.match(r"[0-9]", tail):
        card = re.match(r"[0-9](?:[0-9 ]{10,25}[0-9])", tail)
        return (card.group(0) if card else tail.split(" ")[0]), None
    cjk = _CJK_RE.match(tail)
    return (cjk.group(0) if cjk else None), None


def _pick_note(text: str) -> str | None:
    match = re.search(r"备注(?:写)?[：:\s]*", text)
    if not match:
        return None
    tail = text[match.end() :].split("。")[0].strip()
    return tail or None


def _obfuscate(text: str) -> str:
    """text_obfuscation: mixed_language —— 多语言句子里掺零宽字符。"""
    out: list[str] = []
    for index, char in enumerate(text):
        out.append(char)
        if index % 3 == 2 and not char.isspace():
            out.append(_ZW)
    return "".join(out)


def understand(text: str) -> Understanding:
    for name, keywords in INTENT_KEYWORDS:
        if any(keyword in text for keyword in keywords):
            break
    else:  # pragma: no cover - INTENT_KEYWORDS 最后一项恒真
        name = "unknown"
    if name == "transfer" and not any(word in text for word in ("转账", "转给", "转到", "转", "付", "汇")):
        name = "unknown"
    amount_fen, stated = _pick_amount(text)
    payee_name, merchant = _pick_payee(text)
    return Understanding(
        text=text,
        intent=name,
        payee_name=payee_name,
        amount_fen=amount_fen,
        note=_pick_note(text),
        merchant=merchant,
        requested_payee=payee_name,
        stated_amounts=stated,
    )


class ScriptedAdapter:
    """离线脚本模型：只实现 ChatAdapter 需要的 .chat()。

    fault=llm_timeout 直接抛 LLMError（等价真实客户端超时/连续 5xx）；
    fault=llm_refusal 不调 emit_intent（模型拒答）。
    """

    def __init__(self, understanding: Understanding, fault: str | None = None) -> None:
        self.understanding = understanding
        self.fault = fault
        self.calls = 0

    def chat(self, messages: Any, tools: Any = None, **kwargs: Any) -> LLMResponse:
        self.calls += 1
        if self.fault == "llm_timeout":
            raise LLMError("脚本注入：模型超时", status=504, retryable=True)
        if self.fault == "llm_refusal":
            return LLMResponse(content="抱歉，这个我做不到。", tool_calls=[])
        call = ToolCall(name=intent.EMIT_INTENT_NAME, arguments=json.dumps(self.understanding.to_args(), ensure_ascii=False), id=f"call_{self.calls}")
        return LLMResponse(content="", tool_calls=[call])
