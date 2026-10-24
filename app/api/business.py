"""只读业务视图：给「六大信息区」提供真实数据，一行写操作都没有。

为什么要单开一个模块：原来的接口只有 /api/chat、/api/session、/api/audit，
它们描述的是"某一次会话执行到了哪一步"。财务总览、账单分析、计划与提醒这些
区域要的是"账户现在是什么状态"，两者不是同一种数据——用会话流水去拼首页，
结果就是首页永远缺数据、只能拿示例值垫着。

三条硬规矩：
1. 只读。本模块不写库、不发令牌、不调执行器，端点一律 GET。
2. 不编数据。查不到就给空数组 + unavailable 说明；不返回任何示例值冒充真实结果。
3. 钱一律整数分。同时给一份 yuan 字符串，前端不做浮点运算。

对应文档：docs/业务语义审计与信息架构.md、docs/提交物对照表.md。
"""

from __future__ import annotations

import json
import sqlite3
import time
from contextlib import closing
from datetime import date
from typing import Any, Final

from fastapi import APIRouter, Depends, Query

from app import audit, db, plans, proactive, subscriptions, suitability
from app.api.routes import require_api_token
from app.clock import now_ms
from app.money import fen_to_yuan_str
from app.planner import ticker
from app.tools import bill, queries
from app.tools.base import ToolContext, primary_account
from app.tools.bill import ANOMALY_KINDS

#: 收支趋势最多回看多少个月
MAX_TREND_MONTHS: Final[int] = 12
#: 默认回看 6 个月：足够看出一个季度以上的实际节奏
DEFAULT_TREND_MONTHS: Final[int] = 6
#: 单次列表最多返回多少条
MAX_LIST: Final[int] = 50

#: 交易状态的中文标签。前后端必须共用同一套说法，不能一处叫"处理中"一处叫"执行中"。
TASK_STATE_LABELS: Final[dict[str, str]] = {
    "draft": "草稿",
    "need_info": "等待补充信息",
    "waiting_confirm": "等待确认",
    "waiting_factor": "等待验证",
    "running": "执行中",
    "done": "已成功",
    "failed": "已失败",
    "cancelled": "已取消",
    "manual": "需要人工处理",
}
#: 预约转账的行状态 -> 用户能看懂的说法
SCHEDULED_STATE_LABELS: Final[dict[str, str]] = {
    "scheduled": "已安排",
    "executed": "已完成",
    "cancelled": "已取消",
    "failed": "已失败",
}

#: 三类异常规则的元信息。规则命中只说明"值得核实"，不等于欺诈认定——
#: 这行字必须跟着每一条异常一起返回，不能只写在文档里。
ANOMALY_RULES: Final[dict[str, dict[str, Any]]] = {
    "split_orders": {
        "rule_id": "R-BILL-01",
        "rule_text": "同一商户 60 分钟内 ≥3 笔，且单笔落在 500—1000 元",
        "severity": "info",
        "confidence": "low",
        "possible_false_positive": "分期付款、多人 AA、商家分单结算都会命中；规则只说明值得核实。",
        "actions": [
            {"id": "view_transactions", "label": "查看这几笔流水", "kind": "inspect"},
            {"id": "ask_assistant", "label": "让 AI 助手解释这一段", "kind": "prompt",
             "prompt": "帮我看一下最近在同一个商户连着付的几笔，是拆单吗？"},
        ],
    },
    "odd_hour_overseas": {
        "rule_id": "R-BILL-02",
        "rule_text": "00:00—05:00 之间、境外商户、单笔 ≥500 元的支出",
        "severity": "warning",
        "confidence": "medium",
        "possible_false_positive": "时差、夜间国际航班消费、境外订阅自动续费都可能命中。",
        "actions": [
            {"id": "freeze_card", "label": "临时冻结这张卡", "kind": "tool", "tool": "freeze_card"},
            {"id": "ask_assistant", "label": "让 AI 助手核这一笔", "kind": "prompt",
             "prompt": "帮我核实一下凌晨那笔境外消费，是不是我自己刷的。"},
        ],
    },
    "subscription_hike": {
        "rule_id": "R-BILL-03",
        "rule_text": "同一订阅商户连续两个自然月金额上涨 ≥30%",
        "severity": "info",
        "confidence": "medium",
        "possible_false_positive": "套餐升级、一次性加购、按用量计费都会命中。",
        "actions": [
            {"id": "open_subscriptions", "label": "看订阅管理", "kind": "inspect"},
            {"id": "ask_assistant", "label": "查这个订阅能不能停", "kind": "prompt",
             "prompt": "我这个订阅涨价了，能帮我看下怎么处理吗？"},
        ],
    },
}

