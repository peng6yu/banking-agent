"""POST /api/reminders 与计划页的提醒列表：页面表单和聊天快路必须同一份数据。

为什么单独一组：
1. 页面表单如果自己拼一套落库逻辑，就会出现「表单建的提醒聊天里取消不掉」；
2. 循环提醒响过一次不能消失，否则用户以为提醒被删了；
3. 入参校验要挡住超长文案和乱填的重复规则，不能让脏数据进 plans 表。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest

import app.planner.assist as assist
import app.plans as plans
from app import db
from app.clock import now_ms
from app.main import app
from app.planner import executor
from scripts.seed import seed

BASE_URL = "http://testserver"
USER_ID = "u_9f2c41ab"


@pytest.fixture()
def bank(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(db, "DEFAULT_DB_PATH", tmp_path / "bank.db")
    conn = db.connect()
    db.init_db(conn)
    seed(conn)
    conn.close()
    executor.RUNS.clear()
    yield tmp_path / "bank.db"
    executor.RUNS.clear()


def _request(method: str, url: str, body: dict | None = None) -> httpx.Response:
    async def run() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url=BASE_URL) as http:
            return await http.request(method, url, json=body)

    return asyncio.run(run())


def get(url: str) -> httpx.Response:
    return _request("GET", url)


def post(url: str, body: dict) -> httpx.Response:
    return _request("POST", url, body)


def _reminders() -> dict:
    # 页面「计划与提醒」读的是业务视图 /api/business/plans，不是 /api/plans（后者是计划列表 CRUD）
    data = get("/api/business/plans").json()
    return data["reminders"]


def test_post_creates_a_reminder_the_plans_page_can_see(bank: Path) -> None:
    res = post(
        "/api/reminders",
        {"text": "提醒我明天上午 9 点会员续费", "repeat": "none", "hour": 9, "minute": 0},
    )
    assert res.status_code == 200, res.text
    created = res.json()
    assert created["ok"] is True
    assert created["due_ms"] > now_ms()
    assert created["due_text"].endswith("09:00")

    block = _reminders()
    assert block["count"] == 1
    assert block["empty_reason"] is None
    item = block["items"][0]
    assert item["plan_id"] == created["plan"]["plan_id"]
    assert item["title"] == "提醒我明天上午 9 点会员续费"
    assert item["state_label"] == "等待到点"


def test_reminder_list_is_empty_but_not_unavailable(bank: Path) -> None:
    block = _reminders()
    assert block["count"] == 0
    assert block["items"] == []
    # 空是正常业务状态：给一句能照做的说明，而不是「暂不可用」
    assert block["empty_reason"]


def test_repeating_reminder_from_the_form_gets_a_label(bank: Path) -> None:
    res = post("/api/reminders", {"text": "提醒我每天盯会员续费", "repeat": "daily", "hour": 8, "minute": 30})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["repeat"] == "daily"
    assert body["repeat_label"] == "每天 08:30"
    assert _reminders()["items"][0]["repeat_label"] == "每天 08:30"


@pytest.mark.parametrize(
    "body",
    [
        {"text": "提醒我会员续费", "repeat": "hourly"},  # 只支持日/周/月
        {"text": "", "repeat": "none"},  # 空文案
        {"text": "x" * 41, "repeat": "none"},  # 超长文案
        {"text": "提醒我会员续费", "repeat": "daily", "hour": 24},  # 小时越界
        {"text": "提醒我会员续费", "repeat": "none", "due_ms": -1},  # 负时间戳
        {"text": "提醒我会员续费", "repeat": "none", "unknown": 1},  # 多传字段
    ],
)
def test_bad_input_is_rejected(bank: Path, body: dict) -> None:
    res = post("/api/reminders", body)
    # 全局异常映射把 pydantic 校验错折成 400 + VALIDATION，前端只认 user_text
    assert res.status_code == 400, res.text
    assert res.json()["code"] == "VALIDATION"
    assert _reminders()["count"] == 0


def test_daily_reminder_rearms_after_firing_instead_of_disappearing(bank: Path) -> None:
    conn = db.connect()
    try:
        created = assist.create_reminder(
            conn, text="提醒我每天盯会员续费", user_id=USER_ID, repeat="daily", hour=8, minute=30
        )
        plan_id = created["plan"]["plan_id"]
        stamp = now_ms()
        # 把下一次触发挪到过去，等价于「已经到点了」
        conn.execute(
            "UPDATE plan_steps SET due_ms = ? WHERE plan_id = ? AND kind = 'reminder'",
            (stamp - 60_000, plan_id),
        )
        conn.commit()
        plans.run_due(conn, now=stamp)

        step = conn.execute(
            "SELECT state, due_ms FROM plan_steps WHERE plan_id = ? AND kind = 'reminder'",
            (plan_id,),
        ).fetchone()
        # 循环提醒响过之后仍然 pending，下一次时间往后挪
        assert str(step["state"]) == "pending"
        assert int(step["due_ms"]) > stamp

        status = conn.execute("SELECT status FROM plans WHERE id = ?", (plan_id,)).fetchone()
        assert str(status["status"]) == "active"

        events = [
            json.loads(row["payload_json"] or "{}")
            for row in conn.execute(
                "SELECT payload_json FROM audit_log WHERE event_type = 'plan.step_done'"
            ).fetchall()
        ]
        assert any(event.get("repeat") == "daily" for event in events)
    finally:
        conn.close()

    # 页面上还看得见这条提醒，只是下次时间更新了
    block = _reminders()
    assert block["count"] == 1
    assert block["items"][0]["plan_id"] == plan_id


def test_once_reminder_finishes_and_leaves_the_active_list(bank: Path) -> None:
    conn = db.connect()
    try:
        created = assist.create_reminder(conn, text="提醒我明天上午 9 点会员续费", user_id=USER_ID)
        plan_id = created["plan"]["plan_id"]
        stamp = now_ms()
        conn.execute(
            "UPDATE plan_steps SET due_ms = ? WHERE plan_id = ? AND kind = 'reminder'",
            (stamp - 60_000, plan_id),
        )
        conn.commit()
        plans.run_due(conn, now=stamp)
        step = conn.execute(
            "SELECT state FROM plan_steps WHERE plan_id = ? AND kind = 'reminder'", (plan_id,)
        ).fetchone()
        assert str(step["state"]) == "done"
    finally:
        conn.close()
    # 一次性提醒响过就不再是「进行中的提醒」
    assert _reminders()["count"] == 0
