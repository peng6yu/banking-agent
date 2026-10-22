"""MOCK_MODE=script 的协作回放规则：断网也能把主链路演完的确定性替身。

跟 app/llm.py 里的 MockAdapter 不是一回事。那个是「恶意模型」，只用于对抗演示：
固定吐「转出全部余额到陌生账户」的计划，用来说明「模型被劫持也动不了一分钱」。
这个是「正常办事」：断网时顶上，让查余额与转账这两条主链路照样跑通，
演示时不必为了断网临时改代码。

规则只有三条，认不出来就老实回 unknown，让系统照实反问，绝不瞎猜：
    1. 说了金额又说「转」→ 转账（收款人名、卡尾号能扫出来就带上）；
    2. 说了「余额」→ 查余额（说了卡尾号就带上）；
    3. 都不像 → unknown。

两条纪律：
    - 金额解析一律走 app.money.parse_amount，桩自己不写一套，免得两套规则对不上；
    - 纯规则、纯确定性：同一句话跑两次结果必须完全一样，零网络。

已知局限：注入的 L2 模型分类在这个桩下恒判「没命中」（L1 关键词规则照常生效），
所以这个桩只用来演正常业务链路，不用来演对抗。
"""

from __future__ import annotations

import re
from typing import Any, Final

from app.money import parse_amount

# 桩只回放这一个工具的调用结果。名字与 app/planner/intent_schema.py 的 EMIT_INTENT_NAME 一致；
# 这里不 import 那个模块：规划层在上层，低层模块不许反向依赖它。
EMIT_INTENT_NAME: Final[str] = "emit_intent"

_TAIL_PHRASE_RE: Final[re.Pattern[str]] = re.compile(r"(?:尾号|卡号|卡尾号)\s*\d{4}")
_CARD_TAIL_RE: Final[re.Pattern[str]] = re.compile(r"(?:尾号|卡号|卡尾号)\s*(\d{4})")
_AMOUNT_RE: Final[re.Pattern[str]] = re.compile(
    r"\d+(?:\.\d+)?\s*[元块圆]|[零〇一二两三四五六七八九十百千万]+\s*[元块圆钱]"
)
# 「转 500 给李静」「转500给李静」这种不带货币单位的说法：数字紧跟在动词后面
_NUM_AFTER_VERB_RE: Final[re.Pattern[str]] = re.compile(r"(?:转|付|给)\s*(\d+(?:\.\d+)?)")
# 「转两千给王强」这种中文数字：同样紧跟在动词后面
_CN_AFTER_VERB_RE: Final[re.Pattern[str]] = re.compile(r"(?:转|付|给)\s*([零〇一二两三四五六七八九十百千万]+)")
_PAYEE_RE: Final[re.Pattern[str]] = re.compile(r"(?:给|转给|付给|向)\s*([一-龥]{2,4})")
_POLITE_TAIL: Final[tuple[str, ...]] = ("谢谢", "谢", "啦", "吧", "呀", "啊", "呢")
_DATA_RE: Final[re.Pattern[str]] = re.compile(r"<DATA[^>]*>(.*?)</DATA>", re.S)


def script_intent_args(text: str) -> dict[str, Any]:
    """离线桩回放的 emit_intent 实参：确定性关键词规则，不联网、不猜。"""
    raw = str(text or "")
    if "转" in raw:
        amount_fen = script_amount(raw)
        if amount_fen > 0:
            args: dict[str, Any] = {
                "intent": "transfer",
                "amount_fen": amount_fen,
                "confidence": 0.9,
                "missing": [],
            }
            payee = script_payee(raw)
            if payee:
                args["payee_name"] = payee
            tail = script_card_tail(raw)
            if tail:
                args["card_tail"] = tail
            return args
    if "余额" in raw:
        args = {"intent": "query_balance", "confidence": 0.9, "missing": []}
        tail = script_card_tail(raw)
        if tail:
            args["card_tail"] = tail
        return args
    return {"intent": "unknown", "confidence": 0.0, "missing": ["intent"]}


def script_amount(text: str) -> int:
    """金额一律走 app.money.parse_amount，桩自己不写一套金额解析。

    三种写法依次试：带单位的（500元 / 五百元）、紧跟动词的阿拉伯数字（转 500 给…）、
    紧跟动词的中文数字（转两千给…）。都认不出就返回 0（当作「没解析出金额」）。
    """
    for pattern, group in ((_AMOUNT_RE, 0), (_NUM_AFTER_VERB_RE, 1), (_CN_AFTER_VERB_RE, 1)):
        match = pattern.search(text)
        if not match:
            continue
        amount_fen = parse_amount(match.group(group))
        if amount_fen > 0:
            return amount_fen
    return 0


def script_payee(text: str) -> str:
    """收款人名：给/转给/付给/向 后面的 2-4 个汉字，顺手去掉客套尾巴。"""
    match = _PAYEE_RE.search(_TAIL_PHRASE_RE.sub("", text))
    if not match:
        return ""
    name = match.group(1)
    for suffix in _POLITE_TAIL:
        if name.endswith(suffix) and len(name) > len(suffix):
            name = name[: -len(suffix)]
    return name


def script_card_tail(text: str) -> str:
    """卡尾号：说了「尾号 1234」就填 1234，没说就不填。"""
    match = _CARD_TAIL_RE.search(text)
    return match.group(1) if match else ""


def script_user_text(messages: Any) -> str:
    """取最后一条用户消息的原话（外面包着 <DATA> 信封就拆出来）。"""
    for item in reversed(list(messages or [])):
        if str(item.get("role")) == "user":
            return script_unwrap(str(item.get("content") or ""))
    return ""


def script_unwrap(content: str) -> str:
    """拆掉 <DATA source=user>…</DATA> 信封并还原转义。

    转义顺序见 app/security/envelope.py 的 escape()：先转 & 再转标签，
    所以还原要先还原标签（&lt;）再还原 &（&amp;），顺序反了会把 &amp;lt; 拆坏。
    """
    match = _DATA_RE.search(content)
    body = match.group(1) if match else content
    return body.replace("&lt;", "<").replace("&amp;", "&")


def script_first_tool_name(tools: Any) -> str:
    """调用方要的第一个工具名；不是 emit_intent 就说明这次不要结构化意图。"""
    for tool in tools or []:
        name = ((tool.get("function") or {}).get("name")) or ""
        if name:
            return str(name)
    return ""