#: 常见业务快捷入口。只是给 AI 助手预填一句话，不代表这些业务已经执行过。
QUICK_ACTIONS: Final[tuple[dict[str, str], ...]] = (
    {"id": "transfer", "label": "转账", "prompt": "帮我转 500 元给张三"},
    {"id": "bill", "label": "看看这个月花了多少", "prompt": "这个月我的钱花在哪了？"},
    {"id": "savings", "label": "定一个储蓄目标", "prompt": "我想半年内存 6000 元"},
    {"id": "subs", "label": "管理订阅", "prompt": "我这个月有哪些订阅在扣钱？"},
    {"id": "card", "label": "卡片管理", "prompt": "帮我看一下我的卡，能不能先冻结一下"},
)


# ---------------------------------------------------------------- 小工具


def _conn() -> Any:
    """一次请求一条连接；和 routes.py 一样用 closing 保证关掉。"""
    return closing(db.connect())


def _ctx(conn: sqlite3.Connection, account_id: str | None = None) -> ToolContext:
    """只读工具的执行上下文：没有会话、没有密钥，也不该有。"""
    return ToolContext(
        conn=conn, session_id="view", node_id="view", task_id="view",
        account_id=account_id, secret=None,
    )


def _time_text(ms: int | None) -> str | None:
    if not ms:
        return None
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(int(ms) / 1000))


def _date_text(day: date | str | None) -> str | None:
    if day is None:
        return None
    return day.isoformat() if isinstance(day, date) else str(day)


def _start_of_month(day: date) -> int:
    return int(time.mktime((day.year, day.month, 1, 0, 0, 0, 0, 0, -1)) * 1000)


def _month_start_ms(year: int, month: int) -> int:
    """左闭右开窗口的左端；mktime 会把 month=13 归一化到次年 1 月。"""
    return int(time.mktime((year, month, 1, 0, 0, 0, 0, 0, -1)) * 1000)


