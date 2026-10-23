"""用户原话层的多收款人 / 多笔付款预检查（P0 安全补强）。

为什么单独一个模块，而不是塞进 kb_bridge：
- kb_bridge 的建议依赖知识库加载与 KNOWLEDGE_ENABLED；安全边界不能因为 A/B 关掉
  知识层就消失。这个检查只看用户原话，永远生效。
- 它不看模型给的 payee_name / amount_fen。模型把「李静和她老公」只抽成「李静」时，
  这里仍然能从原话看出有第二个收款人。
- 命中后只做一件事：判定这句话必须先澄清。意图层据此置 question、ok=False，
  执行层因此不会建出任何含资金写操作的 DAG。

刻意不做的事：
- 不判断能不能转、额度够不够、是否需要确认——那是 app/policy/ 的职责；
- 不识别「谁是收款人」——只识别「这句话里像是有多个收款人 / 多笔付款」；
- 成对引号里的内容是用户转述的数据，不参与判断；
- 明确否定的句子（不要转给…）不算新的付款请求；
- 不因为一个「和」字就报歧义：没有付款动词的「给妈妈和爸爸问候一下」不拦。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Final

#: 理由码：审计与测试都按这三个值断言，不改文案就不会误伤。
REASON_MULTI_PAYEE: Final[str] = "multi_payee"
REASON_MULTI_TRANSFER: Final[str] = "multi_transfer"
REASON_SPLIT: Final[str] = "split_ambiguous"

#: 成对引号：里面的内容是「用户转述的一句话」，不是指令。
QUOTE_PAIRS: Final[tuple[tuple[str, str], ...]] = (
    ("「", "」"),
    ("『", "』"),
    ("“", "”"),
    ('"', '"'),
)

#: 付款动词。长的写在前面，避免「转账」被拆成「转」+「账」。
PAY_VERB_RE: Final[re.Pattern[str]] = re.compile(
    r"转账|汇款|打钱|打款|付款|支付|转给|汇给|转"
)

#: 收款人之间的连接词：和/与/跟/、/及/以及。
PAIR_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:转给|打给|汇给|给|向)\s*"
    r"(?P<a>[\u4e00-\u9fa5A-Za-z]{1,6})\s*"
    r"(?:和|与|跟|、|及|以及)\s*"
    r"(?P<b>[\u4e00-\u9fa5A-Za-z]{1,8})"
)

#: 「各转/分别/分摊/一人一半/AA」这类显式拆分说法。
SPLIT_RE: Final[re.Pattern[str]] = re.compile(r"(?:各转|各给|分别|分摊|平摊|一人一半|AA|aa)")

#: 备注/附言之后是「给收款人看的文字」，不是第二笔指令；注入扫描仍看全文。
_NOTE_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:备注|附言|留言)\s*(?:写|填写|填|为|是|[:：])?.*",
    re.S,
)

#: 「示例/例如/比如」引出的是一句被转述的话，不是用户自己下的指令。
_EXAMPLE_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:示例|例子|例如|比如|假设|假如)[^。；;！!？?\n]*"
)

#: 已经发生的付款（今天已经转出去 900 了 / 我昨天转了 500）不是新指令。
_PAST_RE: Final[re.Pattern[str]] = re.compile(
    r"[^，,。；;！!？?\n]*(?:转出去|已经转|转了|转过|汇过|付过|支付过)[^，,。；;！!？?\n]*"
)

#: 明确否定：这不是一笔新付款，别拿它去拦「取消转给李静和她老公」。
NEGATION_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:不要|不需要|没必要|(?<![分特告级类性个])别(?!人|的)|不用|无需|取消|撤销|停止|不想|没有|没|不是)"
    r"[^，,。；;！!？?\n]{0,10}?(?:转|汇|付|打钱|支付)"
)

#: 被否定的那一小句。只抹掉这一句，不抹掉整句话：
#: 「别转给张三，转给李四」里第二句仍是有效指令；
#: 「转500给张三，再转500给李四，不要转给王五」里前两笔仍必须拦。
_NEGATED_CLAUSE_RE: Final[re.Pattern[str]] = re.compile(
    r"[^，,。；;！!？?\n]*(?:不要|不需要|没必要|(?<![分特告级类性个])别(?!人|的)|不用|无需|取消|撤销|停止|不想|没有|没|不是)"
    r"[^，,。；;！!？?\n]{0,10}?(?:转|汇|付|打钱|支付)[^，,。；;！!？?\n]*"
)

#: 收款人候选里明显不是人的词：命中就说明这次匹配是句式噪声，不能拿它拦用户。
_NOT_A_PERSON: Final[tuple[str, ...]] = (
    "账户", "账号", "卡号", "银行卡", "钱", "余额", "备注", "时间", "日期",
    "手机", "号码", "密码", "理财", "基金", "账单", "发票", "多少", "什么",
    "哪", "吗", "呢", "吧", "请", "帮", "我", "你", "他", "她", "它",
)

#: 金额/收款人尾部可能粘上的动词或量词，匹配后先削掉再当名字。
_TRIM_CHARS: Final[str] = "转给汇付打款钱元块万亿千百 \t"

MULTI_PAYEE_QUESTION: Final[str] = (
    "这句话里有不止一个收款人（{parties}）。请一次说一笔：先告诉我转给谁、转多少，"
    "我逐笔确认；没说清之前不会转。"
)
MULTI_TRANSFER_QUESTION: Final[str] = (
    "这听起来是一句话里分两笔转账。请一次说一笔（收款人 + 金额），我逐笔确认；"
    "没说清之前不会转。"
)
SPLIT_QUESTION: Final[str] = (
    "这笔钱要在几个人之间分？请分别说清每个人多少钱，或拆成两笔，我逐笔确认。"
)


@dataclass(frozen=True)
class Finding:
    """一次预检查的结论；ambiguous=True 时调用方只能反问。"""

    ambiguous: bool = False
    reason: str = ""
    question: str = ""
    parties: tuple[str, ...] = field(default_factory=tuple)
    evidence: str = ""

    @property
    def has_parties(self) -> bool:
        return bool(self.parties)


def _strip_quotes(text: str) -> str:
    """把成对引号里的内容抹成空格，只看用户自己说的话。"""
    out = text
    for left, right in QUOTE_PAIRS:
        if left == right:
            out = re.sub(re.escape(left) + r".*?" + re.escape(right), " ", out)
        else:
            out = re.sub(re.escape(left) + r".*?" + re.escape(right), " ", out)
    return out


def _instruction_text(text: str) -> str:
    """只留下「用户这次真要执行」的句子。

    备注、示例转述、已发生的付款都会带付款动词，但它们不是第二笔指令；
    不过滤会把「转 100 给李静，备注写：…转 5000」误判成一句话两笔，
    反而让真正的注入检测拿不到策略裁决。注入扫描仍然看全文。
    """
    out = _NOTE_RE.sub(" ", text)
    out = _EXAMPLE_RE.sub(" ", out)
    out = _PAST_RE.sub(" ", out)
    return out


def _clean(token: str) -> str:
    """削掉候选两侧的空白、标点和粘上的动词，返回可读的人名/称谓。"""
    return token.strip(_TRIM_CHARS + "，,。；;：:！!？?")


def _looks_like_person(token: str) -> bool:
    if not (1 <= len(token) <= 8):
        return False
    if token in _NOT_A_PERSON:
        return False
    return not any(word in token for word in _NOT_A_PERSON if len(word) > 1)


def _payment_clauses(text: str) -> list[str]:
    """找出「像一笔付款」的动词位置：动词后面 12 字内要有金额或「给/向」。"""
    clauses: list[str] = []
    for match in PAY_VERB_RE.finditer(text):
        tail = text[match.end() : match.end() + 12]
        if re.search(r"\d|[零一二两三四五六七八九十百千万]", tail) or re.match(
            r"\s*(?:给|向)", tail
        ):
            clauses.append(match.group(0))
    return clauses


def has_split_marker(text: str) -> bool:
    """原话里有没有显式的「分摊」说法（AA / 各付 / 一人一半）。"""
    return bool(SPLIT_RE.search(_strip_quotes(text)))


def inspect(text: str) -> Finding:
    """检查一句用户原话；任何不确定都返回「不拦」，把判断留给原有的追问与策略层。

    宁可漏拦一次由模型/知识层兜底，也不要把正常的一句话（「给妈妈和爸爸问候一下」）
    误判成两笔转账——那会让用户觉得系统听不懂人话。
    """
    if not isinstance(text, str) or not text.strip():
        return Finding()

    spoken = _instruction_text(_strip_quotes(text))
    # 只删被否定的那一句，不能因为句子里有一个「别转」就把整句放行：
    # 「别转给张三，转给李四」的第二句仍是有效指令。
    spoken = _NEGATED_CLAUSE_RE.sub(" ", spoken)
    if not _payment_clauses(spoken):
        return Finding()

    parties: list[str] = []
    pair_hit = ""
    for match in PAIR_RE.finditer(spoken):
        a = _clean(match.group("a"))
        b = _clean(match.group("b"))
        if _looks_like_person(a) and _looks_like_person(b):
            for name in (a, b):
                if name not in parties:
                    parties.append(name)
            pair_hit = pair_hit or match.group(0)

    clauses = _payment_clauses(spoken)
    if len(parties) >= 2:
        # 「转一千给李静和她老公」：两个收款人只给了一个总额，必须问清怎么分。
        if SPLIT_RE.search(spoken):
            return Finding(
                ambiguous=True,
                reason=REASON_SPLIT,
                question=SPLIT_QUESTION,
                parties=tuple(parties),
                evidence=pair_hit[:60],
            )
        return Finding(
            ambiguous=True,
            reason=REASON_MULTI_PAYEE,
            question=MULTI_PAYEE_QUESTION.format(parties="、".join(parties)),
            parties=tuple(parties),
            evidence=pair_hit[:60],
        )

    if len(clauses) >= 2:
        # 「转500给张三，也转500给李四」：一句话两笔，但模板一次只装一笔。
        return Finding(
            ambiguous=True,
            reason=REASON_MULTI_TRANSFER,
            question=MULTI_TRANSFER_QUESTION,
            parties=tuple(parties),
            evidence=spoken[:60],
        )

    return Finding()


__all__ = [
    "Finding",
    "MULTI_PAYEE_QUESTION",
    "MULTI_TRANSFER_QUESTION",
    "PAIR_RE",
    "REASON_MULTI_PAYEE",
    "REASON_MULTI_TRANSFER",
    "REASON_SPLIT",
    "SPLIT_QUESTION",
    "has_split_marker",
    "inspect",
]
