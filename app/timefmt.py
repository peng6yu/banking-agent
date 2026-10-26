"""毫秒时间戳 → 人看的本地时间串（唯一实现）。

铁律：库里只存毫秒整数，格式化只发生在出口；时区一律取本机本地时间（演示环境
固定 Asia/Shanghai，和种子数据、审计时间同一口径）。这里不查库、不读配置、
不抛业务异常，所以任何一层都可以 import，也不会有循环依赖。

改造前同一个 `_time_text` 在 7 个模块里各写了一遍（生日/订阅/冲突/业务视图/
预约/账单/计划），其中订阅那份是「只要日期」、业务视图那份「None 进 None 出」、
助手那份是「10-26 08:00」——三种口径混在一起，改一处就漏一处。现在只有这里一份。
"""

from __future__ import annotations

import time
from typing import Any, Final

#: 2026-10-25
DATE_FMT: Final[str] = "%Y-%m-%d"
#: 2026-10-25 21:03
MINUTE_FMT: Final[str] = "%Y-%m-%d %H:%M"
#: 10-25 21:03（同名对象靠时间区分时用，省掉年份）
STAMP_FMT: Final[str] = "%m-%d %H:%M"


def date_text(ms: int) -> str:
    """只要日期：订阅的「首次/最近一次」、按月分组这类场景。"""
    return time.strftime(DATE_FMT, time.localtime(int(ms) / 1000))


def minute_text(ms: int) -> str:
    """日期 + 分钟，绝大多数「发生时间/到期时间」用这个。"""
    return time.strftime(MINUTE_FMT, time.localtime(int(ms) / 1000))


def maybe_minute_text(ms: int | None) -> str | None:
    """没有时间就给 None（不是空串）：视图里「没发生过」和「发生了但没记时间」要能分开。"""
    if not ms:
        return None
    return minute_text(int(ms))


def stamp_text(ms: Any) -> str:
    """短戳「10-26 08:00」，脏输入（None/空串/非数字/0）给空串，调用方不必再包 try。"""
    try:
        value = int(ms or 0)
    except (TypeError, ValueError):
        return ""
    if value <= 0:
        return ""
    return time.strftime(STAMP_FMT, time.localtime(value / 1000))


__all__ = ["DATE_FMT", "MINUTE_FMT", "STAMP_FMT", "date_text", "maybe_minute_text", "minute_text", "stamp_text"]
