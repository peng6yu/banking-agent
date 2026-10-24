"""生日计划的一次性预约授权：冻结范围、到点复核、只付一次。

对应赛题验收：
1. 到期前不执行；到期执行且只执行一次（重复触发幂等、重启后仍按库状态判断）；
2. 用户拒绝、修改计划、取消计划后授权失效，后台不得再启动付款；
3. 到点要用**最新**日累计重新裁决，红灯不自动扣款；
4. 余额不够、计划参数漂移时暂停成待确认，而不是先扣了再说；
5. 授权窗口过期后自动 expired，不能变成永久代扣券。
"""

from __future__ import annotations

import datetime as _dt

import pytest

from app import birthday, clock, db, funds, plans as plans_mod
from app.planner import executor, ticker
from scripts.seed import seed

SECRET = "c" * 64
DEBIT = "a_3d7e60c2"
USER = "u_9f2c41ab"
SEED_BALANCE_FEN = 1_286_050
BIRTHDAY = "2026-11-20"


def _ts(
    year: int, month: int, day: int, hour: int = 10, minute: int = 0, second: int = 0
) -> int:
    return int(_dt.datetime(year, month, day, hour, minute, second).timestamp() * 1000)


NOW = _ts(2026, 11, 10)
TRIGGER = _ts(2026, 11, 18, 9)
WINDOW_END = _ts(2026, 11, 20, 23, 59, 59)


@pytest.fixture()
def bank(tmp_path, monkeypatch):
    monkeypatch.setenv("CAPABILITY_SECRET", SECRET)
    monkeypatch.setattr(db, "DEFAULT_DB_PATH", tmp_path / "bank.db")
    conn = db.connect()
    db.init_db(conn)
    seed(conn)
    executor.RUNS.clear()
    clock.freeze(NOW)
    yield conn
    conn.close()
    executor.RUNS.clear()
    clock.unfreeze()


def _create(conn, *, save: bool = True) -> dict:
    return birthday.create_plan(
        conn,
        user_id=USER,
        birthday=BIRTHDAY,
        budget_fen=100_000,
        save_authorization=save,
    )


def _auth(conn, plan_id: str) -> dict:
    view = birthday.authorization(conn, plan_id)
    assert view is not None
    return view


def _statuses(conn, plan_id: str) -> dict[str, str]:
    rows = conn.execute(
        "SELECT item_key, status FROM plan_budgets WHERE plan_id = ?", (plan_id,)
    ).fetchall()
    return {str(row["item_key"]): str(row["status"]) for row in rows}


def _balance(conn) -> int:
    return int(conn.execute("SELECT balance_fen FROM accounts WHERE id = ?", (DEBIT,)).fetchone()[0])


def _plan_status(conn, plan_id: str) -> str:
    return str(conn.execute("SELECT status FROM plans WHERE id = ?", (plan_id,)).fetchone()["status"])


def _holds(conn) -> int:
    return int(conn.execute("SELECT COUNT(*) FROM holds").fetchone()[0])


def test_authorization_is_saved_with_the_frozen_scope(bank) -> None:
    plan = _create(bank)
    plan_id = plan["plan_id"]
    auth = _auth(bank, plan_id)
    assert auth["status"] == "active"
    assert auth["plan_version"] == "1"
    assert auth["account_id"] == DEBIT
    assert auth["expires_ms"] == WINDOW_END
    scope = auth["scope"]
    assert scope["plan_version"] == "1"
    assert scope["account_id"] == DEBIT
    assert scope["budget_fen"] == 100_000
    assert scope["window"]["start_ms"] == TRIGGER
    assert scope["window"]["end_ms"] == WINDOW_END
    assert {item["item_key"] for item in scope["items"]} == {"flowers", "cake"}
    # 保存时的裁决也要落库，之后才能解释「当时是黄灯还是绿灯」
    assert set(auth["decision"]["items"]) == {"flowers", "cake"}
    assert auth["decision"]["items"]["flowers"]["level"] in ("green", "yellow")
    # 授权不影响资金：余额、预留都不变
    assert _balance(bank) == SEED_BALANCE_FEN
    assert funds.reserved_budget_fen(bank, DEBIT) == 100_000
    assert plan["authorization"]["status"] == "active"


