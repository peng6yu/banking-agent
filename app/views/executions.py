"""执行记录与安全：最近的 run、节点、审计与接管。

只读：本模块不写库、不发令牌、不调执行器。
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from app import audit
from app import timefmt
from app.clock import now_ms

from app.views.queue import _handoffs_view
from app.views.labels import MAX_LIST, TASK_STATE_LABELS


def executions_view(
    conn: sqlite3.Connection, *, limit: int = 10, session_id: str | None = None
) -> dict[str, Any]:
    """执行记录与安全中心：业务状态在上，技术细节按需展开。"""
    stamp = now_ms()
    limit = max(1, min(int(limit), MAX_LIST))
    if session_id:
        sessions = conn.execute(
            "SELECT * FROM sessions WHERE id = ?", (session_id,)
        ).fetchall()
    else:
        sessions = conn.execute(
            "SELECT * FROM sessions ORDER BY last_ms DESC LIMIT ?", (limit,)
        ).fetchall()
    session_items = [_session_view(conn, row) for row in sessions]
    denials = conn.execute(
        "SELECT * FROM denials ORDER BY ts_ms DESC LIMIT ?", (limit,)
    ).fetchall()
    chain_ok = audit.verify_chain(conn)
    return {
        "generated_ms": stamp,
        "generated_text": timefmt.maybe_minute_text(stamp),
        "sessions": session_items,
        "handoffs": _handoffs_view(conn, limit=20),
        "denials": [
            {
                "reason": str(row["reason"]),
                "ts_ms": int(row["ts_ms"]),
                "ts_text": timefmt.maybe_minute_text(int(row["ts_ms"])),
                "session_id": str(row["session_id"]) if row["session_id"] else None,
            }
            for row in denials
        ],
        "audit": {
            "chain_ok": chain_ok,
            "head": audit.chain_head(conn),
            "count": audit.count_events(conn),
            "append_only": "审计表由触发器禁止 UPDATE/DELETE。",
        },
        "state_labels": TASK_STATE_LABELS,
        "empty_reason": None if session_items else "还没有执行记录。",
    }


def _node_view(node: sqlite3.Row, run_id: str) -> dict[str, Any]:
    """一个 DAG 节点的技术详情：意图、裁决、令牌要求、结果。"""
    decision: dict[str, Any] = {}
    try:
        decision = json.loads(str(node["decision_json"]) or "{}") or {}
    except (TypeError, ValueError):
        decision = {}
    result: dict[str, Any] = {}
    try:
        result = json.loads(str(node["result_json"]) or "{}") or {}
    except (TypeError, ValueError):
        result = {}
    return {
        "run_id": run_id,
        "node_id": str(node["node_id"]),
        "tool": str(node["tool"]),
        "state": str(node["state"]),
        "state_label": TASK_STATE_LABELS.get(str(node["state"]), str(node["state"])),
        "level": decision.get("level"),
        "rule_id": decision.get("rule_id"),
        "reason_text": decision.get("reason_text"),
        "required_factors": decision.get("required_factors") or [],
        "result_ok": result.get("ok"),
        "summary": result.get("summary") or result.get("message"),
        "ts_ms": int(node["ts_ms"]) if node["ts_ms"] else None,
        "ts_text": timefmt.maybe_minute_text(node["ts_ms"]),
    }


def _session_view(conn: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
    """一个会话的业务面：标题、状态、它下面的 run / 节点 / 确认卡。"""
    sid = str(row["id"])
    runs = conn.execute(
        "SELECT * FROM dag_runs WHERE session_id = ? ORDER BY created_ms DESC", (sid,)
    ).fetchall()
    nodes: list[dict[str, Any]] = []
    for run in runs:
        run_id = str(run["id"])
        node_rows = conn.execute(
            "SELECT * FROM dag_nodes WHERE run_id = ? ORDER BY id", (run_id,)
        ).fetchall()
        for node in node_rows:
            nodes.append(_node_view(node, run_id))
    confs = conn.execute(
        "SELECT id, node_id, status, created_ms, expires_ms, resolved_ms"
        " FROM confirmations WHERE session_id = ? ORDER BY created_ms DESC",
        (sid,),
    ).fetchall()
    return {
        "session_id": sid,
        "title": str(row["title"] or ""),
        "status": str(row["status"]),
        "status_label": {
            "active": "进行中", "done": "已完成", "cancelled": "已取消",
        }.get(str(row["status"]), str(row["status"])),
        "created_ms": int(row["created_ms"]),
        "created_text": timefmt.maybe_minute_text(int(row["created_ms"])),
        "last_ms": int(row["last_ms"]),
        "last_text": timefmt.maybe_minute_text(int(row["last_ms"])),
        "runs": [
            {
                "run_id": str(run["id"]),
                "status": str(run["status"]),
                "state_label": TASK_STATE_LABELS.get(str(run["status"]), str(run["status"])),
                "created_ms": int(run["created_ms"]),
                "updated_ms": int(run["updated_ms"]),
            }
            for run in runs
        ],
        "nodes": nodes,
        "confirmations": [
            {
                "confirmation_id": str(c["id"]),
                "node_id": str(c["node_id"]),
                "status": str(c["status"]),
                "status_label": {
                    "pending": "等待中", "approved": "已批准",
                    "rejected": "已拒绝", "expired": "已过期",
                }.get(str(c["status"]), str(c["status"])),
                "created_text": timefmt.maybe_minute_text(int(c["created_ms"])),
                "expires_text": timefmt.maybe_minute_text(int(c["expires_ms"])),
                "resolved_text": timefmt.maybe_minute_text(int(c["resolved_ms"])),
            }
            for c in confs
        ],
    }
