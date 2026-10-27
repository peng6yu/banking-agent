"""储蓄目标、订阅定性、理财适当性、预警、手动记账与演示重置的接口（第 5 层）。"""

from __future__ import annotations

from fastapi import APIRouter, Query

from typing import Any, Final

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
from app import audit, clock, db
from app.api.http import payload
import app.proactive as proactive
import app.use_cases as use_cases
from app.views.transactions import transactions_view

from app.api.chat_state import (
    _history,
)

#: 每个端点文件自带一个空 router，由 app/api/routes.py 统一挂到 /api 下。
router = APIRouter()




@router.post("/api/demo/reset")
async def reset_demo(request: ResetRequest | None = None) -> dict[str, Any]:
    """演示重置：收回智能体动过的钱、清掉 hold 与限额账，进行中的会话收掉。"""
    _history.clear()
    reason = request.reason if request is not None else "演示重置"
    return payload(use_cases.demo.demo_reset(reason=reason))


@router.post("/api/savings/goals")
async def create_savings_goal(request: SavingsGoalCreate) -> dict[str, Any]:
    """建储蓄目标：同时落一条进度监测规则，给了预算再加预算规则。写审计。"""
    return payload(use_cases.savings.goal_create(**request.model_dump()))


@router.get("/api/savings/goals")
async def list_savings_goals(status: str | None = None, limit: int = 20) -> dict[str, Any]:
    """目标列表 + 进度；status=all 时连已达成/已取消的一起给。"""
    if limit < 1 or limit > proactive.MAX_LIMIT:
        raise ValidationError("limit 超出范围", field="limit", minimum=1, maximum=proactive.MAX_LIMIT)
    with db.open_conn() as conn:
        user_id = proactive.default_user_id(conn)
        goals = proactive.list_goals(
            conn,
            user_id=user_id,
            status=None if status in (None, "", "all") else str(status),
            limit=limit,
        )
    return {"ok": True, "goals": goals, "count": len(goals)}


@router.post("/api/subscriptions/verdict")
async def subscription_verdict(request: SubscriptionVerdictRequest) -> dict[str, Any]:
    """给一条疑似订阅定性（不是我订的 / 确实是我订的）。

    只改 subscription_verdicts 一行并写审计：不动钱、不建计划、也不替用户
    去商户退订。两种结论都会让这条推断从管理页的「疑似订阅」待办里消失——
    「不是我订的」是误报，「确实是我订的」是确认，都不该继续挂着当待办。
    """
    return payload(use_cases.subscriptions.verdict_set(**request.model_dump()))


@router.post("/api/subscriptions/verdict/clear")
async def subscription_verdict_clear(request: SubscriptionVerdictClearRequest) -> dict[str, Any]:
    """撤掉某条疑似订阅的定性，让它重新变回待办。

    同样是只改一行 + 写审计：如果流水仍然满足周期性条件，这条会立刻回到管理
    页的「疑似订阅」里；已经不满足的就自然不再出现。
    """
    return payload(use_cases.subscriptions.verdict_clear(**request.model_dump()))


@router.patch("/api/savings/goals/{goal_id}")
async def update_savings_goal(goal_id: str, request: SavingsGoalUpdate) -> dict[str, Any]:
    """调整目标：只改传了的字段，按新参数重算计划并写审计。"""
    fields = request.model_dump(exclude_unset=True)
    return payload(use_cases.savings.goal_update(goal_id, **fields))


@router.post("/api/investment/suitability")
async def assess_suitability(request: SuitabilityAssessRequest) -> dict[str, Any]:
    """提交 5 题答案：算分、落画像、写一条 suitability.assessed 审计。"""
    return payload(use_cases.investments.assessment_submit(answers=request.answers))


@router.post("/api/proactive/monitor/run")
async def run_proactive_monitor(request: MonitorRunRequest | None = None) -> dict[str, Any]:
    """手动触发一次监测（可重复）：只写提醒与运行记录，不动一分钱。"""
    goal_id = request.goal_id if request is not None else None
    return payload(use_cases.proactive.monitor_run(goal_id=goal_id))


@router.get("/api/proactive/alerts")
async def list_proactive_alerts(
    status: str = "open", goal_id: str | None = None, limit: int = 20
) -> dict[str, Any]:
    """提醒列表：默认只看待处理的 open。"""
    if limit < 1 or limit > proactive.MAX_LIMIT:
        raise ValidationError("limit 超出范围", field="limit", minimum=1, maximum=proactive.MAX_LIMIT)
    with db.open_conn() as conn:
        user_id = proactive.default_user_id(conn)
        alerts = proactive.list_alerts(
            conn, user_id=user_id, status=status, goal_id=goal_id, limit=limit
        )
    return {"ok": True, "alerts": alerts, "count": len(alerts)}


@router.post("/api/proactive/alerts/{alert_id}/resolve")
async def resolve_proactive_alert(
    alert_id: str, request: AlertResolveRequest
) -> dict[str, Any]:
    """处理提醒：ignore=先放着，resolve=已解决；重复处理幂等，写审计。"""
    return payload(use_cases.proactive.alert_resolve(alert_id, action=request.action))


@router.get("/api/transactions")
def list_transactions(
    account_id: str | None = Query(None, max_length=40),
    period: str = Query("month", pattern="^(month|year)$"),
    year: int | None = Query(None, ge=2000, le=2100),
    month: int | None = Query(None, ge=1, le=12),
    direction: str | None = Query(None, max_length=4),
    category: str | None = Query(None, max_length=12),
    q: str = Query("", max_length=40),
    flagged: bool = Query(False),
    limit: int = Query(30, ge=1, le=100),
    offset: int = Query(0, ge=0, le=10000),
) -> dict[str, Any]:
    """流水明细：这张卡在这个时间段里的全量流水（分页）+ 收支合计。

    账单页的「相关流水」只列命中规则的证据，不是对账单；想翻自己的账就用这里。
    账户互转与理财也在结果里，它们只是不算收支（与账单页同一口径）。
    """
    with db.open_conn() as conn:
        return transactions_view(
            conn, account_id=account_id, period=period, year=year, month=month,
            direction=direction, category=category, q=q, flagged=flagged,
            limit=limit, offset=offset,
        )


@router.post("/api/transactions/manual")
async def manual_transaction(request: ManualTransactionRequest) -> dict[str, Any]:
    """手动补录一笔流水（接不了银行官方接口时的入口）。

    只写本行流水、只动演示余额，落一条 transaction.manual 审计；不发起任何外部
    支付、不建计划、不触发代扣。补录之后账单、月度统计与订阅推断都会把它算进去
    （这正是「我买了什么，账上就该有」的意思）。
    """
    return payload(use_cases.transactions.manual_record(**request.model_dump()))


@router.post("/api/proactive/simulate/expense")
async def simulate_expense(request: SimulatedExpenseRequest) -> dict[str, Any]:
    """模拟数据注入（竞赛演示用）：插一条**模拟**支出并同步演示余额，不涉及任何真实支付。

    评审现场要演示「新增一笔支出 → 重跑监测 → 看到预算提醒」，而演示库里没有
    真实消费入口；这个端点只造演示流水（channel='simulated'），既不碰外部渠道，
    也不产生转账/申购/扣款语义。同一请求当天重复提交会复用已有流水（不重复扣钱）。
    """
    return payload(use_cases.transactions.expense_simulate(**request.model_dump()))
