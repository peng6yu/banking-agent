"""路由的最后一米：把用例结果翻成响应体，或者把领域异常原样抛给异常映射。"""

from __future__ import annotations

from typing import Any

from app.use_cases.base import Outcome


def payload(outcome: Outcome) -> dict[str, Any]:
    """成功就回带 ok=true 的响应体；失败就把原始 BankError 抛出去。

    抛原始异常（而不是自己拼 4xx）是为了让 app/main.py 的处理器继续做唯一的
    状态码翻译：改一处话术，所有端点一起变。
    """
    if outcome.error is not None:
        raise outcome.error
    return {"ok": True, **outcome.data}


__all__ = ["payload"]
