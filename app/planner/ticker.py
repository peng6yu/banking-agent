"""后台节拍器：同一拍里推进三类到点任务。

三类任务（各自独立，失败互不牵连）：
1. **预约转账**（app/tools/scheduled.execute_due）——到点真的出账，走确认/令牌/
   因子保质期；抢不到租约就跳过，避免多进程重复扣款。
2. **主动财务监测**（app/proactive.run_due_monitors）——只碰用户显式打开监测开关
   的目标，按 last_monitored_ms 节流，只写提醒与运行记录，不动钱。
3. **跨场景计划**（app/plans.run_due）——check/reminder 步自动走，action 步一律
   交回用户确认，后台不代扣。**唯一例外**是用户在建生日计划时显式保存过一次
   性授权（plan_authorizations）：生日到点后先跑 birthday.run_due_authorized，
   按最新日累计重新裁决，只有绿/黄灯且计划参数与授权完全一致才自动付；红灯、
   额度不够、参数被改都只把计划暂停成待确认，绝不先扣了再说。

两种触发方式共用同一批函数：
- 后台节拍：app/main.py 的 lifespan 启动时 start()，关闭时 stop()；
- 手动端点：POST /api/scheduled/run、/api/proactive/monitor/run、/api/plans/run。

**边界（别把接入当可靠）**：这是单进程 asyncio 节拍。进程退出就停；多 worker
部署时每个 worker 都会起一条节拍，靠的是各任务自己的领取语义（预约转账有
CLAIM_LEASE_MS 租约、计划步骤用条件 UPDATE 领取、监测按 last_monitored_ms 节流），
而不是节拍器本身做了分布式协调。多进程下的可靠性没有被验证过。

对应文档：docs/06 6.11 的定时转账、docs/07 7.5 的端点表。
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Final

from app import birthday, db, plans, proactive
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

    顺序：先跑已授权的生日付款，再跑通用计划推进。先付的好处是通用推进里
    「到今天该提醒了」那一步看到的是付款后的真实状态，不会刚提醒完又自动付。
    和上面两个也分开：计划推进失败不该把预约转账或目标监测一起带停。

    返回 plans.run_due 的 {executed, count}，另加 authorized 子报告，
    保持既有调用方（tests 里断言 count）不受影响。
    """
    conn = db.connect()
    try:
        authorized = birthday.run_due_authorized(conn, now_ms=now)
        result = plans.run_due(conn, now=now)
        result["authorized"] = authorized
        return result
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
    """起后台节拍；SCHEDULED_TICK_MS=0 时不起，返回 None。

    重复调用返回已有任务而不是再起一条：两次 start() 等于同一条流水线每秒跑两遍，
    预约转账有租约挡着，但监测与计划会白跑，日志也会出现两份"同一拍"。
    """
    global _TASK
    if is_started():
        log.info("ticker.already_running")
        return _TASK
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