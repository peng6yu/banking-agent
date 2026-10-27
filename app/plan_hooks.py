"""计划生命周期钩子注册表（第 1 层，只依赖标准库与日志）。

**为什么要有这一层**：`app/plans.py`（第 2 层）与 `app/planner/ticker.py`（第 4 层）
都要在「计划变了」和「到点了该自动付」这两个时刻找生日计划（`app/birthday.py`，第 5 层
——它要驱动执行器、确认卡与能力令牌）。让低层 import 高层就是反向依赖，
`scripts/arch_report.py` 会把它们列成跨层违规。

这里只提供两个函数位，把方向反过来：

  - `notify_plan_updated(conn, plan_id, status=..., changes=...)`：计划状态/参数变了，
    让关心计划的模块做预算与授权的收尾；
  - `run_due_hooks(conn, now_ms=...)`：到点推进「用户显式保存过一次性授权」的动作。

`app/birthday.py` 在自己的模块末尾把两个实现登记进来；低层只管调用，
不认识生日模块，也不 import 它。**没有登记就什么也不做**——进程里没有生日模块，
就等于没有生日计划，钩子空转是对的；这也让只读用途（导出、脚本）不必拉起执行器。

调用方的约定：

  - 注册可以有多个实现（未来的「信用卡还款计划」也走同一组时刻），按登记顺序调用；
  - 单个实现抛错不影响其它实现，也不打断调用方：这里只 log，不改状态、不回滚；
  - `run_due_hooks` 把各实现的计数合并成一个 dict，键固定为
    `checked / paid / skipped / needs_verification / revoked / expired / items / runs`，
    没有实现时这些键都在、值为 0 或空表（前端与测试不必判 None）。
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Final

log = logging.getLogger(__name__)

#: 计划变更通知：`fn(conn, plan_id, *, status, changes) -> None`
PlanUpdatedHook = Callable[..., None]
#: 到点的已授权动作：`fn(conn, *, now_ms=None, plan_id=None) -> dict`
DueAuthorizedHook = Callable[..., dict[str, Any]]

#: 合并结果里一定会出现的键；新增实现只许往这几个键里加数，不许换名
DUE_KEYS: Final[tuple[str, ...]] = (
    "checked",
    "paid",
    "skipped",
    "needs_verification",
    "revoked",
    "expired",
)

_PLAN_UPDATED: list[PlanUpdatedHook] = []
_DUE_AUTHORIZED: list[DueAuthorizedHook] = []


def register_plan_updated(fn: PlanUpdatedHook) -> None:
    """登记一个「计划变了」的实现；重复登记同一个函数只算一次。"""
    if fn not in _PLAN_UPDATED:
        _PLAN_UPDATED.append(fn)


def register_due_authorized(fn: DueAuthorizedHook) -> None:
    """登记一个「到点自动执行」的实现；重复登记同一个函数只算一次。"""
    if fn not in _DUE_AUTHORIZED:
        _DUE_AUTHORIZED.append(fn)


def registered() -> dict[str, int]:
    """当前登记了几个实现（自检与测试用，不含函数对象）。"""
    return {"plan_updated": len(_PLAN_UPDATED), "due_authorized": len(_DUE_AUTHORIZED)}


def clear_hooks() -> None:
    """把登记清空（只给测试用；生产没有任何调用点）。"""
    _PLAN_UPDATED.clear()
    _DUE_AUTHORIZED.clear()


def notify_plan_updated(
    conn: Any,
    plan_id: str,
    *,
    status: str | None = None,
    changes: tuple[str, ...] | list[str] = (),
) -> None:
    """广播「计划变了」。没有实现时直接返回；单个实现抛错只记日志。"""
    for fn in tuple(_PLAN_UPDATED):
        try:
            fn(conn, plan_id, status=status, changes=changes)
        except Exception:  # noqa: BLE001 —— 钩子失败不能把计划本身的写入带回去
            log.exception("plan_hooks.plan_updated_failed plan=%s fn=%s", plan_id, getattr(fn, "__name__", fn))


def run_due_hooks(
    conn: Any, *, now_ms: int | None = None, plan_id: str | None = None
) -> dict[str, Any]:
    """广播「到点自动执行」，把各实现的计数合并成一个 dict。"""
    merged: dict[str, Any] = {key: 0 for key in DUE_KEYS}
    merged["items"] = []
    runs: list[dict[str, Any]] = []
    for fn in tuple(_DUE_AUTHORIZED):
        try:
            report = fn(conn, now_ms=now_ms, plan_id=plan_id) or {}
        except Exception:  # noqa: BLE001
            log.exception(
                "plan_hooks.due_authorized_failed fn=%s", getattr(fn, "__name__", fn)
            )
            continue
        runs.append(report)
        for key in DUE_KEYS:
            merged[key] = int(merged[key]) + int(report.get(key) or 0)
        items = report.get("items")
        if isinstance(items, list):
            merged["items"].extend(items)
    merged["runs"] = runs
    return merged


__all__ = [
    "DUE_KEYS",
    "DueAuthorizedHook",
    "PlanUpdatedHook",
    "clear_hooks",
    "notify_plan_updated",
    "register_due_authorized",
    "register_plan_updated",
    "registered",
    "run_due_hooks",
]
