"""储蓄计划：每期真的划钱、取消原路划回。

用户的定义是「计划每个周期把钱划出来一部分，取消就划回去，也是要动钱的」，
所以这组用例盯的全是钱和流水，不是"字段有没有返回"：

1. 只授权不划钱：建完计划余额不动，卡上多不出流水；
2. 划一笔：余额减少、目标增加、流水一条「储蓄」、同一个 due_ms 不重复划；
3. 余额不够：顺延一期，写 skipped，不把目标当欠款；
4. 取消：钱原路回到卡上、计划关闭，再取消要报错而不是再退一次；
5. 储蓄不算支出：账单的 spend_out 一分不变（它属于内部搬运）。
"""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import httpx
import pytest

import app.savings as savings
from app import db
from app.clock import now_ms
from app.main import app
from scripts.seed import seed

BASE_URL = "http://testserver"


@pytest.fixture()
def bank(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(db, "DEFAULT_DB_PATH", tmp_path / "bank.db")
    conn = db.connect()
    db.init_db(conn)
    seed(conn)
    conn.close()
    yield tmp_path / "bank.db"


def _request(method: str, url: str, body: dict | None = None) -> httpx.Response:
    async def run() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url=BASE_URL) as http:
            return await http.request(method, url, json=body)

    return asyncio.run(run())


def get(url: str) -> httpx.Response:
    return _request("GET", url)


def post(url: str, body: dict | None = None) -> httpx.Response:
    return _request("POST", url, body if body is not None else {})


def _rows(sql: str, args: tuple = ()) -> list[sqlite3.Row]:
    with db.open_conn() as conn:
        return conn.execute(sql, args).fetchall()


def _one(sql: str, args: tuple = ()) -> sqlite3.Row | None:
    rows = _rows(sql, args)
    return rows[0] if rows else None


def _account(kind: str = "debit") -> sqlite3.Row:
    row = _one(
        "SELECT id, tail, balance_fen FROM accounts WHERE type = ? AND status = 'active'"
        " ORDER BY tail LIMIT 1",
        (kind,),
    )
    assert row is not None, "种子里没有 " + kind + " 账户"
    return row


def _balance(account_id: str) -> int:
    row = _one("SELECT balance_fen FROM accounts WHERE id = ?", (account_id,))
    assert row is not None
    return int(row["balance_fen"])


def _new_goal(amount_yuan: int = 6000, authorized: bool = True) -> dict:
    account = _account()
    body: dict = {
        "name": "换一台相机",
        "target_fen": amount_yuan * 100,
        "target_date": "2027-03-31",
    }
    if authorized:
        body["account_id"] = str(account["id"])
        body["cadence"] = "monthly"
    response = post("/api/savings/goals", body)
    assert response.status_code == 200, response.text
    return response.json()


def _deposit(goal_id: str, amount_fen: int | None = None) -> httpx.Response:
    body = {"amount_fen": amount_fen} if amount_fen is not None else {}
    return post("/api/savings/goals/" + goal_id + "/deposit", body)


