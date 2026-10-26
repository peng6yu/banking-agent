"""计划、生日预算、预约转账与提醒的接口（第 5 层，写操作全部经用例层）。"""

from __future__ import annotations

from fastapi import APIRouter

from typing import Any, Final
import asyncio

from app.api.schemas import (
    AlertResolveRequest,
    BirthdayPayRequest,
    BirthdayPlanCreate,
    CancelRequest,
    ManualTransactionRequest,
    MonitorRunRequest,
    PlanCreate,
    PlanRunRequest,
    PlanUpdate,
    ReminderCreate,
    ResetRequest,
    SavingsGoalCreate,
    ScheduledChallengeRequest,
    ScheduledVerifyRequest,
    SavingsGoalUpdate,
    SimulatedExpenseRequest,
    SubscriptionVerdictClearRequest,
    SubscriptionVerdictRequest,
    SuitabilityAssessRequest,
)
from app.clock import BankError, NotFound, ValidationError
import app.birthday as birthday
from app import audit, clock, db
from app.api.http import payload
import app.plans as plans
import app.proactive as proactive
from app.planner import assist, executor, intent, payment_guard, recover, ticker
import app.use_cases as use_cases

#: 每个端点文件自带一个空 router，由 app/api/routes.py 统一挂到 /api 下。
router = APIRouter()




@router.post("/api/scheduled/run")
async def run_scheduled() -> dict[str, Any]:
    """立刻执行一次到点的定时转账；评审现场想马上看到效果时用。"""
    done = await asyncio.to_thread(ticker.run_due_once)
    return {"ok": True, "executed": len(done), "items": done[:5]}


@router.post("/api/scheduled/{scheduled_id}/challenge")
async def scheduled_challenge(
    scheduled_id: str, request: ScheduledChallengeRequest
) -> dict[str, Any]:
    """给一笔到点复判变红的预约签发一次强验证挑战。

    挑战绑定 sched:<id>，复用一次就作废——它不能拿去给别的转账当证明。
    """
    return payload(use_cases.scheduled.scheduled_issue_challenge(scheduled_id, factor=request.factor))


@router.post("/api/scheduled/{scheduled_id}/verify")
async def scheduled_verify(
    scheduled_id: str, request: ScheduledVerifyRequest
) -> dict[str, Any]:
    """补一次强验证并立即尝试执行这笔预约（执行前仍会重新裁决）。"""
    # 这里必须 model_dump()：验证器只认纯 dict，直接把 pydantic 模型传下去会被
    # factors._normalize 静默丢掉，表现为「给对了口令仍说没验证」。
    return payload(
        use_cases.scheduled.scheduled_submit_verify(
            scheduled_id, proofs=[item.model_dump() for item in (request.proofs or [])]
        )
    )


@router.post("/api/reminders")
async def create_reminder(request: ReminderCreate) -> dict[str, Any]:
    """建一条提醒：页面上的表单和聊天里的自然语言走同一个落库函数。"""
    return payload(use_cases.reminders.reminder_create(**request.model_dump()))


@router.post("/api/plans")
async def create_plan(request: PlanCreate) -> dict[str, Any]:
    """建跨场景计划：步骤落库，后台节拍按到点时间推进（资金动作只提醒、不代扣）。"""
    return payload(
        use_cases.plans.plan_create(
            title=request.title,
            steps=[item.model_dump() for item in request.steps],
            meta=request.meta,
        )
    )


@router.get("/api/plans")
async def list_plans(status: str | None = None, limit: int = 20) -> dict[str, Any]:
    """计划列表：默认只看当前用户的计划，status=all 时连已完成/已取消一起给。"""
    if limit < 1 or limit > plans.MAX_LIMIT:
        raise ValidationError("limit 超出范围", field="limit", minimum=1, maximum=plans.MAX_LIMIT)
    with db.open_conn() as conn:
        user_id = proactive.default_user_id(conn)
        items = plans.list_plans(
            conn,
            user_id=user_id,
            status=None if status in (None, "", "all") else str(status),
            limit=limit,
        )
    return {"ok": True, "plans": items, "count": len(items), "boundary": plans.ACTION_BOUNDARY}


@router.get("/api/plans/{plan_id}")
async def get_plan(plan_id: str) -> dict[str, Any]:
    """单个计划 + 每一步的状态；生日计划连预算项和自动付款授权一起给。

    以前这里一律走 plans.view_plan，生日计划的 items / budget / authorization
    只存在于 birthday.plan_with_budgets 里、没有任何 HTTP 出口——页面上那一步
    「付这一项」就永远没有数据可用。付款端点是唯一的真花钱入口，读口必须对齐。
    """
    with db.open_conn() as conn:
        found = plans.view_plan(conn, plan_id)
        if found is not None and birthday.budgets(conn, plan_id):
            found = birthday.plan_with_budgets(conn, plan_id) or found
    if found is None:
        raise NotFound()
    return {"ok": True, "plan": found}


@router.patch("/api/plans/{plan_id}")
async def update_plan(plan_id: str, request: PlanUpdate) -> dict[str, Any]:
    """改计划：标题、状态（用来取消/暂停）、还没跑过的步骤。"""
    fields = request.model_dump(exclude_unset=True)
    return payload(use_cases.plans.plan_update(plan_id, **fields))


@router.post("/api/plans/run")
async def run_plans(request: PlanRunRequest | None = None) -> dict[str, Any]:
    """手动推进到点的计划步骤（演示现场想立刻看到推进时用，等价于后台那次节拍）。

    顺序和 ticker.run_plans_once 一致：先跑**已授权**的生日付款，再推进通用计划。
    手动触发不是旁路——自动付款仍然按最新日累计重新裁决，红灯/参数漂移/取消都不扣。
    plan_id 只缩小扫描范围，不改变任何安全校验。
    """
    target = request.plan_id if request is not None else None
    return payload(use_cases.plans.plan_run_due(plan_id=target))


@router.post("/api/plans/birthday")
async def create_birthday_plan(request: BirthdayPlanCreate) -> dict[str, Any]:
    """建生日计划：预算、鲜花与蛋糕两项、生日前两天到点提醒，全部落库。

    预算预留真实占用所选账户的可支配金额（余额本身不动），所以余额不够时整份计划
    建不起来，不会留「计划建好了但付不出」的半成品。默认只提醒、付款要用户确认；
    save_authorization=True 才保存与计划版本绑定的一次性授权，到点后仍按最新日累计
    重新裁决，红灯/改计划/取消/参数漂移都会停下来，不会无限期代扣。
    """
    return payload(
        use_cases.plans.birthday_create(
            birthday_date=request.birthday,
            budget_fen=request.budget_fen,
            items=[item.model_dump() for item in request.items],
            title=request.title,
            account_id=request.account_id,
            save_authorization=request.save_authorization,
        )
    )


@router.post("/api/plans/{plan_id}/items/{item_key}/pay")
async def pay_birthday_item(
    plan_id: str, item_key: str, request: BirthdayPayRequest | None = None
) -> dict[str, Any]:
    """付生日计划里的一项：确认 → 策略 → 令牌 → 执行，和手动付款同一条路径。

    后台调度只提醒不代扣，所以这个端点是唯一能真正花钱的入口；缺多因子凭证时
    一分钱都不会动，预算退回 reserved 等用户补验证。
    """
    body = request or BirthdayPayRequest()
    return payload(
        await use_cases.plans.birthday_pay(
            plan_id=plan_id,
            item_key=item_key,
            session_id=body.session_id,
            approve=body.approve,
            proofs=[item.model_dump() for item in (body.proofs or [])],
        )
    )
