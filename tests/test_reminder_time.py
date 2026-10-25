"""提醒的下一次触发（app/reminder_time.py）：日/周/月三种重复都按本地日历算。

为什么值得单独钉：同一段「下一次什么时候」既被建提醒用到，也被计划推进用到。
两边各写一份，跨月（31 号）、跨年、月末 clamp 必然分叉，用户就会看到
「每周三的提醒在周四响」这类无法解释的 bug。这里只测纯函数，不碰数据库。
"""

from __future__ import annotations

from datetime import datetime

import pytest

from app import reminder_time


def _ms(*args: int) -> int:
    """本地时间 -> 毫秒，和被测函数同一套时区口径。"""

    return int(datetime(*args).timestamp() * 1000)


def _dt(ms: int) -> datetime:
    return datetime.fromtimestamp(int(ms) / 1000)


def test_repeats_are_exactly_the_three_supported_kinds() -> None:
    assert reminder_time.REPEATS == frozenset({"daily", "weekly", "monthly"})


@pytest.mark.parametrize(
    ("repeat", "hour", "minute", "day", "weekday", "expected"),
    [
        ("daily", 9, 0, None, None, "每天 09:00"),
        ("weekly", 8, 30, None, 2, "每周三 08:30"),
        ("monthly", 9, 0, 1, None, "每月 1 号 09:00"),
        ("none", 7, 5, None, None, "07:05"),
    ],
)
def test_label_is_human_readable(repeat, hour, minute, day, weekday, expected) -> None:
    assert reminder_time.label(repeat, hour, minute, day, weekday) == expected


def test_daily_rolls_to_tomorrow_when_today_time_already_passed() -> None:
    now = _ms(2026, 10, 25, 10, 0)
    # 今天的 09:00 已经过去 -> 下一次是明天 09:00，不能给出一个过去的时间
    assert _dt(reminder_time.next_after(now, repeat="daily", hour=9, minute=0)) == datetime(
        2026, 10, 26, 9, 0
    )
    # 今天的 18:00 还没到 -> 就是今天
    assert _dt(reminder_time.next_after(now, repeat="daily", hour=18, minute=0)) == datetime(
        2026, 10, 25, 18, 0
    )


def test_weekly_picks_the_requested_weekday_strictly_after_now() -> None:
    now = _ms(2026, 10, 25, 10, 0)  # 2026-10-25 是周日
    assert _dt(reminder_time.next_after(now, repeat="weekly", hour=8, minute=0, weekday=0)) == (
        datetime(2026, 10, 26, 8, 0)  # 下一个周一
    )
    # 正好是同一个星期几、但今天这个点已经过去，也要顺延到下周，而不是返回过去
    assert _dt(reminder_time.next_after(now, repeat="weekly", hour=9, minute=0, weekday=6)) == (
        datetime(2026, 11, 1, 9, 0)
    )


def test_monthly_clamps_to_last_day_of_short_month() -> None:
    """31 号的月提醒遇到 2 月必须落到 28/29 号，不能抛异常也不能跳到 3 月。"""

    now = _ms(2026, 1, 31, 12, 0)
    assert _dt(reminder_time.next_after(now, repeat="monthly", hour=9, minute=0, day=31)) == datetime(
        2026, 2, 28, 9, 0
    )


def test_next_from_payload_reads_the_same_fields_the_step_stores() -> None:
    now = _ms(2026, 10, 25, 10, 0)
    payload = {"text": "喝水", "repeat": "daily", "hour": 8, "minute": 15}
    assert _dt(reminder_time.next_from_payload(payload, now)) == datetime(2026, 10, 26, 8, 15)


def test_next_from_payload_returns_none_for_one_shot() -> None:
    # 一次性提醒响过就结束：返回 None 是「不要再排下一次」的信号，不是错误
    assert reminder_time.next_from_payload({"text": "开会", "repeat": "none"}, 0) is None


def test_time_text_is_minute_precision() -> None:
    assert reminder_time.time_text(_ms(2026, 10, 26, 9, 5)) == "2026-10-26 09:05"
