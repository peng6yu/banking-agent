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

from app.money import fen_to_yuan_str
from app.models import Action, Decision
from app.planner import events as planner_events

from app.views.queue import _handoffs_view
from app.views.labels import DENIAL_REASON_LABELS, MAX_LIST, TASK_STATE_LABELS


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
        "ok": True,
        "generated_text": timefmt.maybe_minute_text(stamp),
        "sessions": session_items,
        "handoffs": _handoffs_view(conn, limit=20),
        "denials": [
            {
                "reason": str(row["reason"]),
                "reason_label": DENIAL_REASON_LABELS.get(
                    str(row["reason"]), f"被安全策略拦下（{row['reason']}）"
                ),
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


def _card_view(conn: sqlite3.Connection, row: Any) -> dict[str, Any]:
    """一条确认记录 -> 前端能直接渲染的卡片。

    刷新页面时服务端如果只回确认号，用户就得在看不见金额和收款人的情况下再签一次字。
    所以这里把 action_json 与 dag_nodes.decision_json 重新拼回一张完整的卡：
    字段和第一次推送时是同一套（planner.events.confirmation_frame），不是另写一份文案。
    历史数据解析不出来时 card 为 None，前端退回「只有确认号」的提示，不假装能签字。
    """
    item: dict[str, Any] = {
        "confirmation_id": str(row["id"]),
        "node_id": str(row["node_id"]),
        "status": str(row["status"]),
        "expires_ms": int(row["expires_ms"]),
        "expired": int(row["expires_ms"]) <= now_ms(),
        "card": None,
    }
    try:
        action = Action.model_validate_json(row["action_json"])
        node = conn.execute(
            "SELECT tool, decision_json FROM dag_nodes WHERE run_id = ? AND node_id = ?",
            (row["run_id"], row["node_id"]),
        ).fetchone()
        raw = (node["decision_json"] if node else None) or ""
        if not raw:
            return item
        saved = json.loads(raw)
        # 老数据里只有 level/rule_id/required_factors（当时没存理由），缺的键补空串，
        # 不要用严格模型校验把整张卡判死；理由缺了就退回「看执行记录」的指引。
        decision = Decision(
            level=str(saved.get("level") or "yellow"),
            rule_id=str(saved.get("rule_id") or ""),
            reason_code=str(saved.get("reason_code") or ""),
            reason_text=str(saved.get("reason_text") or ""),
            required_factors=[str(factor) for factor in (saved.get("required_factors") or [])],
            limit_fen=saved.get("limit_fen"),
            spent_today_fen=saved.get("spent_today_fen"),
        )
        remaining = int(row["expires_ms"]) - now_ms()
        item["card"] = planner_events.confirmation_frame(
            conn,
            confirmation_id=str(row["id"]),
            node_id=str(row["node_id"]),
            tool=str(node["tool"]),
            action=action,
            decision=decision,
            expires_in_s=max(0, remaining // 1000),
            status=str(row["status"]),
        )
    except Exception:  # noqa: BLE001 - 历史数据缺字段：宁可只回确认号，也不编卡片
        return item
    return item


def session_detail_view(conn: sqlite3.Connection, session_id: str) -> dict[str, Any]:
    """一个会话的现场：最近一轮的节点、确认卡、当前余额。"""
    head = conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
    if head is None:
        return {"ok": True, "session_id": session_id, "found": False}
    nodes = [
        {
            "node_id": row["node_id"],
            "tool": row["tool"],
            "state": row["state"],
            "state_label": TASK_STATE_LABELS.get(str(row["state"]), str(row["state"])),
            "decision": json.loads(row["decision_json"] or "null"),
            "result": json.loads(row["result_json"] or "null"),
            "ts_ms": int(row["ts_ms"]) if row["ts_ms"] else None,
            "ts_text": timefmt.maybe_minute_text(row["ts_ms"]),
        }
        for row in conn.execute(
            "SELECT * FROM dag_nodes WHERE run_id = (SELECT id FROM dag_runs WHERE session_id = ?"
            " ORDER BY created_ms DESC LIMIT 1) ORDER BY node_id",
            (session_id,),
        )
    ]
    cards = [
        _card_view(conn, row)
        for row in conn.execute(
            "SELECT * FROM confirmations WHERE session_id = ? ORDER BY created_ms", (session_id,)
        )
    ]
    balance = conn.execute(
        "SELECT balance_fen FROM accounts ORDER BY CASE type WHEN 'debit' THEN 0 ELSE 1 END, id LIMIT 1"
    ).fetchone()
    balance_fen = int(balance["balance_fen"]) if balance is not None else None
    return {
        "ok": True,
        "session_id": session_id,
        "found": True,
        "status": str(head["status"]),
        "created_ms": int(head["created_ms"]),
        "created_text": timefmt.maybe_minute_text(int(head["created_ms"])),
        "last_ms": int(head["last_ms"]),
        "last_text": timefmt.maybe_minute_text(int(head["last_ms"])),
        "nodes": nodes,
        "confirmations": cards,
        "balance_fen": balance_fen,
        "balance_yuan": fen_to_yuan_str(balance_fen) if balance_fen is not None else None,
    }


def audit_trail_view(
    conn: sqlite3.Connection, session_id: str, *, limit: int = 100
) -> dict[str, Any]:
    """审计链：链还完不完整 + 这条会话每一步为什么这么判。

    total 是这条会话的全部事件数，不受 limit 影响：页面上的「共 N 条」必须是真的总数。
    """
    rows = conn.execute(
        "SELECT seq, ts_ms, event_type, payload_json FROM audit_log WHERE session_id = ?"
        " ORDER BY seq LIMIT ?",
        (session_id, limit),
    ).fetchall()
    events = [
        {
            "seq": int(row["seq"]),
            "ts_ms": int(row["ts_ms"]),
            "ts_text": timefmt.maybe_minute_text(int(row["ts_ms"])),
            "event_type": str(row["event_type"]),
            "payload": json.loads(row["payload_json"]),
        }
        for row in rows
    ]
    total_row = conn.execute(
        "SELECT COUNT(*) AS n FROM audit_log WHERE session_id = ?", (session_id,)
    ).fetchone()
    return {
        "ok": True,
        "session_id": session_id,
        "count": len(events),
        "total": int(total_row["n"]) if total_row is not None else 0,
        "events": events,
        "chain_ok": audit.verify_chain(conn),
        "chain_head": audit.chain_head(conn),
    }
