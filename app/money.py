"""金额换算、格式化与中文口语解析。

对应文档：docs/04_核心模块实现规格.md 4.3、docs/08_编码规范.md 8.5。

铁律：金额全程 int（分），浮点不参与任何运算；只在出口（展示前）格式化。
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Final

from app.clock import ValidationError

# 单笔上限 100 万元（docs/04 4.3 分支表）
MAX_SINGLE_FEN: Final[int] = 100_000_000

# 账户互转（本人名下卡之间、亲属卡）与理财申购赎回是「钱还在自己口袋里」，收支统计一律排除。
# 对外转账（转给张三这种）不是内部搬运：钱真的离开了净资产，必须算支出——所以它不在这个元组里。
# 口径只写这一份：主动洞察（app/proactive.py）与账单工具（app/tools/bill.py）都从这里取，
# 否则「总览结余 3,476」和「账单净结余 -1,311」就是两套定义各说各话。
INTERNAL_CATEGORIES: Final[tuple[str, ...]] = ("账户互转", "理财")

# 「全部 / 所有钱」不给数字，由调用方替换成余额；money 层不许查库
UNKNOWN_AMOUNT_FEN: Final[int] = -1

_DIGITS: Final[dict[str, int]] = {
    "零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
    "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
}

# 中文单位按分值排成阶梯：万 千 百 十 元 角 分
_UNIT_LADDER: Final[list[int]] = [1_000_000, 100_000, 10_000, 1_000, 100, 10, 1]
_UNIT_INDEX: Final[dict[str, int]] = {
    "万": 0, "千": 1, "百": 2, "十": 3,
    "元": 4, "块": 4, "圆": 4, "钱": 4,
    "角": 5, "毛": 5, "分": 6,
}
# 「五百元」里的「元」只是量词后缀，不该再加一份
_YUAN_SUFFIXES: Final[frozenset[str]] = frozenset({"元", "块", "圆", "钱"})

_ALL_KEYWORDS: Final[tuple[str, ...]] = (
    "全部", "所有钱", "所有", "全款", "剩下的钱", "有多少转多少",
)

_NOISE_RE: Final[re.Pattern[str]] = re.compile(r"[\s,，。、:：;；!！?？'\"“”‘’￥¥]")
_ARABIC_TOKEN_RE: Final[re.Pattern[str]] = re.compile(r"(\d+(?:\.\d+)?)\s*([万千百]?)")
_ARABIC_FULL_RE: Final[re.Pattern[str]] = re.compile(r"^(\d+(?:\.\d+)?\s*[万千百]?)+[块元圆]*$")
_ARABIC_UNIT_YUAN: Final[dict[str, int]] = {"万": 10_000, "千": 1_000, "百": 100}
_CN_ONLY_RE: Final[re.Pattern[str]] = re.compile(
    r"^[零〇一二两三四五六七八九十百千万块元圆钱角毛分]+$"
)
_SMALL_TAIL_RE: Final[re.Pattern[str]] = re.compile(r"^(.*?)(\d{1,2})(毛|角|分)$")


def yuan_to_fen(yuan: float | int | str) -> int:
    """元 -> 分，返回整数；非法输入抛 ValidationError。"""
    amount_fen = _to_fen_exact(yuan)
    if amount_fen < 0:
        raise ValidationError("金额必须为正")
    if amount_fen == 0:
        raise ValidationError("金额必须大于 0")
    if amount_fen > MAX_SINGLE_FEN:
        raise ValidationError("单笔超过 100 万元")
    return amount_fen


def fen_to_yuan_str(fen: int) -> str:
    """分 -> 带千分位的人民币串：1286050 -> '12,860.50'，小数位恒为两位。"""
    value_fen = int(fen)
    sign = "-" if value_fen < 0 else ""
    yuan, cent = divmod(abs(value_fen), 100)
    return f"{sign}{yuan:,}.{cent:02d}"


def yuan_label(fen: int) -> str:
    """带单位的展示金额：1_000_00 -> '1,000.00 元'。确认卡、决策理由、摘要用它。"""
    return f"{fen_to_yuan_str(fen)} 元"


def yuan_short(fen: int) -> str:
    """口语里的短金额：100000 -> '1000'、150050 -> '1500.5'（不要千分位、不补零）。

    只用于「预算 1000 元」「还差 500 元」这类句子；账务数字一律 fen_to_yuan_str，
    因为那里必须两位小数、必须带千分位，不能两种写法混着出现。
    """
    text = f"{int(fen) / 100:.2f}".rstrip("0").rstrip(".")
    return text or "0"


def parse_amount(text: str, *, strict: bool = False) -> int:
    """自然语言金额 -> 分。

    支持 '500' / '五百' / '500块' / '五百五' / '一千二' / '五百五毛' / '两块五' / '全部'。
    '全部' 一类返回 UNKNOWN_AMOUNT_FEN(-1)；无法识别时默认也返回 -1，
    strict=True 时改抛 ValidationError，供必须区分「没听懂」的调用方使用。
    """
    cleaned = _NOISE_RE.sub("", str(text))
    if not cleaned:
        return _unknown_amount(strict)
    if any(keyword in cleaned for keyword in _ALL_KEYWORDS):
        return UNKNOWN_AMOUNT_FEN
    amount_fen = _parse_arabic(cleaned)
    if amount_fen is None:
        amount_fen = _parse_chinese(cleaned)
    if amount_fen is None or amount_fen <= 0:
        return _unknown_amount(strict)
    return amount_fen


# docs/04 4.3/4.4 与 docs/08 8.5 给了两套函数名，两个都留着，别让下游 import 不到


def _unknown_amount(strict: bool) -> int:
    if strict:
        raise ValidationError("没听懂金额")
    return UNKNOWN_AMOUNT_FEN


def _to_fen_exact(yuan: float | int | str) -> int:
    """元 -> 分，不判断正负与上限（判断交给 yuan_to_fen）。"""
    if isinstance(yuan, bool):
        raise ValidationError("金额格式不对")
    if isinstance(yuan, int):
        return yuan * 100
    if isinstance(yuan, float):
        yuan = str(yuan)
    try:
        decimal_value = Decimal(str(yuan).strip())
    except (InvalidOperation, ValueError) as exc:
        raise ValidationError("金额格式不对") from exc
    fen_value = decimal_value * 100
    if fen_value != fen_value.to_integral_value():
        raise ValidationError("精度超过分")
    return int(fen_value)


def _parse_arabic(text: str) -> int | None:
    """阿拉伯数字金额：'500' / '500.5' / '500块' / '3万' / '500块5毛'。"""
    # 「角/毛/分」跟在数字后面：'500块5毛' = 500 元 + 5 角
    small = _SMALL_TAIL_RE.match(text)
    if small:
        head, digits, unit_char = small.groups()
        base_fen = 0 if not head else _parse_arabic(head)
        if base_fen is None:
            return None
        return base_fen + int(digits) * (1 if unit_char == "分" else 10)
    matches = _ARABIC_TOKEN_RE.findall(text)
    if not matches or not _ARABIC_FULL_RE.match(text):
        return None
    total_fen = 0
    for number_str, unit_char in matches:
        yuan_value = Decimal(number_str) * _ARABIC_UNIT_YUAN.get(unit_char, 1)
        total_fen += _to_fen_exact(yuan_value)
    return total_fen


def _parse_chinese(text: str) -> int | None:
    """纯中文金额：按位累加；末尾裸数字取上一个单位的下一档（口语省略单位）。"""
    if not _CN_ONLY_RE.match(text):
        return None
    total_fen = 0
    pending = 0
    last_unit_index = -1
    for char in text:
        if char in _DIGITS:
            pending = _DIGITS[char]
            continue
        unit_index = _UNIT_INDEX[char]
        if unit_index == 0:
            # 「万」是分段单位：先并入本段再乘万，例如 '两万三千五百'
            total_fen = (total_fen + pending) * _UNIT_LADDER[unit_index]
            pending = 0
            last_unit_index = unit_index
            continue
        if char in _YUAN_SUFFIXES and pending == 0 and total_fen > 0:
            last_unit_index = unit_index
            continue
        total_fen += (pending or 1) * _UNIT_LADDER[unit_index]
        pending = 0
        last_unit_index = unit_index
    if pending:
        # '五百五' 末尾的 5 取十位，'两块五' 末尾的 5 取角
        tail_index = min(last_unit_index + 1, len(_UNIT_LADDER) - 1)
        total_fen += pending * _UNIT_LADDER[tail_index]
    return total_fen


__all__ = [
    "MAX_SINGLE_FEN",
    "UNKNOWN_AMOUNT_FEN",
    "yuan_to_fen",
    "fen_to_yuan_str",
    "yuan_label",
    "yuan_short",
    "parse_amount",
]
