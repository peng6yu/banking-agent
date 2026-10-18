"""熔断器：同一会话短时间被拒太多次就先别再问模型了。

对应文档：docs/05_安全层实现规格.md 5.7。

状态放在 denials 表里而不是内存：进程重启后仍然记得这个会话已经被拒过，
这正是「防住」的语义；重试窗口 5 分钟，3 次拒绝打开 60 秒，之后半开放行一次。
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Final

from app import audit
from app.clock import BreakerOpen, now_ms
from app.ids import new_id
from app.log import log

WINDOW_MS: Final[int] = 300_000
THRESHOLD: Final[int] = 3
OPEN_MS: Final[int] = 60_000
DEFAULT_REASON: Final[str] = "POLICY_DENIED"


def _recent_denials_ms(conn: sqlite3.Connection, session_id: str, stamp_ms: int) -> list[int]:
    rows = conn.execute(
        "SELECT ts_ms FROM denials WHERE session_id = ? AND ts_ms > ? ORDER BY ts_ms",
        (session_id, stamp_ms - WINDOW_MS),
    ).fetchall()
    return [int(row["ts_ms"]) for row in rows]


def denial_count(conn: sqlite3.Connection, session_id: str) -> int:
    """窗口内的拒绝次数。"""
    return len(_recent_denials_ms(conn, session_id, now_ms()))


def is_open(conn: sqlite3.Connection, session_id: str) -> bool:
    """熔断是否处于打开状态。打开窗口以窗口内最后一次拒绝起算 60 秒。"""
    stamp_ms = now_ms()
    stamps = _recent_denials_ms(conn, session_id, stamp_ms)
    if len(stamps) < THRESHOLD:
        return False
    return stamp_ms < stamps[-1] + OPEN_MS


def retry_after_ms(conn: sqlite3.Connection, session_id: str) -> int:
    """还剩多少毫秒进入半开；不在熔断中返回 0。"""
    stamp_ms = now_ms()
    stamps = _recent_denials_ms(conn, session_id, stamp_ms)
    if len(stamps) < THRESHOLD:
        return 0
    return max(0, stamps[-1] + OPEN_MS - stamp_ms)


def ensure_available(conn: sqlite3.Connection, session_id: str) -> None:
    """熔断中就直接拒，不调模型——省钱也避免被反复试探。"""
    if not is_open(conn, session_id):
        return
    remaining_ms = retry_after_ms(conn, session_id)
    log.info("breaker.blocked", session_id=session_id, retry_after_ms=remaining_ms)
    raise BreakerOpen(reason="BREAKER_OPEN", retry_after_ms=remaining_ms)


def record_denial(
    conn: sqlite3.Connection,
    session_id: str,
    *,
    reason: str = DEFAULT_REASON,
    action: Any = None,
) -> int:
    """记一次拒绝；跨过阈值的那一刻写 breaker.open 审计（docs/04）。"""
    was_open = is_open(conn, session_id)
    stamp_ms = now_ms()
    payload = action.model_dump(mode="json") if hasattr(action, "model_dump") else action
    conn.execute(
        "INSERT INTO denials (id, session_id, reason, action_json, ts_ms) VALUES (?, ?, ?, ?, ?)",
        (new_id("dn"), session_id, reason, json.dumps(payload, ensure_ascii=False, sort_keys=True) if payload else "{}", stamp_ms),
    )
    count = denial_count(conn, session_id)
    if not was_open and count >= THRESHOLD:
        audit.append(
            conn,
            "breaker.open",
            {"session_id": session_id, "denial_count": count},
            session_id=session_id,
        )
        log.info("breaker.opened", session_id=session_id, denial_count=count)
    return count


def reset(conn: sqlite3.Connection, session_id: str) -> int:
    """清掉这个会话的拒绝记录：半开试探成功就调它，等于熔断器复位。"""
    cursor = conn.execute("DELETE FROM denials WHERE session_id = ?", (session_id,))
    log.info("breaker.reset", session_id=session_id, cleared=cursor.rowcount)
    return cursor.rowcount


__all__ = [
    "DEFAULT_REASON",
    "OPEN_MS",
    "THRESHOLD",
    "WINDOW_MS",
    "denial_count",
    "ensure_available",
    "is_open",
    "record_denial",
    "reset",
    "retry_after_ms",
]