def _accounts(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """全部账户；储蓄卡在前。信用卡不推算"可用额度"，那需要真实账单口径。"""
    rows = conn.execute(
        "SELECT * FROM accounts"
        " ORDER BY CASE type WHEN 'debit' THEN 0 ELSE 1 END, id"
    ).fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        view = queries.account_view(row)
        view["type_label"] = "储蓄卡" if view["type"] == "debit" else "信用卡"
        if view["type"] == "debit":
            view["available_fen"] = view["balance_fen"]
            view["available_yuan"] = view["balance_yuan"]
        else:
            view["available_fen"] = None
            view["available_yuan"] = None
            view["available_note"] = "可用额度要按当期账单口径算，这里不推算。"
        out.append(view)
    return out


def _tx_map(conn: sqlite3.Connection, ids: list[str]) -> dict[str, dict[str, Any]]:
    """按 id 取流水视图（异常卡片要能列出"相关流水"）。

    transactions.id 是文本主键（t_000000ec 这种），全程按字符串传递，
    不要为了排序或去重把它转成整数。
    """
    clean = sorted({str(i) for i in ids if i is not None})
    if not clean:
        return {}
    marks = ",".join("?" * len(clean))
    rows = conn.execute(
        f"SELECT * FROM transactions WHERE id IN ({marks})", clean
    ).fetchall()
    return {str(row["id"]): queries.tx_view(row) for row in rows}


def _anomaly_view(
    item: dict[str, Any], index: int, tx_map: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    """把工具层的一条命中，补成前端能直接渲染、且用户能自己复核的卡片。"""
    kind = str(item.get("kind", ""))
    rule = ANOMALY_RULES.get(kind, {})
    amount_fen = int(item.get("amount_fen") or 0)
    tx_ids = [str(i) for i in (item.get("tx_ids") or [])]
    return {
        "id": f"{kind or 'anomaly'}-{index}",
        # 状态只有「发现异常」：规则命中不是欺诈认定，不能写成「已确认问题」。
        "status": "发现异常",
        "status_note": "规则命中只表示值得核实，需要你自己或客服确认。",
        "kind": kind,
        "title": str(item.get("title") or ""),
        "detail": str(item.get("detail") or ""),
        "amount_fen": amount_fen,
        "amount_yuan": fen_to_yuan_str(amount_fen),
        "merchant": item.get("merchant"),
        "occurrence_count": item.get("occurrence_count"),
        "window_start_ms": item.get("window_start_ms"),
        "window_end_ms": item.get("window_end_ms"),
        "window_text": (
            f"{_time_text(item.get('window_start_ms'))} ~ {_time_text(item.get('window_end_ms'))}"
            if item.get("window_start_ms") and item.get("window_end_ms")
            else None
        ),
        "comparison": item.get("comparison"),
        "rule": {
            "id": rule.get("rule_id", ""),
            "text": rule.get("rule_text", ""),
            "severity": rule.get("severity", "info"),
            "confidence": rule.get("confidence", "low"),
        },
        "possible_false_positive": rule.get("possible_false_positive", ""),
        "evidence": {
            "tx_ids": tx_ids,
            "transactions": [tx_map[i] for i in tx_ids if i in tx_map],
        },
        "actions": list(rule.get("actions", [])),
    }


def _enrich_anomalies(
    conn: sqlite3.Connection, items: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    ids: list[str] = []
    for item in items:
        ids.extend(str(i) for i in (item.get("tx_ids") or []))
    tx_map = _tx_map(conn, ids)
    return [_anomaly_view(item, index, tx_map) for index, item in enumerate(items)]


def _trend(
    conn: sqlite3.Connection, account_id: str, months: int = DEFAULT_TREND_MONTHS
) -> list[dict[str, Any]]:
    """最近 N 个自然月的收支（含当月，当月标 partial）。

    口径：转账与理财属于账户内部搬运，不计入收支——否则"钱从储蓄卡转到信用卡"
    会被算成一次收入加一次支出，趋势图立刻失真。
    """
    months = max(1, min(int(months), MAX_TREND_MONTHS))
    today = proactive.local_date()
    keys: list[tuple[int, int]] = []
    year, month = today.year, today.month
    for _ in range(months):
        keys.append((year, month))
        month -= 1
        if month == 0:
            year, month = year - 1, 12
    keys.reverse()
    start_ms = _month_start_ms(*keys[0])
    end_ms = _month_start_ms(today.year, today.month + 1)
    rows = conn.execute(
        "SELECT direction, amount_fen, category, ts_ms FROM transactions"
        " WHERE account_id = ? AND ts_ms >= ? AND ts_ms < ?",
        (account_id, start_ms, end_ms),
    ).fetchall()
    buckets: dict[tuple[int, int], dict[str, int]] = {
        key: {"income_fen": 0, "expense_fen": 0, "count": 0} for key in keys
    }
    for row in rows:
        if str(row["category"]) in proactive.INTERNAL_CATEGORIES:
            continue
        tm = time.localtime(int(row["ts_ms"]) / 1000)
        key = (tm.tm_year, tm.tm_mon)
        bucket = buckets.get(key)
        if bucket is None:
            continue
        amount = int(row["amount_fen"])
        if str(row["direction"]) == "in":
            bucket["income_fen"] += amount
        else:
            bucket["expense_fen"] += amount
        bucket["count"] += 1
    out: list[dict[str, Any]] = []
    for key in keys:
        bucket = buckets[key]
        net = bucket["income_fen"] - bucket["expense_fen"]
        out.append(
            {
                "month": f"{key[0]:04d}-{key[1]:02d}",
                "label": f"{key[1]} 月",
                "income_fen": bucket["income_fen"],
                "income_yuan": fen_to_yuan_str(bucket["income_fen"]),
                "expense_fen": bucket["expense_fen"],
                "expense_yuan": fen_to_yuan_str(bucket["expense_fen"]),
                "net_fen": net,
                "net_yuan": fen_to_yuan_str(abs(net)),
                "net_positive": net >= 0,
                "transaction_count": bucket["count"],
                "partial": key == (today.year, today.month),
            }
        )
    return out


def _bill_block(
    conn: sqlite3.Connection, account_id: str | None, year: int, month: int
) -> dict[str, Any]:
    """当期账单：直接复用 analyze_bill，避免首页和账单页各算一套数字。"""
    ctx = _ctx(conn, account_id)
    result = bill.analyze_bill(ctx, {"period": "month", "year": int(year), "month": int(month)})
    data = dict(result.data or {})
    anomalies = _enrich_anomalies(conn, list(data.get("anomalies") or []))
    data["anomalies"] = anomalies
    data["anomaly_count"] = len(anomalies)
    return data


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
                    "threshold": int(rule["threshold"]),
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
        " r.status AS run_status"
        " FROM confirmations c LEFT JOIN dag_runs r ON r.id = c.run_id"
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
                "created_text": _time_text(int(row["created_ms"])),
                "expires_ms": expires_ms,
                "expires_in_s": max(0, (expires_ms - stamp) // 1000),
                "expired": expired,
                # 进程重启后，内存里的执行器已经没了：这时卡片还在库里，
                # 但它已经不可能被"点一下"推进，必须如实说是过期/失效。
                "stale": expired or str(row["run_status"] or "") not in ("running", "waiting"),
                "state_label": "已过期，需要重新发起" if expired else "等待确认",
            }
        )
    return {
        "items": items,
        "count": len(items),
        "source": "confirmations 表（服务端持久化，刷新不丢）",
        "empty_reason": None if items else "现在没有等你确认的操作。",
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
        items.append(
            {
                "scheduled_id": str(row["id"]),
                "payee_name": row["payee_name"],
                "account_tail": row["account_tail"],
                "amount_fen": int(row["amount_fen"]),
                "amount_yuan": fen_to_yuan_str(int(row["amount_fen"])),
                "note": str(row["note"] or ""),
                "due_ms": due_ms,
                "due_text": _time_text(due_ms),
                "recurrence": str(row["recurrence"]),
                "status": status,
                "state_label": SCHEDULED_STATE_LABELS.get(status, status),
                "overdue": status == "scheduled" and due_ms <= stamp,
                "executed_ms": int(row["executed_ms"]) if row["executed_ms"] else None,
                "executed_text": _time_text(row["executed_ms"]),
            }
        )
    pending = [i for i in items if i["status"] == "scheduled"]
    return {
        "items": items,
        "count": len(items),
        "scheduled_count": len(pending),
        "overdue_count": sum(1 for i in pending if i["overdue"]),
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
                "created_text": _time_text(int(row["created_ms"])),
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
            "started_text": _time_text(int(row["started_ms"])),
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
    return {
        "kind": "in_process_ticker",
        "enabled": interval > 0,
        "running": running,
        "interval_ms": interval,
        "manual_endpoint": "/api/scheduled/run",
        "note": (
            "预约转账由应用进程内的定时器推进。进程退出期间不会执行，"
            "重启后从数据库恢复未到点的任务。"
        ),
        "monitor": {
            "auto": True,
            "interval_ms": proactive.monitor_interval_ms(),
            "trigger_field": "monitor_runs.trigger",
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


# ---------------------------------------------------------------- 视图装配


def overview_view(conn: sqlite3.Connection) -> dict[str, Any]:
    """财务总览：一张脸看清"我有多少钱、钱在往哪走、有什么事要处理"。"""
    stamp = now_ms()
    today = proactive.local_date()
    account = primary_account(conn)
    account_id = str(account["id"])
    user_id = str(account["user_id"])
    unavailable: list[dict[str, str]] = []

    bill_data = _bill_block(conn, account_id, today.year, today.month)
    trend = _trend(conn, account_id, DEFAULT_TREND_MONTHS)
    anomalies = list(bill_data.get("anomalies") or [])
    pending = _pending_confirmations(conn, limit=5)
    goals = _savings_goals(conn, user_id, limit=3)
    scheduled = _scheduled_tasks(conn, user_id, limit=5)
    alerts = _alerts(conn, user_id, limit=3)

    accounts = _accounts(conn)
    if not accounts:
        unavailable.append({"section": "account", "reason": "还没有任何账户数据。"})
    # 注意：空数据不算"暂不可用"。没有储蓄目标、没有预约任务、本月没有支出，
    # 都是正常的业务状态，各区块已经给了明确的空状态文案；把它们塞进
    # unavailable 会让首页出现"暂不可用：scheduled_tasks（还没有预约任务）"
    # 这种自相矛盾的提示——空是真的空，不是取不到。
    # 只有"本来能算但现在算不出来"的才进这个列表（目前只有缺账户一种）。

    return {
        "generated_ms": stamp,
        "generated_text": _time_text(stamp),
        "month_label": bill_data.get("range", {}).get("label"),
        "account": next((a for a in accounts if a["account_id"] == account_id), None),
        "accounts": accounts,
        "this_month": {
            "label": bill_data.get("range", {}).get("label"),
            "window": bill_data.get("range"),
            "total_out_fen": bill_data.get("total_out_fen", 0),
            "total_out_yuan": bill_data.get("total_out_yuan", "0.00"),
            "total_in_fen": bill_data.get("total_in_fen", 0),
            "total_in_yuan": bill_data.get("total_in_yuan", "0.00"),
            "out_count": bill_data.get("out_count", 0),
            "in_count": bill_data.get("in_count", 0),
            "by_category": list(bill_data.get("by_category") or [])[:6],
            "top_merchants": list(bill_data.get("top_merchants") or [])[:3],
            "summary": bill_data.get("summary"),
        },
        "trend": trend,
        "anomalies": {
            "items": anomalies[:3],
            "count": len(anomalies),
            "more_count": max(0, len(anomalies) - 3),
            "state": "发现异常（待核实）",
            "disclaimer": "规则命中不等于欺诈认定；每一条都可以点开看依据和相关流水。",
        },
        "pending_authorizations": pending,
        "savings_goals": {
            "items": goals,
            "count": len(goals),
            "empty_reason": None if goals else "还没有储蓄目标，可以让 AI 助手帮你定一个。",
        },
        "plans_and_reminders": {
            "scheduled": scheduled,
            "alerts": alerts,
            "scheduler": _scheduler_block(),
        },
        "quick_actions": [dict(item) for item in QUICK_ACTIONS],
        "unavailable": unavailable,
        "unavailable_note": "这里只列真正取不到的数据；空列表属于正常状态，会在各自区块里显示空状态。",
    }


def bill_view(
    conn: sqlite3.Connection, *, period: str, year: int, month: int | None
) -> dict[str, Any]:
    """账单分析：统计 + 分类 + 趋势 + 异常（每条带依据、证据与后续动作）。"""
    stamp = now_ms()
    account = primary_account(conn)
    account_id = str(account["id"])
    today = proactive.local_date()
    period = (period or "month").strip().lower()
    if period not in ("month", "year"):
        period = "month"
    if period == "month":
        month = int(month or today.month)
        year = int(year or today.year)
    data = bill.analyze_bill(
        _ctx(conn, account_id),
        {"period": period, "year": int(year), **({"month": month} if period == "month" else {})},
    ).data
    anomalies = _enrich_anomalies(conn, list(data.get("anomalies") or []))
    return {
        "generated_ms": stamp,
        "generated_text": _time_text(stamp),
        "period": period,
        "year": int(year),
        "month": month if period == "month" else None,
        "range": data.get("range"),
        "account_tail": data.get("account_tail"),
        "total_out_fen": data.get("total_out_fen", 0),
        "total_out_yuan": data.get("total_out_yuan", "0.00"),
        "total_in_fen": data.get("total_in_fen", 0),
        "total_in_yuan": data.get("total_in_yuan", "0.00"),
        "out_count": data.get("out_count", 0),
        "in_count": data.get("in_count", 0),
        "by_category": data.get("by_category") or [],
        "top_merchants": data.get("top_merchants") or [],
        "summary": data.get("summary"),
        "trend": _trend(conn, account_id, DEFAULT_TREND_MONTHS),
        "anomalies": {
            "items": anomalies,
            "count": len(anomalies),
            "state": "发现异常（待核实）",
            "disclaimer": "三条规则都是启发式提示，不是欺诈认定；命中后请自己核对流水。",
            "rules": [
                {
                    "kind": kind,
                    "rule_id": ANOMALY_RULES.get(kind, {}).get("rule_id", ""),
                    "rule_text": ANOMALY_RULES.get(kind, {}).get("rule_text", ""),
                    "confidence": ANOMALY_RULES.get(kind, {}).get("confidence", "low"),
                }
                for kind in ANOMALY_KINDS
            ],
        },
        "method": {
            "currency": "CNY",
            "money_unit": "所有金额以整数分计算与返回，同时给 yuan 字符串",
            "categories": list(queries.CATEGORIES),
            "internal_transfer_note": "转账/理财属于账户内部搬运，收支统计与趋势里不计入。",
            "anomaly_window": "拆单与凌晨境外只扫当前账期；订阅涨价会带上一个自然月的基线。",
        },
    }


def management_view(conn: sqlite3.Connection) -> dict[str, Any]:
    """业务管理：转账、卡片、订阅、理财各自的入口、边界与数据来源。"""
    stamp = now_ms()
    account = primary_account(conn)
    account_id = str(account["id"])
    user_id = str(account["user_id"])
    accounts = _accounts(conn)

    card_rows = conn.execute(
        "SELECT c.*, a.tail AS account_tail, a.name AS account_name FROM cards c"
        " LEFT JOIN accounts a ON a.id = c.account_id"
        " WHERE a.user_id = ? ORDER BY CASE c.status WHEN 'active' THEN 0 ELSE 1 END, c.id",
        (user_id,),
    ).fetchall()
    card_labels = {"active": "正常", "frozen": "已冻结", "lost": "已挂失", "closed": "已销户"}
    cards = [
        {
            "card_id": str(row["id"]),
            "account_tail": row["account_tail"],
            "account_name": row["account_name"],
            "tail": str(row["tail"]),
            "kind": str(row["kind"]),
            "kind_label": "实体卡" if str(row["kind"]) == "physical" else "虚拟卡",
            "status": str(row["status"]),
            "status_label": card_labels.get(str(row["status"]), str(row["status"])),
            "per_tx_limit_fen": int(row["per_tx_limit_fen"]),
            "per_tx_limit_yuan": fen_to_yuan_str(int(row["per_tx_limit_fen"])),
        }
        for row in card_rows
    ]

    sub_rows = conn.execute(
        "SELECT * FROM subs WHERE account_id = ? ORDER BY status, merchant", (account_id,)
    ).fetchall()
    subs_items = [
        {
            "sub_id": str(row["id"]),
            "merchant": str(row["merchant"]),
            "amount_fen": int(row["amount_fen"]),
            "amount_yuan": fen_to_yuan_str(int(row["amount_fen"])),
            "period": str(row["period"]),
            "period_label": "每月" if str(row["period"]) == "monthly" else "每年",
            "status": str(row["status"]),
            "status_label": "生效中" if str(row["status"]) == "active" else "已停止（银行侧）",
            "next_charge_ms": int(row["next_charge_ms"]),
            "next_charge_text": _time_text(int(row["next_charge_ms"])),
            # 这句必须跟着每条订阅一起给：本地表标记取消，只停银行侧代扣，
            # 商户那边的会员并不会因此注销。
            "boundary": "这里只能停止银行侧的周期性代扣，商户会员需要你自己去商户端取消。",
        }
        for row in sub_rows
    ]

    product_rows = conn.execute(
        "SELECT * FROM fund_products ORDER BY risk_level, code LIMIT 12"
    ).fetchall()
    held = {
        str(row["product_code"]): row
        for row in conn.execute(
            "SELECT * FROM fund_holdings WHERE user_id = ?", (user_id,)
        ).fetchall()
    }
    products = []
    for row in product_rows:
        shares = int(held[str(row["code"])]["shares_milli"]) if str(row["code"]) in held else 0
        products.append(
            {
                "code": str(row["code"]),
                "name": str(row["name"]),
                "category": str(row["category"]),
                "risk_level": int(row["risk_level"]),
                "min_buy_fen": int(row["min_buy_fen"]),
                "min_buy_yuan": fen_to_yuan_str(int(row["min_buy_fen"])),
                "nav_micro": int(row["nav_micro"]),
                "nav_updated_ms": int(row["nav_updated_ms"]),
                "nav_updated_text": _time_text(int(row["nav_updated_ms"])),
                "description": str(row["description"]),
                "holding_shares_milli": shares,
            }
        )

    profile = suitability.load(conn, user_id)
    inferred = subscriptions.detect_recurring(conn, account_id)
    upcoming = subscriptions.upcoming(conn, account_id)
    scheduled = _scheduled_tasks(conn, user_id, limit=5)
    holdings_summary = {
        "holding_count": sum(1 for p in products if p["holding_shares_milli"] > 0),
        "product_count": len(products),
    }

    return {
        "generated_ms": stamp,
        "accounts": accounts,
        "cards": cards,
        "subscriptions": {
            "items": subs_items,
            "count": len(subs_items),
            "monthly_active_fen": sum(
                int(row["amount_fen"]) for row in sub_rows
                if str(row["period"]) == "monthly" and str(row["status"]) == "active"
            ),
            "boundary": "订阅数据来自本行代扣记录，不是商户会员系统。",
            # 已知代扣之外：疑似订阅（流水推断）与近期扣费窗口。两者都不动钱。
            "inferred": inferred,
            "upcoming": upcoming,
        },
        "scheduled": scheduled,
        "investments": {
            "products": products,
            "summary": holdings_summary,
            "data_source": "fund_products 表（演示数据：净值是固定的，不接行情）",
            # 画像从 investor_profiles 读：没测过就是没测过，测过就照实给等级与可买范围。
            "suitability_assessed": profile is not None,
            "suitability": {
                "assessed": profile is not None,
                "profile": profile,
                "question_count": len(suitability.QUESTIONS),
                # 题目跟着画像一起下发：界面不用为了填表再单独请求一次。
                "questions": [
                    {
                        "id": item["id"],
                        "text": item["text"],
                        "options": [
                            {"value": option["value"], "label": option["label"]}
                            for option in item["options"]
                        ],
                    }
                    for item in suitability.QUESTIONS
                ],
                "level_bands": [
                    {"floor": floor, "level": level, "label": label, "capacity": capacity}
                    for floor, level, label, capacity in suitability.LEVEL_BANDS
                ],
                "disclaimer": "演示用简化问卷，不是监管口径的正式风险评估；不构成投资建议。",
            },
            "suitability_note": (
                "买基金前必须先做一次 5 题风险测评（C1—C5）：没测评或产品风险高于画像上限，"
                "buy_fund 会直接拒绝，界面也不会替你判断。问卷是演示简化版。"
                if profile is None
                else f"已测评：{profile['level']}（{profile['level_label']}），"
                     f"{profile['capacity_label']} 风险的产品。"
            ),
            "execution": "申购/赎回走 buy_fund / redeem_fund，均为写操作，需要能力令牌。",
        },
        "capabilities": CAPABILITIES,
    }


#: 各项业务在当前原型里的真实边界。前端直接照这个渲染，不用自己猜。
CAPABILITIES: Final[dict[str, dict[str, Any]]] = {
    "transfer": {
        "supported": True,
        "entry": "AI 助手（自然语言）",
        "requires_authorization": True,
        "notes": [
            "收款人重名时必须由你选择，系统不会自己挑第一个。",
            "确认后再改金额或收款人，旧授权作废，需要重新确认。",
            "1000 元以上需要短信+人脸双因子——这是赛题设定的分级策略，不代表所有真实银行的限额。",
        ],
    },
    "scheduled_transfer": {
        "supported": True,
        "entry": "AI 助手 / 计划与提醒",
        "requires_authorization": True,
        "notes": [
            "未执行的预约可以取消。",
            "已执行的转账不能一键撤回：只能发起冲正（记一笔反向流水），且在演示环境里是模拟账务。",
        ],
    },
    "card": {
        "supported": True,
        "entry": "业务管理 / AI 助手",
        "requires_authorization": True,
        "notes": ["冻结、挂失、调额、销卡都会写审计；挂失不可逆。"],
    },
    "subscription": {
        "supported": True,
        "entry": "账单分析 / 业务管理",
        "requires_authorization": True,
        "notes": ["只能停止银行侧代扣，不能注销商户会员。"],
    },
    "investment": {
        "supported": True,
        "entry": "业务管理 / AI 助手",
        "requires_authorization": True,
        "notes": ["有产品查询、风险等级与危险提示；没有适当性评估问卷。"],
    },
    "reversal": {
        "supported": True,
        "entry": "AI 助手",
        "requires_authorization": True,
        "notes": ["冲正是模拟账务，不代表真实银行追回；真实追回需要人工受理。"],
    },
}


def plans_view(conn: sqlite3.Connection) -> dict[str, Any]:
    """计划与提醒：把"保存了"/"安排了"/"执行中"/"已完成"四种状态分清楚。"""
    stamp = now_ms()
    account = primary_account(conn)
    user_id = str(account["user_id"])
    goals = _savings_goals(conn, user_id, limit=10)
    scheduled = _scheduled_tasks(conn, user_id)
    pending = _pending_confirmations(conn)
    alerts = _alerts(conn, user_id, limit=20)
    runs = _monitor_runs(conn)
    charges = subscriptions.upcoming(conn, str(account["id"]))
    persisted = plans.list_plans(conn, user_id=user_id, limit=10)
    return {
        "generated_ms": stamp,
        "generated_text": _time_text(stamp),
        "scheduler": _scheduler_block(),
        "state_legend": [
            {"state": "saved", "label": "计划已保存",
             "meaning": "目标与参数已经存进数据库，但没有任何东西会自己去执行它。"},
            {"state": "scheduled", "label": "任务已安排",
             "meaning": "预约记录在库，节拍器活着时到点会真的执行。"},
            {"state": "running", "label": "任务正在执行", "meaning": "执行器正在跑这一步。"},
            {"state": "done", "label": "任务已完成", "meaning": "有明确的执行结果和审计记录。"},
            {"state": "failed", "label": "已失败/已取消", "meaning": "失败或取消都留痕，可以重试或重新发起。"},
        ],
        "saved_plans": {
            "items": goals,
            "count": len(goals),
            "empty_reason": None if goals else "还没有保存的计划。可以让 AI 助手按你的收支算一个。",
        },
        "scheduled_tasks": scheduled,
        # 落库的跨场景计划：check / reminder 会由节拍推进，action 步到期只会提醒你确认。
        "plans": {
            "items": persisted,
            "count": len(persisted),
            "boundary": plans.ACTION_BOUNDARY,
            "empty_reason": None if persisted else "还没有跨场景计划。可以让 AI 助手按目标生成一个，保存后由后台节拍推进。",
        },
        "pending_authorizations": pending,
        "alerts": alerts,
        # 近期可能发生的支出：已知代扣的下次扣费日期，打开页面时算，不推送。
        "upcoming_charges": charges,
        "monitor_runs": {
            "items": runs,
            "count": len(runs),
            "note": "主动监测是按需触发的（POST /api/proactive/monitor/run），不是常驻扫描。",
        },
    }


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
    session_items: list[dict[str, Any]] = []
    for row in sessions:
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
        session_items.append(
            {
                "session_id": sid,
                "title": str(row["title"] or ""),
                "status": str(row["status"]),
                "status_label": {
                    "active": "进行中", "done": "已完成", "cancelled": "已取消",
                }.get(str(row["status"]), str(row["status"])),
                "created_ms": int(row["created_ms"]),
                "created_text": _time_text(int(row["created_ms"])),
                "last_ms": int(row["last_ms"]),
                "last_text": _time_text(int(row["last_ms"])),
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
                        "created_text": _time_text(int(c["created_ms"])),
                        "expires_text": _time_text(int(c["expires_ms"])),
                        "resolved_text": _time_text(c["resolved_ms"]),
                    }
                    for c in confs
                ],
            }
        )
    denials = conn.execute(
        "SELECT * FROM denials ORDER BY ts_ms DESC LIMIT ?", (limit,)
    ).fetchall()
    chain_ok = audit.verify_chain(conn)
    return {
        "generated_ms": stamp,
        "generated_text": _time_text(stamp),
        "sessions": session_items,
        "denials": [
            {
                "reason": str(row["reason"]),
                "ts_ms": int(row["ts_ms"]),
                "ts_text": _time_text(int(row["ts_ms"])),
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
        "ts_text": _time_text(node["ts_ms"]),
    }


# ---------------------------------------------------------------- 路由


router = APIRouter(prefix="/api/business", tags=["business"],
                   dependencies=[Depends(require_api_token)])


@router.get("/overview")
def api_overview() -> dict[str, Any]:
    """财务总览：账户、本月收支、异常、待确认授权、储蓄目标、计划提醒。"""
    with _conn() as conn:
        return overview_view(conn)


@router.get("/bill")
def api_bill(
    period: str = Query("month", pattern="^(month|year)$"),
    year: int | None = Query(None, ge=2000, le=2100),
    month: int | None = Query(None, ge=1, le=12),
) -> dict[str, Any]:
    """账单分析：统计、分类、趋势、异常（每条带规则依据与相关流水）。"""
    with _conn() as conn:
        today = proactive.local_date()
        return bill_view(conn, period=period, year=int(year or today.year), month=month)


@router.get("/management")
def api_management() -> dict[str, Any]:
    """业务管理：账户、卡片、订阅、理财入口与各自的业务边界。"""
    with _conn() as conn:
        return management_view(conn)


@router.get("/plans")
def api_plans() -> dict[str, Any]:
    """计划与提醒：已保存的计划、已安排的预约、待确认操作、提醒与监测记录。"""
    with _conn() as conn:
        return plans_view(conn)


@router.get("/executions")
def api_executions(
    limit: int = Query(10, ge=1, le=MAX_LIST),
    session_id: str | None = Query(None, max_length=64),
) -> dict[str, Any]:
    """执行记录与安全中心：会话、节点裁决、确认卡、拒绝记录、审计链状态。"""
    with _conn() as conn:
        return executions_view(conn, limit=limit, session_id=session_id)


__all__ = [
    "api_bill",
    "api_executions",
    "api_management",
    "api_overview",
    "api_plans",
    "bill_view",
    "executions_view",
    "management_view",
    "overview_view",
    "plans_view",
    "router",
]
