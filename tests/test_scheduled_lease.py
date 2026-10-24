"""预约转账的两种「时间维度」缺陷：多进程重复执行、以及因子授权永不过期。

1) 租约（claim_token + claimed_ms）：到点执行先原子抢行，抢不到就跳过。
   以前只靠「读一遍 status='scheduled' 再执行」，两个进程同时 tick 会各执行一次，
   同一笔预约扣两次钱——这是只有并发时才现形、但一旦现形就是资金错误的缺陷。

2) 因子保质期（AUTH_TTL_MS=30 天）：创建期验过人脸+短信，不等于这张脸永久有效。
   过期后这笔预约仍然有效，但不再享受「越额度放行」，回到普通额度口径排队。

反证：
- 去掉 _claim 的 WHERE 条件（或改成先 SELECT 再 UPDATE）→ 第 1 组变红；
- 把 verified 改回只看 factor_verified_ms>0 → 第 2 组的过期用例变红。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest

from app import clock, db
from app.tools import scheduled
from scripts.seed import DEBIT_ACCOUNT_ID, PAYEE_LIJING, USER_ID, seed

SECRET = "0123456789abcdef" * 4
START_BALANCE_FEN = 1_286_050
AMOUNT_FEN = 50_000  # 500 元：黄灯档，不依赖因子
BIG_AMOUNT_FEN = 200_000  # 2000 元：超过单笔 1000 元上限，需要创建期因子验证


@pytest.fixture()
def bank(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("CAPABILITY_SECRET", SECRET)
    monkeypatch.setattr(db, "DEFAULT_DB_PATH", tmp_path / "bank.db")
    conn = db.connect()
    db.init_db(conn)
    seed(conn)
    conn.close()
    return db.DEFAULT_DB_PATH


def _conn() -> sqlite3.Connection:
    conn = db.connect()
    db.init_db(conn)
    return conn


def _balance(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT balance_fen FROM accounts WHERE id = ?", (DEBIT_ACCOUNT_ID,)).fetchone()
    return int(row["balance_fen"])


def _insert(
    conn: sqlite3.Connection,
    *,
    sched_id: str,
    amount_fen: int = AMOUNT_FEN,
    due_ms: int | None = None,
    verified_ms: int = 0,
    status: str = "scheduled",
    claim_token: str = "",
    claimed_ms: int = 0,
) -> None:
    now = clock.now_ms()
    conn.execute(
        "INSERT INTO scheduled_transfers (id, user_id, account_id, payee_id, amount_fen, note,"
        " due_ms, recurrence, status, created_ms, executed_ms, factor_verified_ms, defer_count,"
        " idempotency_key, claim_token, claimed_ms)"
        " VALUES (?,?,?,?,?,'',?,'once',?,?,0,?,0,?,?,?)",
        (sched_id, USER_ID, DEBIT_ACCOUNT_ID, PAYEE_LIJING, amount_fen,
         now if due_ms is None else due_ms, status, now, verified_ms, f"idem-{sched_id}",
         claim_token, claimed_ms),
    )


def _row(conn: sqlite3.Connection, sched_id: str) -> Any:
    return conn.execute("SELECT * FROM scheduled_transfers WHERE id = ?", (sched_id,)).fetchone()


def _audit_kinds(conn: sqlite3.Connection, sched_id: str) -> list[str]:
    rows = conn.execute(
        "SELECT event_type FROM audit_log WHERE session_id = ? ORDER BY seq",
        (f"sched:{sched_id}",),
    ).fetchall()
    return [str(row["event_type"]) for row in rows]


# ------------------------------------------------------------------ 租约


def test_a_row_already_held_by_another_worker_is_skipped(bank: Path) -> None:
    """另一个 worker 正持有租约：这一轮必须什么都不做，钱一分不动。"""
    conn = _conn()
    try:
        _insert(conn, sched_id="sch_held", claim_token="otherworker", claimed_ms=clock.now_ms())
        results = scheduled.execute_due(conn, secret=SECRET)
        assert [r["status"] for r in results] == ["skipped"]
        assert results[0]["reason"] == "CLAIMED_BY_ANOTHER_WORKER"
        assert _balance(conn) == START_BALANCE_FEN, "被别的 worker 持有的行不能动钱"
        assert str(_row(conn, "sch_held")["status"]) == "scheduled"
    finally:
        conn.close()


def test_a_stale_lease_is_taken_over_after_it_expires(bank: Path) -> None:
    """持有者崩了、租约过期：下一轮必须能接管，否则这笔预约永远卡死。"""
    conn = _conn()
    try:
        stale = clock.now_ms() - scheduled.CLAIM_LEASE_MS - 1_000
        _insert(conn, sched_id="sch_stale", claim_token="deadworker", claimed_ms=stale)
        results = scheduled.execute_due(conn, secret=SECRET)
        assert [r["status"] for r in results] == ["executed"]
        assert _balance(conn) == START_BALANCE_FEN - AMOUNT_FEN
        row = _row(conn, "sch_stale")
        assert str(row["claim_token"]) == "", "执行完必须把租约放掉，否则下一期（月付）抢不到"
        assert str(row["status"]) == "executed"
    finally:
        conn.close()


def test_claim_is_released_even_when_execution_fails(bank: Path) -> None:
    """执行炸了也要放租约：不然一次失败会让这笔预约被锁 10 分钟再重试。"""
    conn = _conn()
    try:
        # 余额不足 -> funds 抛 InsufficientFunds -> _execute_one 标 failed
        conn.execute("UPDATE accounts SET balance_fen = 100 WHERE id = ?", (DEBIT_ACCOUNT_ID,))
        _insert(conn, sched_id="sch_broke")
        results = scheduled.execute_due(conn, secret=SECRET)
        assert results[0]["status"] == "failed"
        assert str(_row(conn, "sch_broke")["claim_token"]) == ""
        assert str(_row(conn, "sch_broke")["status"]) == "failed"
    finally:
        conn.close()


def test_a_cancelled_row_is_not_claimed(bank: Path) -> None:
    """取消掉的行即便到了 due_ms 也不能被抢到（WHERE 里带 status）。"""
    conn = _conn()
    try:
        _insert(conn, sched_id="sch_cancel", status="cancelled")
        results = scheduled.execute_due(conn, secret=SECRET)
        assert results == []
        assert _balance(conn) == START_BALANCE_FEN
    finally:
        conn.close()


# ------------------------------------------------------------------ 因子保质期


def test_fresh_factor_authorization_still_bypasses_the_daily_line(bank: Path) -> None:
    """对照组：昨天验过的人脸仍然能放行 2000 元（当日额度 1000 不够）。"""
    conn = _conn()
    try:
        fresh = clock.now_ms() - 24 * 3600 * 1000
        _insert(conn, sched_id="sch_fresh", amount_fen=BIG_AMOUNT_FEN, verified_ms=fresh)
        results = scheduled.execute_due(conn, secret=SECRET)
        assert results[0]["status"] == "executed"
        assert _balance(conn) == START_BALANCE_FEN - BIG_AMOUNT_FEN
        assert "scheduled.auth_expired" not in _audit_kinds(conn, "sch_fresh")
    finally:
        conn.close()


def test_expired_factor_authorization_loses_the_quota_bypass(bank: Path) -> None:
    """31 天前验过的人脸：预约仍有效，但不再越额度放行，回到排队（顺延）。"""
    conn = _conn()
    try:
        old = clock.now_ms() - 31 * 24 * 3600 * 1000
        _insert(conn, sched_id="sch_old", amount_fen=BIG_AMOUNT_FEN, verified_ms=old)
        results = scheduled.execute_due(conn, secret=SECRET)
        assert results[0]["status"] == "scheduled", "过期授权应该顺延，而不是硬闯额度线"
        assert results[0]["defer_count"] == 1
        assert _balance(conn) == START_BALANCE_FEN, "过期授权不能真的把钱转出去"
        row = _row(conn, "sch_old")
        assert str(row["status"]) == "scheduled"
        assert str(row["claim_token"]) == ""
        assert "scheduled.auth_expired" in _audit_kinds(conn, "sch_old")
        assert "scheduled.deferred" in _audit_kinds(conn, "sch_old")
    finally:
        conn.close()


def test_ttl_constant_is_stated_in_days_not_magic(bank: Path) -> None:
    """30 天这个数是产品口径，测试把它钉住：改成更短/更长都得是有意为之。"""
    assert scheduled.AUTH_TTL_MS == 30 * 24 * 3600 * 1000
    assert scheduled.CLAIM_LEASE_MS == 10 * 60 * 1000
