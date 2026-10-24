"""生日预算的真实占用语义：可支配金额、取消释放、对账、旧库迁移。

对应赛题验收的七条：
1. 建 1000 元预算预留 → 可支配金额少 1000，余额不变；
2. 用这笔钱再发起转账 → 被 INSUFFICIENT_FUNDS 拦住；
3. 预算转成 hold 时不重复占用；
4. 付款成功后预算、hold、流水能对上账；
5. 拒绝 / 取消 / 失败 / 服务重启后预留状态可恢复；
6. 并发领取同一预算项目只扣一次；
7. 旧 schema（plan_budgets 没有 account_id/hold_id）能被迁移后继续用。
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import sqlite3
from pathlib import Path

import pytest

from app import birthday, clock, db, funds, plans as plans_mod
from app.clock import InsufficientFunds, ValidationError
from app.models import Action
from app.planner import executor
from app.security import token as tk
from scripts.seed import seed

SECRET = "c" * 64
DEBIT = "a_3d7e60c2"
USER = "u_9f2c41ab"
SEED_BALANCE_FEN = 1_286_050


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


def _create(conn, *, budget_fen: int = 100_000, days: int = 30) -> dict:
    return birthday.create_plan(
        conn, user_id=USER, birthday=_future_date(days), budget_fen=budget_fen
    )


def _balance(conn) -> int:
    return int(conn.execute("SELECT balance_fen FROM accounts WHERE id = ?", (DEBIT,)).fetchone()[0])


def _item(conn, plan_id: str, item_key: str) -> sqlite3.Row:
    return conn.execute(
        "SELECT * FROM plan_budgets WHERE plan_id = ? AND item_key = ?", (plan_id, item_key)
    ).fetchone()


def _available(conn) -> int:
    return funds.available_fen(conn, DEBIT)


def test_reservation_reduces_available_but_not_balance(bank) -> None:
    before = _balance(bank)
    plan = _create(bank)
    assert plan["budget"]["account_id"] == DEBIT
    assert plan["budget"]["balance_fen"] == before
    assert plan["budget"]["reserved_budget_fen"] == 100_000
    assert plan["budget"]["available_fen"] == before - 100_000
    # 余额本身一分没动；可支配金额才是被预算占住的那一层
    assert _balance(bank) == before
    assert _available(bank) == before - 100_000
    summary = birthday.account_summary(bank, DEBIT)
    assert summary["balance_fen"] == before
    assert summary["reserved_budget_fen"] == 100_000
    assert summary["available_fen"] == before - 100_000
    assert "可支配" in summary["note"]


def test_reserved_budget_blocks_a_new_transfer(bank) -> None:
    """预算占着 1000 元可支配，再转 1000 元应该被资金层直接拦住。"""
    _create(bank)
    payee = bank.execute("SELECT id FROM payees LIMIT 1").fetchone()["id"]
    huge = Action(
        tool="transfer",
        params={"payee_id": payee},
        account_id=DEBIT,
        amount_fen=1_286_050,
    )
    token = tk.issue(huge, secret=SECRET)
    with pytest.raises(InsufficientFunds) as exc:
        funds.initiate(
            bank,
            huge,
            session_id="s_rival_transfer",
            node_id="n1",
            token=token,
            secret=SECRET,
        )
    assert exc.value.code == "INSUFFICIENT_FUNDS"
    assert _balance(bank) == SEED_BALANCE_FEN
    assert bank.execute("SELECT COUNT(*) FROM holds").fetchone()[0] == 0


def test_partial_transfer_is_blocked_exactly_by_the_reserved_amount(bank) -> None:
    """边界值：余额减掉预留之后刚好差 1 分就转不出去。"""
    _create(bank)
    # 把余额压到「预留 + 5 元」，可支配金额只剩 500 分，边界更清楚
    bank.execute("UPDATE accounts SET balance_fen = 100_500 WHERE id = ?", (DEBIT,))
    bank.commit()
    payee = bank.execute("SELECT id FROM payees LIMIT 1").fetchone()["id"]
    assert _available(bank) == 500
    amount = _available(bank) + 1
    action = Action(
        tool="transfer", params={"payee_id": payee}, account_id=DEBIT, amount_fen=amount
    )
    with pytest.raises(InsufficientFunds):
        funds.initiate(
            bank,
            action,
            session_id="s_edge",
            node_id="n1",
            token=tk.issue(action, secret=SECRET),
            secret=SECRET,
        )
    # 少 1 分就能过，说明拦住它的是「预留」而不是余额
    ok_amount = _available(bank)
    ok = Action(
        tool="transfer", params={"payee_id": payee}, account_id=DEBIT, amount_fen=ok_amount
    )
    outcome = funds.initiate(
        bank, ok, session_id="s_edge_ok", node_id="n1", token=tk.issue(ok, secret=SECRET), secret=SECRET
    )
    assert outcome.hold_id and int(outcome.amount_fen) == ok_amount


def test_paying_does_not_double_occupy_budget_and_hold(bank) -> None:
    """预算转成 hold 后：余额 -400，预留 -400，可支配金额不变（同一笔钱只被占一次）。"""
    plan = _create(bank)
    before = _balance(bank)
    available_before = _available(bank)
    result = asyncio.run(
        birthday.pay_item(plan_id=plan["plan_id"], item_key="flowers", session_id="s_b1")
    )
    assert result["status"] == "paid", result
    flowers = _item(bank, plan["plan_id"], "flowers")
    assert flowers["status"] == "committed"
    assert _balance(bank) == before - 40_000
    assert _available(bank) == available_before - 40_000 + 40_000
    assert funds.reserved_budget_fen(bank, DEBIT) == 60_000


def test_paid_item_reconciles_hold_transaction_and_audit(bank) -> None:
    import json

    plan = _create(bank)
    asyncio.run(birthday.pay_item(plan_id=plan["plan_id"], item_key="flowers", session_id="s_b2"))
    flowers = _item(bank, plan["plan_id"], "flowers")
    assert flowers["hold_id"]
    hold = bank.execute("SELECT * FROM holds WHERE id = ?", (flowers["hold_id"],)).fetchone()
    assert hold is not None and hold["status"] == "committed"
    txs = bank.execute(
        "SELECT * FROM transactions WHERE hold_id = ?", (flowers["hold_id"],)
    ).fetchall()
    assert len(txs) == 1 and int(txs[0]["amount_fen"]) == 40_000
    payload = json.loads(
        bank.execute(
            "SELECT payload_json FROM audit_log WHERE event_type = 'plan.item_paid'"
            " ORDER BY seq DESC LIMIT 1"
        ).fetchone()["payload_json"]
    )
    assert payload["hold_id"] == flowers["hold_id"]
    assert payload["account_id"] == DEBIT


def test_declining_releases_the_reservation(bank) -> None:
    plan = _create(bank)
    before = _balance(bank)
    result = asyncio.run(
        birthday.pay_item(
            plan_id=plan["plan_id"], item_key="cake", session_id="s_b3", approve=False
        )
    )
    assert result["status"] == "declined"
    assert _balance(bank) == before
    # declined 是终态，不再计入预留：可支配金额回到只被鲜花占着的状态
    assert funds.reserved_budget_fen(bank, DEBIT) == 40_000
    assert _available(bank) == before - 40_000


def test_cancelling_the_plan_frees_every_reservation(bank) -> None:
    plan = _create(bank)
    assert _available(bank) == SEED_BALANCE_FEN - 100_000
    plans_mod.update(bank, plan["plan_id"], status="cancelled")
    view = birthday.plan_with_budgets(bank, plan["plan_id"])
    assert {item["status"] for item in view["items"]} == {"released"}
    assert funds.reserved_budget_fen(bank, DEBIT) == 0
    assert _available(bank) == SEED_BALANCE_FEN
    released = bank.execute(
        "SELECT COUNT(*) FROM audit_log WHERE event_type = 'plan.updated'"
        " AND payload_json LIKE '%budget_released%'"
    ).fetchone()[0]
    assert released >= 1


def test_restart_keeps_the_reservation_until_it_is_released(bank) -> None:
    plan = _create(bank)
    plan_id = plan["plan_id"]
    bank.close()
    fresh = db.connect()
    try:
        assert funds.reserved_budget_fen(fresh, DEBIT) == 100_000
        assert _available(fresh) == SEED_BALANCE_FEN - 100_000
        report = birthday.reconcile(fresh)
        assert report["repaired"] == []
        assert report["issues"] == []
        plans_mod.update(fresh, plan_id, status="cancelled")
        assert funds.reserved_budget_fen(fresh, DEBIT) == 0
    finally:
        fresh.close()
    reopened = db.connect()
    try:
        assert funds.reserved_budget_fen(reopened, DEBIT) == 0
    finally:
        reopened.close()


def test_reconcile_returns_interrupted_payment_to_reserved(bank) -> None:
    plan = _create(bank)
    stale = clock.now_ms() - 700_000
    bank.execute(
        "UPDATE plan_budgets SET status = 'paying', updated_ms = ?"
        " WHERE plan_id = ? AND item_key = 'flowers'",
        (stale, plan["plan_id"]),
    )
    report = birthday.reconcile(bank, stale_ms=600_000)
    assert {"plan_id": plan["plan_id"], "item_key": "flowers", "to": "reserved"} in report["repaired"]
    assert _item(bank, plan["plan_id"], "flowers")["status"] == "reserved"


def test_reconcile_promotes_paying_row_whose_hold_committed(bank) -> None:
    plan = _create(bank)
    asyncio.run(birthday.pay_item(plan_id=plan["plan_id"], item_key="flowers", session_id="s_b4"))
    bank.execute(
        "UPDATE plan_budgets SET status = 'paying' WHERE plan_id = ? AND item_key = 'flowers'",
        (plan["plan_id"],),
    )
    report = birthday.reconcile(bank)
    assert {"plan_id": plan["plan_id"], "item_key": "flowers", "to": "committed"} in report["repaired"]
    assert _item(bank, plan["plan_id"], "flowers")["status"] == "committed"


def test_reconcile_reports_committed_rows_without_evidence(bank) -> None:
    plan = _create(bank)
    asyncio.run(birthday.pay_item(plan_id=plan["plan_id"], item_key="flowers", session_id="s_b5"))
    bank.execute(
        "UPDATE plan_budgets SET hold_id = NULL WHERE plan_id = ? AND item_key = 'flowers'",
        (plan["plan_id"],),
    )
    report = birthday.reconcile(bank)
    kinds = [issue["kind"] for issue in report["issues"]]
    assert "committed_without_evidence" in kinds
    # 只报不修：不能把一笔真付款自动抹掉
    assert _item(bank, plan["plan_id"], "flowers")["status"] == "committed"


def test_concurrent_claim_pays_only_once(bank) -> None:
    plan = _create(bank)

    async def attempt(session: str):
        return await birthday.pay_item(
            plan_id=plan["plan_id"], item_key="flowers", session_id=session
        )

    async def race():
        results = await asyncio.gather(
            attempt("s_race_1"), attempt("s_race_2"), return_exceptions=True
        )
        return results

    results = asyncio.run(race())
    paid = [r for r in results if isinstance(r, dict) and r.get("status") == "paid"]
    others = [r for r in results if not (isinstance(r, dict) and r.get("status") == "paid")]
    assert len(paid) == 1
    assert others and isinstance(others[0], ValidationError)
    assert _balance(bank) == SEED_BALANCE_FEN - 40_000
    hold_id = _item(bank, plan["plan_id"], "flowers")["hold_id"]
    assert hold_id
    paid_tx = bank.execute(
        "SELECT COUNT(*) FROM transactions WHERE hold_id = ?", (hold_id,)
    ).fetchone()[0]
    assert paid_tx == 1


def test_budget_not_covered_rolls_the_whole_plan_back(bank, tmp_path: Path) -> None:
    bank.execute("UPDATE accounts SET balance_fen = 50_000 WHERE id = ?", (DEBIT,))
    bank.commit()
    with pytest.raises(birthday.BudgetNotCovered) as exc:
        _create(bank, budget_fen=100_000)
    assert exc.value.code == "BUDGET_NOT_COVERED"
    assert exc.value.fields.get("available_fen") == 50_000
    assert bank.execute("SELECT COUNT(*) FROM plans").fetchone()[0] == 0
    assert bank.execute("SELECT COUNT(*) FROM plan_budgets").fetchone()[0] == 0
    assert bank.execute("SELECT COUNT(*) FROM plan_steps").fetchone()[0] == 0
    assert _available(bank) == 50_000


def test_old_schema_plan_budgets_is_migrated_and_still_counts(bank, tmp_path: Path) -> None:
    """旧库没有 account_id/hold_id：init_db 补列之后，预留照样能占住可支配金额。"""
    path = tmp_path / "old_bank.db"
    old = sqlite3.connect(str(path), isolation_level=None)
    old.execute(
        "CREATE TABLE plan_budgets ("
        " id TEXT PRIMARY KEY, plan_id TEXT NOT NULL, item_key TEXT NOT NULL,"
        " label TEXT NOT NULL, merchant TEXT NOT NULL,"
        " amount_fen INTEGER NOT NULL CHECK (amount_fen > 0),"
        " extra_json TEXT NOT NULL DEFAULT '{}', status TEXT NOT NULL,"
        " reason TEXT NOT NULL DEFAULT '', created_ms INTEGER NOT NULL,"
        " updated_ms INTEGER NOT NULL, UNIQUE(plan_id, item_key))"
    )
    old.close()
    fresh = db.connect(str(path))
    try:
        # 老库必须先 ensure_columns 再 init_db：SCHEMA_SQL 里 plan_budgets 的
        # account_id 索引要先有列才建得出来。顺序反了会报 no such column。
        db.ensure_columns(fresh)
        db.init_db(fresh)
        columns = {row["name"] for row in fresh.execute("PRAGMA table_info(plan_budgets)")}
        assert {"account_id", "hold_id"} <= columns
        seed(fresh)
        plan = birthday.create_plan(
            fresh, user_id=USER, birthday=_future_date(15), budget_fen=100_000
        )
        row = fresh.execute(
            "SELECT * FROM plan_budgets WHERE plan_id = ? AND item_key = 'flowers'",
            (plan["plan_id"],),
        ).fetchone()
        assert row["account_id"] == DEBIT
        assert funds.available_fen(fresh, DEBIT) == SEED_BALANCE_FEN - 100_000
    finally:
        fresh.close()


def test_budget_boundary_text_explains_the_new_semantics() -> None:
    assert "可支配金额" in birthday.BUDGET_BOUNDARY
    assert "不是银行侧冻结" in birthday.BUDGET_BOUNDARY
    assert "reconcile" in birthday.__doc__
