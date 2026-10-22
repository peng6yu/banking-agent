"""场景四卡片管理五件套：申请虚拟卡、调限额、冻结、解冻、挂失。

对应文档：docs/06_工具层与场景实现规格.md 6.3 场景 4。

三类不变量：
1. 写工具的第一条语句是 require_token，没有令牌一律 MissingCapability；
2. 状态机只走 active -> frozen -> active 与 (active|frozen) -> lost，非法迁移拒绝；
3. 幂等：目标状态已达成时返回成功且不重复写审计。
"""

from __future__ import annotations

import pytest

import app.tools.cards as cards
from app.clock import ValidationError
from app.policy.rules import SENSITIVE_TOOLS
from app.policy import decide
from app.models import Action
from app.tools import registry
from tests.tool_fixtures import (
    MINIMAL_PARAMS,
    _token,
    bank,
    ctx,
)

__all__ = ["bank", "ctx"]

PHYSICAL_TAIL = "1234"


def _params(tool: str, **extra) -> dict:
    return {**MINIMAL_PARAMS[tool], **extra}


def _run(tool: str, params: dict, ctx, token: str | None = None):
    return registry.execute(tool, params, token=token or _token(tool, params), ctx=ctx)


def _card(ctx, tail: str):
    return ctx.conn.execute(
        "SELECT c.* FROM cards c JOIN accounts a ON a.id = c.account_id"
        " WHERE c.tail = ? AND a.id = ?",
        (tail, "a_debit"),
    ).fetchone()


def _state_changes(ctx, marker: str) -> int:
    """数一条特定状态变更被写进审计的次数（registry 每条执行都会记一条，不能用总数）。"""
    rows = ctx.conn.execute(
        "SELECT payload_json FROM audit_log WHERE event_type = 'tool.executed' ORDER BY seq"
    ).fetchall()
    return sum(1 for row in rows if marker in row["payload_json"])


# ---------------------------------------------------------------- 申请虚拟卡


def test_apply_virtual_card_creates_an_active_card_with_default_limit(ctx) -> None:
    """开卡：立刻 active，默认单笔限额 5000 元，尾号不与已有卡重复。"""
    params = _params("apply_virtual_card")
    result = _run("apply_virtual_card", params, ctx)
    assert result.ok, result.error
    assert result.data["status"] == "active"
    assert result.data["kind"] == "virtual"
    assert result.data["per_tx_limit_fen"] == 500_000
    assert result.data["card_tail"] != PHYSICAL_TAIL
    row = _card(ctx, result.data["card_tail"])
    assert row is not None and str(row["kind"]) == "virtual"


def test_apply_virtual_card_is_yellow_confirm_not_red(ctx) -> None:
    """赛题把虚拟卡申请写在黄灯：只要确认，不要人脸。"""
    decision = decide(Action(tool="apply_virtual_card", params={}, account_id="a_debit"))
    assert (decision.level, decision.rule_id) == ("yellow", "R05_confirm_only")
    assert decision.required_factors == []


# ---------------------------------------------------------------- 调限额


def test_set_card_limit_changes_limit_and_writes_audit(ctx) -> None:
    params = _params("set_card_limit", per_tx_limit_fen=200_000)
    result = _run("set_card_limit", params, ctx)
    assert result.ok, result.error
    assert result.data["per_tx_limit_fen"] == 200_000
    assert result.data["previous_limit_fen"] == 500_000
    assert result.data["idempotent"] is False
    assert int(_card(ctx, PHYSICAL_TAIL)["per_tx_limit_fen"]) == 200_000
    assert _state_changes(ctx, "limit:500000->200000") == 1


def test_set_card_limit_twice_is_idempotent_and_does_not_double_audit(ctx) -> None:
    params = _params("set_card_limit", per_tx_limit_fen=200_000)
    _run("set_card_limit", params, ctx)
    again = _run("set_card_limit", params, ctx)
    assert again.ok and again.data["idempotent"] is True
    assert _state_changes(ctx, "limit:500000->200000") == 1


def test_set_card_limit_rejects_out_of_range(ctx) -> None:
    """超上界在 Schema 层就拦下；handler 里的 LIMIT_OUT_OF_RANGE 是 Defense in depth，
    直接调 handler 才验得到它。"""
    params = _params("set_card_limit", per_tx_limit_fen=9_000_000)
    with pytest.raises(ValidationError):
        _run("set_card_limit", params, ctx)
    assert int(_card(ctx, PHYSICAL_TAIL)["per_tx_limit_fen"]) == 500_000
    direct = cards.set_card_limit(
        ctx,
        {"card_tail": PHYSICAL_TAIL, "per_tx_limit_fen": 9_000_000},
        _token("set_card_limit", params),
    )
    assert not direct.ok
    assert direct.error_code == "LIMIT_OUT_OF_RANGE"


