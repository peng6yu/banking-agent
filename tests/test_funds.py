"""两阶段资金测试：hold / commit / release / 超时 / 幂等。

对应文档：docs/05_安全层实现规格.md 5.4。
"""

from __future__ import annotations

import hashlib

import pytest

from app import audit, clock, db
from app.clock import HoldExpired, InsufficientFunds, MissingCapability, TokenMismatch, ValidationError
from app.models import Action
from app.security import token as tk
from app import funds

SECRET = "d" * 64
ACCOUNT = "a_1"
PAYEE = "p_zhangsan"
START_FEN = 1_286_050
AMOUNT_FEN = 50_000


@pytest.fixture()
def bank(tmp_path):
    """一个只有最必要行的库：用户、账户、收款人。"""
    clock.freeze(1_760_000_000_000)
    conn = db.connect(str(tmp_path / "bank.db"))
    db.init_db(conn)
    conn.execute("INSERT INTO users (id, name, phone, created_ms) VALUES ('u_1','张伟','13800000000',0)")
    conn.execute(
        "INSERT INTO accounts (id, user_id, name, tail, type, balance_fen, credit_limit_fen)"
        " VALUES (?, 'u_1', '储蓄卡', '1234', 'debit', ?, 0)",
        (ACCOUNT, START_FEN),
    )
    conn.execute(
        "INSERT INTO payees (id, user_id, name, bank, card_tail, alias) VALUES (?, 'u_1', '张三', '招商银行', '8888', NULL)",
        (PAYEE,),
    )
    try:
        yield conn
    finally:
        clock.unfreeze()
        conn.close()


def action(amount_fen: int = AMOUNT_FEN, **kwargs: object) -> Action:
    return Action(tool="transfer", params={"payee_id": PAYEE}, account_id=ACCOUNT, amount_fen=amount_fen, **kwargs)


def balance(conn) -> int:
    return conn.execute("SELECT balance_fen FROM accounts WHERE id = ?", (ACCOUNT,)).fetchone()["balance_fen"]


def fresh_token(target: Action | None = None) -> str:
    return tk.issue(target or action(), secret=SECRET)


def initiate(conn, target: Action | None = None, *, session_id: str = "s_1", node_id: str = "n_1") -> funds.HoldOutcome:
    payload = target or action()
    return funds.initiate(
        conn, payload, session_id=session_id, node_id=node_id, token=fresh_token(payload), secret=SECRET
    )


# ---------- 幂等键 ----------

def test_idempotency_key_is_sha256_first_32() -> None:
    expected = hashlib.sha256(b"s_1|n_1|abc").hexdigest()[:32]
    assert funds.idempotency_key_for("s_1", "n_1", "abc") == expected
    assert len(expected) == 32


def test_idempotency_key_is_stable_and_sensitive() -> None:
    assert funds.idempotency_key_for("s", "n", "p") == funds.idempotency_key_for("s", "n", "p")
    assert funds.idempotency_key_for("s", "n", "p") != funds.idempotency_key_for("s", "n", "q")


def test_hold_stores_the_computed_key(bank) -> None:
    outcome = initiate(bank)
    expected = funds.idempotency_key_for("s_1", "n_1", tk.params_hash(action().params))
    assert outcome.idempotency_key == expected


# ---------- initiate ----------

def test_initiate_creates_held_record(bank) -> None:
    outcome = initiate(bank)
    hold = funds.fetch_hold(bank, outcome.hold_id)
    assert hold is not None
    assert hold.status == "held"
    assert hold.amount_fen == AMOUNT_FEN
    assert hold.expires_ms == clock.now_ms() + funds.HOLD_TTL_MS
    assert outcome.reused is False
    assert balance(bank) == START_FEN


def test_initiate_writes_transfer_initiated_audit(bank) -> None:
    outcome = initiate(bank)
    row = bank.execute("SELECT payload_json FROM audit_log WHERE event_type = 'transfer.initiated'").fetchone()
    assert row is not None
    assert outcome.hold_id in row["payload_json"]
    assert audit.verify_chain(bank)


