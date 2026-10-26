"""提醒：页面表单和聊天里的自然语言落的是同一条记录（都走 assist.create_reminder）。"""

from __future__ import annotations

from typing import Any

import app.proactive as proactive
from app.planner import assist
from app.use_cases.base import write_case


@write_case
def reminder_create(
    conn: Any,
    *,
    text: str,
    due_ms: int | None = None,
    repeat: str = "none",
    hour: int = 9,
    minute: int = 0,
    day: int | None = None,
    weekday: int | None = None,
) -> dict[str, Any]:
    """建一条提醒：提醒必须挂在一个资金时间点上，这条口径由 app/scope.py 保证。"""
    created = assist.create_reminder(
        conn,
        text=text,
        user_id=proactive.default_user_id(conn),
        due_ms=due_ms,
        repeat=repeat,
        hour=hour,
        minute=minute,
        day=day,
        weekday=weekday,
    )
    return {
        "plan": created["plan"],
        "due_ms": created["due_ms"],
        "due_text": created["due_text"],
        "repeat": created["repeat"],
        "repeat_label": created["repeat_label"],
    }


__all__ = ["reminder_create"]