def test_creating_without_saving_keeps_manual_only(bank) -> None:
    plan = _create(bank, save=False)
    assert birthday.authorization(bank, plan["plan_id"]) is None
    report = birthday.run_due_authorized(bank, now_ms=TRIGGER + 60_000)
    assert report["checked"] == 0 and report["paid"] == 0
    assert _balance(bank) == SEED_BALANCE_FEN
    assert _statuses(bank, plan["plan_id"]) == {"flowers": "reserved", "cake": "reserved"}


def test_nothing_runs_before_the_window(bank) -> None:
    plan = _create(bank)
    plan_id = plan["plan_id"]
    clock.freeze(TRIGGER - 60_000)
    report = birthday.run_due_authorized(bank, now_ms=TRIGGER - 60_000)
    assert report["paid"] == 0
    assert report["checked"] == 1 and report["skipped"] >= 1
    assert _statuses(bank, plan_id) == {"flowers": "reserved", "cake": "reserved"}
    assert _balance(bank) == SEED_BALANCE_FEN
    assert _auth(bank, plan_id)["status"] == "active"


def test_window_pays_once_and_marks_authorization_used(bank, monkeypatch) -> None:
    plan = _create(bank)
    plan_id = plan["plan_id"]
    clock.freeze(TRIGGER + 60_000)
    report = birthday.run_due_authorized(bank, now_ms=TRIGGER + 60_000)
    assert report["paid"] == 2, report
    assert _statuses(bank, plan_id) == {"flowers": "committed", "cake": "committed"}
    assert _balance(bank) == SEED_BALANCE_FEN - 100_000
    assert funds.reserved_budget_fen(bank, DEBIT) == 0
    auth = _auth(bank, plan_id)
    assert auth["status"] == "used" and auth["used_ms"] == TRIGGER + 60_000
    # 流水与预算对得上：每一项一条 committed hold + 一条出账流水
    rows = bank.execute(
        "SELECT b.item_key, b.hold_id, t.amount_fen FROM plan_budgets b"
        " JOIN transactions t ON t.hold_id = b.hold_id WHERE b.plan_id = ?",
        (plan_id,),
    ).fetchall()
    assert {str(row["item_key"]): int(row["amount_fen"]) for row in rows} == {
        "flowers": 40_000,
        "cake": 60_000,
    }
    # 重复触发幂等：used 之后不再被选中，余额不再变化
    second = birthday.run_due_authorized(bank, now_ms=TRIGGER + 120_000)
    assert second["checked"] == 0 and second["paid"] == 0
    assert _balance(bank) == SEED_BALANCE_FEN - 100_000


def test_restart_does_not_duplicate_payment(bank) -> None:
    plan = _create(bank)
    plan_id = plan["plan_id"]
    bank.close()
    fresh = db.connect()
    try:
        clock.freeze(TRIGGER + 60_000)
        report = birthday.run_due_authorized(fresh, now_ms=TRIGGER + 60_000)
        assert report["paid"] == 2
        assert _balance(fresh) == SEED_BALANCE_FEN - 100_000
    finally:
        fresh.close()
    reopened = db.connect()
    try:
        again = birthday.run_due_authorized(reopened, now_ms=TRIGGER + 120_000)
        assert again["checked"] == 0 and again["paid"] == 0
        assert _balance(reopened) == SEED_BALANCE_FEN - 100_000
        assert _auth(reopened, plan_id)["status"] == "used"
    finally:
        reopened.close()
    executor.RUNS.clear()


