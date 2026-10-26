"""财务总览：一次把首页需要的六块数据拼好。

只读：本模块不写库、不发令牌、不调执行器。
"""

from __future__ import annotations

import sqlite3
from typing import Any

from app import proactive
from app import timefmt
from app.planner import assist
from app.clock import now_ms
from app.tools.base import primary_account

from app.views.money import _accounts, _bill_block, _trend
from app.views.queue import (
    _alerts,
    _handoffs_view,
    _pending_confirmations,
    _savings_goals,
    _scheduled_tasks,
    _scheduler_block,
)
from app.views.labels import DEFAULT_TREND_MONTHS, QUICK_ACTIONS


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
    reminders = assist.active_reminders(conn, user_id)

    accounts = _accounts(conn)
    if not accounts:
        unavailable.append({"section": "account", "reason": "还没有任何账户数据。"})
    # 注意：空数据不算"暂不可用"。没有储蓄目标、没有预约任务、本月没有支出，
    # 都是正常的业务状态，各区块已经给了明确的空状态文案；把它们塞进
    # unavailable 会让首页出现"暂不可用：scheduled_tasks（还没有预约任务）"
    # 这种自相矛盾的提示——空是真的空，不是取不到。
    # 只有"本来能算但现在算不出来"的才进这个列表（目前只有缺账户一种）。

    return {
        "generated_text": timefmt.maybe_minute_text(stamp),
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
            # 空列表也写「发现异常」就是撒谎：没有命中规则时说没有。
            "state": "发现异常（待核实）" if anomalies else "本期没有命中任何规则",
            "disclaimer": "规则命中不等于欺诈认定；每一条都可以点开看依据和相关流水。",
        },
        "pending_authorizations": pending,
        "handoffs": _handoffs_view(conn, limit=5),
        "savings_goals": {
            "items": goals,
            "count": len(goals),
            "empty_reason": None if goals else "还没有储蓄目标，可以让 AI 助手帮你定一个。",
        },
        "plans_and_reminders": {
            "scheduled": scheduled,
            "alerts": alerts,
            # 首页以前只有「预约 / 预警 / 调度器」，用户自己建的提醒和卡住的人工单
            # 只在计划页或根本看不见；总览要把这两类都摊开，才谈得上「可疑的东西有地方处理」。
            "reminders": {
                "items": reminders,
                "count": len(reminders),
                "empty_reason": None if reminders else "还没有提醒。",
            },
            "handoffs": _handoffs_view(conn, limit=5),
            "scheduler": _scheduler_block(),
        },
        "quick_actions": [dict(item) for item in QUICK_ACTIONS],
        "unavailable": unavailable,
        "unavailable_note": "这里只列真正取不到的数据；空列表属于正常状态，会在各自区块里显示空状态。",
    }


