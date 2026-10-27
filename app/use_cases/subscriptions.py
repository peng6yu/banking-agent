"""疑似订阅的定性：只改一行结论 + 写审计，不动钱、不替用户去商户退订。"""

from __future__ import annotations

from typing import Any

import app.proactive as proactive
import app.subscriptions as subscriptions
from app.use_cases.base import write_case


@write_case
def verdict_set(conn: Any, *, merchant: str, verdict: str, note: str = "") -> dict[str, Any]:
    """给一条疑似订阅定性：两种结论都算处理完，都会让这条从待办里消失。"""
    return {
        "verdict": subscriptions.record_verdict(
            conn,
            user_id=proactive.default_user_id(conn),
            merchant=merchant,
            verdict=verdict,
            note=note,
        )
    }


@write_case
def verdict_clear(conn: Any, *, merchant: str) -> dict[str, Any]:
    """撤掉定性，让它重新按流水推断（仍满足周期性就会回到待办）。"""
    return subscriptions.clear_verdict(
        conn, user_id=proactive.default_user_id(conn), merchant=merchant
    )


__all__ = ["verdict_clear", "verdict_set"]
