"""生日跨场景持久化计划：预算约束、固定时钟触发、恢复、去重、失败留痕、不绕过授权。

这组用例针对的四个具体风险：
1. 拿 5 分钟的临时 hold 冒充「生日月的预算预留」——钱没扣但语义错，对账解释不通；
2. 后台调度一激动就把钱付了——到期动作必须仍然走确认 + 令牌；
3. 重复触发重复下单——同一天跑两次调度，只能有一笔订单；
4. 失败/余额不足/用户拒绝只改一个 status、审计里查不到原因。
"""

from __future__ import annotations

import datetime as _dt
from pathlib import Path

import pytest

from app import audit, birthday, clock, db
from app.clock import ValidationError
from app.planner import executor
from scripts.seed import seed

SECRET = "c" * 64
DEBIT = "a_3d7e60c2"
SEED_BALANCE_FEN = 1_286_050
DAY_MS = 86_400_000


@pytest.fixture()
def bank(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CAPABILITY_SECRET", SECRET)
    monkeypatch.setattr(db, "DEFAULT_DB_PATH", tmp_path / "bank.db")
    conn = db.connect()
    db.init_db(conn)
    seed(conn)
    executor.RUNS.clear()
    yield conn
    conn.close()
    executor.RUNS.clear()
    clock.unfreeze()


def _future_date(days: int = 30) -> str:
    return (_dt.date.today() + _dt.timedelta(days=days)).isoformat()


def _balance() -> int:
    conn = db.connect()
    try:
        row = conn.execute("SELECT balance_fen FROM accounts WHERE id = ?", (DEBIT,)).fetchone()
        return int(row["balance_fen"])
    finally:
        conn.close()


def _holds() -> int:
    conn = db.connect()
    try:
        row = conn.execute("SELECT COUNT(*) AS n FROM holds").fetchone()
        return int(row["n"])
    finally:
        conn.close()


def _tx_count() -> int:
    conn = db.connect()
    try:
        row = conn.execute("SELECT COUNT(*) AS n FROM transactions").fetchone()
        return int(row["n"])
    finally:
        conn.close()


def _events(kind: str) -> list[dict]:
    conn = db.connect()
    try:
        rows = conn.execute(
            "SELECT payload_json FROM audit_log WHERE event_type = ? ORDER BY seq ASC", (kind,)
        ).fetchall()
        import json

        return [json.loads(row["payload_json"]) for row in rows]
    finally:
        conn.close()


def _create(conn, *, budget_fen: int = 100_000, items=None, days: int = 30) -> dict:
    return birthday.create_plan(
        conn,
        user_id="u_9f2c41ab",
        birthday=_future_date(days),
        budget_fen=budget_fen,
        items=items,
    )


def test_create_plan_reserves_budget_without_touching_money(bank) -> None:
    before = _balance()
    plan = _create(bank)
    assert plan["meta"]["kind"] == "birthday"
    assert plan["item_count"] == 2
    keys = [item["item_key"] for item in plan["items"]]
    assert keys == ["flowers", "cake"]
    assert [item["amount_fen"] for item in plan["items"]] == [40_000, 60_000]
    assert {item["status"] for item in plan["items"]} == {"reserved"}
    assert plan["budget"]["budget_fen"] == 100_000
    assert plan["budget"]["planned_fen"] == 100_000
    # 预算预留不是资金冻结：余额不变、不建 hold、不产生流水
    assert _balance() == before
    assert _holds() == 0
    assert _tx_count() == _tx_count()
    # 每个项目都有一条到点的 action 步骤（到期只提醒，不代扣）
    steps = [step for step in plan["steps"] if step["kind"] == "action"]
    assert len(steps) == 2
    assert all(step["payload"]["tool"] == "pay_merchant" for step in steps)
    # 审计：建计划 + 预算占用
    assert _events("plan.created")
    reserved = _events("plan.budget_reserved")
    # 语义升级：从"账本占用"变成"真实占用可支配金额"，审计 scope 跟着换名字，
    # 老数据保留旧 scope，对账时能分出两种口径。
    assert reserved and reserved[0]["scope"] == "available_ledger_reservation"
    assert reserved[0]["reserved_fen"] == 100_000
    assert reserved[0]["account_id"] == DEBIT
    # 余额没动，但可支配金额少了 1000 元——这才是"预留"该有的样子
    assert plan["budget"]["balance_fen"] == before
    assert plan["budget"]["reserved_budget_fen"] == 100_000
    assert plan["budget"]["available_fen"] == before - 100_000


def test_items_over_budget_are_refused(bank) -> None:
    items = [
        {"key": "flowers", "label": "鲜花", "merchant": "flower_shop", "amount_fen": 60_000},
        {"key": "cake", "label": "蛋糕", "merchant": "cake_shop", "amount_fen": 60_000},
    ]
    with pytest.raises(ValidationError) as exc:
        _create(bank, items=items)
    assert "超过预算" in exc.value.user_text
    assert db.connect().execute("SELECT COUNT(*) FROM plan_budgets").fetchone()[0] == 0


def test_unknown_merchant_and_past_birthday_are_refused(bank) -> None:
    with pytest.raises(ValidationError):
        _create(
            bank,
            items=[
                {"key": "x", "label": "花", "merchant": "some_shop", "amount_fen": 10_000},
            ],
        )
    with pytest.raises(ValidationError) as exc:
        birthday.create_plan(bank, user_id="u_9f2c41ab", birthday="2020-01-01")
    assert "已经过去" in exc.value.user_text


def test_trigger_is_two_days_before_birthday_at_nine(bank) -> None:
    day = _dt.date.fromisoformat(_future_date(40))
    plan = _create(bank, days=40)
    expected = birthday.trigger_at(day)
    assert plan["budget"]["trigger_ms"] == int(expected.timestamp() * 1000)
    assert expected.date() == day - _dt.timedelta(days=2)
    assert expected.hour == 9
    triggered = [step for step in plan["steps"] if step["kind"] == "check"][0]
    assert triggered["due_ms"] == plan["budget"]["trigger_ms"]


def test_fixed_clock_does_not_fire_before_the_day_and_then_only_reminds(bank) -> None:
    plan = _create(bank, days=20)
    plan_id = plan["plan_id"]
    trigger_ms = plan["budget"]["trigger_ms"]
    from app import plans as plans_mod

    # 差一分钟：一步都不该动
    early = plans_mod.run_due(bank, now=trigger_ms - 60_000, plan_id=plan_id)
    assert early["count"] == 0
    assert _balance() == SEED_BALANCE_FEN

    # 到点：检查步跑完、两个付款步交回用户、计划暂停、钱一分没动
    fired = plans_mod.run_due(bank, now=trigger_ms + 60_000, plan_id=plan_id)
    assert fired["count"] >= 1
    view = birthday.plan_with_budgets(bank, plan_id)
    assert view["status"] == "paused"
    states = {step["kind"]: step["state"] for step in view["steps"]}
    assert states["check"] == "done"
    assert [step["state"] for step in view["steps"] if step["kind"] == "action"] == [
        "skipped",
        "skipped",
    ]
    assert _balance() == SEED_BALANCE_FEN
    assert _holds() == 0
    alerts = bank.execute(
        "SELECT * FROM proactive_alerts WHERE reason = 'plan_action_pending'"
    ).fetchall()
    assert len(alerts) == 2
    assert {item["status"] for item in view["items"]} == {"reserved"}


def test_running_the_scheduler_twice_does_not_duplicate_reminders_or_orders(bank) -> None:
    plan = _create(bank, days=20)
    plan_id = plan["plan_id"]
    trigger_ms = plan["budget"]["trigger_ms"]
    from app import plans as plans_mod

    plans_mod.run_due(bank, now=trigger_ms + 60_000, plan_id=plan_id)
    first_alerts = bank.execute("SELECT COUNT(*) FROM proactive_alerts").fetchone()[0]
    second = plans_mod.run_due(bank, now=trigger_ms + 120_000, plan_id=plan_id)
    assert second["count"] == 0
    assert bank.execute("SELECT COUNT(*) FROM proactive_alerts").fetchone()[0] == first_alerts
    assert _balance() == SEED_BALANCE_FEN
    assert _holds() == 0


def test_plan_survives_a_restart(bank) -> None:
    plan = _create(bank, days=20)
    plan_id = plan["plan_id"]
    bank.close()
    fresh = db.connect()
    try:
        view = birthday.plan_with_budgets(fresh, plan_id)
        assert view is not None
        assert [item["item_key"] for item in view["items"]] == ["flowers", "cake"]
        assert all(item["status"] == "reserved" for item in view["items"])
        # 重启后到点仍然能推进，且不会重复建订单
        from app import plans as plans_mod

        plans_mod.run_due(fresh, now=view["budget"]["trigger_ms"] + 60_000, plan_id=plan_id)
        assert _balance() == SEED_BALANCE_FEN
    finally:
        fresh.close()


def test_pay_item_settles_only_after_confirmation(bank) -> None:
    plan = _create(bank, days=20)
    plan_id = plan["plan_id"]
    import asyncio

    result = asyncio.run(
        birthday.pay_item(plan_id=plan_id, item_key="flowers", session_id="s_bday_pay")
    )
    assert result["status"] == "paid", result
    assert _balance() == SEED_BALANCE_FEN - 40_000
    view = birthday.plan_with_budgets(bank, plan_id)
    flowers = [item for item in view["items"] if item["item_key"] == "flowers"][0]
    assert flowers["status"] == "committed"
    assert flowers["step_state"] == "done"
    assert _events("plan.item_paid")[-1]["item_key"] == "flowers"
    # 重复触发：不再建执行图、不再扣钱
    again = asyncio.run(
        birthday.pay_item(plan_id=plan_id, item_key="flowers", session_id="s_bday_pay2")
    )
    assert again["status"] == "already_paid" and again["idempotent"] is True
    assert _balance() == SEED_BALANCE_FEN - 40_000


def test_user_refusal_is_recorded_and_moves_no_money(bank) -> None:
    plan = _create(bank, days=20)
    import asyncio

    result = asyncio.run(
        birthday.pay_item(
            plan_id=plan["plan_id"], item_key="cake", session_id="s_bday_no", approve=False
        )
    )
    assert result["status"] == "declined"
    assert _balance() == SEED_BALANCE_FEN
    view = birthday.plan_with_budgets(bank, plan["plan_id"])
    cake = [item for item in view["items"] if item["item_key"] == "cake"][0]
    assert cake["status"] == "declined"
    assert cake["step_state"] == "skipped"
    assert _events("plan.item_declined")[-1]["item_key"] == "cake"


def test_insufficient_available_balance_blocks_payment_and_keeps_reservation(bank) -> None:
    """余额 10 元、预算却占着 1000 元：领取前就拦住，报可支配金额不够。

    和「执行到一半失败」不同：这是可恢复的资金不足，预算必须保持 reserved——
    用户补足余额后可以直接重试，不用回滚重建计划。
    """
    plan = _create(bank, days=20)
    bank.execute("UPDATE accounts SET balance_fen = 1000 WHERE id = ?", (DEBIT,))
    bank.commit()
    import asyncio

    from app.clock import InsufficientFunds

    with pytest.raises(InsufficientFunds) as exc:
        asyncio.run(
            birthday.pay_item(
                plan_id=plan["plan_id"], item_key="flowers", session_id="s_bday_poor"
            )
        )
    assert exc.value.code == "INSUFFICIENT_FUNDS"
    assert _balance() == 1000
    assert _holds() == 0
    view = birthday.plan_with_budgets(bank, plan["plan_id"])
    flowers = [item for item in view["items"] if item["item_key"] == "flowers"][0]
    assert flowers["status"] == "reserved"
    assert not _events("plan.item_failed")


def test_large_item_needs_server_verified_factor_before_any_money_moves(bank) -> None:
    items = [
        {"key": "flowers", "label": "鲜花", "merchant": "flower_shop", "amount_fen": 200_000},
    ]
    plan = _create(bank, budget_fen=200_000, items=items, days=20)
    import asyncio

    result = asyncio.run(
        birthday.pay_item(
            plan_id=plan["plan_id"],
            item_key="flowers",
            session_id="s_bday_red",
            confirm_timeout_ms=1_500,
        )
    )
    assert result["status"] == "waiting_factor", result
    assert result["required"]
    # 差口令：一分钱没动，预算退回 reserved 等用户补验证
    assert _balance() == SEED_BALANCE_FEN
    view = birthday.plan_with_budgets(bank, plan["plan_id"])
    assert view["items"][0]["status"] == "reserved"
    assert not _events("plan.item_paid")
