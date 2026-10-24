"""后台节拍器（app/planner/ticker.py）的一致性：

这一组守的不是"能跑"，而是下面几条会被写坏的边界：

1. 启动真的起、关闭真的停——停了之后一行都不许再跑；
2. 同一拍里三类任务各跑一次，谁也不替谁跑两遍；
3. 任何一类抛异常都不能把另外两类带停，也不能让循环退出；
4. 重复 start() 不会起第二条节拍（同一个 task）；
5. SCHEDULED_TICK_MS=0 时干脆不起，is_started() 必须如实回答 False；
6. 到点任务重复触发是幂等的：同一份数据跑两遍，钱与提醒都只动一次。
"""

from __future__ import annotations

import asyncio

import pytest

import app.plans as plans
import app.proactive as proactive
from app import db
from app.clock import now_ms
from app.planner import ticker

USER_ID = "u_9f2c41ab"


@pytest.fixture(autouse=True)
def _reset_ticker():
    """每个用例前后都把全局任务句柄清干净，避免用例之间互相干扰。"""
    ticker._TASK = None
    yield
    ticker._TASK = None


def _counter(name: str, calls: dict[str, int], *, raises: bool = False):
    def _call(now=None):
        calls[name] = calls.get(name, 0) + 1
        if raises:
            raise RuntimeError(f"{name} 故意炸一次")
        return []

    return _call


def test_start_runs_all_three_jobs_and_stop_ends_them(monkeypatch) -> None:
    """起节拍 → 三类任务都跑到；关掉之后计数冻结。"""
    calls: dict[str, int] = {}
    monkeypatch.setattr(ticker, "run_due_once", _counter("due", calls))
    monkeypatch.setattr(ticker, "run_monitor_once", _counter("monitor", calls))
    monkeypatch.setattr(ticker, "run_plans_once", _counter("plans", calls))
    monkeypatch.setenv(ticker.TICK_ENV, "20")

    async def scenario():
        task = ticker.start()
        assert task is not None
        assert ticker.is_started() is True
        await asyncio.sleep(0.15)
        assert calls.get("due", 0) >= 1
        assert calls.get("monitor", 0) >= 1
        assert calls.get("plans", 0) >= 1
        await ticker.stop(task)
        assert ticker.is_started() is False
        frozen = dict(calls)
        await asyncio.sleep(0.1)
        assert calls == frozen, "节拍已经停了，不该再有任务被跑起来"
        return task

    task = asyncio.run(scenario())
    assert task.cancelled() or task.done()


def test_one_failing_job_does_not_block_the_other_two(monkeypatch) -> None:
    """预约转账炸了，监测与计划还得照跑；循环也不能退出。"""
    calls: dict[str, int] = {}
    monkeypatch.setattr(ticker, "run_due_once", _counter("due", calls, raises=True))
    monkeypatch.setattr(ticker, "run_monitor_once", _counter("monitor", calls))
    monkeypatch.setattr(ticker, "run_plans_once", _counter("plans", calls))
    monkeypatch.setenv(ticker.TICK_ENV, "20")

    async def scenario():
        task = ticker.start()
        try:
            await asyncio.sleep(0.18)
            assert calls.get("due", 0) >= 2, "失败的那一类也要按拍继续被调用（失败不等于停表）"
            assert calls.get("monitor", 0) >= 2
            assert calls.get("plans", 0) >= 2
            assert not task.done(), "单类异常不该让节拍任务结束"
        finally:
            await ticker.stop(task)

    asyncio.run(scenario())


def test_start_twice_keeps_exactly_one_task(monkeypatch) -> None:
    """重复 start() 返回同一个任务，不会起第二条流水线。"""
    calls: dict[str, int] = {}
    monkeypatch.setattr(ticker, "run_due_once", _counter("due", calls))
    monkeypatch.setattr(ticker, "run_monitor_once", _counter("monitor", calls))
    monkeypatch.setattr(ticker, "run_plans_once", _counter("plans", calls))
    monkeypatch.setenv(ticker.TICK_ENV, "25")

    async def scenario():
        first = ticker.start()
        second = ticker.start()
        assert first is second
        await asyncio.sleep(0.08)
        await ticker.stop(first)
        return first

    asyncio.run(scenario())