def test_red_light_pauses_instead_of_paying(bank) -> None:
    plan = _create(bank)
    plan_id = plan["plan_id"]
    bank.execute(
        "INSERT INTO daily_user_limits (user_id, day, spent_fen, tx_count) VALUES (?, ?, ?, 1)",
        (USER, funds.day_of(TRIGGER), 100_000),
    )
    bank.commit()
    clock.freeze(TRIGGER + 60_000)
    report = birthday.run_due_authorized(bank, now_ms=TRIGGER + 60_000)
    assert report["paid"] == 0
    assert report["needs_verification"] == 1
    assert report["items"][0]["result"] == "policy_red"
    assert _plan_status(bank, plan_id) == "paused"
    assert _statuses(bank, plan_id) == {"flowers": "reserved", "cake": "reserved"}
    assert _balance(bank) == SEED_BALANCE_FEN
    assert _holds(bank) == 0
    meta = plan["meta"]
    stored = birthday.plan_with_budgets(bank, plan_id)["meta"]
    assert stored["needs_verification"]["reason"] == "POLICY_RED"
    # 暂停后重跑不扣钱；授权仍 active 等用户处理（不是偷偷作废）
    again = birthday.run_due_authorized(bank, now_ms=TRIGGER + 120_000)
    assert again["paid"] == 0 and again["skipped"] == 1
    assert _balance(bank) == SEED_BALANCE_FEN


def test_declining_an_item_revokes_the_authorization(bank) -> None:
    plan = _create(bank)
    plan_id = plan["plan_id"]
    clock.freeze(TRIGGER + 30_000)
    import asyncio

    result = asyncio.run(
        birthday.pay_item(plan_id=plan_id, item_key="flowers", session_id="s_reject", approve=False)
    )
    assert result["status"] == "declined"
    assert _auth(bank, plan_id)["status"] == "revoked"
    report = birthday.run_due_authorized(bank, now_ms=TRIGGER + 60_000)
    assert report["paid"] == 0
    # 蛋糕仍 reservation 占着额度，但没有被自动付掉
    assert _statuses(bank, plan_id) == {"flowers": "declined", "cake": "reserved"}
    assert _balance(bank) == SEED_BALANCE_FEN


def test_modifying_the_plan_invalidates_the_authorization(bank) -> None:
    plan = _create(bank)
    plan_id = plan["plan_id"]
    plans_mod.update(bank, plan_id, title="换个名字")
    view = birthday.plan_with_budgets(bank, plan_id)
    assert view["meta"]["version"] == 2
    auth = _auth(bank, plan_id)
    assert auth["status"] == "revoked"
    assert auth["revoked_ms"] is not None
    clock.freeze(TRIGGER + 60_000)
    report = birthday.run_due_authorized(bank, now_ms=TRIGGER + 60_000)
    assert report["checked"] == 0 and report["paid"] == 0
    assert _balance(bank) == SEED_BALANCE_FEN
    assert _statuses(bank, plan_id) == {"flowers": "reserved", "cake": "reserved"}


def test_cancelling_the_plan_revokes_and_releases(bank) -> None:
    plan = _create(bank)
    plan_id = plan["plan_id"]
    plans_mod.update(bank, plan_id, status="cancelled")
    assert _auth(bank, plan_id)["status"] == "revoked"
    assert set(_statuses(bank, plan_id).values()) == {"released"}
    assert funds.reserved_budget_fen(bank, DEBIT) == 0
    clock.freeze(TRIGGER + 60_000)
    report = birthday.run_due_authorized(bank, now_ms=TRIGGER + 60_000)
    assert report["paid"] == 0
    assert _balance(bank) == SEED_BALANCE_FEN


def test_user_pause_revokes_but_background_pause_keeps_it(bank) -> None:
    plan = _create(bank)
    plan_id = plan["plan_id"]
    bank.execute("UPDATE daily_user_limits SET spent_fen = 0")  # 确保下面不是策略红
    bank.commit()
    # 用户主动暂停：授权作废
    plans_mod.update(bank, plan_id, status="paused")
    assert _auth(bank, plan_id)["status"] == "revoked"
    # 另一个计划：后台到点提醒导致 paused 只写 SQL，不触发钩子，授权保持 active
    other = _create(bank)
    other_id = other["plan_id"]
    plans_mod.run_due(bank, now=TRIGGER + 60_000, plan_id=other_id)
    assert _plan_status(bank, other_id) == "paused"
    assert _auth(bank, other_id)["status"] == "active"