# ---------------------------------------------------------------- 冻结/解冻


def test_freeze_then_unfreeze_round_trip(ctx) -> None:
    frozen = _run("freeze_card", _params("freeze_card"), ctx)
    assert frozen.ok and frozen.data["status"] == "frozen"
    assert str(_card(ctx, PHYSICAL_TAIL)["status"]) == "frozen"
    thawed = _run("unfreeze_card", _params("unfreeze_card"), ctx)
    assert thawed.ok and thawed.data["status"] == "active"
    assert str(_card(ctx, PHYSICAL_TAIL)["status"]) == "active"


def test_freeze_is_idempotent(ctx) -> None:
    _run("freeze_card", _params("freeze_card"), ctx)
    again = _run("freeze_card", _params("freeze_card"), ctx)
    assert again.ok and again.data["idempotent"] is True
    assert _state_changes(ctx, "active->frozen") == 1


def test_unfreeze_on_active_card_is_a_no_op(ctx) -> None:
    result = _run("unfreeze_card", _params("unfreeze_card"), ctx)
    assert result.ok and result.data["idempotent"] is True
    assert result.data["status"] == "active"


# ---------------------------------------------------------------- 挂失


def test_report_loss_freezes_the_card_permanently(ctx) -> None:
    result = _run("report_card_loss", _params("report_card_loss"), ctx)
    assert result.ok and result.data["status"] == "lost"
    assert str(_card(ctx, PHYSICAL_TAIL)["status"]) == "lost"
    assert "柜面补办" in result.data["summary"]


def test_lost_card_cannot_be_unfrozen_by_the_user(ctx) -> None:
    """挂失不可自助撤销：解冻会得到 CARD_LOST，引导去柜面。"""
    _run("report_card_loss", _params("report_card_loss"), ctx)
    result = _run("unfreeze_card", _params("unfreeze_card"), ctx)
    assert not result.ok
    assert result.error_code == "CARD_LOST"
    assert str(_card(ctx, PHYSICAL_TAIL)["status"]) == "lost"


def test_frozen_card_can_still_be_reported_lost(ctx) -> None:
    """冻了之后发现卡丢了，挂失要照样能办。"""
    _run("freeze_card", _params("freeze_card"), ctx)
    result = _run("report_card_loss", _params("report_card_loss"), ctx)
    assert result.ok and result.data["status"] == "lost"


def test_report_loss_twice_is_idempotent(ctx) -> None:
    _run("report_card_loss", _params("report_card_loss"), ctx)
    again = _run("report_card_loss", _params("report_card_loss"), ctx)
    assert again.ok and again.data["idempotent"] is True
    assert _state_changes(ctx, "active->lost") == 1


# ---------------------------------------------------------------- 定位与越权


def test_unknown_card_tail_is_not_found(ctx) -> None:
    result = _run("freeze_card", {"card_tail": "0000"}, ctx)
    assert not result.ok
    assert result.error_code == "CARD_NOT_FOUND"


def test_empty_card_tail_is_rejected_before_the_handler_runs(ctx) -> None:
    """空尾号在 JSON Schema 层就被拦下，到不了 CARD_TAIL_REQUIRED 那条兜底。"""
    with pytest.raises(ValidationError):
        _run("freeze_card", {"card_tail": ""}, ctx)


def test_another_accounts_card_is_invisible(ctx) -> None:
    """5678 属于信用卡账户：按储蓄卡会话找不到它，也就动不了它。"""
    result = _run("freeze_card", {"card_tail": "5678"}, ctx)
    assert not result.ok
    assert result.error_code == "CARD_NOT_FOUND"
    row = ctx.conn.execute("SELECT status FROM cards WHERE tail = '5678'").fetchone()
    assert str(row["status"]) == "active"


# ---------------------------------------------------------------- 策略层红黄


@pytest.mark.parametrize("tool", sorted(SENSITIVE_TOOLS & set(registry.TOOL_NAMES)))
def test_card_writes_that_touch_account_security_are_red(tool: str) -> None:
    """调限额与冻结/解冻/挂失都动账户安全，落到 R09 红，并要三重验证。"""
    decision = decide(Action(tool=tool, params={}, account_id="a_debit"))
    assert decision.level == "red"
    assert decision.rule_id == "R09_sensitive_red"
    assert decision.required_factors == ["face", "sms", "otp"]