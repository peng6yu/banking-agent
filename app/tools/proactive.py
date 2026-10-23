"""主动财务管理工具：储蓄目标、储蓄计划、提醒与手动监测。

对应文档：docs/改造实施基线.md 9.2、docs/06_工具层实现规格.md 6.1。

这里的七个工具只做三件事：读目标/提醒、给确定性的储蓄计划、在用户手动触发时
跑一次监测并写提醒。**没有**任何转账、申购、扣款或改余额的入口——监测只写
proactive_alerts 与 monitor_runs，金额全部走 app.proactive 的 int 分计算。

「手动触发」是设计约束：run_proactive_monitor 由用户或前端显式调用，
没有任何定时器或后台循环挂在它后面（定时接入不是本阶段的范围）。

写工具第一句都是 require_token：黄灯工具也要求用户确认后拿到的能力令牌，
工具层不自己判断该不该做，裁决统一在 app/policy/rules.py。
"""

from __future__ import annotations

from typing import Any

import app.proactive as proactive
from app.models import ToolResult
from app.tools.base import (
    ToolContext,
    fail_result,
    ok_result,
    primary_account,
    require_token,
)

__all__ = [
    "compute_savings_plan",
    "create_savings_goal",
    "list_proactive_alerts",
    "list_savings_goals",
    "resolve_proactive_alert",
    "run_proactive_monitor",
    "update_savings_goal",
]


def _owner(ctx: ToolContext) -> str:
    """本次操作的用户：沿用主账户解析，演示环境里就是唯一那个用户。"""
    account = primary_account(ctx.conn, ctx.account_id)
    return str(account["user_id"])


def _tx_rows(ctx: ToolContext, user_id: str) -> list[Any]:
    return proactive.transactions_for_user(ctx.conn, user_id)


def _int_param(params: dict[str, Any], key: str, fallback: int) -> int:
    """模型偶尔把可选参数显式传成 null；缺省与 null 都按默认值处理。"""
    value = params.get(key)
    return fallback if value is None else int(value)


def _result(payload: dict[str, Any], audit_seq: int | None = None) -> ToolResult:
    return ToolResult(ok=True, data=payload, audit_seq=audit_seq)


def list_savings_goals(
    ctx: ToolContext, params: dict[str, Any], token: str | None = None
) -> ToolResult:
    """列储蓄目标：默认只回 active，每条都带当前进度与监测规则。"""
    status = params.get("status")
    status = None if status in (None, "", "all") else str(status)
    limit = _int_param(params, "limit", proactive.DEFAULT_LIMIT)
    user_id = _owner(ctx)
    goals = proactive.list_goals(
        ctx.conn, user_id=user_id, status=status, limit=limit
    )
    return ok_result({"user_id": user_id, "goals": goals, "count": len(goals)})


def compute_savings_plan(
    ctx: ToolContext, params: dict[str, Any], token: str | None = None
) -> ToolResult:
    """算储蓄计划：给 goal_id 就按目标算，否则按临时参数算，不写库。"""
    user_id = _owner(ctx)
    goal_id = params.get("goal_id")
    transactions = _tx_rows(ctx, user_id)
    lookback = _int_param(params, "lookback_months", proactive.LOOKBACK_MONTHS)
    if goal_id:
        row = proactive.get_goal(ctx.conn, str(goal_id), user_id=user_id)
        if row is None:
            return fail_result(
                "GOAL_NOT_FOUND", "没找到这个储蓄目标，先用 list_savings_goals 看看。"
            )
        plan = proactive.compute_savings_plan(
            target_fen=int(row["target_fen"]),
            target_date=str(row["target_date"]),
            saved_fen=int(row["saved_fen"]),
            transactions=transactions,
            lookback_months=lookback,
        )
        return ok_result({"goal_id": str(row["id"]), "source": "goal", "plan": plan})
    plan = proactive.compute_savings_plan(
        target_fen=params.get("target_fen"),
        target_date=params.get("target_date"),
        saved_fen=_int_param(params, "saved_fen", 0),
        transactions=transactions,
        lookback_months=lookback,
    )
    return ok_result({"goal_id": None, "source": "adhoc", "plan": plan})


