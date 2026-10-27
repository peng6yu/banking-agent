"""用例层骨架：一次写操作 = 一个事务 + 一个 Outcome。

W3 起，写操作只有一条路径：

    路由（绑定入参） -> 用例（开事务、翻译异常） -> 领域（真正的业务规则）

为什么要有这一层：
- 事务边界只在这里定。路由不再自己 db.tx(conn)，也不会再出现「有的端点包了事务、
  有的没包」这种靠记忆维持的不一致；
- 领域层继续只抛 BankError 子类；翻译成 Outcome 只做一次。路由用
  app.api.http.payload 把原始异常重新抛出去，HTTP 状态码与中文 user_text 一个字
  都不变（app/main.py 仍是唯一的异常映射点）；
- 用例只收基本类型，不 import app.api.schemas，所以「谁依赖谁」始终单向。
"""

from __future__ import annotations

import functools
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Final, TypeVar

from app import db
from app.clock import BankError

log = logging.getLogger(__name__)

#: 两种终态。没有 pending：需要用户点确认的写操作走「先建确认、再执行」两段，
#: 那是 planner 与 executor 的职责；用例层只负责确定性的写。
STATUS_OK: Final[str] = "ok"
STATUS_FAILED: Final[str] = "failed"

_F = TypeVar("_F", bound=Callable[..., Any])


@dataclass(slots=True)
class Outcome:
    """一次用例的结果：要么是可回给前端的数据，要么是一个待翻译的领域异常。"""

    status: str
    data: dict[str, Any] = field(default_factory=dict)
    error: BankError | None = None

    @property
    def ok(self) -> bool:
        return self.status == STATUS_OK


def ok(data: Mapping[str, Any] | None = None) -> Outcome:
    """成功：data 会被路由并进响应体（自动带 ok=true）。"""
    return Outcome(STATUS_OK, dict(data or {}))


def fail(error: BankError) -> Outcome:
    """失败：保留原始异常，交给 app/main.py 的处理器决定状态码与话术。"""
    return Outcome(STATUS_FAILED, {}, error)


def _as_outcome(result: Any) -> Outcome:
    if isinstance(result, Outcome):
        return result
    return ok(result)


def write_case(fn: _F) -> _F:
    """一次写用例的三样样板：开连接、开事务、把 BankError 收成 Outcome。"""

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Outcome:
        try:
            with db.open_conn() as conn, db.tx(conn):
                return _as_outcome(fn(conn, *args, **kwargs))
        except BankError as err:
            return fail(err)

    return wrapper  # type: ignore[return-value]


__all__ = ["Outcome", "STATUS_FAILED", "STATUS_OK", "fail", "ok", "write_case"]
