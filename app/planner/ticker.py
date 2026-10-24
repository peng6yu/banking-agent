"""定时转账的到点执行节拍器。

为什么需要它：app/tools/scheduled.py 的 execute_due 只负责「把到点的行划出去」，
它自己不产生时间。没有调用点的话，用户预约成功之后那笔钱永远不会动，
在演示现场看起来就是「定时转账没做完」——这正是 t26 那次 breaker.record_denial
「实现了但生产从来走不到」的同一类问题。

两种触发方式，都要在：
1. 后台节拍：应用启动时起一个 asyncio 任务，每 SCHEDULED_TICK_MS 毫秒跑一次；
2. 手动端点：POST /api/scheduled/run，评审现场想立刻看到效果时用。

边界：节拍器只调用 execute_due，不自己判时间、不自己算钱；
执行失败的预约由 execute_due 自己置 failed 并写审计。
对应文档：docs/06 6.11 的定时转账、docs/07 7.5 的端点表。
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Final

from app import db, plans, proactive
from app.tools import scheduled

log = logging.getLogger(__name__)

TICK_ENV: Final[str] = "SCHEDULED_TICK_MS"
DEFAULT_TICK_MS: Final[int] = 30_000
TICK_OFF: Final[int] = 0


def tick_interval_ms() -> int:
    """读环境变量；非法值退回默认，0 表示关掉后台节拍。"""
    raw = os.getenv(TICK_ENV, "").strip()
    if not raw:
        return DEFAULT_TICK_MS
    try:
        value = int(raw)
    except ValueError:
        log.warning("ticker.bad_interval %r", raw)
        return DEFAULT_TICK_MS
    return max(value, TICK_OFF)


def run_due_once(now: int | None = None) -> list[dict[str, Any]]:
    """执行一次到点预约，返回本次真正划出去的记录。

    now 只在测试里传；生产走 clock.now_ms()。
    """
    conn = db.connect()
    try:
        with db.tx(conn):
            return scheduled.execute_due(conn, now=now)
    finally:
        conn.close()


def run_monitor_once(now: int | None = None) -> dict[str, Any]:
    """按一次节拍推进主动财务管理（只碰用户开了监测开关的目标）。

    和 run_due_once 分开成两个函数，是因为它们失败互不牵连：
    预约转账炸了不能让储蓄目标监测跟着停，反过来也一样。
    """
    conn = db.connect()
    try:
        return proactive.run_due_monitors(conn, now=now)
    finally:
        conn.close()


def run_plans_once(now: int | None = None) -> dict[str, Any]:
    """按一次节拍推进跨场景计划（check / reminder 自动走，action 交回用户）。

    和上面两个也分开：计划推进失败不该把预约转账或目标监测一起带停。
    """
    conn = db.connect()
    try:
        return plans.run_due(conn, now=now)
    finally:
        conn.close()


async def tick_forever(interval_ms: int) -> None:
    """按间隔检查到点预约 + 推进主动监测；单次失败只记日志，不停表。"""
    while True:
        await asyncio.sleep(interval_ms / 1000)
        try:
            await asyncio.to_thread(run_due_once)
        except Exception:
            log.exception("ticker.tick_failed")
        try:
            await asyncio.to_thread(run_monitor_once)
        except Exception:
            log.exception("ticker.monitor_failed")
        try:
            await asyncio.to_thread(run_plans_once)
        except Exception:
            log.exception("ticker.plans_failed")


#: 当前后台节拍任务。保存它只为一件事：让 /api/business/plans 能如实回答
#: 「预约转账现在到底有没有人在推进」。没有这个句柄时，界面只能靠环境变量猜，
#: 猜错就会出现「显示已安排、其实没人执行」——那是最坏的一种谎报。
_TASK: asyncio.Task[None] | None = None


def start() -> asyncio.Task[None] | None:
    """起后台节拍；SCHEDULED_TICK_MS=0 时不起，返回 None。"""
    global _TASK
    interval = tick_interval_ms()
    if interval == TICK_OFF:
        return None
    _TASK = asyncio.create_task(tick_forever(interval))
    return _TASK


def is_started() -> bool:
    """后台节拍是否真的在跑（任务已起且尚未结束）。"""
    return _TASK is not None and not _TASK.done()


async def stop(task: asyncio.Task[None] | None) -> None:
    """关掉后台节拍，吞掉取消异常。"""
    global _TASK
    target = task if task is not None else _TASK
    if target is None:
        return
    target.cancel()
    try:
        await target
    except asyncio.CancelledError:
        pass
    if _TASK is target:
        _TASK = None


__all__ = [
    "TICK_ENV",
    "is_started",
    "run_due_once",
    "run_monitor_once",
    "run_plans_once",
    "start",
    "stop",
    "tick_forever",
    "tick_interval_ms",
]