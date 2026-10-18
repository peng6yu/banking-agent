"""熔断器测试：5 分钟 3 次拒绝开闸，60 秒后半开。

对应文档：docs/05_安全层实现规格.md 5.7。
"""

from __future__ import annotations

import pytest

from app import audit, clock
from app.clock import BreakerOpen
from app.security import breaker as bk

SESSION = "s_1"


@pytest.fixture(autouse=True)
def frozen_clock():
    clock.freeze(1_760_000_000_000)
    try:
        yield
    finally:
        clock.unfreeze()


def deny(conn, session_id: str = SESSION) -> int:
    return bk.record_denial(conn, session_id)


def test_two_denials_do_not_open(conn) -> None:
    deny(conn)
    deny(conn)
    assert bk.denial_count(conn, SESSION) == 2
    assert bk.is_open(conn, SESSION) is False
    bk.ensure_available(conn, SESSION)


def test_third_denial_in_window_opens(conn) -> None:
    for _ in range(3):
        deny(conn)
    assert bk.is_open(conn, SESSION) is True
    assert bk.retry_after_ms(conn, SESSION) == bk.OPEN_MS


def test_breaker_raises_and_refuses_work(conn) -> None:
    for _ in range(3):
        deny(conn)
    with pytest.raises(BreakerOpen) as excinfo:
        bk.ensure_available(conn, SESSION)
    assert excinfo.value.fields["retry_after_ms"] == bk.OPEN_MS


def test_half_open_after_60_seconds(conn) -> None:
    for _ in range(3):
        deny(conn)
    clock.freeze(clock.now_ms() + bk.OPEN_MS - 1)
    assert bk.is_open(conn, SESSION) is True
    clock.freeze(clock.now_ms() + 1)
    assert bk.is_open(conn, SESSION) is False
    bk.ensure_available(conn, SESSION)


def test_reset_clears_the_breaker(conn) -> None:
    for _ in range(3):
        deny(conn)
    assert bk.reset(conn, SESSION) == 3
    assert bk.is_open(conn, SESSION) is False
    assert bk.denial_count(conn, SESSION) == 0


def test_successful_trial_closes_the_breaker(conn) -> None:
    for _ in range(3):
        deny(conn)
    clock.freeze(clock.now_ms() + bk.OPEN_MS + 1)
    bk.ensure_available(conn, SESSION)
    bk.reset(conn, SESSION)
    assert bk.is_open(conn, SESSION) is False


def test_denials_outside_window_are_forgotten(conn) -> None:
    deny(conn)
    deny(conn)
    clock.freeze(clock.now_ms() + bk.WINDOW_MS + 1)
    deny(conn)
    assert bk.denial_count(conn, SESSION) == 1
    assert bk.is_open(conn, SESSION) is False


def test_breaker_open_audit_written_once_per_opening(conn) -> None:
    for _ in range(3):
        deny(conn)
    deny(conn)
    rows = conn.execute("SELECT payload_json FROM audit_log WHERE event_type = 'breaker.open'").fetchall()
    assert len(rows) == 1
    assert "3" in rows[0]["payload_json"]
    assert audit.verify_chain(conn)


def test_denial_rows_are_persisted(conn) -> None:
    deny(conn, "s_2")
    row = conn.execute("SELECT * FROM denials WHERE session_id = 's_2'").fetchone()
    assert row is not None
    assert row["reason"] == bk.DEFAULT_REASON
    assert row["action_json"] == "{}"


def test_breaker_is_per_session(conn) -> None:
    for _ in range(3):
        deny(conn, "s_3")
    assert bk.is_open(conn, "s_3") is True
    assert bk.is_open(conn, "s_other") is False


def test_retry_after_is_zero_when_closed(conn) -> None:
    assert bk.retry_after_ms(conn, SESSION) == 0