def test_insufficient_available_pauses_before_charging(bank) -> None:
    plan = _create(bank)
    plan_id = plan["plan_id"]
    bank.execute("UPDATE accounts SET balance_fen = 50_000 WHERE id = ?", (DEBIT,))
    bank.commit()
    clock.freeze(TRIGGER + 60_000)
    report = birthday.run_due_authorized(bank, now_ms=TRIGGER + 60_000)
    assert report["paid"] == 0
    assert report["needs_verification"] == 1
    assert report["items"][0]["result"] == "insufficient_funds"
    assert _plan_status(bank, plan_id) == "paused"
    assert _holds(bank) == 0
    assert _balance(bank) == 50_000
    assert _statuses(bank, plan_id) == {"flowers": "reserved", "cake": "reserved"}


def test_expired_window_marks_authorization_expired(bank) -> None:
    plan = _create(bank)
    plan_id = plan["plan_id"]
    later = WINDOW_END + 60_000
    clock.freeze(later)
    report = birthday.run_due_authorized(bank, now_ms=later)
    assert report["expired"] == 1
    assert report["paid"] == 0
    auth = _auth(bank, plan_id)
    assert auth["status"] == "expired"
    assert _balance(bank) == SEED_BALANCE_FEN
    assert _statuses(bank, plan_id) == {"flowers": "reserved", "cake": "reserved"}


def test_scope_drift_pauses_and_revokes(bank) -> None:
    plan = _create(bank)
    plan_id = plan["plan_id"]
    # 绕过 API 直接改预算金额，模拟「计划参数被别的路径改了」
    bank.execute(
        "UPDATE plan_budgets SET amount_fen = amount_fen + 1 WHERE plan_id = ? AND item_key = 'flowers'",
        (plan_id,),
    )
    bank.commit()
    clock.freeze(TRIGGER + 60_000)
    report = birthday.run_due_authorized(bank, now_ms=TRIGGER + 60_000)
    assert report["paid"] == 0
    assert report["needs_verification"] == 1
    assert report["revoked"] == 1
    assert report["items"][0]["result"] == "scope_changed"
    assert _auth(bank, plan_id)["status"] == "revoked"
    assert _plan_status(bank, plan_id) == "paused"
    assert _balance(bank) == SEED_BALANCE_FEN


def test_reconcile_revokes_authorization_of_a_dead_plan(bank) -> None:
    """绕过钩子直接改库，模拟异常退出留下的「死计划 + active 授权」。"""
    plan = _create(bank)
    plan_id = plan["plan_id"]
    bank.execute("UPDATE plans SET status = 'cancelled' WHERE id = ?", (plan_id,))
    bank.execute("UPDATE plan_authorizations SET status = 'active' WHERE plan_id = ?", (plan_id,))
    bank.commit()
    report = birthday.reconcile(bank)
    assert report["revoked"] >= 1
    assert report["released_fen"] >= 100_000
    assert _auth(bank, plan_id)["status"] == "revoked"
    assert funds.reserved_budget_fen(bank, DEBIT) == 0


def test_ticker_runs_authorized_payment_and_keeps_existing_keys(bank, monkeypatch) -> None:
    plan = _create(bank)
    plan_id = plan["plan_id"]
    clock.freeze(TRIGGER + 60_000)
    result = ticker.run_plans_once(now=TRIGGER + 60_000)
    # 老调用方仍能拿到 count/executed；授权报告放在 authorized 子键里
    assert "count" in result and "executed" in result
    assert result["authorized"]["paid"] == 2
    assert _statuses(bank, plan_id) == {"flowers": "committed", "cake": "committed"}
    assert _balance(bank) == SEED_BALANCE_FEN - 100_000
    second = ticker.run_plans_once(now=TRIGGER + 120_000)
    assert second["authorized"]["paid"] == 0
    assert _balance(bank) == SEED_BALANCE_FEN - 100_000
