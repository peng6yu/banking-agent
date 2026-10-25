"""提醒时间的下一次触发：日/周/月三种重复，全部按本地日历算。

为什么单独成文件：建提醒（app/planner/assist.py）和节拍器推进（app/plans.py）
都要算「下一次是什么时候」。两处各写一份，跨月、跨年、月末 31 号必然对不上。
"""

from __future__ import annotations

import calendar
import time
from datetime import date, datetime, timedelta
from typing import Any, Final

REPEATS: Final[frozenset[str]] = frozenset({"daily", "weekly", "monthly"})
WEEKDAYS: Final[dict[str, int]] = {
    "一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6,
}


def label(repeat: str, hour: int, minute: int, day: int | None, weekday: int | None) -> str:
    """人话：每天 09:00 / 每周三 09:00 / 每月 1 号 09:00。"""
    clock = f"{int(hour):02d}:{int(minute):02d}"
    if repeat == "daily":
        return f"每天 {clock}"
    if repeat == "weekly":
        names = "一二三四五六日"
        return f"每周{names[int(weekday or 0) % 7]} {clock}"
    if repeat == "monthly":
        return f"每月 {int(day or 1)} 号 {clock}"
    return clock


def _at(day: date, hour: int, minute: int) -> int:
    return int(time.mktime((day.year, day.month, day.day, int(hour), int(minute), 0, 0, 0, -1)) * 1000)


def _clamp_day(year: int, month: int, day: int) -> date:
    last = calendar.monthrange(year, month)[1]
    return date(year, month, min(max(1, int(day)), last))


def next_after(
    now_ms: int,
    *,
    repeat: str,
    hour: int = 9,
    minute: int = 0,
    day: int | None = None,
    weekday: int | None = None,
) -> int:
    """严格晚于 now_ms 的下一次触发（毫秒）。"""
    now = datetime.fromtimestamp(int(now_ms) / 1000)
    today = now.date()
    if repeat == "daily":
        candidate = _at(today, hour, minute)
        if candidate <= now_ms:
            candidate = _at(today + timedelta(days=1), hour, minute)
        return candidate
    if repeat == "weekly":
        target = int(weekday if weekday is not None else today.weekday()) % 7
        delta = (target - today.weekday()) % 7
        candidate = _at(today + timedelta(days=delta), hour, minute)
        if candidate <= now_ms:
            candidate = _at(today + timedelta(days=delta + 7), hour, minute)
        return candidate
    if repeat == "monthly":
        target_day = int(day or today.day)
        candidate = _at(_clamp_day(today.year, today.month, target_day), hour, minute)
        if candidate <= now_ms:
            year, month = (today.year + 1, 1) if today.month == 12 else (today.year, today.month + 1)
            candidate = _at(_clamp_day(year, month, target_day), hour, minute)
        return candidate
    # 不重复：就按给定的这一次
    return _at(today, hour, minute)


def next_from_payload(payload: dict[str, Any], now_ms: int) -> int | None:
    """从计划步骤的 payload 算下一次；没有 repeat 返回 None（一次性）。"""
    repeat = str(payload.get("repeat") or "").strip().lower()
    if repeat not in REPEATS:
        return None
    return next_after(
        now_ms,
        repeat=repeat,
        hour=int(payload.get("hour") or 9),
        minute=int(payload.get("minute") or 0),
        day=int(payload["day"]) if payload.get("day") is not None else None,
        weekday=int(payload["weekday"]) if payload.get("weekday") is not None else None,
    )


def time_text(ms: int | None) -> str:
    if ms is None:
        return "—"
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(int(ms) / 1000))


__all__ = ["REPEATS", "WEEKDAYS", "label", "next_after", "next_from_payload", "time_text"]
