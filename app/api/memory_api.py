"""长期记忆的 HTTP 入口：看、改、删。

只做三件事，够用就好：
- GET    /api/memory        看 agent 记住了我什么（顺带给前端展示用的 prompt_block）
- POST   /api/memory        自己补一条/改一条（用户比模型更知道自己想被怎么记）
- DELETE /api/memory/{key}  忘掉一条（「别记这个」必须好使，否则记忆就是负担）

单用户演示：user_id 走 app.proactive.default_user_id，和 business.py 一个口径，
不额外造会话概念。错误不包装成 HTTPException：ValidationError 由 main.py 的
BankError 处理器统一翻成 400 + 中文 user_text，包一层反而会把中文 detail 压成通用错误。
"""

from __future__ import annotations

from contextlib import closing
from typing import Any, Final, Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

import app.db as db
import app.memory as memory
from app.api.routes import require_api_token
from app.proactive import default_user_id

router = APIRouter(
    prefix="/api/memory",
    tags=["memory"],
    dependencies=[Depends(require_api_token)],
)

#: 一次最多回多少条：演示库的记忆量很小，50 足够，也避免接口被刷。
LIST_LIMIT: Final[int] = 50
#: source 是「谁写的凭据」，不是正文，长成这样足够。
SOURCE_MAX: Final[int] = 120


class MemoryWriteRequest(BaseModel):
    """写一条记忆：键/值长度与 app.memory 的常量保持一致，超了在入口就 422。"""

    key: str = Field(min_length=1, max_length=memory.KEY_MAX, description="短标签")
    value: str = Field(min_length=1, max_length=memory.VALUE_MAX, description="一句话内容")
    kind: Literal["fact", "preference", "profile"] = Field(
        default="fact", description="fact=事实 / preference=偏好 / profile=画像"
    )
    source: str = Field(default="", max_length=SOURCE_MAX, description="来源，如 user")


def _conn() -> Any:
    return db.connect()


@router.get("")
def list_memory() -> dict[str, Any]:
    """这个用户现在被记住了什么。prompt_block 原样给前端，方便对照提示词里到底注入了什么。"""
    with closing(_conn()) as conn:
        user_id = default_user_id(conn)
        facts = memory.all_facts(conn, user_id=user_id, limit=LIST_LIMIT)
        return {
            "ok": True,
            "facts": facts,
            "count": len(facts),
            "prompt_block": memory.prompt_block(conn, user_id=user_id),
        }


@router.post("")
def write_memory(body: MemoryWriteRequest) -> dict[str, Any]:
    """手动记一条；同一个 key 覆盖旧的，不新增长期记忆行。"""
    with closing(_conn()) as conn:
        user_id = default_user_id(conn)
        fact = memory.remember(
            conn,
            user_id=user_id,
            key=body.key,
            value=body.value,
            kind=body.kind,
            source=body.source or "user",
        )
        return {"ok": True, "fact": fact}


@router.delete("/{key}")
def delete_memory(key: str) -> dict[str, Any]:
    """忘掉一条；本来就没有也返回 ok，removed=False 说明没什么可忘的。"""
    with closing(_conn()) as conn:
        user_id = default_user_id(conn)
        result = memory.forget(conn, user_id=user_id, key=key)
        return {"ok": True, "key": result["key"], "removed": result["removed"]}


__all__ = ["LIST_LIMIT", "MemoryWriteRequest", "router"]
