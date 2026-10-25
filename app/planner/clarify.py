"""澄清后续答：让「尾号4417」「第一个」这类短回答接回上一轮那句话。

为什么需要这个模块：收款人歧义闸门（app/planner/runner.py 的 _payee_gate）是以
一句反问结束整轮的——DAG 停了，下一轮又是一次全新的意图解析。用户只回一句
「尾号4417」时，解析器既不知道金额也不知道给谁，只能把整句重问一遍
（README「已知限制 3」记的就是这个体验）。

做法：闸门抛问题的那一刻，把「用户原话 + 候选收款人 + 问题」按会话记下来；
下一轮如果只是一句短回答（4 位尾号，或「第一个/第二位」这类序号），就把原话
补全成「原话（收款人卡尾号4417）」再交给同一个解析器。用户不必重说，
而「不盲选同名收款人」这条安全约束一点没松——补全用的尾号必须是候选里真实存在的。

刻意留在内存里：这是一次澄清的临时上下文，TTL 5 分钟，进程重启即失效；
不落库、不进审计原文（候选里只有姓名与尾号，本来就在 SSE 里给用户看过）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Final

from app.clock import now_ms

#: 澄清上下文有效期：5 分钟。超过就当作新问题，不再往上一轮上接。
CLARIFY_TTL_MS: Final[int] = 300_000
#: 尾号：允许「尾号4417」「后四位4417」「4417」三种写法。
_TAIL_RE: Final[re.Pattern[str]] = re.compile(r"(?:尾号|后四位|末四位|卡号)?\s*(\d{4})(?!\d)")
#: 序号：「第一个」「第2位」「第三张」。
_ORDINAL_RE: Final[re.Pattern[str]] = re.compile(r"第\s*([一二三四五六七八九十]|\d{1,2})\s*(?:个|位|张|条)")
_ORDINAL_DIGITS: Final[dict[str, int]] = {
    "一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
    "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
}


#: 这一轮反问需要用户补哪个槽位。记下来只为了「下一句短回答接回上一句」，
#: 不参与任何放行判断——真正执行仍然重新解析、重新裁决。
_SHORT_REPLY_MAX: Final[int] = 16
#: 明显是在说别的事（新请求 / 提问）：这些开头不当作「回答上一轮」。
_NEW_TALK_RE: Final[re.Pattern[str]] = re.compile(
    r"^(?:帮我|帮忙|麻烦|请|我想|我要|我要看|查|看|给我|取消|停掉|结束|挂失|冻结|"
    r"解冻|调整|赎回|申购|买|卖|建|创建|新建|记录|添加|把|为什么|怎么|什么)"
)
#: 带这些疑问词的是新问题，不是槽位取值。
_QUESTION_WORDS: Final[tuple[str, ...]] = ("怎么", "为什么", "什么", "能不能", "可以吗", "是不是", "多少")


@dataclass
class Pending:
    """一次待澄清的上下文：原话、被问到的收款人查询词、候选、问题。

    kind="payee"：收款人歧义闸门，候选里必须有一致的尾号才补全；
    kind="slots"：普通反问（缺金额/缺对象…），只有「短回答」才拼回上一句。
    """

    text: str
    query: str = ""
    candidates: list[dict[str, Any]] = field(default_factory=list)
    question: str = ""
    kind: str = "payee"
    created_ms: int = field(default_factory=now_ms)


_PENDING: dict[str, Pending] = {}


def remember(
    session_id: str,
    *,
    text: str,
    query: str,
    candidates: list[dict[str, Any]],
    question: str,
) -> None:
    """记下这次澄清；候选为空（「没找到这个人」）也记，但补全时只按名字重试。"""
    _PENDING[session_id] = Pending(
        text=text,
        query=query,
        candidates=[dict(item) for item in candidates if isinstance(item, dict)],
        question=question,
        kind="payee",
    )


def remember_slots(session_id: str, *, text: str, question: str, slots: list[str]) -> None:
    """普通反问也记一笔：下一句短回答（「100 元」「李静」）要能拼回这句话。

    不记的话，用户照着问句答一句就会被当成全新消息重新解析，于是只能收到
    「你是想查询、转账还是看账单？」——页面上那些可点选项点了也是白点。
    """
    _PENDING[session_id] = Pending(
        text=str(text or "").strip(),
        query=",".join(str(item) for item in slots),
        candidates=[],
        question=str(question or ""),
        kind="slots",
    )


def forget(session_id: str) -> None:
    """清掉澄清上下文（用户换了话题，或已经接上了）。"""
    _PENDING.pop(session_id, None)


def peek(session_id: str) -> Pending | None:
    """取未过期的澄清上下文；顺带清掉过期的（避免字典无限长）。"""
    pending = _PENDING.get(session_id)
    if pending is None:
        return None
    if now_ms() - pending.created_ms > CLARIFY_TTL_MS:
        _PENDING.pop(session_id, None)
        return None
    return pending


def augment(session_id: str, reply: str) -> str | None:
    """短回答 → 补全后的原话；接不上返回 None（调用方按正常解析走）。

    接得上的两种回答：
    - 尾号：必须是候选里真实出现过的 4 位尾号，否则不接（防手滑填错人）；
    - 序号：1 起的序号必须落在候选范围内。
    """
    pending = peek(session_id)
    if pending is None:
        return None
    text = str(reply or "").strip()
    if not text:
        return None
    if pending.kind == "slots":
        return _merge_slots(pending, text)
    chosen = _match_tail(pending, text) or _match_ordinal(pending, text)
    if chosen is None:
        return None
    forget(session_id)
    tail = str(chosen.get("card_tail") or "").strip()
    if tail:
        return f"{pending.text}（收款人卡尾号{tail}）"
    name = str(chosen.get("name") or "").strip()
    if name:
        return f"{pending.text}（收款人是{name}）"
    return None


def _merge_slots(pending: Pending, text: str) -> str | None:
    """短回答拼回上一句；不像回答（新请求、提问、太长）就返回 None 走正常解析。

    刻意不在这里 forget：拼完还会重新解析，如果仍然缺槽位，调用方会再记一笔
    新的上下文，累计成「我想转钱，100 元，李静」这种完整句子。
    """
    if len(text) > _SHORT_REPLY_MAX:
        return None
    if any(word in text for word in _QUESTION_WORDS):
        return None
    if len(text) > 4 and _NEW_TALK_RE.search(text):
        return None
    base = pending.text.strip()
    if not base:
        return None
    return f"{base}，{text}"


def _match_tail(pending: Pending, text: str) -> dict[str, Any] | None:
    """只在候选的尾号里找，避免把金额或日期里的 4 位数字当尾号。"""
    match = _TAIL_RE.search(text)
    if match is None:
        return None
    tail = match.group(1)
    for item in pending.candidates:
        if str(item.get("card_tail") or "").strip() == tail:
            return item
    return None


def _match_ordinal(pending: Pending, text: str) -> dict[str, Any] | None:
    match = _ORDINAL_RE.search(text)
    if match is None:
        return None
    raw = match.group(1)
    index = _ORDINAL_DIGITS.get(raw) if raw in _ORDINAL_DIGITS else _int_or_none(raw)
    if index is None or not 1 <= index <= len(pending.candidates):
        return None
    return pending.candidates[index - 1]


def _int_or_none(raw: str) -> int | None:
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


__all__ = [
    "CLARIFY_TTL_MS",
    "Pending",
    "augment",
    "forget",
    "peek",
    "remember",
    "remember_slots",
]
