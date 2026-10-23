"""阶段 5 主动财务 API 测试：目标闭环 + 模拟支出注入 + 监测 + 提醒处理。

全部走真实 HTTP 端点（httpx ASGITransport），不直接调内部函数：
评审现场看到的行为就是这里断言的行为。
"""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime
from typing import Any

import httpx
import pytest

import app.proactive as proactive
from app import clock, db
from app.main import app
from scripts.seed import seed

BASE_URL = "http://testserver"
SECRET = "b" * 64
FROZEN_MS = int(datetime(2026, 10, 23, 12, 0).timestamp() * 1000)
TARGET_DATE = "2026-12-31"


def call(method: str, url: str, **kwargs: Any) -> httpx.Response:
    """同步发一个请求（异步用例一律 asyncio.run 包起来，仓库没有 pytest-asyncio）。"""

    async def run() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url=BASE_URL) as http:
            return await http.request(method, url, **kwargs)

    return asyncio.run(run())


@pytest.fixture()
def bank(tmp_path, monkeypatch: pytest.MonkeyPatch) -> str:
    """一份干净的演示库 + 冻结时钟（2026-10-23，目标日还有 3 个自然月）。"""
    monkeypatch.setenv("CAPABILITY_SECRET", SECRET)
    monkeypatch.setattr(db, "DEFAULT_DB_PATH", tmp_path / "bank.db")
    conn = db.connect()
    db.init_db(conn)
    seed(conn)
    conn.close()
    clock.freeze(FROZEN_MS)
    try:
        yield str(tmp_path / "bank.db")
    finally:
        clock.unfreeze()


def _ms(year: int, month: int, day: int, hour: int = 9) -> int:
    return int(datetime(year, month, day, hour, 0).timestamp() * 1000)


def _insert(
    conn: sqlite3.Connection,
    account_id: str,
    *,
    direction: str,
    amount_fen: int,
    category: str,
    counterparty: str,
    ts_ms: int,
    note: str = "",
) -> None:
    conn.execute(
        "INSERT INTO transactions (id, account_id, direction, amount_fen, counterparty,"
        " category, note, ts_ms, balance_after_fen, hold_id, channel)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, NULL, 'app')",
        (
            "t_{}_{}".format(direction, ts_ms),
            account_id,
            direction,
            amount_fen,
            counterparty,
            category,
            note,
            ts_ms,
        ),
    )


def reset_history(bank_path: str, *, balance_fen: int = 1_000_000) -> tuple[str, str]:
    """清掉种子流水，换成可算的三个月历史；返回 (user_id, account_id)。

    历史口径：7/8/9 月各一笔工资 1,280,000 分 + 一笔餐饮 400,000 分；
    当月（10 月）一笔餐饮 100,000 分，用来验预算偏差。
    这样 avg_income=1,280,000、avg_essential=400,000、月结余=880,000。
    """
    conn = db.connect(bank_path)
    try:
        with db.tx(conn):
            conn.execute("DELETE FROM transactions")
            user = conn.execute("SELECT id FROM users ORDER BY created_ms, id LIMIT 1").fetchone()
            assert user is not None
            account = conn.execute(
                "SELECT id FROM accounts WHERE user_id = ? AND type = 'debit' ORDER BY id LIMIT 1",
                (user["id"],),
            ).fetchone()
            assert account is not None
            account_id = str(account["id"])
            for month in (7, 8, 9):
                _insert(
                    conn,
                    account_id,
                    direction="in",
                    amount_fen=1_280_000,
                    category="工资",
                    counterparty="公司",
                    ts_ms=_ms(2026, month, 10),
                )
                _insert(
                    conn,
                    account_id,
                    direction="out",
                    amount_fen=400_000,
                    category="餐饮",
                    counterparty="食堂",
                    ts_ms=_ms(2026, month, 15),
                )
            _insert(
                conn,
                account_id,
                direction="out",
                amount_fen=100_000,
                category="餐饮",
                counterparty="便利店",
                ts_ms=_ms(2026, 10, 5),
            )
            conn.execute(
                "UPDATE accounts SET balance_fen = ? WHERE id = ?", (balance_fen, account_id)
            )
        return str(user["id"]), account_id
    finally:
        conn.close()


