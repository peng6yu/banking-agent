"""长期记忆的两条写入：用户自己补一条、忘掉一条。

记忆只当默认值用：涉及金额、账户、收款人时，仍以用户当次原话为准。
"""

from __future__ import annotations

from typing import Any

import app.memory as memory_store
import app.proactive as proactive
from app.use_cases.base import write_case


@write_case
def memory_remember(
    conn: Any,
    *,
    key: str,
    value: str,
    kind: str = "fact",
    source: str = "",
) -> dict[str, Any]:
    """手动记一条；同一个 key 覆盖旧的，不新增长期记忆行。"""
    return {
        "fact": memory_store.remember(
            conn,
            user_id=proactive.default_user_id(conn),
            key=key,
            value=value,
            kind=kind,
            source=source or "user",
        )
    }


@write_case
def memory_forget(conn: Any, key: str) -> dict[str, Any]:
    """忘掉一条；本来就没有也返回 ok，removed=False 说明没什么可忘的。"""
    result = memory_store.forget(conn, user_id=proactive.default_user_id(conn), key=key)
    return {"key": result["key"], "removed": result["removed"]}


__all__ = ["memory_forget", "memory_remember"]
