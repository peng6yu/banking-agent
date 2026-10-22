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

from app import db
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


async def tick_forever(interval_ms: int) -> None:
    """按间隔检查到点预约；单次失败只记日志，不停表。"""
    while True:
        await asyncio.sleep(interval_ms / 1000)
        try:
            await asyncio.to_thread(run_due_once)
        except Exception:
            log.exception("ticker.tick_failed")


def start() -> asyncio.Task[None] | None:
    """起后台节拍；SCHEDULED_TICK_MS=0 时不起，返回 None。"""
    interval = tick_interval_ms()
    if interval == TICK_OFF:
        return None
    return asyncio.create_task(tick_forever(interval))


async def stop(task: asyncio.Task[None] | None) -> None:
    """关掉后台节拍，吞掉取消异常。"""
    if task is None:
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


__all__ = ["TICK_ENV", "run_due_once", "start", "stop", "tick_forever", "tick_interval_ms"]