def _expected_amount(goal: dict) -> int:
    """缺口 ÷ 剩余期数，向上取整到元 —— 和后端同一套算法，避免时钟漂移打翻断言。"""
    gap = int(goal["remaining_fen"])
    periods = max(1, int(goal["periods_remaining"]))
    per = -(-gap // periods)
    return -(-per // 100) * 100


def test_authorizing_a_plan_does_not_move_money_yet(bank: Path) -> None:
    """授权只是「允许以后每期划」，授权这一刻余额与流水都不该变。"""
    account = _account()
    before_balance = int(account["balance_fen"])
    before_tx = _one("SELECT COUNT(*) AS n FROM transactions")["n"]
    data = _new_goal()
    goal = data["goal"]
    assert data["authorized"] is True
    assert goal["state"] == "active"
    assert goal["account_id"] == str(account["id"])
    assert goal["cadence"] == "monthly"
    assert goal["amount_fen"] == _expected_amount(goal)
    assert goal["amount_fen"] % 100 == 0
    # 建完当场就要能说出「从哪张卡划」，否则提示里会漏成「每月从  划 800 元」
    tail = str(account["tail"])
    assert tail in goal["account_label"]
    assert goal["account_label"].endswith(tail)
    assert "+" not in goal["amount_yuan"]
    assert _balance(str(account["id"])) == before_balance
    assert _one("SELECT COUNT(*) AS n FROM transactions")["n"] == before_tx


def test_one_deposit_takes_money_from_the_card_and_leaves_a_trace(bank: Path) -> None:
    """划一笔：卡上少、目标里多、流水一条「储蓄」、审计一条。"""
    account = _account()
    before = int(account["balance_fen"])
    goal = _new_goal()["goal"]
    response = _deposit(goal["goal_id"])
    assert response.status_code == 200, response.text
    data = response.json()
    deposit = data["deposit"]
    amount = int(deposit["amount_fen"])
    assert amount == goal["amount_fen"]
    assert _balance(str(account["id"])) == before - amount
    assert data["goal"]["saved_fen"] == amount
    assert data["goal"]["deposit_count"] == 1
    tx = _one("SELECT * FROM transactions WHERE id = ?", (deposit["tx_id"],))
    assert tx is not None
    assert str(tx["category"]) == "储蓄"
    assert str(tx["direction"]) == "out"
    assert "换一台相机" in str(tx["note"])
    event = _one(
        "SELECT * FROM audit_log WHERE event_type = 'savings.deposited' ORDER BY seq DESC LIMIT 1"
    )
    assert event is not None
    assert str(deposit["tx_id"]) in str(event["payload_json"])


def test_the_same_cycle_never_deposits_twice(bank: Path) -> None:
    """节拍器重复跑到同一天，只能划一次：按 (goal_id, due_ms) 幂等。"""
    goal_id = _new_goal()["goal"]["goal_id"]
    with db.open_conn() as conn:
        first = savings.deposit(conn, goal_id=goal_id, source="cycle", due_ms=1_800_000_000_000)
        conn.commit()
        second = savings.deposit(conn, goal_id=goal_id, source="cycle", due_ms=1_800_000_000_000)
        conn.commit()
    assert first["repeated"] is False
    assert second["repeated"] is True
    assert second["deposit"]["deposit_id"] == first["deposit"]["deposit_id"]
    count = _one("SELECT COUNT(*) AS n FROM savings_deposits WHERE goal_id = ?", (goal_id,))["n"]
    assert count == 1


def test_not_enough_money_skips_the_cycle_instead_of_failing(bank: Path) -> None:
    """余额不够：顺延一期、写 skipped、下次到点再试，不把目标记成欠款。"""
    account = _account()
    goal = _new_goal()["goal"]
    with db.open_conn() as conn:
        conn.execute("UPDATE accounts SET balance_fen = 100 WHERE id = ?", (str(account["id"]),))
        conn.execute(
            "UPDATE savings_goals SET next_due_ms = ? WHERE id = ?",
            (now_ms() - 1000, goal["goal_id"]),
        )
        conn.commit()
        result = savings.run_due_goals(conn)
        conn.commit()
    assert result["skipped"] >= 1
    row = _one("SELECT * FROM savings_goals WHERE id = ?", (goal["goal_id"],))
    assert str(row["last_result"]) == "insufficient"
    assert int(row["next_due_ms"]) > now_ms() - 1000
    assert int(row["saved_fen"]) == 0
    event = _one(
        "SELECT * FROM audit_log WHERE event_type = 'savings.skipped' ORDER BY seq DESC LIMIT 1"
    )
    assert event is not None
    assert _balance(str(account["id"])) == 100


def test_cancel_sends_the_money_back_and_cannot_refund_twice(bank: Path) -> None:
    """取消：钱原路划回、计划关闭；再点一次必须报错，不能再退一次。"""
    account = _account()
    before = int(account["balance_fen"])
    goal = _new_goal()["goal"]
    amount = int(_deposit(goal["goal_id"]).json()["deposit"]["amount_fen"])
    assert _balance(str(account["id"])) == before - amount
    response = post("/api/savings/goals/" + goal["goal_id"] + "/refund", {"reason": "不攒了"})
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["refunded_fen"] == amount
    assert data["deposit_count"] == 1
    assert _balance(str(account["id"])) == before
    assert data["goal"]["state"] == "cancelled"
    assert data["goal"]["saved_fen"] == 0
    back = _one("SELECT * FROM transactions WHERE id = ?", (data["tx_id"],))
    assert back is not None and str(back["direction"]) == "in"
    rows = _rows("SELECT status FROM savings_deposits WHERE goal_id = ?", (goal["goal_id"],))
    assert [str(item["status"]) for item in rows] == ["refunded"]
    again = post("/api/savings/goals/" + goal["goal_id"] + "/refund", {})
    assert again.status_code == 400
    assert "已经取消过" in again.json()["user_text"]
    assert _balance(str(account["id"])) == before



def test_a_credit_card_cannot_fund_a_savings_plan(bank: Path) -> None:
    """信用卡的钱是银行的，不能拿来「攒」：授权时就拒绝，并说清原因。"""
    credit = _account("credit")
    goal = _new_goal(authorized=False)["goal"]
    response = post(
        "/api/savings/goals/" + goal["goal_id"] + "/authorize",
        {"account_id": str(credit["id"]), "cadence": "monthly", "auto_fen": 0},
    )
    assert response.status_code == 400
    assert "储蓄卡" in response.json()["user_text"]


def test_depositing_without_an_authorized_card_is_refused(bank: Path) -> None:
    """没选卡就想划钱：拒绝，而不是悄悄挑一张。"""
    goal = _new_goal(authorized=False)["goal"]
    response = _deposit(goal["goal_id"])
    assert response.status_code == 400
    assert "哪张卡" in response.json()["user_text"]


def test_a_too_large_period_amount_says_what_the_cap_is(bank: Path) -> None:
    """建计划时每期金额填太大：要说清上限是多少，而不是「我没法理解你发的内容」。"""
    account = _account()
    response = post("/api/savings/goals", {
        "name": "去冰岛看极光",
        "target_fen": 600000,
        "target_date": "2027-06-30",
        "account_id": str(account["id"]),
        "cadence": "monthly",
        "auto_fen": 999999,
    })
    assert response.status_code == 400
    text = response.json()["user_text"]
    assert "每期金额最多 5,000.00 元" in text
    assert "9,999.99" in text
    assert _one("SELECT COUNT(*) AS n FROM savings_goals")["n"] == 0


def test_one_period_cannot_exceed_the_cap(bank: Path) -> None:
    """单期上限 5,000 元：超过就得自己手动存，不让接口一次划走一大笔。"""
    account = _account()
    before = int(account["balance_fen"])
    goal = _new_goal()["goal"]
    response = _deposit(goal["goal_id"], 500001)
    assert response.status_code == 400
    assert "金额最多 5,000.00 元" in response.json()["user_text"]
    assert _balance(str(account["id"])) == before
    assert _one("SELECT COUNT(*) AS n FROM savings_deposits")["n"] == 0


def test_savings_is_not_counted_as_spending(bank: Path) -> None:
    """储蓄属于内部搬运：账单的「花了多少」不该因为自己攒钱而变大。"""
    before = get("/api/business/bill?period=month&year=2026&month=10").json()
    goal = _new_goal()["goal"]
    assert _deposit(goal["goal_id"]).status_code == 200
    after = get("/api/business/bill?period=month&year=2026&month=10").json()
    assert after["spend_out_fen"] == before["spend_out_fen"]
    assert after["net_fen"] == before["net_fen"]


def test_plans_page_knows_which_cards_can_fund_a_plan(bank: Path) -> None:
    """计划页要能解释「为什么只有一张卡可选」：信用卡下发了但 can_save=False。"""
    _new_goal()
    data = get("/api/business/plans").json()
    accounts = data["funding_accounts"]
    debit = [item for item in accounts if item["type"] == "debit"]
    credit = [item for item in accounts if item["type"] == "credit"]
    assert debit and all(item["can_save"] is True for item in debit)
    assert credit and all(item["can_save"] is False for item in credit)
    assert all("尾号" in item["label"] for item in accounts)
    goals = data["saved_plans"]["items"]
    assert len(goals) == 1
    assert goals[0]["state"] == "active"
    assert goals[0]["account_label"].startswith("工资储蓄卡")
    assert goals[0]["account_label"].endswith(str(_account()["tail"]))


def test_pausing_stops_the_clock_but_keeps_the_money(bank: Path) -> None:
    """暂停：不再按期划，已经划进去的钱原地留着，继续时从下个周期起算。"""
    account = _account()
    goal = _new_goal()["goal"]
    amount = int(_deposit(goal["goal_id"]).json()["deposit"]["amount_fen"])
    paused = post("/api/savings/goals/" + goal["goal_id"] + "/pause")
    assert paused.status_code == 200, paused.text
    assert paused.json()["goal"]["state"] == "paused"
    assert paused.json()["goal"]["saved_fen"] == amount
    with db.open_conn() as conn:
        conn.execute(
            "UPDATE savings_goals SET next_due_ms = ? WHERE id = ?",
            (now_ms() - 1000, goal["goal_id"]),
        )
        conn.commit()
        result = savings.run_due_goals(conn)
    assert result["deposited"] == 0
    assert _balance(str(account["id"])) == int(account["balance_fen"]) - amount
    resumed = post("/api/savings/goals/" + goal["goal_id"] + "/resume")
    assert resumed.status_code == 200, resumed.text
    assert resumed.json()["goal"]["state"] == "active"
    assert resumed.json()["goal"]["next_due_ms"] > now_ms()

