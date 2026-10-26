
"""定时转账：落一条预约、到日子才划钱、取消与幂等。"""

from __future__ import annotations

from typing import Any

import app.tools.scheduled as scheduled
from tests.tool_fixtures import (
    DEBIT_BALANCE,
    FROZEN_MS,
    SECRET,
    PAYEE_LIJING,
    _balance,
    _token,
    bank,
    ctx,
)

__all__ = ["bank", "ctx"]

DUE = FROZEN_MS + 86_400_000


def _params(**extra: Any) -> dict[str, Any]:
    return {
        "payee_id": PAYEE_LIJING,
        "amount_fen": 30_000,
        "due_ms": DUE,
        "payee_name": "李静",
        **extra,
    }


def test_schedule_creates_a_row_and_does_not_move_money_yet(ctx) -> None:
    params = _params()
    result = scheduled.schedule_transfer(ctx, params, _token("schedule_transfer", params, 30_000))
    assert result.ok is True
    assert _balance(ctx.conn) == DEBIT_BALANCE, "约好的钱不能当场划走"
    row = ctx.conn.execute("SELECT status, due_ms FROM scheduled_transfers").fetchone()
    assert row["status"] == "scheduled"
    assert row["due_ms"] == DUE


def test_list_shows_the_pending_schedule(ctx) -> None:
    params = _params()
    scheduled.schedule_transfer(ctx, params, _token("schedule_transfer", params, 30_000))
    result = scheduled.list_scheduled(ctx, {})
    assert result.ok is True
    assert len(result.data["items"]) == 1
    assert result.data["items"][0]["payee_name"] == "李静"


def test_past_due_is_refused(ctx) -> None:
    params = _params(due_ms=FROZEN_MS - 1)
    result = scheduled.schedule_transfer(ctx, params, _token("schedule_transfer", params, 30_000))
    assert result.ok is False
    assert result.error_code == "DUE_IN_PAST"
    assert scheduled.list_scheduled(ctx, {}).data["items"] == []


def test_cancel_marks_it_cancelled_and_is_idempotent(ctx) -> None:
    params = _params()
    created = scheduled.schedule_transfer(ctx, params, _token("schedule_transfer", params, 30_000))
    sid = created.data["scheduled_id"]
    token = _token("cancel_scheduled", {"scheduled_id": sid})
    first = scheduled.cancel_scheduled(ctx, {"scheduled_id": sid}, token)
    assert first.ok is True
    row = ctx.conn.execute("SELECT status FROM scheduled_transfers WHERE id = ?", (sid,)).fetchone()
    assert row["status"] == "cancelled"
    again = scheduled.cancel_scheduled(ctx, {"scheduled_id": sid}, token)
    assert again.ok is True
    assert again.data.get("idempotent") is True


def test_execute_due_transfers_the_money_once(ctx) -> None:
    params = _params()
    scheduled.schedule_transfer(ctx, params, _token("schedule_transfer", params, 30_000))
    done = scheduled.execute_due(ctx.conn, now=DUE, secret=SECRET)
    assert len(done) == 1
    assert _balance(ctx.conn) == DEBIT_BALANCE - 30_000
    row = ctx.conn.execute("SELECT status FROM scheduled_transfers").fetchone()
    assert row["status"] == "executed"
    assert scheduled.execute_due(ctx.conn, now=DUE, secret=SECRET) == [], "执行过的不该再划一次"


def test_monthly_recurring_rolls_forward(ctx) -> None:
    params = _params(recurrence="monthly")
    scheduled.schedule_transfer(ctx, params, _token("schedule_transfer", params, 30_000))
    scheduled.execute_due(ctx.conn, now=DUE, secret=SECRET)
    row = ctx.conn.execute("SELECT status, due_ms FROM scheduled_transfers").fetchone()
    assert row["status"] == "scheduled"
    assert row["due_ms"] > DUE


# --------------------------------------------------------------- HTTP 边界回归

def test_verify_route_passes_plain_dicts_to_the_use_case(monkeypatch) -> None:
    """回归：/api/scheduled/{id}/verify 曾把 pydantic 模型直接传下去。

    factors._normalize 只认 dict，模型会被静默丢弃，表现成「口令全对却仍报
    FACTOR_NOT_VERIFIED」。这条从路由函数这一层盯住，不依赖库和策略。
    """
    import asyncio

    from app.api import plans_api
    from app.api.schemas import ScheduledVerifyRequest
    from app.models import FactorProof

    seen: list[Any] = []
    monkeypatch.setattr(plans_api, "payload", lambda data: data)
    monkeypatch.setattr(
        plans_api.use_cases.scheduled,
        "scheduled_submit_verify",
        lambda scheduled_id, *, proofs: seen.append(proofs) or {"ok": True},
    )
    request = ScheduledVerifyRequest(
        proofs=[FactorProof(factor="face", challenge_id="fc_1", code="123456")]
    )
    asyncio.run(plans_api.scheduled_verify("sch_1", request))
    assert seen and isinstance(seen[0][0], dict), "路由层必须先把模型转成 dict"
    assert seen[0][0]["challenge_id"] == "fc_1"