def test_replayed_initiate_reuses_the_same_hold(bank) -> None:
    first = initiate(bank)
    second = initiate(bank)
    assert second.hold_id == first.hold_id
    assert second.reused is True
    assert bank.execute("SELECT COUNT(*) AS c FROM holds").fetchone()["c"] == 1


def test_different_node_creates_new_hold(bank) -> None:
    first = initiate(bank, node_id="n_1")
    second = initiate(bank, node_id="n_2")
    assert second.hold_id != first.hold_id
    assert second.reused is False


def test_initiate_without_token_rejected(bank) -> None:
    with pytest.raises(MissingCapability):
        funds.initiate(bank, action(), session_id="s_1", node_id="n_1", token=None, secret=SECRET)
    assert bank.execute("SELECT COUNT(*) AS c FROM holds").fetchone()["c"] == 0


def test_initiate_with_tampered_amount_rejected(bank) -> None:
    with pytest.raises(TokenMismatch):
        funds.initiate(bank, action(), session_id="s_1", node_id="n_1", token=fresh_token(action(AMOUNT_FEN + 1)), secret=SECRET)
    assert bank.execute("SELECT COUNT(*) AS c FROM holds").fetchone()["c"] == 0


def test_initiate_insufficient_funds_audited(bank) -> None:
    huge = action(99_999_999)
    with pytest.raises(InsufficientFunds):
        initiate(bank, huge)
    row = bank.execute(
        "SELECT payload_json FROM audit_log WHERE event_type = 'transfer.initiated' AND payload_json LIKE '%INSUFFICIENT_FUNDS%'"
    ).fetchone()
    assert row is not None
    assert bank.execute("SELECT COUNT(*) AS c FROM holds").fetchone()["c"] == 0


def test_unknown_payee_rejected(bank) -> None:
    payload = Action(tool="transfer", params={"payee_id": "p_ghost"}, account_id=ACCOUNT, amount_fen=AMOUNT_FEN)
    with pytest.raises(ValidationError):
        initiate(bank, payload)
    assert bank.execute("SELECT COUNT(*) AS c FROM holds").fetchone()["c"] == 0


# ---------- 路径一：held -> committed ----------

def test_commit_deducts_and_writes_ledger(bank) -> None:
    outcome = initiate(bank)
    result = funds.commit(bank, outcome.hold_id)
    assert result.balance_after_fen == START_FEN - AMOUNT_FEN
    assert result.idempotent is False
    assert balance(bank) == START_FEN - AMOUNT_FEN
    assert funds.fetch_hold(bank, outcome.hold_id).status == "committed"
    row = bank.execute("SELECT * FROM transactions WHERE hold_id = ?", (outcome.hold_id,)).fetchone()
    assert row["direction"] == "out"
    assert row["amount_fen"] == AMOUNT_FEN
    assert row["counterparty"] == "张三"
    assert row["balance_after_fen"] == START_FEN - AMOUNT_FEN


def test_commit_updates_daily_limit(bank) -> None:
    outcome = initiate(bank)
    funds.commit(bank, outcome.hold_id)
    assert funds.spent_today_fen(bank, ACCOUNT) == AMOUNT_FEN
    row = bank.execute("SELECT * FROM daily_limits WHERE account_id = ?", (ACCOUNT,)).fetchone()
    assert row["tx_count"] == 1


def test_commit_writes_transfer_committed_audit(bank) -> None:
    outcome = initiate(bank)
    funds.commit(bank, outcome.hold_id)
    rows = bank.execute("SELECT payload_json FROM audit_log WHERE event_type = 'transfer.committed'").fetchall()
    assert len(rows) == 1
    assert outcome.hold_id in rows[0]["payload_json"]
    assert audit.verify_chain(bank)


def test_repeated_commit_is_idempotent(bank) -> None:
    outcome = initiate(bank)
    first = funds.commit(bank, outcome.hold_id)
    second = funds.commit(bank, outcome.hold_id)
    assert first.balance_after_fen == second.balance_after_fen
    assert second.idempotent is True
    assert balance(bank) == START_FEN - AMOUNT_FEN
    assert bank.execute("SELECT COUNT(*) AS c FROM transactions").fetchone()["c"] == 1


