"""计划与提醒：持久计划、预约转账、提醒与预警的总装。

只读：本模块不写库、不发令牌、不调执行器。
"""

from __future__ import annotations

import sqlite3
from typing import Any

from app import plans
from app import subscriptions
from app import timefmt
from app.planner import assist
from app.clock import now_ms
from app.tools.base import primary_account

from app.views.queue import (
    _alerts,
    _monitor_runs,
    _pending_confirmations,
    _savings_goals,
    _scheduled_tasks,
    _scheduler_block,
)
from app.views.labels import REMINDER_STATE_LABELS


def plans_view(conn: sqlite3.Connection) -> dict[str, Any]:
    """计划与提醒：把"保存了"/"安排了"/"执行中"/"已完成"四种状态分清楚。"""
    stamp = now_ms()
    account = primary_account(conn)
    user_id = str(account["user_id"])
    goals = _savings_goals(conn, user_id, limit=10)
    scheduled = _scheduled_tasks(conn, user_id)
    pending = _pending_confirmations(conn)
    alerts = _alerts(conn, user_id, limit=20)
    reminders = assist.active_reminders(conn, user_id)
    runs = _monitor_runs(conn)
    charges = subscriptions.upcoming(conn, str(account["id"]))
    # 纯提醒类计划（meta.reminder=true）和下面 reminders 是同一批行：
    # 提醒列表就是从 plans/plan_steps 派生的，两个列表都列会在页面上出现两遍。
    persisted = [
        item
        for item in plans.list_plans(conn, user_id=user_id, limit=10)
        if not (isinstance(item.get("meta"), dict) and item["meta"].get("reminder") is True)
    ]
    return {
        "ok": True,
        "generated_text": timefmt.maybe_minute_text(stamp),
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
        # 提醒和「提醒类预警」不是一回事：这里是用户自己建、会到点触发的提醒。
        "reminders": {
            "items": [
                {**item, "state_label": REMINDER_STATE_LABELS.get(item["state"], item["state"])}
                for item in reminders
            ],
            "count": len(reminders),
            "empty_reason": None if reminders else "还没有提醒。可以在下面直接建一条，也可以跟助手说「提醒我明天上午 9 点开会」。",
        },
        # 近期可能发生的支出：已知代扣的下次扣费日期，打开页面时算，不推送。
        "upcoming_charges": charges,
        "monitor_runs": {
            "items": runs,
            "count": len(runs),
            "note": "主动监测是按需触发的（POST /api/proactive/monitor/run），不是常驻扫描。",
        },
    }


