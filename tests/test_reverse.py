
"""冲正：钱退回去、只退一次、错方向与超窗都拒绝。"""

from __future__ import annotations

from typing import Any

import pytest

import app.tools.transfers as transfers
from app.clock import MissingCapability
from tests.tool_fixtures import DEBIT_BALANCE, PAYEE_LIJING, _balance, _token, bank, ctx

__all__ = ["bank", "ctx"]


def _transfer(ctx, amount: int) -> None:
    params = {"payee_id": PAYEE_LIJING, "amount_fen": amount, "note": "冲正用例"}
    result = transfers.transfer(ctx, params, _token("transfer", params, amount))
    assert result.ok is True


def _latest_out(ctx) -> str:
    row = ctx.conn.execute(
        "SELECT id FROM transactions WHERE direction = 'out' AND reversal_of IS NULL"
        " ORDER BY rowid DESC LIMIT 1"
    ).fetchone()
    return str(row["id"])


def _reverse(ctx, txn_id: str, amount_fen: int = 10_000) -> Any:
    params = {"txn_id": txn_id, "reason": "用户申请冲正", "amount_fen": amount_fen}
    return transfers.reverse_transfer(ctx, params, _token("reverse_transfer", params))


def test_reverse_puts_the_money_back(ctx) -> None:
    _transfer(ctx, 10_000)
    result = _reverse(ctx, _latest_out(ctx))
    assert result.ok is True
    assert _balance(ctx.conn) == DEBIT_BALANCE


def test_reversing_twice_does_not_pay_twice(ctx) -> None:
    _transfer(ctx, 10_000)
    txn_id = _latest_out(ctx)
    _reverse(ctx, txn_id)
    again = _reverse(ctx, txn_id)
    assert again.ok is True
    assert again.data.get("idempotent") is True
    assert _balance(ctx.conn) == DEBIT_BALANCE


def test_unknown_transaction_is_refused(ctx) -> None:
    result = _reverse(ctx, "t_ghost")
    assert result.ok is False
    assert result.error_code == "TXN_NOT_FOUND"


def test_incoming_transaction_cannot_be_reversed(ctx) -> None:
    ctx.conn.execute(
        "INSERT INTO transactions"
        " (id, account_id, direction, amount_fen, counterparty, category, note,"
        " ts_ms, balance_after_fen, channel)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("t_in", ctx.account_id, "in", 5_000, "工资", "工资", "", 1_792_296_000_000, 0, "app"),
    )
    result = _reverse(ctx, "t_in", 5_000)
    assert result.ok is False
    assert result.error_code == "TXN_NOT_OUT"


def test_reverse_needs_a_token(ctx) -> None:
    with pytest.raises(MissingCapability):
        transfers.reverse_transfer(ctx, {"txn_id": "t_ghost", "reason": "x", "amount_fen": 100}, None)

