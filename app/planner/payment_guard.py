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
#: 一句话给了多个金额 / 金额完全没定：同样必须先问清，不能替用户选一个。
REASON_MULTI_AMOUNT: Final[str] = "multi_amount"
#: 条件句 / 收款人只是指代：都需要先问清，不能当成一笔能立刻执行的转账。
REASON_CONDITIONAL: Final[str] = "conditional"
REASON_VAGUE_PAYEE: Final[str] = "vague_payee"

#: 成对引号：里面的内容是「用户转述的一句话」，不是指令。
QUOTE_PAIRS: Final[tuple[tuple[str, str], ...]] = (
    ("「", "」"),
    ("『", "』"),
    ("“", "”"),
    ('"', '"'),
)

#: 付款动词。长的写在前面，避免「转账」被拆成「转」+「账」。
PAY_VERB_RE: Final[re.Pattern[str]] = re.compile(
    r"转账|汇款|打钱|打款|付款|支付|转给|汇给|转|打给|打(?=[0-9零〇一二两三四五六七八九十百千万])"
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

#: 转述型冒号：「朋友发我一条消息：…」「客服说：…」「短信内容：…」冒号后面是
#: 别人的话，不是用户自己的指令。只吃到本句结束，避免把后面真正的请求一起吞掉。
_RELAY_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:转发|转载|短信|消息|通知|邮件|留言|客服说|对方说|朋友说|他说|她说|内容)"
    r"\s*(?:是|为)?\s*[:：][^。；;！!？?\n]*"
)

#: 一句话里给了两个金额（「转 300 或者 500 都行」「200 还是 300」）：金额没定，不能替用户选。
_MULTI_AMOUNT_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:\d+(?:\.\d+)?|[零〇一二两三四五六七八九十百千万]+)\s*(?:元|块|圆)?\s*"
    r"(?:或者|或|还是|要么)\s*"
    r"(?:\d+(?:\.\d+)?|[零〇一二两三四五六七八九十百千万]+)"
)

#: 条件句：「如果这个月还有余钱就转」「余额够的话帮我转」「等工资到了再转」。
#: 这类句子的执行时点取决于一个现在还不成立的条件，不能当成立即转账执行。
_CONDITIONAL_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:如果|要是|假如|万一|等到|等[^，,。；;\n]{0,8}?(?:到|了)|有余钱|还有钱|余额够|够的话)"
)

#: 收款人只是「上次那个人」「我朋友」这种指代：系统无法确定是谁，必须先问。
_VAGUE_PAYEE_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:上次|上回|之前|那个|那个谁|某|朋友|同事|同学|家人|亲人|亲戚|客户|老板|对方)"
)
#: 「谁」只有出现在收款人槽位时才算含糊：「转给谁」「给谁转 200」要问，
#: 而「谁都别告诉」「谁也别提」是在说保密，不是收款人——不能因为一个「谁」
#: 就把一笔明确写了卡号的转账拦下来（红队 A02 就是这么被误拦的）。
_VAGUE_WHO_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:给|向|转给|付给|打给|汇给)\s*谁|谁(?:那儿|那里|那边)"
)

#: 金额完全没给（「随便转点」「你看着给」）：不是没有金额，是用户还没决定。
_VAGUE_AMOUNT_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:随便|都行|你看着给|看着给|看着办|多少都行|随便转|转点|给点|发点)"
)

