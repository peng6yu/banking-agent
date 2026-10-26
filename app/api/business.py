"""只读业务视图的路由面：给「六大信息区」提供真实数据，一行写操作都没有。

本文件在 W4 之后**只剩路由**：鉴权 + 开只读连接 + 调 app/views 里的装配函数 + 返回。
视图装配（读数积木、状态文案、卡片组装）都在 app/views/，这样改一张卡片不必动路由，
也不会有人顺手在路由里加判断。

三条硬规矩（从前身继承，仍然成立）：
1. 只读。本模块不写库、不发令牌、不调执行器，端点一律 GET。
2. 不编数据。查不到就给空数组 + unavailable 说明；不返回任何示例值冒充真实结果。
3. 钱一律整数分。同时给一份 yuan 字符串，前端不做浮点运算。

对应文档：docs/业务语义审计与信息架构.md、docs/提交物对照表.md、docs/07 7.5 端点表。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query

from app import db, proactive
from app.api.routes import require_api_token
from app.views import bill_view, executions_view, management_view, overview_view, plans_view
from app.views.labels import MAX_LIST

# ---------------------------------------------------------------- 路由


router = APIRouter(prefix="/api/business", tags=["business"],
                   dependencies=[Depends(require_api_token)])


@router.get("/overview")
def api_overview() -> dict[str, Any]:
    """财务总览：账户、本月收支、异常、待确认授权、储蓄目标、计划提醒。"""
    with db.open_conn() as conn:
        return overview_view(conn)


@router.get("/bill")
def api_bill(
    period: str = Query("month", pattern="^(month|year)$"),
    year: int | None = Query(None, ge=2000, le=2100),
    month: int | None = Query(None, ge=1, le=12),
    account_id: str | None = Query(None, max_length=40),
) -> dict[str, Any]:
    """账单分析：统计、分类、趋势、异常（每条带规则依据与相关流水）。

    account_id 不传就看主储蓄卡；传了就必须真实存在（否则 400），
    因为「切错卡」比「报错」更难被用户发现。
    """
    with db.open_conn() as conn:
        today = proactive.local_date()
        return bill_view(
            conn, period=period, year=int(year or today.year), month=month,
            account_id=account_id,
        )


@router.get("/management")
def api_management() -> dict[str, Any]:
    """业务管理：账户、卡片、订阅、理财入口与各自的业务边界。"""
    with db.open_conn() as conn:
        return management_view(conn)


@router.get("/plans")
def api_plans() -> dict[str, Any]:
    """计划与提醒：已保存的计划、已安排的预约、待确认操作、提醒与监测记录。"""
    with db.open_conn() as conn:
        return plans_view(conn)


@router.get("/executions")
def api_executions(
    limit: int = Query(10, ge=1, le=MAX_LIST),
    session_id: str | None = Query(None, max_length=64),
) -> dict[str, Any]:
    """执行记录与安全中心：会话、节点裁决、确认卡、拒绝记录、审计链状态。"""
    with db.open_conn() as conn:
        return executions_view(conn, limit=limit, session_id=session_id)


# 视图函数在这里再导出一次：既有调用方（tests/test_real_channels.py）按
# 「from app.api.business import management_view」取用，保持向后兼容。
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
