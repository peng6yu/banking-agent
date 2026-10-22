"""定时转账的到点执行：后台节拍与 /api/scheduled/run 都必须真的把钱划出去。

本文件盯的是一条真实存在过的缺口：execute_due 实现完成、也有单测，
但全仓没有任何生产调用点——用户预约成功之后那笔钱永远不会动，
演示现场看起来就是「定时转账没做完」。

这和 breaker.record_denial 只在单测里被调用是同一类病，
所以这里断言的不是函数返回值，而是「真实入口（HTTP 端点）打进来之后余额确实变了」。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import httpx
import pytest

from app import db
from app.api import routes
from app.main import app
from app.planner import executor, ticker
from scripts.seed import DEBIT_ACCOUNT_ID, PAYEE_LIJING, USER_ID, seed

SECRET = "a" * 64
BASE_URL = "http://testserver"
AMOUNT_FEN = 50_000
SEED_BALANCE_FEN = 1_286_050
DUE_ID = "sch_due01"


@pytest.fixture()
def bank(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("CAPABILITY_SECRET", SECRET)
    monkeypatch.setattr(db, "DEFAULT_DB_PATH", tmp_path / "bank.db")
    conn = db.connect()
    db.init_db(conn)
    seed(conn)
    conn.close()
    executor.RUNS.clear()
    routes._history.clear()
    yield tmp_path / "bank.db"
    executor.RUNS.clear()
    routes._history.clear()


def _conn() -> Any:
    return db.connect()


def _insert(conn: Any, due_ms: int, amount_fen: int = AMOUNT_FEN) -> str:
    """直接写一行预约；不经过工具层，避开头寸问题。"""
    conn.execute(
        "INSERT INTO scheduled_transfers (id, user_id, account_id, payee_id, amount_fen, note,"
        " due_ms, recurrence, status, created_ms, idempotency_key)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'scheduled', ?, ?)",
        (DUE_ID, USER_ID, DEBIT_ACCOUNT_ID, PAYEE_LIJING, amount_fen, "",
         due_ms, "once", ticker_now(), "k_" + DUE_ID),
    )
    return DUE_ID


def ticker_now() -> int:
    from app import clock

    return clock.now_ms()


def _balance() -> int:
    conn = _conn()
    try:
        row = conn.execute("SELECT balance_fen FROM accounts WHERE id = ?", (DEBIT_ACCOUNT_ID,)).fetchone()
        return int(row["balance_fen"])
    finally:
        conn.close()


def _status() -> str:
    conn = _conn()
    try:
        row = conn.execute("SELECT status FROM scheduled_transfers WHERE id = ?", (DUE_ID,)).fetchone()
        return str(row["status"])
    finally:
        conn.close()


def call(url: str) -> httpx.Response:
    """同步打端点；ASGITransport 不占端口。"""

    async def run() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url=BASE_URL) as http:
            return await http.post(url)

    return asyncio.run(run())


def test_endpoint_moves_the_money(bank: Path) -> None:
    """端点打进来要真的扣钱，且第二次不能再扣一次。"""
    assert _balance() == SEED_BALANCE_FEN
    conn = _conn()
    with db.tx(conn):
        _insert(conn, ticker_now() - 1_000)
    conn.close()

    first = call("/api/scheduled/run")
    assert first.status_code == 200
    assert first.json()["executed"] == 1
    assert _balance() == SEED_BALANCE_FEN - AMOUNT_FEN
    assert _status() == "executed"

    second = call("/api/scheduled/run")
    assert second.json()["executed"] == 0
    assert _balance() == SEED_BALANCE_FEN - AMOUNT_FEN


def test_future_row_is_left_alone(bank: Path) -> None:
    """没到点的预约一个字都不能动。"""
    conn = _conn()
    with db.tx(conn):
        _insert(conn, ticker_now() + 86_400_000)
    conn.close()

    assert ticker.run_due_once() == []
    assert _balance() == SEED_BALANCE_FEN
    assert _status() == "scheduled"


def test_huge_interval_is_not_mistaken_for_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """只有 0 表示关闭，负数与非法值都退回默认，不能把节拍关掉。"""
    monkeypatch.setenv(ticker.TICK_ENV, "0")
    assert ticker.tick_interval_ms() == ticker.TICK_OFF
    monkeypatch.setenv(ticker.TICK_ENV, "-5")
    assert ticker.tick_interval_ms() == 0
    monkeypatch.setenv(ticker.TICK_ENV, "abc")
    assert ticker.tick_interval_ms() == ticker.DEFAULT_TICK_MS


def test_start_returns_none_when_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """关掉时不起后台任务，免得测试进程里留一坨永远 sleep 的任务。"""
    monkeypatch.setenv(ticker.TICK_ENV, "0")

    async def run() -> Any:
        return ticker.start()

    assert asyncio.run(run()) is None