#: 用户自己改口：「不对，改成转给王强」「说错了，是 800」。改口之前的草稿不是指令，
#: 必须以最后一个改口标记为界，否则「转给李静 300，不对，改成王强」会被当成两个人。
_CORRECTION_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:不对|不是|说错|写错|改成|改为|换成|换为|重新说|更正|纠正)"
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
MULTI_AMOUNT_QUESTION: Final[str] = (
    "这句话里不止一个金额（{evidence}）。请告诉我这笔到底转多少，我再生成确认卡片；"
    "没定下来之前不会转。"
)
CONDITIONAL_QUESTION: Final[str] = (
    "这句话带条件（{evidence}），现在还不是一笔确定的转账。要我按条件帮你盯着、"
    "还是现在就转？条件不成立之前不会动钱。"
)
VAGUE_PAYEE_QUESTION: Final[str] = (
    "这句话里的收款人不够具体（{evidence}），我没法确定是谁。请给我一个明确的收款人"
    "（姓名，或者姓名 + 卡尾号），我先查一下同名再让你确认。"
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
    out = _RELAY_RE.sub(" ", out)
    return out


def _after_correction(text: str) -> str:
    """只保留最后一次改口之后的内容；改口前是草稿，不该再参与歧义判定。

    「转给李静 300，不对，改成转给王强 300」= 一笔，收款人王强。
    改口标记后面如果什么都没有（「不对」），保留原文，交给下游继续追问。
    """
    last: re.Match[str] | None = None
    for match in _CORRECTION_RE.finditer(text):
        last = match
    if last is None:
        return text
    tail = text[last.end() :]
    return tail.strip() if tail.strip() else text


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


def has_live_payment(text: str) -> bool:
    """这句话里还有没有「用户现在就想要的付款」：否定的、转述的、改口前的都不算。

    兜底解析用它挡住「不用转钱」「别转给张三」这类句子——否则术语表一看到「转钱」
    就把它当成转账请求，反过来替用户执行了他明确否定的事。
    """
    if not isinstance(text, str) or not text.strip():
        return False
    spoken = _instruction_text(_strip_quotes(text))
    spoken = _NEGATED_CLAUSE_RE.sub(" ", spoken)
    # 这里只看「还有没有付款动词」，不看金额是否紧跟在动词后面：
    # 「给王强转 200，金额改成 800」改口后的尾巴只有 800，但它仍然是一笔转账。
    if PAY_VERB_RE.search(spoken):
        return True
    return bool(PAY_VERB_RE.search(_after_correction(spoken)))


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
    # 改口只认最后一次：先前那句是被用户自己撤掉的草稿。
    spoken = _after_correction(spoken)
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

    conditional = _CONDITIONAL_RE.search(spoken)
    if conditional is not None:
        return Finding(
            ambiguous=True,
            reason=REASON_CONDITIONAL,
            question=CONDITIONAL_QUESTION.format(evidence=conditional.group(0)[:16]),
            parties=tuple(parties),
            evidence=spoken[:60],
        )

    vague = _VAGUE_PAYEE_RE.search(spoken) or _VAGUE_WHO_RE.search(spoken)
    names = payee_candidates(text)
    if vague is not None and (not names or any(_VAGUE_PAYEE_RE.search(name) for name in names)):
        return Finding(
            ambiguous=True,
            reason=REASON_VAGUE_PAYEE,
            question=VAGUE_PAYEE_QUESTION.format(evidence=vague.group(0)[:12]),
            parties=tuple(parties),
            evidence=spoken[:60],
        )

    amount_match = _MULTI_AMOUNT_RE.search(spoken)
    if amount_match is not None:
        # 「转 300 或者 500 都行」：这不是两个收款人，是金额没定。
        # 随便挑一个就是把用户的犹豫变成一笔确定交易，必须反问。
        return Finding(
            ambiguous=True,
            reason=REASON_MULTI_AMOUNT,
            question=MULTI_AMOUNT_QUESTION.format(evidence=amount_match.group(0)[:20]),
            parties=tuple(parties),
            evidence=amount_match.group(0)[:60],
        )
    if _VAGUE_AMOUNT_RE.search(spoken) and not re.search(
        r"\d|(?:[零〇一二两三四五六七八九十百千万])\s*(?:元|块)", spoken
    ):
        # 「随便转点钱给张三」：金额是空的，问一次比猜一次便宜。
        return Finding(
            ambiguous=True,
            reason=REASON_MULTI_AMOUNT,
            question=MULTI_AMOUNT_QUESTION.format(evidence="没写金额"),
            parties=tuple(parties),
            evidence=spoken[:60],
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


#: 单一收款人的抽取：只在兜底解析里用，不参与「是否歧义」的判断。
_SINGLE_PAYEE_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:转给|汇给|打给|付给|给|向)\s*"
    r"(?P<name>[\u4e00-\u9fa5A-Za-z·]{1,6}?)"
    r"(?=\s*(?:转|汇|打|付|发)|\d|[，,。；;！!？?\s]|$)"
)


def after_correction(text: str) -> str:
    """公开版：只保留用户最后一次改口之后的内容（改口前是草稿）。"""
    if not isinstance(text, str):
        return ""
    return _after_correction(_instruction_text(_strip_quotes(text)))


def payee_candidates(text: str, *, apply_correction: bool = True) -> tuple[str, ...]:
    """从一句正常转账话里抽收款人候选（去重、保序）。

    只回答「这句话里提到的像人名的对象」，不判断该不该转、也不判断够不够钱——
    那些仍然由策略层与工具层决定。抽不出就返回空元组，交给反问。
    apply_correction=False 用于「用户只改了金额」的句子：那时收款人还在改口前的前半句里。
    """
    if not isinstance(text, str) or not text.strip():
        return ()
    spoken = _instruction_text(_strip_quotes(text))
    spoken = _NEGATED_CLAUSE_RE.sub(" ", spoken)
    if apply_correction:
        spoken = _after_correction(spoken)
    seen: list[str] = []
    for match in _SINGLE_PAYEE_RE.finditer(spoken):
        name = _clean(match.group("name"))
        if _looks_like_person(name) and name not in seen:
            seen.append(name)
    return tuple(seen)


__all__ = [
    "Finding",
    "MULTI_PAYEE_QUESTION",
    "MULTI_TRANSFER_QUESTION",
    "PAIR_RE",
    "REASON_MULTI_PAYEE",
    "REASON_MULTI_TRANSFER",
    "REASON_SPLIT",
    "SPLIT_QUESTION",
    "CONDITIONAL_QUESTION",
    "MULTI_AMOUNT_QUESTION",
    "REASON_CONDITIONAL",
    "REASON_VAGUE_PAYEE",
    "VAGUE_PAYEE_QUESTION",
    "after_correction",
    "has_live_payment",
    "REASON_MULTI_AMOUNT",
    "has_split_marker",
    "inspect",
    "payee_candidates",
]
