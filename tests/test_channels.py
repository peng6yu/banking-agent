"""出账通道闸门（app/channels.py）的回归用例。

这一层回答「这笔钱现在能不能从这个账户出去」：账户状态、卡状态、可用卡的单笔限额。
它和 policy（该不该做、要不要授权）是两件事，所以这里既测 channels 本身，
也测它真的接进了 funds 的 hold / commit 两条路径——只在 channels 里断言、
不验资金链路，等于没测（历史 bug 就是「卡冻结了照样能转账」）。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from app import channels, db, funds
from app.clock import PolicyDenied, ValidationError
from app.models import Action
from app.security import token as token_mod

SECRET = "c" * 64
USER = "u_chan"
ACCOUNT = "a_chan"
CARD_OK = "c_ok"
CARD_2 = "c_virtual"
PAYEE = "p_chan"
START_FEN = 1_000_000


@pytest.fixture()
def bank(tmp_path: Path) -> sqlite3.Connection:
    """最小可出账环境：一个用户、一个借记账户、一张实体卡、一个收款人。"""
    conn = db.connect(tmp_path / "bank.db")
    db.init_db(conn)
    conn.execute(
        "INSERT INTO users (id, name, phone, created_ms) VALUES (?,?,?,?)",
        (USER, "通道测试", "13900000000", 0),
    )
    conn.execute(
        "INSERT INTO accounts (id, user_id, name, tail, type, balance_fen, credit_limit_fen)"
        " VALUES (?,?,?,?,?,?,0)",
        (ACCOUNT, USER, "工资卡", "1234", "debit", START_FEN),
    )
    conn.execute(
        "INSERT INTO cards (id, account_id, tail, kind, status, per_tx_limit_fen)"
        " VALUES (?,?,?,?,?,?)",
        (CARD_OK, ACCOUNT, "1234", "physical", "active", 500_000),
    )
    conn.execute(
        "INSERT INTO payees (id, user_id, name, bank, card_tail) VALUES (?,?,?,?,?)",
        (PAYEE, USER, "李静", "招商银行", "4417"),
    )
    try:
        yield conn
    finally:
        conn.close()


def _set_account_status(conn: sqlite3.Connection, status: str) -> None:
    conn.execute("UPDATE accounts SET status = ? WHERE id = ?", (status, ACCOUNT))
    conn.commit()


def _set_card_status(conn: sqlite3.Connection, status: str, card_id: str = CARD_OK) -> None:
    conn.execute("UPDATE cards SET status = ? WHERE id = ?", (status, card_id))
    conn.commit()


def _balance(conn: sqlite3.Connection) -> int:
    return int(
        conn.execute("SELECT balance_fen FROM accounts WHERE id = ?", (ACCOUNT,)).fetchone()[
            "balance_fen"
        ]
    )


def _action(amount_fen: int) -> Action:
    return Action(
        tool="transfer",
        params={"payee_id": PAYEE},
        account_id=ACCOUNT,
        amount_fen=amount_fen,
        source="user",
    )


def _initiate(conn: sqlite3.Connection, amount_fen: int = 10_000, *, node: str = "n_chan") -> funds.HoldOutcome:
    action = _action(amount_fen)
    token = token_mod.issue(action, secret=SECRET)
    return funds.initiate(
        conn, action, session_id="s_chan", node_id=node, token=token, secret=SECRET
    )


# ---------------------------------------------------------------- channels 本体


def test_active_account_and_card_pass_and_report_the_channel(bank: sqlite3.Connection) -> None:
    info = channels.assert_spendable(bank, ACCOUNT, 10_000)
    assert info["account_status"] == "active"
    assert info["card_id"] == CARD_OK
    assert info["card_tail"] == "1234"
    assert info["per_tx_limit_fen"] == 500_000


def test_frozen_account_is_refused(bank: sqlite3.Connection) -> None:
    _set_account_status(bank, "frozen")
    with pytest.raises(PolicyDenied) as exc:
        channels.assert_spendable(bank, ACCOUNT, None)
    assert "已冻结" in exc.value.user_text
    assert "不能出账" in exc.value.user_text


def test_frozen_card_blocks_the_channel(bank: sqlite3.Connection) -> None:
    _set_card_status(bank, "frozen")
    with pytest.raises(PolicyDenied) as exc:
        channels.assert_spendable(bank, ACCOUNT, 10_000)
    assert "不能用" in exc.value.user_text
    assert "尾号 1234" in exc.value.user_text


def test_per_tx_limit_is_enforced_with_human_readable_numbers(bank: sqlite3.Connection) -> None:
    bank.execute("UPDATE cards SET per_tx_limit_fen = 50000 WHERE id = ?", (CARD_OK,))
    bank.commit()
    with pytest.raises(PolicyDenied) as exc:
        channels.assert_spendable(bank, ACCOUNT, 60_000)
    assert "500.00 元" in exc.value.user_text
    assert "600.00 元" in exc.value.user_text


def test_amount_none_only_checks_usability(bank: sqlite3.Connection) -> None:
    bank.execute("UPDATE cards SET per_tx_limit_fen = 1000 WHERE id = ?", (CARD_OK,))
    bank.commit()
    assert channels.assert_spendable(bank, ACCOUNT, None)["per_tx_limit_fen"] == 1_000


def test_account_without_cards_skips_the_card_gate(bank: sqlite3.Connection) -> None:
    """账户一张卡都没有时不做卡级校验（模型边界写在 channels 模块注释里）。"""
    bank.execute("DELETE FROM cards")
    bank.commit()
    info = channels.assert_spendable(bank, ACCOUNT, 10_000)
    assert info["card_id"] is None
    assert info["per_tx_limit_fen"] is None


def test_unknown_account_is_a_validation_error_not_a_denial(bank: sqlite3.Connection) -> None:
    with pytest.raises(ValidationError):
        channels.assert_spendable(bank, "a_missing", 1)


def test_limit_uses_the_most_permissive_active_card(bank: sqlite3.Connection) -> None:
    bank.execute(
        "INSERT INTO cards (id, account_id, tail, kind, status, per_tx_limit_fen)"
        " VALUES (?,?,?,?,?,?)",
        (CARD_2, ACCOUNT, "8888", "virtual", "active", 900_000),
    )
    bank.commit()
    info = channels.assert_spendable(bank, ACCOUNT, 500_000)
    assert info["per_tx_limit_fen"] == 900_000
    assert info["card_id"] == CARD_2


def test_usable_cards_lists_all_cards_including_unusable_ones(bank: sqlite3.Connection) -> None:
    _set_card_status(bank, "lost")
    rows = channels.usable_cards(bank, ACCOUNT)
    assert [row["id"] for row in rows] == [CARD_OK]
    assert str(rows[0]["status"]) == "lost"


# ------------------------------------------------------- funds 的两条真实资金路径


def test_funds_initiate_refuses_a_frozen_card(bank: sqlite3.Connection) -> None:
    """历史 bug 的正面回归：卡冻结后，转账在建立 hold 这一步就该被拦。"""
    _set_card_status(bank, "frozen")
    with pytest.raises(PolicyDenied):
        _initiate(bank)
    assert bank.execute("SELECT COUNT(*) AS n FROM holds").fetchone()["n"] == 0
    assert _balance(bank) == START_FEN


def test_funds_initiate_refuses_a_closed_account(bank: sqlite3.Connection) -> None:
    _set_account_status(bank, "closed")
    with pytest.raises(PolicyDenied) as exc:
        _initiate(bank)
    assert "已销户" in exc.value.user_text


def test_funds_initiate_enforces_the_card_limit(bank: sqlite3.Connection) -> None:
    bank.execute("UPDATE cards SET per_tx_limit_fen = 50000 WHERE id = ?", (CARD_OK,))
    bank.commit()
    with pytest.raises(PolicyDenied) as exc:
        _initiate(bank, 60_000)
    assert "单笔限额" in exc.value.user_text
    assert bank.execute("SELECT COUNT(*) AS n FROM holds").fetchone()["n"] == 0


def test_funds_commit_rechecks_the_card(bank: sqlite3.Connection) -> None:
    """hold 建立后、提交前把卡冻结：结算必须拒绝，钱不能出账。"""
    hold = _initiate(bank, 10_000)
    _set_card_status(bank, "frozen")
    with pytest.raises(PolicyDenied):
        funds.commit(bank, hold.hold_id, daily_risk_threshold_fen=None)
    assert _balance(bank) == START_FEN
    row = bank.execute("SELECT status FROM holds WHERE id = ?", (hold.hold_id,)).fetchone()
    assert str(row["status"]) == "held"


def test_funds_commit_succeeds_on_the_normal_path(bank: sqlite3.Connection) -> None:
    """对照组：卡正常时同一串调用必须真的出账，否则上面的拒绝说明不了任何事。"""
    hold = _initiate(bank, 10_000)
    outcome = funds.commit(bank, hold.hold_id, daily_risk_threshold_fen=None)
    assert outcome.amount_fen == 10_000
    assert _balance(bank) == START_FEN - 10_000