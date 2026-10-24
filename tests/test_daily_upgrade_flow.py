"""E2 升级路径：黄卡确认之后、提交之前，日累计被别的交易推过线。

这是并发时序里最危险的一条：用户在确认卡上点的是「黄」，钱却要按红档出。
正确行为是资金层拒绝整笔、执行器拿最新累计重新裁决、旧的黄确认作废，用户补完
人脸+短信后同一笔才落账——而不是「确认过就放行」，也不是「验完也永远不放行」。

本用例专门盯 execute_node 的 DAILY_RISK_UPGRADE_REQUIRED → _retry_after_upgrade
这一段，包括重试时旧的黄色 hold 不能把新红令牌顶回去。
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from app import clock, db
from app.policy import rules
from app.planner import executor
from scripts.seed import seed
from tests import mfa, test_planner as harness

SECRET = "a" * 64


@pytest.fixture()
def bank(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("CAPABILITY_SECRET", SECRET)
    monkeypatch.setattr(db, "DEFAULT_DB_PATH", tmp_path / "bank.db")
    conn = db.connect()
    db.init_db(conn)
    seed(conn)
    conn.close()
    executor.RUNS.clear()
    yield db.DEFAULT_DB_PATH
    executor.RUNS.clear()


def _seed_spend(amount_fen: int = 90_000) -> None:
    """把当日累计推到 900 元：80_000 的新转账会越线。"""
    conn = db.connect()
    row = conn.execute("SELECT id, user_id FROM accounts WHERE type='debit' LIMIT 1").fetchone()
    day = datetime.fromtimestamp(clock.now_ms() // 1000).strftime("%Y-%m-%d")
    conn.execute(
        "INSERT INTO daily_limits (account_id, day, spent_fen, tx_count) VALUES (?, ?, ?, 2)",
        (row["id"], day, amount_fen),
    )
    conn.execute(
        "INSERT INTO daily_user_limits (user_id, day, spent_fen, tx_count) VALUES (?, ?, ?, 2)",
        (row["user_id"], day, amount_fen),
    )
    conn.close()


def _audit_kinds(session_id: str) -> list[str]:
    conn = db.connect()
    try:
        return [
            str(r[0])
            for r in conn.execute(
                "SELECT event_type FROM audit_log WHERE session_id=? ORDER BY seq", (session_id,)
            )
        ]
    finally:
        conn.close()


def test_yellow_confirmation_cannot_settle_once_the_day_moves(
    bank: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_issue = executor.issue_token
    seeded: dict[str, bool] = {"done": False}

    def issue_then_move_the_day(state, action, decision):  # type: ignore[no-untyped-def]
        # 模拟另一个会话在「确认已点、钱还没扣」的窗口里提交了一笔 900 元。
        if action.tool == "transfer" and not seeded["done"]:
            seeded["done"] = True
            _seed_spend()
        return real_issue(state, action, decision)

    monkeypatch.setattr(executor, "issue_token", issue_then_move_the_day)

    events = harness.drive(
        "s_upgrade",
        harness.transfer_dag(amount_fen=80_000),
        answers=[{"approve": True}, {"approve": True, "factor": "face+sms"}],
    )

    kinds = _audit_kinds("s_upgrade")
    assert "policy.upgraded" in kinds, "升级必须留痕，不能悄悄换一张卡"

    factor = harness.payload(events, "factor")
    assert sorted(factor["required"]) == ["face", "sms"]
    assert factor.get("rule_id") == "R07_transfer_red_daily"

    assert harness.balance(db.connect()) == 1_286_050 - 80_000, "验完强验证之后这笔应该落账"
    assert funds_spent() == 90_000 + 80_000


def funds_spent() -> int:
    conn = db.connect()
    try:
        from app import funds

        row = conn.execute("SELECT id FROM accounts WHERE type='debit' LIMIT 1").fetchone()
        return funds.spent_today_fen(conn, str(row["id"]))
    finally:
        conn.close()
