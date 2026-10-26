"""视图用的读数积木·待办与队列（第 5 层）：待确认、接管单、预约、提醒、监测。

每块都只读库里的既有事实：待确认卡读的是 confirmations + dag_nodes 上已经写下的
裁决，接管单读的是 handoffs 行，预约读的是 scheduled_transfers 行。**这里不产生任何
新的裁决**，也不改状态；用户点按钮之后的写操作一律走 app/api/routes.py → use_cases。
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Final

from app import proactive
from app import handoffs
from app import timefmt
from app.planner import ticker
from app.tools import scheduled as scheduled_mod
from app.money import fen_to_yuan_str
from app.clock import now_ms

from app.views.labels import (
    HANDOFF_REASON_LABELS,
    HANDOFF_STATUS_LABELS,
    MAX_LIST,
    SCHEDULED_STATE_LABELS,
)


def _savings_goals(
    conn: sqlite3.Connection, user_id: str, *, limit: int = 5, with_plan: bool = True
) -> list[dict[str, Any]]:
    """储蓄目标 + 可解释的计划（每期要存多少、缺口在哪、凭什么这么算）。"""
    rows = conn.execute(
        "SELECT * FROM savings_goals WHERE user_id = ? AND status != 'cancelled'"
        " ORDER BY created_ms DESC LIMIT ?",
        (user_id, int(limit)),
    ).fetchall()
    if not rows:
        return []
    transactions: list[sqlite3.Row] = []
    if with_plan:
        # transactions 表没有 user_id 列，只能经 accounts 反查；
        # 顺序按时间倒序再截断，算的是"最近一年左右的真实节奏"。
        transactions = conn.execute(
            "SELECT t.* FROM transactions t JOIN accounts a ON a.id = t.account_id"
            " WHERE a.user_id = ? ORDER BY t.ts_ms DESC LIMIT 500",
            (user_id,),
        ).fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        rule_rows = proactive.rules_for_goal(conn, str(row["id"]))
        goal = {
            "goal_id": str(row["id"]),
            "name": str(row["name"]),
            "target_fen": int(row["target_fen"]),
            "target_yuan": fen_to_yuan_str(int(row["target_fen"])),
            "target_date": str(row["target_date"]),
            "saved_fen": int(row["saved_fen"]),
            "saved_yuan": fen_to_yuan_str(int(row["saved_fen"])),
            "status": str(row["status"]),
            "created_ms": int(row["created_ms"]),
            "updated_ms": int(row["updated_ms"]),
            # 监测开关由 monitor_rules.enabled 决定：后台节拍器只唤醒开了开关的目标，
            # 所以界面必须把这个状态如实摆出来，不能让用户以为"存了就会自动盯"。
            "monitor_rules": [
                {
                    "rule_id": str(rule["id"]),
                    "kind": str(rule["kind"]),
                    # 分成两列下发：前端画「预算 3000 元」还是「容差 5 个百分点」，
                    # 不用再按 kind 猜单位。
                    "threshold_fen": rule["threshold_fen"],
                    "threshold_pp": rule["threshold_pp"],
                    "period": str(rule["period"]),
                    "enabled": bool(int(rule["enabled"])),
                }
                for rule in rule_rows
            ],
            "monitor_enabled": any(bool(int(rule["enabled"])) for rule in rule_rows),
        }
        if with_plan:
            try:
                goal["plan"] = proactive.compute_savings_plan(
                    target_fen=goal["target_fen"],
                    target_date=goal["target_date"],
                    saved_fen=goal["saved_fen"],
                    transactions=transactions,
                )
            except Exception as exc:  # noqa: BLE001 - 展示层不该因为一条算不出来就整页 500
                goal["plan"] = None
                goal["plan_error"] = f"这个目标暂时算不出计划（{type(exc).__name__}）。"
        out.append(goal)
    return out


def _pending_confirmations(
    conn: sqlite3.Connection, *, limit: int = 10, session_id: str | None = None
) -> dict[str, Any]:
    """待处理的授权。从 confirmations 表读——刷新页面不会丢，这是持久化过的。"""
    stamp = now_ms()
    sql = (
        "SELECT c.id AS confirmation_id, c.session_id, c.run_id, c.node_id,"
        " c.action_json, c.status, c.created_ms, c.expires_ms,"
        " r.status AS run_status, n.decision_json"
        " FROM confirmations c LEFT JOIN dag_runs r ON r.id = c.run_id"
        " LEFT JOIN dag_nodes n ON n.run_id = c.run_id AND n.node_id = c.node_id"
        " WHERE c.status = 'pending'"
    )
    args: list[Any] = []
    if session_id:
        sql += " AND c.session_id = ?"
        args.append(session_id)
    sql += " ORDER BY c.created_ms DESC LIMIT ?"
    args.append(int(limit))
    rows = conn.execute(sql, args).fetchall()
    items: list[dict[str, Any]] = []
    for row in rows:
        action: dict[str, Any] = {}
        try:
            action = json.loads(str(row["action_json"]) or "{}")
        except (TypeError, ValueError):
            action = {}
        params = action.get("params") if isinstance(action.get("params"), dict) else {}
        try:
            decision = json.loads(str(row["decision_json"] or "{}"))
        except (TypeError, ValueError):
            decision = {}
        required_factors = [str(item) for item in (decision.get("required_factors") or [])]
        expires_ms = int(row["expires_ms"])
        expired = expires_ms <= stamp
        items.append(
            {
                "confirmation_id": str(row["confirmation_id"]),
                "session_id": str(row["session_id"]),
                "run_id": str(row["run_id"]) if row["run_id"] else None,
                "node_id": str(row["node_id"]),
                "tool": str(action.get("tool") or ""),
                "amount_fen": action.get("amount_fen"),
                "amount_yuan": (
                    fen_to_yuan_str(int(action["amount_fen"]))
                    if action.get("amount_fen") is not None
                    else None
                ),
                "payee_name": params.get("payee_name"),
                "created_ms": int(row["created_ms"]),
                "created_text": timefmt.maybe_minute_text(int(row["created_ms"])),
                "expires_ms": expires_ms,
                "expires_in_s": max(0, (expires_ms - stamp) // 1000),
                "expired": expired,
                # 进程重启后，内存里的执行器已经没了：这时卡片还在库里，
                # 但它已经不可能被"点一下"推进，必须如实说是过期/失效。
                "stale": expired or str(row["run_status"] or "") not in ("running", "waiting"),
                "level": str(decision.get("level") or "yellow"),
                "required_factors": required_factors,
                "can_confirm_here": not expired
                and str(row["run_status"] or "") in ("running", "waiting")
                and not required_factors,
                # 需要人脸/短信的那一档也能在页面上办完：签发挑战 -> 填码 -> 批准，
                # 只是多一步验证。以前这里只给「去完成验证」跳回聊天，等于没入口。
                "can_verify_here": not expired
                and str(row["run_status"] or "") in ("running", "waiting")
                and bool(required_factors),
                "state_label": "已过期，需要重新发起" if expired else "等待确认",
            }
        )
    return {
        "items": items,
        "count": len(items),
        "source": "confirmations 表（服务端持久化，刷新不丢）",
        "empty_reason": None if items else "现在没有等你确认的操作。",
    }



def _handoffs_view(conn: sqlite3.Connection, *, limit: int = 10) -> dict[str, Any]:
    """人工接管队列：把「卡住的一轮」和「能做的动作」一起交给页面。

    以前后端有 /api/handoffs，页面上却没有任何入口——用户只能看着一轮任务
    停在 manual，没有任何按钮可按。动作清单在服务端算：哪个状态允许哪个动作
    是状态机的知识，不该让前端再猜一遍。
    """
    try:
        rows = handoffs.list_handoffs(conn, limit=max(1, min(int(limit), 20)))
        total = _handoff_total(conn)
    except Exception:  # noqa: BLE001 - 队列读不到不能让整个首页返回 500
        return {
            "ok": True,
            "items": [],
            "count": 0,
            "total": 0,
            "empty_reason": "接管队列暂时读不到。",
        }
    items: list[dict[str, Any]] = []
    for row in rows:
        status = str(row.get("status") or "")
        reason = str(row.get("reason") or "")
        needs_user = reason in handoffs.USER_DECISION_REASONS
        active = status in handoffs.ACTIVE_STATUSES
        actions: list[dict[str, Any]] = []
        if status == "pending":
            actions.append({"key": "claim", "label": "领取这张单", "needs_note": False})
        if active:
            actions.append({"key": "need-info", "label": "要求补充信息", "needs_note": True})
            actions.append({"key": "reject", "label": "拒绝（不动钱）", "needs_note": True})
            actions.append({"key": "cancel", "label": "取消这一轮", "needs_note": True})
        if status in ("claimed", "need_info") and not needs_user:
            actions.append({"key": "resume", "label": "恢复执行", "needs_note": True})
        failure = row.get("failure") if isinstance(row.get("failure"), dict) else {}
        failure_text = str(failure.get("user_text") or failure.get("message") or "")[:200]
        created_ms = int(row.get("created_ms") or 0)
        items.append(
            {
                "handoff_id": str(row.get("id") or ""),
                "run_id": str(row.get("run_id") or ""),
                "session_id": str(row.get("session_id") or ""),
                "status": status,
                "status_label": HANDOFF_STATUS_LABELS.get(status, status),
                "reason": reason,
                "reason_label": HANDOFF_REASON_LABELS.get(reason, reason or "需要人工看一眼"),
                "request_text": str(row.get("request_text") or "")[:200],
                "failure_text": failure_text,
                "operator": str(row.get("operator") or ""),
                "note": str(row.get("note") or "")[:200],
                "needs_user": needs_user,
                "needs_user_note": (
                    "这单缺的是你本人的决定（选哪个收款人），操作员不能替你选。"
                    if needs_user
                    else ""
                ),
                "created_ms": created_ms,
                "created_text": timefmt.maybe_minute_text(created_ms) if created_ms else "",
                "actions": actions,
            }
        )
    return {
        "ok": True,
        "items": items,
        "count": len(items),
        "total": total,
        "empty_reason": None if items else "现在没有需要人工接管的单。",
        "source": "handoffs 表（收款人歧义或找不到时会自动挂单）",
    }

def _scheduled_tasks(
    conn: sqlite3.Connection, user_id: str, *, limit: int = MAX_LIST
) -> dict[str, Any]:
    """预约任务。已安排 / 已执行 / 已取消 / 已失败 四种状态严格分开。"""
    rows = conn.execute(
        "SELECT s.*, p.name AS payee_name, a.tail AS account_tail"
        " FROM scheduled_transfers s"
        " LEFT JOIN payees p ON p.id = s.payee_id"
        " LEFT JOIN accounts a ON a.id = s.account_id"
        " WHERE s.user_id = ?"
        " ORDER BY CASE s.status WHEN 'scheduled' THEN 0 ELSE 1 END, s.due_ms DESC"
        " LIMIT ?",
        (user_id, int(limit)),
    ).fetchall()
    stamp = now_ms()
    items: list[dict[str, Any]] = []
    for row in rows:
        due_ms = int(row["due_ms"])
        status = str(row["status"])
        # 到点复判为红之后，行状态仍然是 scheduled，只是被挂起等强验证。
        # 页面必须能看出这一档，否则用户以为「已安排」，钱却永远不动。
        needs_verification = status == "scheduled" and bool(
            int(row["needs_verification"] or 0)
        )
        required_factors = (
            scheduled_mod.required_factors_for(conn, str(row["id"])) if needs_verification else []
        )
        items.append(
            {
                "scheduled_id": str(row["id"]),
                "payee_name": row["payee_name"],
                "account_tail": row["account_tail"],
                "amount_fen": int(row["amount_fen"]),
                "amount_yuan": fen_to_yuan_str(int(row["amount_fen"])),
                "note": str(row["note"] or ""),
                "due_ms": due_ms,
                "due_text": timefmt.maybe_minute_text(due_ms),
                "recurrence": str(row["recurrence"]),
                "status": status,
                "state_label": (
                    SCHEDULED_STATE_LABELS["needs_verification"]
                    if needs_verification
                    else SCHEDULED_STATE_LABELS.get(status, status)
                ),
                "needs_verification": needs_verification,
                "required_factors": required_factors,
                "can_verify_here": bool(needs_verification),
                "overdue": status == "scheduled" and due_ms <= stamp,
                "executed_ms": int(row["executed_ms"]) if row["executed_ms"] else None,
                "executed_text": timefmt.maybe_minute_text(row["executed_ms"]),
            }
        )
    return {
        "items": items,
        "count": len(items),
        "empty_reason": None if items else "还没有任何预约任务。",
    }


def _alerts(conn: sqlite3.Connection, user_id: str, *, limit: int = 10) -> dict[str, Any]:
    rows = conn.execute(
        "SELECT a.*, g.name AS goal_name FROM proactive_alerts a"
        " LEFT JOIN savings_goals g ON g.id = a.goal_id"
        " WHERE g.user_id = ? AND a.status = 'open'"
        " ORDER BY a.created_ms DESC LIMIT ?",
        (user_id, int(limit)),
    ).fetchall()
    items = []
    for row in rows:
        evidence: Any = None
        try:
            evidence = json.loads(str(row["evidence_json"]) or "null")
        except (TypeError, ValueError):
            evidence = None
        items.append(
            {
                "alert_id": str(row["id"]),
                "goal_id": str(row["goal_id"]),
                "goal_name": row["goal_name"],
                "reason": str(row["reason"]),
                "reason_text": ALERT_REASON_TEXT.get(str(row["reason"]), str(row["reason"])),
                "suggestion": str(row["suggestion"] or ""),
                "evidence": evidence,
                "created_ms": int(row["created_ms"]),
                "created_text": timefmt.maybe_minute_text(int(row["created_ms"])),
                "status": str(row["status"]),
            }
        )
    return {"items": items, "count": len(items),
            "empty_reason": None if items else "没有需要提醒的事。"}


#: 提醒原因码 -> 人话。机器码不上界面。
ALERT_REASON_TEXT: Final[dict[str, str]] = {
    proactive.REASON_PROGRESS_BEHIND: "进度落后于计划",
    proactive.REASON_BUDGET_OVERSPENT: "本月花了超过预算",
    proactive.REASON_CASHFLOW_INSUFFICIENT: "结余不足以支撑目标",
}


def _monitor_runs(conn: sqlite3.Connection, *, limit: int = 5) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM monitor_runs ORDER BY started_ms DESC LIMIT ?", (int(limit),)
    ).fetchall()
    return [
        {
            "run_id": str(row["id"]),
            "started_ms": int(row["started_ms"]),
            "started_text": timefmt.maybe_minute_text(int(row["started_ms"])),
            "finished_ms": int(row["finished_ms"]) if row["finished_ms"] else None,
            "status": str(row["status"]),
            "trigger": str(row["trigger"] or proactive.TRIGGER_MANUAL),
            "trigger_label": (
                "后台节拍"
                if str(row["trigger"] or "") == proactive.TRIGGER_SCHEDULER
                else "手动触发"
            ),
            "alerts_created": int(row["alerts_created"]),
            "detail": str(row["detail"] or ""),
        }
        for row in rows
    ]


def _scheduler_block() -> dict[str, Any]:
    """如实说明"谁在推进预约任务"。

    进程内节拍器一旦跟着 web 进程退掉就停了；部署成单进程时它一直在跑，
    部署成多副本时每个副本都会跑（execute_due 靠行锁与幂等键保证只执行一次）。
    """
    interval = ticker.tick_interval_ms()
    running = ticker.is_started()
    monitor_interval = proactive.monitor_interval_ms()
    return {
        "kind": "in_process_ticker",
        "enabled": interval > 0,
        "running": running,
        "interval_ms": interval,
        "interval_text": "已关闭" if interval <= 0 else f"{interval / 1000:.0f} 秒",
        "manual_endpoint": "/api/scheduled/run",
        "note": (
            "预约转账由应用进程内的定时器推进。进程退出期间不会执行，"
            "重启后从数据库恢复未到点的任务。"
        ),
        "monitor": {
            "auto": True,
            "interval_ms": monitor_interval,
            "interval_text": (
                "已关闭" if monitor_interval <= 0 else f"{monitor_interval / 1000:.0f} 秒"
            ),
            "note": (
                "储蓄目标监测：只有你自己打开监测开关的目标才会被节拍器唤起，"
                "同一个目标按上面的间隔节流（默认 6 小时一次），只写提醒、不动钱。"
            ),
            "manual_endpoint": "/api/proactive/monitor/run",
        },
        "monitoring_scope": (
            "会自动推进的是两类：已持久化的预约转账；以及你显式打开监测的储蓄目标。"
            "没打开开关的目标不会自动跑，需要手动点一次。"
        ),
    }


def _handoff_total(conn: sqlite3.Connection, status: str | None = None) -> int:
    """接管单的总张数（不受 limit 影响）：页面上的「共 N 张」用它。"""
    if status is None:
        row = conn.execute("SELECT COUNT(*) AS n FROM handoffs").fetchone()
    else:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM handoffs WHERE status = ?", (status,)
        ).fetchone()
    return int(row["n"]) if row is not None else 0


def handoff_list_view(
    conn: sqlite3.Connection, *, status: str | None = None, limit: int = 50
) -> dict[str, Any]:
    """接管单列表：只给操作员列表页需要的字段。

    单张详情才带 DAG、失败节点、裁决与授权快照；列表上这些既没用又容易把
    库里的内部 JSON 直接摊到接口上。total 是不受 limit 影响的真实张数。
    """
    rows = handoffs.list_handoffs(conn, status=status, limit=limit)
    items: list[dict[str, Any]] = []
    for row in rows:
        status_text = str(row.get("status") or "")
        reason = str(row.get("reason") or "")
        failure = row.get("failure") if isinstance(row.get("failure"), dict) else {}
        created_ms = int(row.get("created_ms") or 0)
        items.append(
            {
                "handoff_id": str(row.get("id") or ""),
                "run_id": str(row.get("run_id") or ""),
                "session_id": str(row.get("session_id") or ""),
                "status": status_text,
                "status_label": HANDOFF_STATUS_LABELS.get(status_text, status_text),
                "reason": reason,
                "reason_label": HANDOFF_REASON_LABELS.get(reason, reason or "需要人工看一眼"),
                "request_text": str(row.get("request_text") or "")[:400],
                "failure_text": str(
                    failure.get("user_text") or failure.get("message") or ""
                )[:200],
                "operator": str(row.get("operator") or ""),
                "note": str(row.get("note") or "")[:500],
                "created_ms": created_ms,
                "created_text": timefmt.maybe_minute_text(created_ms) if created_ms else "",
            }
        )
    return {
        "ok": True,
        "handoffs": items,
        "count": len(items),
        "total": _handoff_total(conn, status=status),
        "note": "列表只给列表页需要的字段；DAG、失败节点、裁决与授权在单张详情里。",
    }


def handoff_detail_view(conn: sqlite3.Connection, handoff_id: str) -> dict[str, Any]:
    """单张工单：DAG、失败节点、裁决、授权快照都保留，操作员恢复时要看。"""
    return {"ok": True, "handoff": handoffs.get(conn, handoff_id)}