def test_zero_interval_disables_the_ticker(monkeypatch) -> None:
    """SCHEDULED_TICK_MS=0：不起任务，is_started() 必须说 False。"""
    monkeypatch.setenv(ticker.TICK_ENV, "0")
    assert ticker.start() is None
    assert ticker.is_started() is False


def test_stop_without_a_task_is_safe() -> None:
    """没起过也能安全关：关闭路径不该因为句柄为空而抛异常。"""
    asyncio.run(ticker.stop(None))
    assert ticker.is_started() is False


def test_plan_advance_is_idempotent_across_two_ticks(tmp_path, monkeypatch) -> None:
    """同一份到点数据连跑两拍：提醒只发一条，第二次不重复执行。"""
    monkeypatch.setattr(db, "DEFAULT_DB_PATH", tmp_path / "bank.db")
    conn = db.connect()
    try:
        db.init_db(conn)
        from scripts import seed
        seed.seed(conn)
        stamp = now_ms()
        plan_id = plans.create(
            conn,
            user_id=USER_ID,
            title="生日支出计划",
            steps=[
                {"kind": "reminder", "title": "生日快到了，先看看预算", "due_ms": stamp - 1000},
            ],
        )["plan_id"]
    finally:
        conn.close()

    first = ticker.run_plans_once(now=now_ms())
    second = ticker.run_plans_once(now=now_ms())
    assert first["count"] == 1, "第一拍应当推进那一步提醒"
    assert second["count"] == 0, "第二拍不该重复推进同一步"

    conn = db.connect()
    try:
        alerts = conn.execute(
            "SELECT COUNT(*) AS n FROM proactive_alerts WHERE reason = ?",
            (plans.REASON_PLAN_REMINDER,),
        ).fetchone()["n"]
        step = conn.execute(
            "SELECT state FROM plan_steps WHERE plan_id = ?", (plan_id,)
        ).fetchone()["state"]
        status = conn.execute(
            "SELECT status FROM plans WHERE id = ?", (plan_id,)
        ).fetchone()["status"]
    finally:
        conn.close()
    assert alerts == 1
    assert step == "done"
    assert status == "completed"


def test_monitor_job_only_touches_opted_in_goals(tmp_path, monkeypatch) -> None:
    """节拍里的监测只推进打开了开关的目标；没开的连运行记录都不产生。"""
    monkeypatch.setattr(db, "DEFAULT_DB_PATH", tmp_path / "bank.db")
    conn = db.connect()
    try:
        db.init_db(conn)
        from scripts import seed
        seed.seed(conn)
        on = proactive.create_goal(
            conn, user_id=USER_ID, name="开了监测的目标", target_fen=600_000,
            target_date="2027-04-30",
        )
        off = proactive.create_goal(
            conn, user_id=USER_ID, name="没开监测的目标", target_fen=600_000,
            target_date="2027-04-30",
        )
        with db.tx(conn):
            conn.execute(
                "UPDATE monitor_rules SET enabled = 0 WHERE goal_id = ?",
                (off["goal"]["goal_id"],),
            )
        on_id, off_id = on["goal"]["goal_id"], off["goal"]["goal_id"]
    finally:
        conn.close()

    result = ticker.run_monitor_once(now=now_ms())
    assert result["checked"] == 1

    conn = db.connect()
    try:
        marks = {
            str(row["id"]): int(row["last_monitored_ms"])
            for row in conn.execute(
                "SELECT id, last_monitored_ms FROM savings_goals WHERE id IN (?, ?)",
                (on_id, off_id),
            ).fetchall()
        }
    finally:
        conn.close()
    assert marks[on_id] > 0, "打开开关的目标应当被推进一次"
    assert marks[off_id] == 0, "没打开开关的目标连节流时间戳都不该被改写"
