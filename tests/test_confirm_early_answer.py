"""确认卡答案「早到即丢」竞态：答案比卡片先到时，也必须被接住。

背景：执行器先把确认卡推给前端（app/planner/executor.py:365 的 emit_event），
之后才 await wait_for_answer(conf)（:375）。以前 future 是**惰性**建的
（app/planner/state.py:202-205），而 resolve_pending 只把答案递给
「已经有 future」的确认（state.py:224-228）——于是程序化客户端
（IM Bot、自动化脚本）收到 confirm 帧就立刻 POST /api/confirm 时，
那条答案会被**静默丢掉**，执行器只能干等到 120 秒 CONFIRM_TIMEOUT。

本文件用三枚钉子把它钉住：

1. 结构钉子（test_confirmation_future_exists_right_after_creation）：
   new_confirmation 返回时 conf.future 必须已经存在——
   「答案可能先到」要被结构性接受，不能靠运气。
2. 行为钉子（test_early_answer_is_not_lost_on_yellow_card）：
   在 emit 回调**内部**立刻回答（模拟 IM Bot 收到帧就回），
   不能出现 CONFIRM_TIMEOUT，钱要真的转出去。
3. 红灯因子钉子（test_early_factor_answer_is_not_lost_on_red_card）：
   2000 元触发 R08（要 face+sms），同样在 emit 回调里带因子回答，
   必须放行、扣款正确、不出现 CONFIRM_TIMEOUT。

反证：把 future 的创建挪回「惰性建卡」（改回旧行为），
2、3 两条会立刻变红（错误码 CONFIRM_TIMEOUT、钱没转出去）。
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

import pytest

from app import audit, db
from app.models import Action, Dag, Decision
from app.planner import dag as planner_dag
from app.planner import executor
from app.planner import state as planner_state
from scripts.seed import seed

SECRET = "a" * 64
LIJING_AMOUNT_FEN = 50_000  # 500 元 -> R06 黄牌（单笔上限 100_000 分）
BIG_AMOUNT_FEN = 200_000  # 2000 元 -> R08 红灯（要 face+sms）
SEED_BALANCE_FEN = 1_286_050
SEED_TRANSACTIONS = 320
EARLY_TIMEOUT_MS = 300  # 旧行为会在这窗口里等满并作废


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


def transfer_dag(amount_fen: int) -> Dag:
    return planner_dag.build(
        {"intent": "transfer", "slots": {"payee_name": "李静", "amount_fen": amount_fen}}
    )


def names(events: list[tuple[str, dict[str, Any]]]) -> list[str]:
    return [name for name, _ in events]


def payload(events: list[tuple[str, dict[str, Any]]], name: str) -> dict[str, Any]:
    return next(data for event, data in events if event == name)


def drive_answering_in_emit(
    session_id: str,
    dag_obj: Dag,
    *,
    confirm_timeout_ms: int = EARLY_TIMEOUT_MS,
    factor: str | None = None,
) -> tuple[list[tuple[str, dict[str, Any]]], float]:
    """跑一轮，答案在 emit 回调**内部**就给出。

    这正是程序化客户端的时序：收到确认帧 -> 立刻 POST /api/confirm。
    旧实现里这条答案会被 resolve_pending 丢掉（那时 future 还没建），
    执行器只能等满 confirm_timeout_ms 再作废，钱也转不出去。
    """
    events: list[tuple[str, dict[str, Any]]] = []

    async def emit(name: str, data: dict[str, Any]) -> None:
        events.append((name, dict(data)))
        if name in ("confirm", "factor"):
            await executor.resume(
                session_id, str(data["confirmation_id"]), True, factor=factor
            )

    started = time.monotonic()
    asyncio.run(
        executor.run(session_id, dag_obj, emit, confirm_timeout_ms=confirm_timeout_ms)
    )
    return events, time.monotonic() - started


def _balance() -> int:
    conn = db.connect()
    try:
        return int(
            conn.execute(
                "SELECT balance_fen FROM accounts WHERE type='debit' LIMIT 1"
            ).fetchone()["balance_fen"]
        )
    finally:
        conn.close()


def _audit_kinds() -> list[str]:
    conn = db.connect()
    try:
        return [
            row["event_type"]
            for row in conn.execute(
                "SELECT event_type FROM audit_log ORDER BY seq"
            ).fetchall()
        ]
    finally:
        conn.close()


def test_confirmation_future_exists_right_after_creation(bank: Path) -> None:
    """结构钉子：确认卡一建出来，future 就必须在。"""
    async def probe() -> None:
        state = planner_state.open_run(
            "s_early_struct", transfer_dag(LIJING_AMOUNT_FEN), None, SECRET
        )
        try:
            node = state.dag.nodes[-1]
            action = Action(
                tool="transfer",
                params={"amount_fen": LIJING_AMOUNT_FEN},
                account_id=state.account_id,
                amount_fen=LIJING_AMOUNT_FEN,
            )
            decision = Decision(
                level="yellow",
                rule_id="R06_transfer_yellow",
                reason_code="TRANSFER_YELLOW",
                reason_text="这笔要你点头才转。",
                required_factors=[],
                limit_fen=100_000,
                spent_today_fen=0,
            )
            conf = planner_state.new_confirmation(state, node, action, decision)
            assert conf.future is not None, (
                "确认卡一建出来就必须有 future：答案可能早到（IM Bot 收到帧就回），"
                "future 建晚了那条答案会被 resolve_pending 静默丢掉。"
            )
            assert not conf.future.done()
        finally:
            state.conn.close()

    asyncio.run(probe())


def test_early_answer_is_not_lost_on_yellow_card(bank: Path) -> None:
    """行为钉子（黄牌）：答案在 emit 回调里就给出，必须被接住，不能等超时。"""
    events, elapsed = drive_answering_in_emit(
        "s_early_yellow", transfer_dag(LIJING_AMOUNT_FEN)
    )

    assert "confirm" in names(events)
    errors = [data for name, data in events if name == "error"]
    assert not [e for e in errors if e.get("code") == "CONFIRM_TIMEOUT"], (
        "早到的答案被丢掉了：执行器一直等到超时才作废。"
    )
    tools = [data for name, data in events if name == "tool"]
    assert any(t.get("ok") and t.get("node_id") == "n3" for t in tools), (
        f"转账节点没有成功：{tools}"
    )
    assert elapsed < 5.0, f"不该等这么久才走完：{elapsed:.1f} 秒"

    assert _balance() == SEED_BALANCE_FEN - LIJING_AMOUNT_FEN
    conn = db.connect()
    try:
        assert (
            conn.execute("SELECT COUNT(*) AS c FROM transactions").fetchone()["c"]
            == SEED_TRANSACTIONS + 1
        )
        assert (
            conn.execute(
                "SELECT COUNT(*) AS c FROM holds WHERE status='held'"
            ).fetchone()["c"]
            == 0
        )
        assert audit.verify_chain(conn) is True
    finally:
        conn.close()
    assert "confirm.approved" in _audit_kinds()


def test_early_factor_answer_is_not_lost_on_red_card(bank: Path) -> None:
    """红灯因子钉子：2000 元要 face+sms，答案同样在 emit 回调里给出。"""
    events, elapsed = drive_answering_in_emit(
        "s_early_red", transfer_dag(BIG_AMOUNT_FEN), factor="face+sms"
    )

    assert "factor" in names(events)
    card = payload(events, "factor")
    assert card["level"] == "red"
    assert sorted(card["required"]) == ["face", "sms"]
    errors = [data for name, data in events if name == "error"]
    assert not [e for e in errors if e.get("code") == "CONFIRM_TIMEOUT"], (
        "红灯下早到的因子答案同样被丢掉了。"
    )
    tools = [data for name, data in events if name == "tool"]
    assert any(t.get("ok") and t.get("node_id") == "n3" for t in tools), (
        f"红灯转账没有成功：{tools}"
    )
    assert elapsed < 5.0, f"不该等这么久才走完：{elapsed:.1f} 秒"

    assert _balance() == SEED_BALANCE_FEN - BIG_AMOUNT_FEN
    conn = db.connect()
    try:
        assert (
            conn.execute("SELECT COUNT(*) AS c FROM transactions").fetchone()["c"]
            == SEED_TRANSACTIONS + 1
        )
        assert audit.verify_chain(conn) is True
    finally:
        conn.close()
    assert "confirm.approved" in _audit_kinds()