def create_savings_goal(
    ctx: ToolContext, params: dict[str, Any], token: str | None = None
) -> ToolResult:
    """建储蓄目标：必然建一条进度监测规则，给了月度预算才再建预算规则。"""
    require_token("create_savings_goal", params, token)
    user_id = _owner(ctx)
    created = proactive.create_goal(
        ctx.conn,
        user_id=user_id,
        name=str(params.get("name", "")),
        target_fen=params.get("target_fen"),
        target_date=params.get("target_date"),
        saved_fen=_int_param(params, "saved_fen", 0),
        budget_fen=params.get("budget_fen"),
        progress_tolerance=params.get("progress_tolerance"),
        period=str(params.get("period", "monthly")),
        session_id=ctx.session_id,
        task_id=ctx.task_id,
    )
    return _result(
        {
            "user_id": user_id,
            "goal": created["goal"],
            "rule_ids": created["rule_ids"],
            "message": "储蓄目标已建好，进度规则也开始盯着了。",
        },
        created.get("audit_seq"),
    )


def update_savings_goal(
    ctx: ToolContext, params: dict[str, Any], token: str | None = None
) -> ToolResult:
    """调整目标：改金额/日期/已存、开关监测；达成与回退会自动改状态。"""
    require_token("update_savings_goal", params, token)
    user_id = _owner(ctx)
    goal_id = str(params.get("goal_id") or "")
    updatable = (
        "name",
        "target_fen",
        "target_date",
        "saved_fen",
        "status",
        "budget_fen",
        "progress_tolerance",
        "monitor_enabled",
    )
    fields = {key: params[key] for key in updatable if key in params}
    updated = proactive.update_goal(
        ctx.conn,
        goal_id,
        user_id=user_id,
        session_id=ctx.session_id,
        task_id=ctx.task_id,
        **fields,
    )
    if updated is None:
        return fail_result(
            "GOAL_NOT_FOUND", "没找到这个储蓄目标，先用 list_savings_goals 看看。"
        )
    return _result(
        {
            "goal": updated["goal"],
            "changes": updated["changes"],
            "rule_changes": updated["rule_changes"],
            "message": "目标已更新，计划按新参数重算。",
        },
        updated.get("audit_seq"),
    )


def list_proactive_alerts(
    ctx: ToolContext, params: dict[str, Any], token: str | None = None
) -> ToolResult:
    """列提醒：默认只看待处理的 open，status=all 才把历史一锅端。"""
    user_id = _owner(ctx)
    status = str(params.get("status") or "open")
    goal_id = params.get("goal_id")
    limit = _int_param(params, "limit", proactive.DEFAULT_LIMIT)
    alerts = proactive.list_alerts(
        ctx.conn,
        user_id=user_id,
        status=status,
        goal_id=None if goal_id in (None, "") else str(goal_id),
        limit=limit,
    )
    return ok_result({"alerts": alerts, "count": len(alerts), "status": status})


def resolve_proactive_alert(
    ctx: ToolContext, params: dict[str, Any], token: str | None = None
) -> ToolResult:
    """处理提醒：ignore 先放着，resolve 算处理完；重复处理幂等。"""
    require_token("resolve_proactive_alert", params, token)
    user_id = _owner(ctx)
    alert_id = str(params.get("alert_id") or "")
    action = str(params.get("action") or "")
    resolved = proactive.resolve_alert(
        ctx.conn,
        alert_id,
        action,
        user_id=user_id,
        session_id=ctx.session_id,
        task_id=ctx.task_id,
    )
    if resolved is None:
        return fail_result("ALERT_NOT_FOUND", "没找到这条提醒，可能已经不在待处理列表里了。")
    verb = "先放着" if action == proactive.ALERT_ACTION_IGNORE else "已处理"
    return _result(
        {
            "alert": resolved["alert"],
            "idempotent": bool(resolved.get("idempotent", False)),
            "message": f"提醒{verb}。",
        },
        resolved.get("audit_seq"),
    )


def run_proactive_monitor(
    ctx: ToolContext, params: dict[str, Any], token: str | None = None
) -> ToolResult:
    """手动跑一次监测：只写提醒与运行记录，不动钱；可重复触发。"""
    require_token("run_proactive_monitor", params, token)
    user_id = _owner(ctx)
    goal_id = params.get("goal_id")
    run = proactive.run_monitor(
        ctx.conn,
        user_id=user_id,
        goal_id=None if goal_id in (None, "") else str(goal_id),
        session_id=ctx.session_id,
        task_id=ctx.task_id,
    )
    data = dict(run)
    if run.get("status") != "ok":
        return ToolResult(
            ok=False,
            data=data,
            error_code="MONITOR_FAILED",
            error_text="这次监测没跑成，运行明细已经记下来了，稍后再试一次。",
            audit_seq=run.get("audit_seq"),
        )
    data["message"] = (
        f"监测完了：查了 {run['goals_checked']} 个目标，新生成 {run['alerts_created']} 条提醒。"
    )
    return _result(data, run.get("audit_seq"))
