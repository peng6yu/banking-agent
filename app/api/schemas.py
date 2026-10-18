"""API 层的入参模型。

放在 app/api/ 而不是 app/models.py：models.py 是 docs/03 定死的公共契约，
这里补的是文档里有、契约表里没有的端点专用入参，不污染公共模型。
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class CancelRequest(BaseModel):
    """POST /api/cancel 的入参。"""

    model_config = ConfigDict(extra="forbid")

    session_id: str = Field(min_length=1)
    reason: str = "用户中途取消"


class ResetRequest(BaseModel):
    """POST /api/reset-demo 的入参；不传也允许。"""

    model_config = ConfigDict(extra="forbid")

    reason: str = "演示重置"


__all__ = ["CancelRequest", "ResetRequest"]
