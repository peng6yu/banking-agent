"""助手主动提醒的 HTTP 面（受 API token 保护）。

读走只读视图；两个写动作（刷新 / 反馈）一律走用例层再 payload 回来——
这是仓库对写操作唯一路径的硬规矩，好处是路由里不出现 SQL 与事务。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from app import db
from app.api.http import payload
from app.api.routes import require_api_token
from app.use_cases import messages as message_cases
from app.views.messages import insights_view, messages_view

router = APIRouter(prefix="/api/messages", tags=["messages"],
                   dependencies=[Depends(require_api_token)])


class FeedbackBody(BaseModel):
    """反馈只有一个动作字段；note 给「忽略」留一句原因，可空。"""

    action: str = Field(min_length=1, max_length=16, description="adopted / dismissed / muted")
    note: str = Field("", max_length=300)


@router.get("")
def api_list_messages(limit: int = Query(20, ge=1, le=50)) -> dict[str, Any]:
    """读已有提醒；不在这里生成——生成是写动作，走 /refresh。"""
    with db.open_conn() as conn:
        return messages_view(conn, limit=limit)


@router.post("/refresh")
def api_refresh() -> dict[str, Any]:
    """现在想一想：跑一轮探测器，按去重/封顶/静默规则落库后返回最新列表。"""
    return payload(message_cases.message_refresh())


@router.get("/insights")
def api_insights() -> dict[str, Any]:
    """洞察目录：每类提醒的依据与数据来源，供页面解释「你凭什么提醒我」。"""
    return insights_view()


@router.post("/{message_id}/feedback")
def api_feedback(message_id: str, body: FeedbackBody) -> dict[str, Any]:
    """采纳 / 忽略 / 不再提醒；重复提交同一动作幂等。"""
    return payload(message_cases.message_feedback(
        message_id, action=body.action, note=body.note,
    ))


__all__ = ["router"]
