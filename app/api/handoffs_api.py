"""人工接管的 HTTP 接口（受 API token 保护）。

和 /api/business 一样挂在 require_api_token 后面：转人工队列能直接看到用户的
账户与失败原因，绝不能是匿名可读的运维页面。写动作（领取/拒绝/补充信息/取消/恢复）
全部要求显式 operator，审计里才追得到「是谁动的手」。

为什么 resume 没有 approve 参数：操作员不是用户本人，替用户点确认卡等于绕开
「钱必须经用户确认」这条核心边界；新出现的确认卡一律把单打回 need_info。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from app import db, handoffs
from app.api.routes import require_api_token

router = APIRouter(prefix="/api/handoffs", tags=["handoffs"],
                   dependencies=[Depends(require_api_token)])


class OperatorBody(BaseModel):
    """最小操作员动作：只写是谁。"""

    operator: str = Field(min_length=1, max_length=64, description="操作员工号或名字")


class NoteBody(OperatorBody):
    """带备注的动作：拒绝、要求补充信息、取消、恢复。"""

    note: str = Field("", max_length=500, description="给用户/下一位操作员看的说明")


class CancelBody(OperatorBody):
    reason: str = Field("", max_length=500, description="取消原因")


class CreateBody(BaseModel):
    """手动建单：自动转人工之外，也允许客服在电话里替用户挂一张单。"""

    run_id: str = Field(min_length=1, max_length=64)
    reason: str = Field(min_length=1, max_length=64)
    operator: str = Field("", max_length=64)
    note: str = Field("", max_length=500)
    request_text: str = Field("", max_length=4000)
    account_id: str = Field("", max_length=64)


@router.get("")
def api_list(
    status: str | None = Query(None, description="pending/claimed/need_info/rejected/cancelled/resumed/failed"),
    limit: int = Query(50, ge=1, le=handoffs.MAX_LIST),
) -> dict[str, Any]:
    """按状态列工单（新单在前）；不带 status 就是全量最近 N 张。"""
    with db.open_conn() as conn:
        rows = handoffs.list_handoffs(conn, status=status, limit=limit)
    return {"handoffs": rows, "count": len(rows)}


@router.get("/{handoff_id}")
def api_get(handoff_id: str) -> dict[str, Any]:
    """单张工单：原请求、DAG、已完成/失败节点、裁决、授权与备注都在里面。"""
    with db.open_conn() as conn:
        return {"handoff": handoffs.get(conn, handoff_id)}


@router.post("")
def api_create(body: CreateBody) -> dict[str, Any]:
    """手动建单（幂等）：同一轮已有活跃单时返回原单，不会重复挂单。"""
    with db.open_conn() as conn:
        row = handoffs.create(
            conn,
            run_id=body.run_id,
            reason=body.reason,
            note=body.note,
            request_text=body.request_text,
            account_id=body.account_id,
        )
    return {"handoff": row}


@router.post("/{handoff_id}/claim")
def api_claim(handoff_id: str, body: OperatorBody) -> dict[str, Any]:
    """领取一张 pending 单；并发领取只有一个成功，另一个拿到 HANDOFF_ALREADY_CLAIMED。"""
    with db.open_conn() as conn:
        return {"handoff": handoffs.claim(conn, handoff_id, body.operator)}


@router.post("/{handoff_id}/reject")
def api_reject(handoff_id: str, body: NoteBody) -> dict[str, Any]:
    """拒绝这张单：不执行、不动钱，只关闭队列项。"""
    with db.open_conn() as conn:
        return {"handoff": handoffs.reject(conn, handoff_id, body.operator, body.note)}


@router.post("/{handoff_id}/need-info")
def api_need_info(handoff_id: str, body: NoteBody) -> dict[str, Any]:
    """要求补充信息：必须写清楚缺什么（note 空会被拒）。"""
    with db.open_conn() as conn:
        return {"handoff": handoffs.need_info(conn, handoff_id, body.operator, body.note)}


@router.post("/{handoff_id}/cancel")
async def api_cancel(handoff_id: str, body: CancelBody) -> dict[str, Any]:
    """取消工单，并顺带取消还卡着的那一轮执行（释放 hold）。"""
    with db.open_conn() as conn:
        return {"handoff": await handoffs.cancel(conn, handoff_id, body.operator, body.reason)}


@router.post("/{handoff_id}/resume")
async def api_resume(handoff_id: str, body: NoteBody) -> dict[str, Any]:
    """恢复：先复核实时业务状态，再按原 DAG 续跑；失败会落 failed + 审计。"""
    with db.open_conn() as conn:
        return await handoffs.resume(conn, handoff_id, operator=body.operator, note=body.note)


__all__ = ["router"]
