
"""销户与改卡密码：余额没清零不许销、密码只存哈希、状态机不许乱跳。"""

from __future__ import annotations

from typing import Any

import pytest

import app.tools.cards as cards
from app.clock import MissingCapability
from tests.tool_fixtures import DEBIT_BALANCE, _token, bank, ctx

__all__ = ["bank", "ctx"]

TAIL = "1234"


def _status(conn: Any) -> str:
    row = conn.execute("SELECT status FROM cards WHERE tail = ?", (TAIL,)).fetchone()
    return str(row["status"])


def _zero_out(conn: Any, account_id: str) -> None:
    conn.execute("UPDATE accounts SET balance_fen = 0 WHERE id = ?", (account_id,))


def test_close_card_refuses_while_money_is_still_there(ctx) -> None:
    params = {"card_tail": TAIL}
    result = cards.close_card(ctx, params, _token("close_card", params))
    assert result.ok is False
    assert result.error_code == "CARD_BALANCE_NOT_ZERO"
    assert _balance_untouched(ctx) == DEBIT_BALANCE
    assert _status(ctx.conn) != "closed"


def _balance_untouched(ctx) -> int:
    row = ctx.conn.execute("SELECT balance_fen FROM accounts WHERE id = ?", (ctx.account_id,)).fetchone()
    return int(row["balance_fen"])


def test_close_card_succeeds_after_balance_is_zero(ctx) -> None:
    _zero_out(ctx.conn, ctx.account_id)
    params = {"card_tail": TAIL}
    result = cards.close_card(ctx, params, _token("close_card", params))
    assert result.ok is True
    assert _status(ctx.conn) == "closed"


def test_closing_twice_is_idempotent(ctx) -> None:
    _zero_out(ctx.conn, ctx.account_id)
    params = {"card_tail": TAIL}
    token = _token("close_card", params)
    cards.close_card(ctx, params, token)
    again = cards.close_card(ctx, params, token)
    assert again.ok is True
    assert again.data.get("idempotent") is True


def test_lost_card_cannot_be_closed_from_the_app(ctx) -> None:
    _zero_out(ctx.conn, ctx.account_id)
    ctx.conn.execute("UPDATE cards SET status = 'lost' WHERE tail = ?", (TAIL,))
    params = {"card_tail": TAIL}
    result = cards.close_card(ctx, params, _token("close_card", params))
    assert result.ok is False
    assert result.error_code == "CARD_LOST"


@pytest.mark.parametrize("pin", ["12345", "1234567", "abcdef", "12345a"])
def test_bad_pin_is_refused_by_the_tool_not_the_schema(ctx, pin: str) -> None:
    params = {"card_tail": TAIL, "pin": pin}
    result = cards.change_card_pin(ctx, params, _token("change_card_pin", params))
    assert result.ok is False
    assert result.error_code == "PIN_INVALID"


def test_pin_is_stored_as_salted_hash_never_plaintext(ctx) -> None:
    params = {"card_tail": TAIL, "pin": "246810"}
    result = cards.change_card_pin(ctx, params, _token("change_card_pin", params))
    assert result.ok is True
    stored = ctx.conn.execute("SELECT pin_hash FROM cards WHERE tail = ?", (TAIL,)).fetchone()["pin_hash"]
    assert stored
    assert "246810" not in stored
    assert "$" in stored


def test_pin_is_never_echoed_back(ctx) -> None:
    params = {"card_tail": TAIL, "pin": "135790"}
    result = cards.change_card_pin(ctx, params, _token("change_card_pin", params))
    assert "135790" not in str(result.data)


def test_card_ops_need_a_token(ctx) -> None:
    with pytest.raises(MissingCapability):
        cards.close_card(ctx, {"card_tail": TAIL}, None)


def _pin_hash(conn: Any) -> str:
    row = conn.execute("SELECT pin_hash FROM cards WHERE tail = ?", (TAIL,)).fetchone()
    return str(row["pin_hash"] or "")


def test_first_pin_needs_no_old_pin(ctx) -> None:
    assert _pin_hash(ctx.conn) == ""
    params = {"card_tail": TAIL, "pin": "246810"}
    result = cards.change_card_pin(ctx, params, _token("change_card_pin", params))
    assert result.ok is True


def test_changing_a_set_pin_requires_the_old_one(ctx) -> None:
    first = {"card_tail": TAIL, "pin": "246810"}
    cards.change_card_pin(ctx, first, _token("change_card_pin", first))
    before = _pin_hash(ctx.conn)
    params = {"card_tail": TAIL, "pin": "135790"}
    result = cards.change_card_pin(ctx, params, _token("change_card_pin", params))
    assert result.ok is False
    assert result.error_code == "PIN_OLD_REQUIRED"
    assert _pin_hash(ctx.conn) == before


def test_wrong_old_pin_is_refused_and_nothing_changes(ctx) -> None:
    first = {"card_tail": TAIL, "pin": "246810"}
    cards.change_card_pin(ctx, first, _token("change_card_pin", first))
    before = _pin_hash(ctx.conn)
    params = {"card_tail": TAIL, "pin": "135790", "old_pin": "000000"}
    result = cards.change_card_pin(ctx, params, _token("change_card_pin", params))
    assert result.ok is False
    assert result.error_code == "PIN_OLD_WRONG"
    assert _pin_hash(ctx.conn) == before


def test_correct_old_pin_lets_the_change_through(ctx) -> None:
    first = {"card_tail": TAIL, "pin": "246810"}
    cards.change_card_pin(ctx, first, _token("change_card_pin", first))
    before = _pin_hash(ctx.conn)
    params = {"card_tail": TAIL, "pin": "135790", "old_pin": "246810"}
    result = cards.change_card_pin(ctx, params, _token("change_card_pin", params))
    assert result.ok is True
    assert _pin_hash(ctx.conn) != before