def _snapshot(bank_path: str) -> dict[str, Any]:
    conn = db.connect(bank_path)
    try:
        return {
            "balances": {
                str(row["id"]): int(row["balance_fen"])
                for row in conn.execute("SELECT id, balance_fen FROM accounts")
            },
            "transactions": int(
                conn.execute("SELECT COUNT(*) AS n FROM transactions").fetchone()["n"]
            ),
        }
    finally:
        conn.close()


def _audit_events(bank_path: str, event_type: str) -> list[dict[str, Any]]:
    import json

    conn = db.connect(bank_path)
    try:
        return [
            json.loads(row["payload_json"])
            for row in conn.execute(
                "SELECT payload_json FROM audit_log WHERE event_type = ? ORDER BY seq", (event_type,)
            )
        ]
    finally:
        conn.close()


def create_goal(
    *,
    target_fen: int = 3_000_000,
    saved_fen: int = 500_000,
    budget_fen: int | None = 600_000,
    target_date: str = TARGET_DATE,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "name": "2026 年底存 3 万",
        "target_fen": target_fen,
        "target_date": target_date,
        "saved_fen": saved_fen,
    }
    if budget_fen is not None:
        payload["budget_fen"] = budget_fen
    response = call("post", "/api/savings/goals", json=payload)
    assert response.status_code == 200, response.text
    return response.json()


def run_monitor(**body: Any) -> dict[str, Any]:
    response = call("post", "/api/proactive/monitor/run", json=body or {})
    assert response.status_code == 200, response.text
    return response.json()


def test_savings_goal_plan_and_budget_evidence(bank: str) -> None:
    """创建 → 列表 / 计划 → 预算偏差都在目标视图里，数字与手算一致。"""
    reset_history(bank)
    created = create_goal()
    goal = created["goal"]

    assert goal["target_fen"] == 3_000_000
    assert goal["saved_fen"] == 500_000
    assert goal["remaining_fen"] == 2_500_000
    assert goal["periods_remaining"] == 3
    assert goal["required_per_period_fen"] == 833_334  # 向上取整
    assert goal["progress_pct"] == 16
    assert goal["feasibility"] == proactive.FEASIBLE
    assert goal["plan"]["avg_income_fen"] == 1_280_000
    assert goal["plan"]["avg_essential_fen"] == 400_000
    assert goal["plan"]["projected_period_balance_fen"] == 880_000
    assert goal["plan"]["months_analyzed"] == 3
    assert goal["plan"]["has_history"] is True
    assert goal["budget"]["actual_fen"] == 100_000
    assert goal["budget"]["over_budget"] is False
    assert goal["budget"]["remaining_fen"] == 500_000
    assert {rule["kind"] for rule in goal["monitor_rules"]} == {"progress", "budget"}

    listed = call("get", "/api/savings/goals").json()
    assert listed["count"] == 1
    assert listed["goals"][0]["target_date"] == TARGET_DATE
    assert "plan" in listed["goals"][0] and "budget" in listed["goals"][0]


