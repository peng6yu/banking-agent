"""后台监测（P1-8）：节拍器真的会推进，但只碰用户自己打开了开关的目标。

这三条边界是本轮补的实现，也是这组用例盯的东西：
1. 没打开监测开关的目标，后台一次都不该跑（否则就是"偷偷的管家"）；
2. 同一个目标按 PROACTIVE_MONITOR_MS 节流，second tick 不该重复算；
3. monitor_runs.trigger 必须能区分"后台节拍"和"用户手动点"，审计同理。
再加上一条红线：监测只写提醒与运行记录，不动一分钱。
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Any

import pytest

import app.proactive as proactive
from app import db
from app.planner import ticker
from scripts.seed import seed

SECRET = "c" * 64
FROZEN_MS = int(datetime(2026, 10, 23, 12, 0).timestamp() * 1000)
TARGET_DATE = "2027-04-30"


@pytest.fixture()
def bank(tmp_path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CAPABILITY_SECRET", SECRET)
    monkeypatch.setattr(db, "DEFAULT_DB_PATH", tmp_path / "bank.db")
    conn = db.connect()
    db.init_db(conn)
    seed(conn)
    user = conn.execute("SELECT id FROM users ORDER BY created_ms, id LIMIT 1").fetchone()
    account = conn.execute(
        "SELECT id FROM accounts WHERE user_id = ? AND type = 'debit' ORDER BY id LIMIT 1",
        (user["id"],),
    ).fetchone()
    try:
        yield conn, str(user["id"]), str(account["id"])
    finally:
        conn.close()


def _goal(conn: sqlite3.Connection, user_id: str, name: str = "半年存 6000 元") -> str:
    goal = proactive.create_goal(
        conn,
        user_id=user_id,
        name=name,
        target_fen=600_000,
        target_date=TARGET_DATE,
        saved_fen=50_000,
        progress_tolerance=10,
    )
    return str(goal["goal"]["goal_id"])


def _balance(conn: sqlite3.Connection, account_id: str) -> int:
    row = conn.execute(
        "SELECT balance_fen FROM accounts WHERE id = ?", (account_id,)
    ).fetchone()
    return int(row["balance_fen"])


def test_goal_without_the_monitor_switch_is_never_run_in_the_background(
    bank: tuple[sqlite3.Connection, str, str],
) -> None:
    conn, user_id, _account = bank
    goal_id = _goal(conn, user_id)
    proactive.update_goal(conn, goal_id, user_id=user_id, monitor_enabled=False)

    result = proactive.run_due_monitors(conn, now=FROZEN_MS, interval_ms=0)

    assert result["checked"] == 0
    assert result["skipped_no_rule"] == 1
    assert result["runs"] == []
    assert proactive.list_runs(conn) == []


def test_enabled_goal_is_monitored_and_then_throttled(
    bank: tuple[sqlite3.Connection, str, str],
) -> None:
    conn, user_id, _account = bank
    _goal(conn, user_id)

    first = proactive.run_due_monitors(conn, now=FROZEN_MS, interval_ms=proactive.DEFAULT_MONITOR_INTERVAL_MS)
    assert first["checked"] == 1
    assert first["failed"] == 0
    assert first["skipped_throttled"] == 0
    assert first["runs"][0]["status"] == "ok"
    assert first["trigger"] == proactive.TRIGGER_SCHEDULER

    row = conn.execute("SELECT last_monitored_ms FROM savings_goals").fetchone()
    assert int(row["last_monitored_ms"]) == FROZEN_MS

    second = proactive.run_due_monitors(conn, now=FROZEN_MS + 60_000, interval_ms=proactive.DEFAULT_MONITOR_INTERVAL_MS)
    assert second["checked"] == 0
    assert second["skipped_throttled"] == 1


def test_interval_env_is_clamped_and_bad_values_fall_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(proactive.MONITOR_INTERVAL_ENV, "1000")
    assert proactive.monitor_interval_ms() == proactive.MIN_MONITOR_INTERVAL_MS
    monkeypatch.setenv(proactive.MONITOR_INTERVAL_ENV, "abc")
    assert proactive.monitor_interval_ms() == proactive.DEFAULT_MONITOR_INTERVAL_MS
    monkeypatch.setenv(proactive.MONITOR_INTERVAL_ENV, "7200000")
    assert proactive.monitor_interval_ms() == 7_200_000


def test_background_and_manual_runs_are_labelled_differently(
    bank: tuple[sqlite3.Connection, str, str],
) -> None:
    conn, user_id, _account = bank
    goal_id = _goal(conn, user_id)

    manual = proactive.run_monitor(conn, goal_id=goal_id)
    assert manual["status"] == "ok"
    scheduled = proactive.run_due_monitors(conn, now=FROZEN_MS, interval_ms=0)
    assert scheduled["runs"][0]["status"] == "ok"

    runs = proactive.list_runs(conn)
    triggers = {run["run_id"]: run["trigger"] for run in runs}
    assert triggers[manual["run_id"]] == proactive.TRIGGER_MANUAL
    assert triggers[scheduled["runs"][0]["run_id"]] == proactive.TRIGGER_SCHEDULER
    labels = {run["trigger"]: run["trigger_label"] for run in runs}
    assert labels[proactive.TRIGGER_MANUAL] == "手动触发"
    assert labels[proactive.TRIGGER_SCHEDULER] == "后台节拍"

    audit_rows = conn.execute(
        "SELECT payload_json FROM audit_log WHERE event_type = 'proactive.monitor_run'"
    ).fetchall()
    import json as _json

    triggers_in_audit = {_json.loads(row["payload_json"]).get("trigger") for row in audit_rows}
    assert triggers_in_audit == {proactive.TRIGGER_MANUAL, proactive.TRIGGER_SCHEDULER}


def test_monitoring_never_moves_money(bank: tuple[sqlite3.Connection, str, str]) -> None:
    conn, user_id, account_id = bank
    _goal(conn, user_id)
    before = _balance(conn, account_id)

    proactive.run_due_monitors(conn, now=FROZEN_MS, interval_ms=0)

    assert _balance(conn, account_id) == before


def test_one_broken_goal_does_not_stop_the_others(
    bank: tuple[sqlite3.Connection, str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    conn, user_id, _account = bank
    broken = _goal(conn, user_id, name="会失败的目标")
    _goal(conn, user_id, name="正常目标")
    real = proactive.run_monitor

    def flaky(conn, *, goal_id=None, trigger="manual", **kwargs: Any):
        if goal_id == broken:
            raise RuntimeError("MONITOR_BOOM")
        return real(conn, goal_id=goal_id, trigger=trigger, **kwargs)

    monkeypatch.setattr(proactive, "run_monitor", flaky)
    result = proactive.run_due_monitors(conn, now=FROZEN_MS, interval_ms=0)

    assert result["checked"] == 2
    assert result["failed"] == 1
    assert len(result["runs"]) == 1
    assert result["runs"][0]["status"] == "ok"


def test_ticker_exposes_the_monitor_tick(bank: tuple[sqlite3.Connection, str, str]) -> None:
    """节拍器这个入口要真的能跑一次：不是只有单元函数在。"""
    conn, user_id, _account = bank
    _goal(conn, user_id)

    result = ticker.run_monitor_once(now=FROZEN_MS)

    assert result["checked"] >= 1
    assert result["trigger"] == proactive.TRIGGER_SCHEDULER
    assert "只监测" in result["note"]
