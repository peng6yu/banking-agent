"""主动财务管理的两个写动作：手动跑一次监测、处理一条提醒。

两者都只写提醒与运行记录，一分钱都不动。
"""

from __future__ import annotations

from typing import Any

import app.proactive as proactive
from app.clock import NotFound
from app.use_cases.base import write_case


@write_case
def monitor_run(conn: Any, *, goal_id: str | None = None) -> dict[str, Any]:
    """手动触发一次监测（可重复）：只写提醒与运行记录。"""
    run = proactive.run_monitor(
        conn, user_id=proactive.default_user_id(conn), goal_id=goal_id
    )
    return {"ok": run["status"] == "ok", **run}


@write_case
def alert_resolve(conn: Any, alert_id: str, *, action: str) -> dict[str, Any]:
    """处理提醒：ignore=先放着，resolve=已解决；重复处理幂等，写审计。"""
    resolved = proactive.resolve_alert(
        conn, alert_id, action, user_id=proactive.default_user_id(conn)
    )
    if resolved is None:
        raise NotFound()
    return {
        "alert": resolved["alert"],
        "idempotent": bool(resolved.get("idempotent", False)),
        "audit_seq": resolved["audit_seq"],
    }


__all__ = ["alert_resolve", "monitor_run"]
