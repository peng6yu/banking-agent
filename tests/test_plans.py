"""跨场景计划（P1-9）：落库、后台推进、重启恢复、action 绝不自动动钱。

这一组测试要守住的不是"计划能跑"，而是下面几条更容易被写坏的边界：

1. 状态只在库里：换一个连接（等价于进程重启）还能接着推，内存里什么都不留；
2. 幂等：同一次到点重复调用不会执行第二遍，不会重复发提醒；
3. action 步永远不会在后台扣钱——它只生成一条待确认提醒，然后把计划挂起；
4. 条件不满足要能看见：重试次数用完就 failed + 审计，不许无声无息地消失。
"""

from __future__ import annotations

import asyncio
import json

import pytest

import app.plans as plans
import app.proactive as proactive
from app import db
from app.clock import now_ms
from app.planner import ticker

USER_ID = "u_9f2c41ab"
DAY_MS = 86_400_000


@pytest.fixture()
def bank(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DEFAULT_DB_PATH", tmp_path / "bank.db")
    conn = db.connect()
    db.init_db(conn)
    from scripts import seed
    seed.seed(conn)
    yield conn
    conn.close()


def _goal(conn, *, target_fen=600_000, saved_fen=0) -> str:
    goal_id = "sg_test01"
    stamp = now_ms()
    conn.execute(
        "INSERT INTO savings_goals (id, user_id, name, target_fen, target_date, saved_fen,"
        " status, created_ms, updated_ms, last_monitored_ms)"
        " VALUES (?, ?, '半年存 6000 元', ?, '2027-04-30', ?, 'active', ?, ?, 0)",
        (goal_id, USER_ID, target_fen, saved_fen, stamp, stamp),
    )
    return goal_id


def _plan(conn, steps, *, title="生日支出计划"):
    return plans.create(conn, user_id=USER_ID, title=title, steps=steps)


def _kinds(conn) -> list[str]:
    return [
        str(row["event_type"])
        for row in conn.execute("SELECT event_type FROM audit_log ORDER BY seq").fetchall()
    ]


def _balance(conn) -> int:
    return int(
        conn.execute("SELECT balance_fen FROM accounts WHERE type='debit'").fetchone()["balance_fen"]
    )


# --------------------------------------------------------------- 落库与读回


def test_plan_is_written_to_the_database_and_read_back_in_order(bank):
    created = _plan(
        bank,
        [
            {"kind": "check", "title": "看储蓄进度", "payload": {"check": "balance_at_least", "amount_fen": 100}},
            {"kind": "reminder", "title": "提醒转存", "payload": {"text": "记得转存 500 元"}},
        ],
    )
    assert created["status"] == "active"
    assert [step["seq"] for step in created["steps"]] == [1, 2]
    assert [step["state"] for step in created["steps"]] == ["pending", "pending"]
    assert created["boundary"] == plans.ACTION_BOUNDARY
    assert "plan.created" in _kinds(bank)

    # 换一个连接读：证明状态不是内存里的对象
    other = db.connect()
    try:
        again = plans.view_plan(other, created["plan_id"])
    finally:
        other.close()
    assert again is not None
    assert again["title"] == created["title"]
    assert again["step_count"] == 2


def test_step_validation_rejects_unknown_kinds_and_missing_payload(bank):
    from app.clock import ValidationError

    with pytest.raises(ValidationError):
        _plan(bank, [{"kind": "sorcery", "title": "x"}])
    with pytest.raises(ValidationError):
        _plan(bank, [{"kind": "check", "title": "x", "payload": {"check": "savings_progress"}}])
    with pytest.raises(ValidationError):
        _plan(bank, [{"kind": "action", "title": "x", "payload": {}}])
    # 提醒不写正文是允许的：正文留空就退回用标题，别为了校验逼用户写两遍。
    made = _plan(bank, [{"kind": "reminder", "title": "只用标题的提醒", "payload": {}}])
    assert made["steps"][0]["payload"] == {}
    with pytest.raises(ValidationError):
        _plan(bank, [{"kind": "action", "title": "x", "payload": {"tool": "transfer", "params": ["不是对象"]}}])


# --------------------------------------------------------------- 三类步骤


def test_check_step_completes_when_the_goal_is_already_reached(bank):
    goal_id = _goal(bank, target_fen=600_000, saved_fen=600_000)
    created = _plan(
        bank,
        [{"kind": "check", "title": "目标是否达成", "payload": {"check": "savings_progress", "goal_id": goal_id}}],
    )
    result = plans.run_due(bank)
    assert result["count"] == 1
    assert result["executed"][0]["outcome"] == "done"
    view = plans.view_plan(bank, created["plan_id"])
    assert view["steps"][0]["state"] == "done"
    assert view["status"] == "completed"
    assert "plan.step_done" in _kinds(bank)


def test_unmet_check_retries_then_fails_visibly(bank):
    goal_id = _goal(bank, target_fen=600_000, saved_fen=0)
    created = _plan(
        bank,
        [{
            "kind": "check",
            "title": "目标是否达成",
            "payload": {"check": "savings_progress", "goal_id": goal_id, "repeat_ms": 1000},
            "max_attempts": 2,
        }],
    )
    first = plans.run_due(bank)
    assert first["executed"][0]["outcome"] == "pending"
    step = plans.view_plan(bank, created["plan_id"])["steps"][0]
    assert step["attempts"] == 1
    assert step["state"] == "pending"
    assert "条件还没满足" in step["last_error"]
    # 被推到了下一次：同一时刻再跑不会立刻重复
    assert plans.run_due(bank)["count"] == 0

    second = plans.run_due(bank, now=step["due_ms"] + 1)
    assert second["executed"][0]["outcome"] == "failed"
    view = plans.view_plan(bank, created["plan_id"])
    assert view["steps"][0]["state"] == "failed"
    assert "连续 2 次" in view["steps"][0]["last_error"]
    assert view["status"] == "failed"
    assert "plan.step_failed" in _kinds(bank)


def test_reminder_step_creates_a_proactive_alert_and_never_repeats(bank):
    created = _plan(
        bank,
        [{"kind": "reminder", "title": "生日前提醒", "payload": {"text": "下周三妈妈生日，先留 500 元"}}],
    )
    result = plans.run_due(bank)
    assert result["executed"][0]["outcome"] == "done"
    alerts = proactive.list_alerts(bank, user_id=USER_ID, status="all")
    mine = [item for item in alerts if item["reason"] == plans.REASON_PLAN_REMINDER]
    assert len(mine) == 1
    assert mine[0]["evidence"]["plan_id"] == created["plan_id"]
    assert "妈妈生日" in mine[0]["suggestion"]

    # 同一个到点时刻再跑一次：不许重复发第二条
    assert plans.run_due(bank, now=now_ms() + 1000)["count"] == 0
    again = [i for i in proactive.list_alerts(bank, user_id=USER_ID, status="all") if i["reason"] == plans.REASON_PLAN_REMINDER]
    assert len(again) == 1


def test_action_step_hands_back_to_the_user_and_moves_no_money(bank):
    before = _balance(bank)
    payee = bank.execute("SELECT id FROM payees WHERE name='李静'").fetchone()
    created = _plan(
        bank,
        [{
            "kind": "action",
            "title": "到期转 300 元给李静",
            "payload": {
                "tool": "transfer",
                "params": {"payee_id": str(payee["id"]), "amount_fen": 30_000},
                "prompt": "到期了：要转 300 元给李静，请回来看确认卡片",
            },
        }],
    )
    result = plans.run_due(bank)
    assert result["executed"][0]["outcome"] == "skipped"
    assert result["executed"][0]["detail"]["needs_user"] is True

    view = plans.view_plan(bank, created["plan_id"])
    assert view["status"] == "paused"
    assert view["steps"][0]["state"] == "skipped"
    assert view["steps"][0]["needs_user"] is True
    # 一分钱都不能动：后台不代扣，也不写 hold
    assert _balance(bank) == before
    assert bank.execute("SELECT COUNT(*) AS n FROM holds").fetchone()["n"] == 0
    assert bank.execute("SELECT COUNT(*) AS n FROM transactions WHERE channel='app' AND direction='out' AND amount_fen=30000").fetchone()["n"] == 0

    alerts = [i for i in proactive.list_alerts(bank, user_id=USER_ID, status="all") if i["reason"] == plans.REASON_PLAN_ACTION]
    assert len(alerts) == 1
    assert alerts[0]["evidence"]["tool"] == "transfer"
    assert "确认" in alerts[0]["suggestion"]

    # 再跑也不会重复提醒
    assert plans.run_due(bank, now=now_ms() + 60_000)["count"] == 0


# --------------------------------------------------------------- 修改 / 取消 / 恢复


def test_pending_steps_can_be_rescheduled_but_finished_ones_cannot(bank):
    created = _plan(
        bank,
        [
            {"kind": "reminder", "title": "第一条", "payload": {"text": "a"}},
            {"kind": "reminder", "title": "第二条", "payload": {"text": "b"}},
        ],
    )
    future = now_ms() + 5 * DAY_MS
    updated = plans.update(bank, created["plan_id"], user_id=USER_ID, steps=[{"seq": 2, "due_ms": future, "title": "改过的第二条"}])
    assert updated["steps"][1]["due_ms"] == future
    assert updated["steps"][1]["title"] == "改过的第二条"

    plans.run_due(bank)
    with pytest.raises(Exception):
        plans.update(bank, created["plan_id"], user_id=USER_ID, steps=[{"seq": 1, "title": "来不及了"}])
    assert "plan.updated" in _kinds(bank)


def test_cancelled_plan_is_never_executed(bank):
    created = _plan(bank, [{"kind": "reminder", "title": "会被取消", "payload": {"text": "x"}}])
    plans.update(bank, created["plan_id"], user_id=USER_ID, status="cancelled")
    assert plans.run_due(bank)["count"] == 0
    assert plans.view_plan(bank, created["plan_id"])["steps"][0]["state"] == "pending"


def test_plan_survives_a_restart_and_runs_from_the_database(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DEFAULT_DB_PATH", tmp_path / "bank.db")
    conn = db.connect()
    db.init_db(conn)
    from scripts import seed
    seed.seed(conn)
    created = plans.create(
        conn,
        user_id=USER_ID,
        title="重启后还要在",
        steps=[{"kind": "reminder", "title": "重启后的提醒", "payload": {"text": "我还在"}}],
    )
    conn.close()

    # 新进程等价物：重新 connect，旧对象全部丢掉
    fresh = db.connect()
    try:
        assert plans.view_plan(fresh, created["plan_id"]) is not None
        result = plans.run_due(fresh)
        assert result["count"] == 1
        assert plans.view_plan(fresh, created["plan_id"])["status"] == "completed"
    finally:
        fresh.close()


# --------------------------------------------------------------- 接进节拍与接口


def test_ticker_advances_plans(bank):
    _plan(bank, [{"kind": "reminder", "title": "节拍提醒", "payload": {"text": "由节拍推进"}}])
    result = ticker.run_plans_once()
    assert result["count"] == 1


def test_plans_are_part_of_the_plans_view(bank):
    from app.api import business

    _plan(bank, [{"kind": "reminder", "title": "出现在计划中心", "payload": {"text": "x"}}])
    view = business.plans_view(bank)
    assert view["plans"]["count"] == 1
    assert view["plans"]["items"][0]["steps"][0]["kind"] == "reminder"
    assert "plan_action_pending" not in view["plans"]["boundary"]


def _http(method: str, url: str, body=None):
    import httpx
    from app.main import app

    async def go():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.request(method, url, json=body)

    return asyncio.run(go())


def test_plan_endpoints_create_list_patch_and_run(bank):
    payload = {
        "title": "接口建的计划",
        "steps": [
            {"kind": "reminder", "title": "接口提醒", "payload": {"text": "来自接口"}},
            {"kind": "check", "title": "余额检查", "payload": {"check": "balance_at_least", "amount_fen": 100}},
        ],
    }
    created = _http("POST", "/api/plans", payload)
    assert created.status_code == 200, created.text
    plan_id = created.json()["plan"]["plan_id"]
    assert created.json()["plan"]["step_count"] == 2

    listed = _http("GET", "/api/plans")
    assert listed.status_code == 200
    assert any(item["plan_id"] == plan_id for item in listed.json()["plans"])

    detail = _http("GET", f"/api/plans/{plan_id}")
    assert detail.json()["plan"]["title"] == "接口建的计划"

    ran = _http("POST", "/api/plans/run", {"plan_id": plan_id})
    assert ran.status_code == 200
    assert ran.json()["count"] >= 1

    cancelled = _http("PATCH", f"/api/plans/{plan_id}", {"status": "cancelled"})
    assert cancelled.json()["plan"]["status"] == "cancelled"

    missing = _http("GET", "/api/plans/pl_missing")
    assert missing.status_code == 404
