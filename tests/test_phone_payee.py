
"""手机号转账：resolve_payee 按号码或后四位收窄到一个人（场景一）。"""

from __future__ import annotations

from typing import Any

import pytest

import app.tools.queries as queries
from tests.tool_fixtures import PAYEE_LIJING, bank, ctx

__all__ = ["bank", "ctx"]

PHONE = "13900138001"


def _set_phone(conn: Any) -> None:
    conn.execute("UPDATE payees SET phone = ? WHERE id = ?", (PHONE, PAYEE_LIJING))


def _candidates(result: Any) -> list[dict[str, Any]]:
    return list(result.data.get("candidates") or [])


def test_full_phone_number_finds_exactly_one(ctx) -> None:
    _set_phone(ctx.conn)
    result = queries.resolve_payee(ctx, {"name": PHONE})
    assert result.ok is True
    rows = _candidates(result)
    assert len(rows) == 1
    assert rows[0]["payee_id"] == PAYEE_LIJING


def test_last_four_digits_also_find_the_person(ctx) -> None:
    _set_phone(ctx.conn)
    result = queries.resolve_payee(ctx, {"name": PHONE[-4:]})
    assert result.ok is True
    assert [row["payee_id"] for row in _candidates(result)] == [PAYEE_LIJING]


def test_explicit_phone_param_is_honoured(ctx) -> None:
    _set_phone(ctx.conn)
    result = queries.resolve_payee(ctx, {"name": "李静", "phone": PHONE})
    assert result.ok is True
    assert len(_candidates(result)) == 1


def test_phone_that_matches_nobody_returns_no_candidate(ctx) -> None:
    """查不到人不是工具报错，而是空候选——由上层决定反问还是拒绝。"""
    _set_phone(ctx.conn)
    result = queries.resolve_payee(ctx, {"name": "19999999999"})
    assert result.ok is True
    assert _candidates(result) == []


def test_unknown_number_does_not_fall_back_to_someone_else(ctx) -> None:
    _set_phone(ctx.conn)
    result = queries.resolve_payee(ctx, {"name": "李静", "phone": "19999999999"})
    assert result.ok is True
    assert _candidates(result) == [], "号码对不上就不能退回按姓名命中的人"


@pytest.mark.parametrize("query", [PHONE, PHONE[-4:]])
def test_phone_candidates_carry_the_number_back(ctx, query: str) -> None:
    _set_phone(ctx.conn)
    rows = _candidates(queries.resolve_payee(ctx, {"name": query}))
    assert rows and rows[0]["phone"] == PHONE

