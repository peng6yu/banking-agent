"""确认卡超时：确认卡必须在「120 秒没人点头」时真的作废。

这条安全承诺以前只活在文档里：app/planner/executor.py 把毫秒值
（confirm_timeout_ms = 120_000，单位是毫秒）直接当秒传给了 asyncio.wait_for，
于是「120 秒」实际是 120000 秒 ≈ 33.3 小时，确认卡永远不会自己作废。

本文件用三枚钉子把它钉住：

1. 行为钉子（test_confirm_card_expires_on_time）：给一个很小的
   confirm_timeout_ms（50 毫秒），必须在**墙钟**上很快真的超时。
   旧代码会实打实等满 50 **秒**（因为它把 50 当成秒），墙钟断言失败。
2. 单位钉子（test_confirm_timeout_constant_is_exactly_120_seconds）：
   CONFIRM_TIMEOUT_MS 换算成秒必须正好是 120.0，也就是对外承诺的那个数。
3. 对照钉子（test_capability_token_ttl_is_120_seconds_too）：
   令牌那条 120 秒是**毫秒对毫秒**（exp_ms = now_ms() + ttl_ms），
   不是同一个坑——别把两处一起改坏。
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

import pytest

from app import audit, db
from app.models import Action, Dag
from app.planner import dag as planner_dag
from app.planner import executor
from app.planner import state as planner_state
from app.security import token
from scripts.seed import seed

SECRET = "a" * 64
LIJING_AMOUNT_FEN = 50_000  # 500 元 -> R06 黄灯（单笔上限 100_000 分）


@pytest.fixture()
def bank(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("CAPABILITY_SECRET", SECRET)
    monkeypatch.setattr(db, "DEFAULT_DB_PATH", tmp_path / "bank.db")
    conn = db.connect()
    db.init_db(conn)
    seed(conn)
    conn.close()
    executor.RUNS.clear()
    return db.DEFAULT_DB_PATH


def transfer_dag(amount_fen: int = LIJING_AMOUNT_FEN) -> Dag:
    return planner_dag.build(
        {"intent": "transfer", "slots": {"payee_name": "李静", "amount_fen": amount_fen}}
    )


def drive_without_answer(
    session_id: str, dag_obj: Dag, confirm_timeout_ms: int
) -> tuple[list[tuple[str, dict[str, Any]]], float]:
    """跑一轮，**不**替用户回答确认，返回事件流与墙钟耗时（秒）。"""
    events: list[tuple[str, dict[str, Any]]] = []

    async def emit(name: str, data: dict[str, Any]) -> None:
        events.append((name, dict(data)))

    started = time.monotonic()
    asyncio.run(executor.run(session_id, dag_obj, emit, confirm_timeout_ms=confirm_timeout_ms))
    return events, time.monotonic() - started


def names(events: list[tuple[str, dict[str, Any]]]) -> list[str]:
    return [name for name, _ in events]


def payload(events: list[tuple[str, dict[str, Any]]], name: str) -> dict[str, Any]:
    return next(data for event, data in events if event == name)


def test_confirm_card_expires_on_time(bank: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """确认卡到期真的会自己作废：超时、释放、审计、节点标失败，缺一不可。"""
    created: list[Any] = []
    real_new_confirmation = executor.new_confirmation

    def spy(*args: Any, **kwargs: Any) -> Any:
        conf = real_new_confirmation(*args, **kwargs)
        created.append(conf)
        return conf

    monkeypatch.setattr(executor, "new_confirmation", spy)

    events, elapsed = drive_without_answer("s_to_ms", transfer_dag(), confirm_timeout_ms=50)

    # 墙钟断言是这枚钉子的关键：旧代码把 50（毫秒）当 50 秒传给 wait_for，
    # 这一步就会等满 50 秒才返回 -> 断言失败。
    assert elapsed < 5.0, (
        f"确认卡没有在超时窗口内作废：等了 {elapsed:.1f} 秒。"
        "confirm_timeout_ms 的单位是毫秒，旧代码把它当秒传给了 asyncio.wait_for。"
    )

    # 卡片确实弹给用户了
    assert "confirm" in names(events)
    confirm = payload(events, "confirm")
    assert confirm["level"] == "yellow"
    assert confirm["expires_in_s"] == 120

    # 超时作废：错误事件 + 确认卡状态
    error = payload(events, "error")
    assert error["code"] == "CONFIRM_TIMEOUT"
    assert "作废" in error["user_text"]
    assert created, "执行器没有创建确认卡"
    assert created[0].status == "expired"

    conn = db.connect()
    try:
        # 审计：请求过、也被拒了，理由是超时
        rows = conn.execute(
            "SELECT event_type, payload_json FROM audit_log ORDER BY seq"
        ).fetchall()
        kinds = [row["event_type"] for row in rows]
        assert "confirm.requested" in kinds
        expired = [
            row for row in rows if row["event_type"] == "confirm.expired"
        ]
        assert expired, "超时没有留下 confirm.expired 审计"
        assert not [row for row in rows if row["event_type"] == "confirm.rejected"], (
            "超时不是用户拒绝，不应该记 confirm.rejected"
        )
        payload_json = json.loads(expired[0]["payload_json"])
        assert payload_json["reason"] == "TIMEOUT"
        assert payload_json["node_id"] == "n3"

        # 钱没动，也没有留下悬着的预授权
        balance = conn.execute(
            "SELECT balance_fen FROM accounts WHERE type='debit' LIMIT 1"
        ).fetchone()["balance_fen"]
        assert balance == 1_286_050
        assert conn.execute("SELECT COUNT(*) AS c FROM transactions").fetchone()["c"] == 320
        held = conn.execute(
            "SELECT COUNT(*) AS c FROM holds WHERE status='held'"
        ).fetchone()["c"]
        assert held == 0, "超时后还有悬着的预授权"

        # 节点标失败并带错误码
        node = conn.execute(
            "SELECT state, result_json FROM dag_nodes WHERE node_id='n3'"
        ).fetchone()
        assert node["state"] == "failed"
        assert json.loads(node["result_json"])["error_code"] == "CONFIRM_TIMEOUT"

        # 审计链没被这次作废打断
        assert audit.verify_chain(conn) is True
    finally:
        conn.close()


def test_confirm_timeout_constant_is_exactly_120_seconds() -> None:
    """对外承诺的是 120 秒：常量是毫秒，换算后必须正好 120.0 秒。"""
    assert planner_state.CONFIRM_TIMEOUT_MS == 120_000
    assert planner_state.ms_to_seconds(planner_state.CONFIRM_TIMEOUT_MS) == 120.0


def test_capability_token_ttl_is_120_seconds_too() -> None:
    """令牌那条 120 秒是毫秒对毫秒（app/security/token.py:140/:238），不是同一个坑。"""
    from app.clock import now_ms

    issued = token.issue(
        Action(tool="transfer", params={"payee_id": "p_1d6f92b0"}, account_id="a_3d7e60c2", amount_fen=1),
        level="yellow",
        secret=SECRET,
    )
    decoded = token.decode(issued, secret=SECRET)
    remaining_ms = decoded.exp_ms - now_ms()
    assert 119_000 <= remaining_ms <= 120_000
    assert planner_state.ms_to_seconds(remaining_ms) == pytest.approx(120.0, abs=1.0)
