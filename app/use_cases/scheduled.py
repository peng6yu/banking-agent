"""预约转账的强验证两段：签挑战、交证明并立刻尝试执行（执行前仍重新裁决）。"""

from __future__ import annotations

from typing import Any

import app.tools.scheduled as scheduled
from app.use_cases.base import write_case


@write_case
def scheduled_issue_challenge(conn: Any, scheduled_id: str, *, factor: str = "face") -> dict[str, Any]:
    """给一笔到点复判变红的预约签发一次强验证挑战（绑定 sched:<id>，用一次就作废）。"""
    return scheduled.challenge_scheduled(conn, scheduled_id, factor)


@write_case
def scheduled_submit_verify(
    conn: Any, scheduled_id: str, *, proofs: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    """补一次强验证并立即尝试执行这笔预约。"""
    return scheduled.verify_and_execute(conn, scheduled_id, proofs=proofs)


__all__ = ["scheduled_issue_challenge", "scheduled_submit_verify"]
