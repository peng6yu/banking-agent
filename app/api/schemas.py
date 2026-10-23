"""API 层的入参模型。

放在 app/api/ 而不是 app/models.py：models.py 是 docs/03 定死的公共契约，
这里补的是文档里有、契约表里没有的端点专用入参，不污染公共模型。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

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


__all__ = [
    "AlertResolveRequest",
    "CancelRequest",
    "MonitorRunRequest",
    "ResetRequest",
    "SIMULATED_EXPENSE_CATEGORIES",
    "SavingsGoalCreate",
    "SavingsGoalUpdate",
    "SimulatedExpenseRequest",
]