def test_commit_of_unknown_hold_rejected(bank) -> None:
    with pytest.raises(ValidationError):
        funds.commit(bank, "h_missing")


def test_commit_is_atomic_when_audit_fails(bank, monkeypatch) -> None:
    """最后一步审计失败时，五件事整体回滚：钱不动、流水不写、hold 还是 held。"""
    real_append = audit.append

    class FlakyAudit:
        def append(self, conn, event_type, payload, **kwargs):
            if event_type == "transfer.committed":
                raise RuntimeError("audit down")
            return real_append(conn, event_type, payload, **kwargs)

    monkeypatch.setattr(funds, "audit", FlakyAudit())
    outcome = initiate(bank)
    with pytest.raises(RuntimeError):
        funds.commit(bank, outcome.hold_id)
    assert balance(bank) == START_FEN
    assert funds.fetch_hold(bank, outcome.hold_id).status == "held"
    assert bank.execute("SELECT COUNT(*) AS c FROM transactions").fetchone()["c"] == 0
    assert funds.spent_today_fen(bank, ACCOUNT) == 0


# ---------- 路径二：held -> released ----------

def test_release_marks_released_and_audits(bank) -> None:
    outcome = initiate(bank)
    result = funds.release(bank, outcome.hold_id, reason="USER_CANCELLED")
    assert result.status == "released"
    assert result.idempotent is False
    assert funds.fetch_hold(bank, outcome.hold_id).status == "released"
    assert balance(bank) == START_FEN
    row = bank.execute("SELECT payload_json FROM audit_log WHERE event_type = 'transfer.released'").fetchone()
    assert "USER_CANCELLED" in row["payload_json"]


def test_release_is_idempotent(bank) -> None:
    outcome = initiate(bank)
    funds.release(bank, outcome.hold_id)
    again = funds.release(bank, outcome.hold_id)
    assert again.idempotent is True
    assert again.status == "released"
    assert bank.execute("SELECT COUNT(*) AS c FROM audit_log WHERE event_type = 'transfer.released'").fetchone()["c"] == 1


def test_release_of_unknown_hold_rejected(bank) -> None:
    with pytest.raises(ValidationError):
        funds.release(bank, "h_missing")


def test_commit_after_release_rejected(bank) -> None:
    outcome = initiate(bank)
    funds.release(bank, outcome.hold_id)
    with pytest.raises(HoldExpired):
        funds.commit(bank, outcome.hold_id)
    assert balance(bank) == START_FEN


# ---------- 路径三：超时 -> expired ----------

def test_expired_hold_cannot_commit(bank) -> None:
    outcome = initiate(bank)
    clock.freeze(clock.now_ms() + funds.HOLD_TTL_MS + 1)
    with pytest.raises(HoldExpired):
        funds.commit(bank, outcome.hold_id)
    assert funds.fetch_hold(bank, outcome.hold_id).status == "expired"
    assert balance(bank) == START_FEN
    row = bank.execute("SELECT payload_json FROM audit_log WHERE event_type = 'transfer.released'").fetchone()
    assert "HOLD_TIMEOUT" in row["payload_json"]


def test_hold_just_before_expiry_still_commits(bank) -> None:
    outcome = initiate(bank)
    clock.freeze(clock.now_ms() + funds.HOLD_TTL_MS - 1)
    result = funds.commit(bank, outcome.hold_id)
    assert result.balance_after_fen == START_FEN - AMOUNT_FEN


def test_sweep_expired_holds_matches_funds_view(bank) -> None:
    outcome = initiate(bank)
    clock.freeze(clock.now_ms() + funds.HOLD_TTL_MS + 1)
    assert db.sweep_expired_holds(bank) == 1
    assert funds.fetch_hold(bank, outcome.hold_id).status == "expired"
    with pytest.raises(HoldExpired):
        funds.commit(bank, outcome.hold_id)


def test_day_of_is_date_string() -> None:
    assert funds.day_of(1_760_000_000_000) == funds.day_of(1_760_000_000_000)
    assert len(funds.day_of(1_760_000_000_000)) == 10