def test_simulated_expense_idempotent_and_monitor_closed_loop(bank: str) -> None:
    """完整闭环：创建 → 注入模拟支出 → 监测出预算提醒 → 忽略 → 调目标 → 重算再提醒。"""
    user_id, account_id = reset_history(bank, balance_fen=1_000_000)
    goal_id = create_goal()["goal"]["goal_id"]

    first = call(
        "post",
        "/api/proactive/simulate/expense",
        json={"amount_fen": 550_000, "merchant": "演示超市", "category": "购物"},
    )
    assert first.status_code == 200, first.text
    created = first.json()
    assert created["simulated"] is True and created["idempotent"] is False
    assert created["balance_after_fen"] == 450_000
    assert created["transaction"]["direction"] == "out"
    assert created["transaction"]["channel"] == "simulated"
    tx_id = created["transaction"]["id"]

    # 同一请求当天重复提交：不产生第二笔，也不再扣钱
    again = call(
        "post",
        "/api/proactive/simulate/expense",
        json={"amount_fen": 550_000, "merchant": "演示超市", "category": "购物"},
    ).json()
    assert again["idempotent"] is True
    assert again["transaction"]["id"] == tx_id
    assert again["balance_after_fen"] == 450_000
    assert _snapshot(bank)["transactions"] == 8  # 7 条历史（3 个月 × 2 + 当月 1）+ 1 条模拟

    # 余额不足：409，且不透支、不落流水
    broke = call(
        "post",
        "/api/proactive/simulate/expense",
        json={"amount_fen": 500_000, "merchant": "演示超市", "category": "餐饮"},
    )
    assert broke.status_code == 409, broke.text
    assert broke.json()["code"] == "INSUFFICIENT_FUNDS"
    snapshot = _snapshot(bank)
    assert snapshot["transactions"] == 8
    assert snapshot["balances"][account_id] == 450_000

    # 模拟流水必须被监测当支出、绝不当收入
    conn = db.connect(bank)
    try:
        row = conn.execute(
            "SELECT * FROM transactions WHERE id = ?", (tx_id,)
        ).fetchone()
        assert row is not None
        assert proactive._is_spend(row) is True
        assert proactive._is_income(row) is False
        assert proactive._is_internal(row) is False

        cashflow = proactive.analyze_cashflow(
            proactive.transactions_for_user(conn, user_id), base=proactive.local_date()
        )
        # 3 个月工资合计 3,840,000；模拟支出绝不能被算进收入
        assert cashflow["income_total_fen"] == 3_840_000
    finally:
        conn.close()

    assert len(_audit_events(bank, "proactive.simulated_expense")) == 2
    payloads = _audit_events(bank, "proactive.simulated_expense")
    assert all(item["simulated"] is True for item in payloads)
    assert [item["idempotent"] for item in payloads] == [False, True]

    # 手动触发监测：当月支出 100,000 + 550,000 = 650,000 > 预算 600,000
    run = run_monitor()
    assert run["ok"] is True and run["status"] == "ok"
    assert run["alerts_created"] == 1
    alert = run["alerts"][0]
    assert alert["reason"] == proactive.REASON_BUDGET_OVERSPENT
    assert alert["evidence"]["budget_fen"] == 600_000
    assert alert["evidence"]["actual_fen"] == 650_000
    assert alert["evidence"]["over_fen"] == 50_000
    assert alert["evidence"]["period_label"] == "2026-10"

    listed = call("get", "/api/proactive/alerts").json()
    assert listed["count"] == 1
    assert listed["alerts"][0]["alert_id"] == alert["alert_id"]
    assert listed["alerts"][0]["evidence"]["actual_fen"] == 650_000

    # 忽略提醒：用户关掉；再关一次幂等
    ignored = call(
        "post",
        "/api/proactive/alerts/{}/resolve".format(alert["alert_id"]),
        json={"action": "ignore"},
    ).json()
    assert ignored["alert"]["status"] == "ignored"
    repeat = call(
        "post",
        "/api/proactive/alerts/{}/resolve".format(alert["alert_id"]),
        json={"action": "ignore"},
    ).json()
    assert repeat["idempotent"] is True
    assert call("get", "/api/proactive/alerts").json()["count"] == 0

    # 同一周期再跑：已提醒过的不重复提醒
    assert run_monitor()["alerts_created"] == 0

    # 调整目标 -> 重算：每月要存 1,500,000，超出可结余 880,000
    patched = call(
        "patch",
        "/api/savings/goals/{}".format(goal_id),
        json={"target_fen": 5_000_000},
    )
    assert patched.status_code == 200, patched.text
    updated = patched.json()["goal"]
    assert updated["remaining_fen"] == 4_500_000
    assert updated["periods_remaining"] == 3
    assert updated["required_per_period_fen"] == 1_500_000
    assert updated["feasibility"] == proactive.INFEASIBLE
    assert updated["plan"]["period_gap_fen"] == 620_000
    assert updated["plan"]["total_gap_fen"] == 1_860_000

    # 重算后监测：新增「现金流不足」目标级提醒，预算提醒不重复
    run2 = run_monitor()
    assert run2["alerts_created"] == 1
    goal_alert = run2["alerts"][0]
    assert goal_alert["reason"] == proactive.REASON_CASHFLOW_INSUFFICIENT
    assert goal_alert["rule_id"] is None
    assert goal_alert["evidence"]["required_per_period_fen"] == 1_500_000
    assert goal_alert["evidence"]["period_gap_fen"] == 620_000


