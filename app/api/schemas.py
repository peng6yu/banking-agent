"""API 层的入参模型。

放在 app/api/ 而不是 app/models.py：models.py 是 docs/03 定死的公共契约，
这里补的是文档里有、契约表里没有的端点专用入参，不污染公共模型。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.models import FactorProof

#: 目标金额上限与工具层同一条线：1 亿元。超出就不是储蓄目标是资产迁移了。
MAX_TARGET_FEN: int = 100_000_000_00
TARGET_DATE_PATTERN = r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$"


class CancelRequest(BaseModel):
    """POST /api/cancel 的入参。"""

    model_config = ConfigDict(extra="forbid")

    session_id: str = Field(min_length=1)
    reason: str = "用户中途取消"


class ResetRequest(BaseModel):
    """POST /api/reset-demo 的入参；不传也允许。"""

    model_config = ConfigDict(extra="forbid")

    reason: str = "演示重置"


class SavingsGoalCreate(BaseModel):
    """POST /api/savings/goals 的入参。"""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=40)
    target_fen: int = Field(ge=1, le=MAX_TARGET_FEN)
    target_date: str = Field(min_length=10, max_length=10, pattern=TARGET_DATE_PATTERN)
    saved_fen: int = Field(default=0, ge=0, le=MAX_TARGET_FEN)
    budget_fen: int | None = Field(default=None, ge=1, le=MAX_TARGET_FEN)
    progress_tolerance: int | None = Field(default=None, ge=1, le=100)
    period: Literal["monthly", "weekly"] = "monthly"


class SavingsGoalUpdate(BaseModel):
    """PATCH /api/savings/goals/{id} 的入参：只改传了的字段。"""

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=40)
    target_fen: int | None = Field(default=None, ge=1, le=MAX_TARGET_FEN)
    target_date: str | None = Field(default=None, min_length=10, max_length=10, pattern=TARGET_DATE_PATTERN)
    saved_fen: int | None = Field(default=None, ge=0, le=MAX_TARGET_FEN)
    status: Literal["active", "achieved", "cancelled"] | None = None
    budget_fen: int | None = Field(default=None, ge=1, le=MAX_TARGET_FEN)
    progress_tolerance: int | None = Field(default=None, ge=1, le=100)
    monitor_enabled: bool | None = None


class MonitorRunRequest(BaseModel):
    """POST /api/proactive/monitor/run 的入参；body 可以整个不传。"""

    model_config = ConfigDict(extra="forbid")

    goal_id: str | None = Field(default=None, min_length=1)


class AlertResolveRequest(BaseModel):
    """POST /api/proactive/alerts/{id}/resolve 的入参。"""

    model_config = ConfigDict(extra="forbid")

    action: Literal["ignore", "resolve"]


#: 模拟支出允许的分类：只收 7 个真实支出类。
#: 故意排除 工资（那是收入）、理财 与 转账（那是内部搬运）——演示接口不该有
#: 任何造收入或伪造资金归集的能力。
SIMULATED_EXPENSE_CATEGORIES: tuple[str, ...] = (
    "餐饮",
    "交通",
    "购物",
    "生活缴费",
    "娱乐",
    "医疗",
    "订阅",
)


class SimulatedExpenseRequest(BaseModel):
    """POST /api/proactive/simulate/expense 的入参（竞赛演示用的模拟数据注入）。

    这不是真实支付：端点只往演示库里插一条**模拟**流水并同步演示余额，
    方便评审现场「加一笔支出 → 重跑监测 → 看到预算提醒」。不对应任何真实扣款。
    """

    model_config = ConfigDict(extra="forbid", title="模拟支出注入（竞赛演示用）")

    amount_fen: int = Field(ge=1, le=MAX_TARGET_FEN)
    merchant: str = Field(min_length=1, max_length=40)
    category: Literal[
        "餐饮", "交通", "购物", "生活缴费", "娱乐", "医疗", "订阅"
    ] = "餐饮"
    note: str | None = Field(default=None, max_length=60)


class SuitabilityAssessRequest(BaseModel):
    """POST /api/investment/suitability 的入参：5 道题的题号 -> 选项值。

    选项值由 app/suitability.py 的 QUESTIONS 定义；写成宽松 dict 是为了让
    "答错题号/选项不存在" 由业务层给出人话错误，而不是 Pydantic 的英文报错。
    """

    model_config = ConfigDict(extra="forbid", title="风险测评答案")

    answers: dict[str, str] = Field(min_length=1, max_length=10)


class PlanStepIn(BaseModel):
    """计划里的一步：检查条件 / 提醒 / 资金动作。

    action 步只登记「要做什么」，不在后台执行——执行入口永远是助手的确认卡片。
    """

    model_config = ConfigDict(extra="forbid", title="计划步骤")

    kind: Literal["check", "action", "reminder"]
    title: str = Field(min_length=1, max_length=60)
    payload: dict = Field(default_factory=dict)
    due_ms: int | None = Field(default=None, ge=0)
    due_in_ms: int | None = Field(default=None, ge=0)
    max_attempts: int | None = Field(default=None, ge=1, le=10)


class PlanStepPatch(BaseModel):
    """PATCH 计划时改某一步；只能改还没跑过的（pending）步骤。"""

    model_config = ConfigDict(extra="forbid", title="计划步骤修改")

    seq: int = Field(ge=1)
    title: str | None = Field(default=None, min_length=1, max_length=60)
    payload: dict | None = None
    due_ms: int | None = Field(default=None, ge=0)
    max_attempts: int | None = Field(default=None, ge=1, le=10)


class PlanCreate(BaseModel):
    """POST /api/plans 的入参。"""

    model_config = ConfigDict(extra="forbid", title="新建计划")

    title: str = Field(min_length=1, max_length=60)
    steps: list[PlanStepIn] = Field(min_length=1, max_length=12)
    meta: dict = Field(default_factory=dict)


class ReminderCreate(BaseModel):
    """POST /api/reminders 的入参：一条独立提醒，可一次性也可循环。"""

    model_config = ConfigDict(extra="forbid", title="新建提醒")

    text: str = Field(min_length=1, max_length=40)
    due_ms: int | None = Field(default=None, ge=0)
    repeat: Literal["none", "daily", "weekly", "monthly"] = "none"
    hour: int = Field(default=9, ge=0, le=23)
    minute: int = Field(default=0, ge=0, le=59)
    day: int | None = Field(default=None, ge=1, le=31)
    weekday: int | None = Field(default=None, ge=0, le=6)


class PlanUpdate(BaseModel):
    """PATCH /api/plans/{plan_id} 的入参：只改传了的字段。"""

    model_config = ConfigDict(extra="forbid", title="修改计划")

    title: str | None = Field(default=None, min_length=1, max_length=60)
    status: Literal["active", "paused", "completed", "cancelled", "failed"] | None = None
    steps: list[PlanStepPatch] | None = None


class PlanRunRequest(BaseModel):
    """POST /api/plans/run 的入参：不传 plan_id 就推进所有到点的计划。"""

    model_config = ConfigDict(extra="forbid", title="手动推进计划")

    plan_id: str | None = Field(default=None, min_length=1)


class BirthdayItemIn(BaseModel):
    """生日计划里的一个项目（鲜花、蛋糕……）。"""

    model_config = ConfigDict(extra="forbid", title="生日项目")

    key: str = Field(min_length=1, max_length=20)
    label: str = Field(min_length=1, max_length=20)
    merchant: str = Field(min_length=1, max_length=40)
    amount_fen: int = Field(ge=1, le=MAX_TARGET_FEN)


class BirthdayPlanCreate(BaseModel):
    """POST /api/plans/birthday 的入参：生日、预算、项目、账户与一次性授权。

    预算预留会真实占用所选账户的可支配金额，所以账户要能显式指定；不指定时
    用用户名下第一张储蓄卡（与旧行为一致）。save_authorization=True 表示用户
    在创建时明确同意「到点按这份清单自动付款」，服务端保存与计划版本绑定的一次性
    授权；它仍然要过最新日累计裁决，参数变了/红灯/取消都不再自动扣款。
    """

    model_config = ConfigDict(extra="forbid", title="新建生日计划")

    birthday: str = Field(min_length=10, max_length=10, description="YYYY-MM-DD")
    budget_fen: int = Field(default=100_000, ge=1, le=MAX_TARGET_FEN)
    items: list[BirthdayItemIn] = Field(default_factory=list, max_length=4)
    title: str | None = Field(default=None, min_length=1, max_length=60)
    account_id: str | None = Field(
        default=None, min_length=1, max_length=40, description="扣款账户；默认第一张储蓄卡"
    )
    save_authorization: bool = Field(
        default=False, description="是否保存一次性预约授权（到点自动付款）"
    )


class BirthdayPayRequest(BaseModel):
    """POST /api/plans/{plan_id}/items/{item_key}/pay 的入参。

    approve=False 表示用户明确拒绝付款；proofs 与 /api/confirm 同构（服务端挑战 + 校验）。
    """

    model_config = ConfigDict(extra="forbid", title="生日项目付款")

    approve: bool = True
    proofs: list[FactorProof] | None = None
    session_id: str | None = Field(default=None, min_length=1, max_length=80)


class ScheduledChallengeRequest(BaseModel):
    """POST /api/scheduled/{id}/challenge 的入参：给到点复判变红的预约补签强验证挑战。"""

    model_config = ConfigDict(extra="forbid", title="预约强验证挑战")

    factor: Literal["face", "sms"] = "face"


class ScheduledVerifyRequest(BaseModel):
    """POST /api/scheduled/{id}/verify 的入参：证明与 /api/confirm 同构。"""

    model_config = ConfigDict(extra="forbid", title="预约强验证")

    proofs: list[FactorProof] | None = None


class SubscriptionVerdictRequest(BaseModel):
    """POST /api/subscriptions/verdict 的入参：用户对一条疑似订阅定性。

    只有两种结论：不是我订的（以后不再当候选端上来）/ 确实是我订的（留痕，
    但也不会自动变成银行侧代扣）。推断永远不自动改写这个结论。
    """

    model_config = ConfigDict(extra="forbid", title="疑似订阅定性")

    merchant: str = Field(min_length=1, max_length=60)
    verdict: Literal["not_subscription", "confirmed"]
    note: str = Field(default="", max_length=200)


__all__ = [
    "AlertResolveRequest",
    "BirthdayItemIn",
    "BirthdayPayRequest",
    "BirthdayPlanCreate",
    "CancelRequest",
    "MonitorRunRequest",
    "PlanCreate",
    "PlanRunRequest",
    "PlanStepIn",
    "PlanStepPatch",
    "PlanUpdate",
    "ResetRequest",
    "SIMULATED_EXPENSE_CATEGORIES",
    "ScheduledChallengeRequest",
    "ScheduledVerifyRequest",
    "SavingsGoalCreate",
    "SavingsGoalUpdate",
    "SimulatedExpenseRequest",
    "SubscriptionVerdictRequest",
    "SuitabilityAssessRequest",
]
