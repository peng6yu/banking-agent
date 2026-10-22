
"""AA 拆分的分支表：份额怎么分、总额怎么校、谁能收款。"""

from __future__ import annotations

from typing import Any

import pytest

import app.tools.split as split_tools
from app.clock import MissingCapability
from tests.tool_fixtures import DEBIT_BALANCE, PAYEE_LIJING, _balance, _token, bank, ctx

__all__ = ["bank", "ctx"]

TOTAL = 40_000


def _params(**extra: Any) -> dict[str, Any]:
    return {"payee_ids": [PAYEE_LIJING, "p_zhang_a"], "amount_fen": TOTAL, **extra}


def test_shares_divide_evenly_and_remainder_goes_first() -> None:
    assert split_tools.split_shares(10_000, 3) == [3334, 3333, 3333]
    assert sum(split_tools.split_shares(10_000, 3)) == 10_000


def test_shares_never_lose_or_invent_a_fen() -> None:
    for total in (10, 999, 100_001):
        for parts in (2, 3, 7, 10):
            if total < parts:
                continue
            assert sum(split_tools.split_shares(total, parts)) == total


def test_too_small_total_is_refused() -> None:
    with pytest.raises(ValueError):
        split_tools.split_shares(2, 3)


def test_split_moves_the_whole_total_in_one_call(ctx) -> None:
    params = _params()
    result = split_tools.split_transfer(ctx, params, _token("split_transfer", params, TOTAL))
    assert result.ok is True
    assert _balance(ctx.conn) == DEBIT_BALANCE - TOTAL
    holds = ctx.conn.execute("SELECT status FROM holds").fetchall()
    assert [row["status"] for row in holds] == ["committed", "committed"]


def test_one_payee_is_not_a_split(ctx) -> None:
    params = {"payee_ids": [PAYEE_LIJING], "amount_fen": TOTAL}
    result = split_tools.split_transfer(ctx, params, _token("split_transfer", params, TOTAL))
    assert result.ok is False
    assert result.error_code == "SPLIT_TOO_FEW"


def test_duplicate_payee_is_refused(ctx) -> None:
    params = {"payee_ids": [PAYEE_LIJING, PAYEE_LIJING], "amount_fen": TOTAL}
    result = split_tools.split_transfer(ctx, params, _token("split_transfer", params, TOTAL))
    assert result.ok is False
    assert result.error_code == "SPLIT_DUPLICATE_PAYEE"


def test_stranger_payee_is_refused(ctx) -> None:
    params = {"payee_ids": [PAYEE_LIJING, "p_ghost"], "amount_fen": TOTAL}
    result = split_tools.split_transfer(ctx, params, _token("split_transfer", params, TOTAL))
    assert result.ok is False
    assert result.error_code == "PAYEE_NOT_OWNED"


def test_split_needs_a_token(ctx) -> None:
    with pytest.raises(MissingCapability):
        split_tools.split_transfer(ctx, _params(), None)
    assert _balance(ctx.conn) == DEBIT_BALANCE