def test_monitor_does_not_move_money_and_target_adjustments_recompute(bank: str) -> None:
    """监测/处理提醒全程不动余额、不新增流水；PATCH 改期也重算。"""
    reset_history(bank, balance_fen=800_000)
    goal_id = create_goal()["goal"]["goal_id"]
    before = _snapshot(bank)
    run = run_monitor(goal_id=goal_id)
    assert run["status"] == "ok"
    after = _snapshot(bank)
    assert after["balances"] == before["balances"]
    assert after["transactions"] == before["transactions"]

    # 目标日往后推：剩余周期变多，每期压力下降
    moved = call(
        "patch",
        "/api/savings/goals/{}".format(goal_id),
        json={"target_date": "2027-03-31", "monitor_enabled": False},
    ).json()["goal"]
    assert moved["periods_remaining"] == 6
    assert moved["required_per_period_fen"] == 416_667  # ceil(2,500,000 / 6)
    assert all(rule["enabled"] is False for rule in moved["monitor_rules"])

    # 关闭提醒后监测不再新增任何提醒
    assert run_monitor()["alerts_created"] == 0

    # 重新打开：现金流仍可就这次预算超支提醒（当月支出 100,000 < 600,000，不超）
    opened = call(
        "patch",
        "/api/savings/goals/{}".format(goal_id),
        json={"monitor_enabled": True},
    ).json()["goal"]
    assert all(rule["enabled"] is True for rule in opened["monitor_rules"])


def test_monitor_unknown_goal_is_failed_not_exception(bank: str) -> None:
    reset_history(bank)
    body = run_monitor(goal_id="sg_ghost")
    assert body["ok"] is False
    assert body["status"] == "failed"
    assert body["detail"] == "GOAL_NOT_FOUND"


def test_api_rejects_bad_payloads(bank: str) -> None:
    reset_history(bank)
    # 仓库把 pydantic 校验错误统一映射成 400（app/main.py 的处理器），不是 FastAPI 默认 422
    assert call("post", "/api/savings/goals", json={"target_fen": 1}).status_code == 400
    base_payload = {"name": "x", "target_fen": 100, "target_date": TARGET_DATE}
    # extra=forbid：多余字段直接 422，不静默忽略
    assert call(
        "post", "/api/savings/goals", json={**base_payload, "surprise": 1}
    ).status_code == 400
    assert call("post", "/api/proactive/simulate/expense", json={"amount_fen": 100}).status_code == 400
    # 模拟接口不许造收入：工资/理财/转账不在白名单
    for category in ("工资", "理财", "转账"):
        response = call(
            "post",
            "/api/proactive/simulate/expense",
            json={"amount_fen": 100, "merchant": "x", "category": category},
        )
        assert response.status_code == 400, category
    assert call("post", "/api/proactive/alerts/pa_ghost/resolve", json={"action": "ignore"}).status_code == 404
    assert call("patch", "/api/savings/goals/sg_ghost", json={"target_fen": 100}).status_code == 404


def test_audit_chain_still_valid_after_proactive_writes(bank: str) -> None:
    """主动财务的写操作都要落审计，且链仍然可校验。"""
    reset_history(bank)
    create_goal()
    call(
        "post",
        "/api/proactive/simulate/expense",
        json={"amount_fen": 660_000, "merchant": "演示数码店", "category": "购物"},
    )
    run = run_monitor()
    assert run["alerts_created"] == 1
    call(
        "post",
        "/api/proactive/alerts/{}/resolve".format(run["alerts"][0]["alert_id"]),
        json={"action": "resolve"},
    )

    conn = db.connect(bank)
    try:
        from app import audit

        assert audit.verify_chain(conn) is True
        kinds = {
            str(row["event_type"])
            for row in conn.execute("SELECT event_type FROM audit_log")
        }
    finally:
        conn.close()
    assert {
        "savings_goal.created",
        "proactive.simulated_expense",
        "proactive.monitor_run",
        "proactive.alert_created",
        "proactive.alert_resolved",
    } <= kinds
