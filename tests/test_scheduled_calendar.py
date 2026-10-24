"""按月重复的日期推算：日历月 + 月末钳制，不是固定 30 天。

以前 _next_month_ms 是 due_ms + 30 天，1 月 31 日的月付会漂到 3 月 2 日、
把 2 月整个跳过。这里钉死三件事：按日历月走、日期不存在钳到月末、时分秒保留。
"""

from __future__ import annotations

import datetime

from app.tools import scheduled


def _ms(year: int, month: int, day: int, hour: int = 10, minute: int = 30) -> int:
    return int(datetime.datetime(year, month, day, hour, minute).timestamp() * 1000)


def _local(ms: int) -> datetime.datetime:
    return datetime.datetime.fromtimestamp(ms / 1000)


def test_january_31st_lands_on_february_28th() -> None:
    assert _local(scheduled._next_month_ms(_ms(2027, 1, 31))) == datetime.datetime(2027, 2, 28, 10, 30)


def test_leap_year_lands_on_february_29th() -> None:
    assert _local(scheduled._next_month_ms(_ms(2028, 1, 31))) == datetime.datetime(2028, 2, 29, 10, 30)


def test_march_31st_lands_on_april_30th() -> None:
    assert _local(scheduled._next_month_ms(_ms(2027, 3, 31))) == datetime.datetime(2027, 4, 30, 10, 30)


def test_a_normal_day_keeps_its_day_and_time() -> None:
    assert _local(scheduled._next_month_ms(_ms(2027, 3, 15, 8, 5))) == datetime.datetime(2027, 4, 15, 8, 5)


def test_december_rolls_into_next_year() -> None:
    assert _local(scheduled._next_month_ms(_ms(2027, 12, 31))) == datetime.datetime(2028, 1, 31, 10, 30)


def test_it_is_not_thirty_days() -> None:
    """1 月 31 日到 2 月 28 日是 28 天；固定 +30 天会变成 3 月 2 日，这条就是回归钉子。"""
    start, nxt = _ms(2027, 1, 31), scheduled._next_month_ms(_ms(2027, 1, 31))
    days = round((nxt - start) / 86_400_000, 3)
    assert days == 28.0, f"实际 {days} 天"
    assert _local(nxt).month == 2
