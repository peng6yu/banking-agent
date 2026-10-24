"""生日计划接口层：预算预留、一次性授权、手动推进与可支配金额口径。

模块级测试已经覆盖状态机；这里补的是「接口真的把参数接上了」：
- POST /api/plans/birthday 能把 account_id / save_authorization 传到底；
- POST /api/plans/run 手动触发也会先跑已授权付款，且重复触发不重复扣钱；
- /api/business/overview 的可支配金额与资金写操作读的是同一个 funds.available_fen。
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import json

import pytest

from app import clock, db, funds
from app.planner import executor
from scripts.seed import seed

SECRET = "c" * 64
DEBIT = "a_3d7e60c2"
SEED_BALANCE_FEN = 1_286_050
BIRTHDAY = "2026-11-20"


def _ts(year: int, month: int, day: int, hour: int = 10) -> int:
    return int(_dt.datetime(year, month, day, hour, 0, 0).timestamp() * 1000)


NOW = _ts(2026, 11, 10)
TRIGGER = _ts(2026, 11, 18, 9)


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


def _http(method: str, url: str, body=None):
    import httpx
    from app.main import app

    async def go():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.request(method, url, json=body)

    return asyncio.run(go())


def _create_via_api(*, save_authorization: bool = False) -> dict:
    response = _http(
        "POST",
        "/api/plans/birthday",
        {
            "birthday": BIRTHDAY,
            "budget_fen": 100_000,
            "account_id": DEBIT,
            "save_authorization": save_authorization,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()["plan"]


def _balance(conn) -> int:
    return int(conn.execute("SELECT balance_fen FROM accounts WHERE id = ?", (DEBIT,)).fetchone()[0])


def _statuses(conn, plan_id: str) -> dict[str, str]:
    rows = conn.execute(
        "SELECT item_key, status FROM plan_budgets WHERE plan_id = ?", (plan_id,)
    ).fetchall()
    return {str(row["item_key"]): str(row["status"]) for row in rows}


def test_api_create_reserves_the_selected_account_and_reduces_available(bank) -> None:
    plan = _create_via_api()
    assert plan["budget"]["account_id"] == DEBIT
    assert plan["budget"]["reserved_budget_fen"] == 100_000
    assert plan["budget"]["available_fen"] == SEED_BALANCE_FEN - 100_000
    assert _balance(bank) == SEED_BALANCE_FEN  # 余额本身不动
    assert funds.available_fen(bank, DEBIT) == SEED_BALANCE_FEN - 100_000

    overview = _http("GET", "/api/business/overview")
    assert overview.status_code == 200, overview.text
    account = overview.json()["account"]
    assert account["account_id"] == DEBIT
    assert account["balance_fen"] == SEED_BALANCE_FEN
    assert account["available_fen"] == SEED_BALANCE_FEN - 100_000
    assert account["reserved_budget_fen"] == 100_000
    assert account["held_fen"] == 0


def test_api_create_with_save_authorization_persists_a_bound_authorization(bank) -> None:
    plan = _create_via_api(save_authorization=True)
    row = bank.execute(
        "SELECT * FROM plan_authorizations WHERE plan_id = ?", (plan["plan_id"],)
    ).fetchone()
    assert row is not None and row["status"] == "active"
    meta = json.loads(
        bank.execute(
            "SELECT meta_json FROM plans WHERE id = ?", (plan["plan_id"],)
        ).fetchone()["meta_json"]
    )
    assert row["plan_version"] == str(meta["version"])
    assert row["account_id"] == DEBIT

    plain = _create_via_api(save_authorization=False)
    assert (
        bank.execute(
            "SELECT COUNT(*) FROM plan_authorizations WHERE plan_id = ?", (plain["plan_id"],)
        ).fetchone()[0]
        == 0
    )


def test_manual_plan_run_executes_authorized_payment_once_when_due(bank) -> None:
    plan = _create_via_api(save_authorization=True)
    plan_id = plan["plan_id"]

    # 到期前：窗口没到，一分钱不扣
    early = _http("POST", "/api/plans/run", {"plan_id": plan_id})
    assert early.status_code == 200, early.text
    assert early.json()["authorized"]["paid"] == 0
    assert _statuses(bank, plan_id) == {"flowers": "reserved", "cake": "reserved"}

    clock.freeze(TRIGGER + 3_600_000)
    due = _http("POST", "/api/plans/run", {"plan_id": plan_id})
    assert due.status_code == 200, due.text
    report = due.json()["authorized"]
    assert report["paid"] == 2, report
    assert _balance(bank) == SEED_BALANCE_FEN - 100_000
    assert _statuses(bank, plan_id) == {"flowers": "committed", "cake": "committed"}
    auth = bank.execute(
        "SELECT status FROM plan_authorizations WHERE plan_id = ?", (plan_id,)
    ).fetchone()
    assert auth["status"] == "used"

    # 重复触发：没有 active 授权，余额不再变化
    again = _http("POST", "/api/plans/run", {"plan_id": plan_id})
    assert again.status_code == 200, again.text
    assert again.json()["authorized"]["checked"] == 0
    assert _balance(bank) == SEED_BALANCE_FEN - 100_000


def test_authorized_payment_pauses_instead_of_charging_when_the_day_is_red(bank) -> None:
    """到点重新裁决：当天已转满风险阈值时，授权付款必须停下待确认，而不是照扣。"""
    plan = _create_via_api(save_authorization=True)
    plan_id = plan["plan_id"]
    clock.freeze(TRIGGER + 3_600_000)
    stamp = TRIGGER + 3_600_000
    day = _dt.date.fromtimestamp(stamp / 1000).isoformat()
    with db.tx(bank):
        bank.execute(
            "INSERT INTO daily_user_limits (user_id, day, spent_fen, tx_count)"
            " VALUES ((SELECT user_id FROM accounts WHERE id = ?), ?, 100_000, 1)"
            " ON CONFLICT(user_id, day) DO UPDATE SET spent_fen = 100_000",
            (DEBIT, day),
        )

    ran = _http("POST", "/api/plans/run", {"plan_id": plan_id})
    assert ran.status_code == 200, ran.text
    assert ran.json()["authorized"]["paid"] == 0
    assert _balance(bank) == SEED_BALANCE_FEN
    assert _statuses(bank, plan_id) == {"flowers": "reserved", "cake": "reserved"}
    assert _http("GET", f"/api/plans/{plan_id}").status_code == 200
