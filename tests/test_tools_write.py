"""四个写工具的分支表测试（docs/06 6.3）：令牌闸门、两阶段资金、订阅与商户白名单。

transfer 一次调用就把两阶段资金走完（commit 在工具内部完成），所以断言直接查
holds/transactions 表，不再依赖 planner 补第二次扣款。
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from app import audit, db, funds
from app.clock import HoldExpired, MissingCapability
from app.tools import registry
from app.tools.base import ToolContext
from app.tools.registry import WRITE_TOOL_NAMES
from tests.tool_fixtures import (
    DEBIT_BALANCE,
    DEBIT_ID,
    FROZEN_MS,
    MINIMAL_PARAMS,
    PAYEE_LIJING,
    SUB_ACTIVE,
    _balance,
    _token,
    bank,
    ctx,
)

__all__ = ["bank", "ctx"]

def _params(tool: str, **extra: Any) -> dict[str, Any]:
    """按 MINIMAL_PARAMS 起手再补参数，保证和签令牌时用的参数一字不差。"""
    return {**MINIMAL_PARAMS[tool], **extra}


def test_transfer_commits_both_phases_in_one_call(ctx: ToolContext) -> None:
    """正常分支：两阶段资金都在工具内部走完，一次调用就完成 hold + commit。"""
    params = _params("transfer", payee_id=PAYEE_LIJING, amount_fen=10_000, note="十月房租")
    result = registry.execute("transfer", params, token=_token("transfer", params), ctx=ctx)
    assert result.ok is True
    assert result.data["status"] == "committed"
    assert result.data["hold_id"]
    assert result.data["amount_fen"] == 10_000
    assert result.data["amount_yuan"] == "100.00"
    assert result.data["payee_name"] == "李静"
    assert result.data["balance_after_fen"] == DEBIT_BALANCE - 10_000
    assert _balance(ctx.conn) == DEBIT_BALANCE - 10_000
    hold = funds.fetch_hold(ctx.conn, result.data["hold_id"])
    assert hold is not None
    assert hold.status == "committed"
    assert hold.expires_ms == FROZEN_MS + funds.HOLD_TTL_MS


def test_transfer_reports_settle_error_instead_of_raising(ctx: ToolContext, monkeypatch) -> None:
    """第二阶段失败不往 planner 抛：结果照样 ok，钱仍锁在 hold 里并带 settle_error。"""
    params = _params("transfer", payee_id=PAYEE_LIJING, amount_fen=10_000)

    def boom(*_args: Any, **_kwargs: Any) -> None:
        raise HoldExpired("预授权已经过期了", hold_id="h_x")

    monkeypatch.setattr(funds, "commit", boom)
    result = registry.execute("transfer", params, token=_token("transfer", params), ctx=ctx)
    assert result.ok is True
    assert result.data["status"] == "held"
    assert result.data["settle_error"] == "HOLD_EXPIRED"
    assert _balance(ctx.conn) == DEBIT_BALANCE


def test_transfer_rejects_unknown_payee_and_insufficient_balance(ctx: ToolContext) -> None:
    """错误分支：收款人不存在是 PAYEE_NOT_FOUND；余额不足要报出差额。"""
    ghost = _params("transfer", payee_id="p_ghost")
    miss = registry.execute("transfer", ghost, token=_token("transfer", ghost), ctx=ctx)
    assert miss.ok is False
    assert miss.error_code == "PAYEE_NOT_FOUND"
    poor = _params("transfer", payee_id=PAYEE_LIJING, amount_fen=2_000_000)
    short = registry.execute("transfer", poor, token=_token("transfer", poor), ctx=ctx)
    assert short.ok is False
    assert short.error_code == "INSUFFICIENT_FUNDS"
    assert short.data["short_fen"] == 2_000_000 - DEBIT_BALANCE
    assert _balance(ctx.conn) == DEBIT_BALANCE


def _seed_foreign_payee(conn) -> str:
    """在账本里放一个属于别人的收款人，复现红队 C03 的越权场景。"""
    conn.execute(
        "INSERT INTO users (id, name, phone, created_ms) VALUES (?, ?, ?, ?)",
        ("u_other", "赵四", "13800000002", FROZEN_MS),
    )
    conn.execute(
        "INSERT INTO payees (id, user_id, name, bank, card_tail, alias) VALUES (?, ?, ?, ?, ?, ?)",
        ("p_other", "u_other", "李静", "工商银行", "9999", None),
    )
    return "p_other"


def test_transfer_refuses_payee_owned_by_someone_else(ctx: ToolContext) -> None:
    """纵深防御：绕开 resolve_payee 直调 transfer，也不能把他人的收款人转走。"""
    payee_id = _seed_foreign_payee(ctx.conn)
    holds_before = ctx.conn.execute("SELECT COUNT(*) AS n FROM holds").fetchone()["n"]
    rows_before = ctx.conn.execute(
        "SELECT COUNT(*) AS n FROM transactions WHERE hold_id IS NOT NULL"
    ).fetchone()["n"]
    params = _params("transfer", payee_id=payee_id, amount_fen=50_000)
    result = registry.execute("transfer", params, token=_token("transfer", params), ctx=ctx)
    assert result.ok is False
    assert result.error_code == "PAYEE_NOT_OWNED"
    assert result.error_text == "通讯录里没有属于你的这个收款人。"
    assert "hold_id" not in result.data
    assert _balance(ctx.conn) == DEBIT_BALANCE
    assert ctx.conn.execute("SELECT COUNT(*) AS n FROM holds").fetchone()["n"] == holds_before
    assert (
        ctx.conn.execute(
            "SELECT COUNT(*) AS n FROM transactions WHERE hold_id IS NOT NULL"
        ).fetchone()["n"]
        == rows_before
    )


def test_transfer_still_commits_for_payee_of_the_same_user(ctx: ToolContext) -> None:
    """同一归属校验不能误伤：自己名下的收款人照常两阶段走完。"""
    foreign = _seed_foreign_payee(ctx.conn)
    denied = _params("transfer", payee_id=foreign, amount_fen=50_000)
    registry.execute("transfer", denied, token=_token("transfer", denied), ctx=ctx)
    params = _params("transfer", payee_id=PAYEE_LIJING, amount_fen=50_000)
    result = registry.execute("transfer", params, token=_token("transfer", params), ctx=ctx)
    assert result.ok is True
    assert result.data["status"] == "committed"
    assert result.data["payee_name"] == "李静"
    assert _balance(ctx.conn) == DEBIT_BALANCE - 50_000
    assert audit.verify_chain(ctx.conn) is True


def test_cross_user_rejection_stays_on_the_audit_chain(ctx: ToolContext) -> None:
    """越权尝试本身要留痕：tool.executed(ok=false) 带上错误码，链仍然可校验。"""
    payee_id = _seed_foreign_payee(ctx.conn)
    params = _params("transfer", payee_id=payee_id, amount_fen=50_000)
    registry.execute("transfer", params, token=_token("transfer", params), ctx=ctx)
    rows = ctx.conn.execute(
        "SELECT event_type, payload_json FROM audit_log ORDER BY seq"
    ).fetchall()
    codes = [
        json.loads(row["payload_json"]).get("error_code")
        for row in rows
        if row["event_type"] == "tool.executed"
    ]
    assert "PAYEE_NOT_OWNED" in codes
    assert all(row["event_type"] in audit.EVENT_TYPES for row in rows)
    assert "transfer.initiated" not in [row["event_type"] for row in rows]
    assert audit.verify_chain(ctx.conn) is True


def test_cancel_subscription_marks_row_and_audits(ctx: ToolContext) -> None:
    """正常分支：状态改掉并留审计；重复取消幂等。"""
    params = _params("cancel_subscription")
    first = registry.execute(
        "cancel_subscription", params, token=_token("cancel_subscription", params), ctx=ctx
    )
    assert first.ok is True
    assert first.data["idempotent"] is False
    assert first.audit_seq is not None
    row = ctx.conn.execute("SELECT status FROM subs WHERE id = ?", (SUB_ACTIVE,)).fetchone()
    assert row["status"] == "cancelled"
    again = registry.execute(
        "cancel_subscription", params, token=_token("cancel_subscription", params), ctx=ctx
    )
    assert again.ok is True
    assert again.data["idempotent"] is True


def test_cancel_subscription_rejects_unknown_id(ctx: ToolContext) -> None:
    """错误分支：订阅不存在是 SUB_NOT_FOUND。"""
    params = _params("cancel_subscription", sub_id="sb_ghost")
    result = registry.execute(
        "cancel_subscription", params, token=_token("cancel_subscription", params), ctx=ctx
    )
    assert result.ok is False
    assert result.error_code == "SUB_NOT_FOUND"


def test_reserve_funds_holds_for_system_payee_without_ledger(ctx: ToolContext) -> None:
    """正常分支：预授权挂到 p_system，不产生转账流水。"""
    params = _params("reserve_funds", amount_fen=50_000, purpose="下月房租")
    result = registry.execute(
        "reserve_funds", params, token=_token("reserve_funds", params), ctx=ctx
    )
    assert result.ok is True
    assert result.data["hold_id"]
    assert result.data["expires_ms"] == FROZEN_MS + funds.HOLD_TTL_MS
    ledger = ctx.conn.execute(
        "SELECT COUNT(*) AS n FROM transactions WHERE hold_id = ?", (result.data["hold_id"],)
    ).fetchone()
    assert ledger["n"] == 0
    assert _balance(ctx.conn) == DEBIT_BALANCE


def test_reserve_funds_rejects_amount_above_balance(ctx: ToolContext) -> None:
    """错误分支：余额不足。"""
    params = _params("reserve_funds", amount_fen=9_999_999, purpose="太多")
    result = registry.execute(
        "reserve_funds", params, token=_token("reserve_funds", params), ctx=ctx
    )
    assert result.ok is False
    assert result.error_code == "INSUFFICIENT_FUNDS"
    assert result.data["short_fen"] == 9_999_999 - DEBIT_BALANCE


def test_pay_merchant_creates_and_commits_hold_in_one_call(ctx: ToolContext) -> None:
    """正常分支：白名单商户现建预授权并立刻 commit，余额真的少了。"""
    params = _params("pay_merchant", merchant="flower_shop", item="生日鲜花", amount_fen=52_000)
    result = registry.execute(
        "pay_merchant", params, token=_token("pay_merchant", params), ctx=ctx
    )
    assert result.ok is True
    assert result.data["status"] == "committed"
    assert result.data["hold_id"]
    assert _balance(ctx.conn) == DEBIT_BALANCE - 52_000
    hold = funds.fetch_hold(ctx.conn, result.data["hold_id"])
    assert hold is not None and hold.status == "committed"


def test_pay_merchant_rejects_merchant_outside_whitelist(ctx: ToolContext) -> None:
    """错误分支：白名单只有 flower_shop 和 cake_shop。"""
    params = _params("pay_merchant", merchant="luxury_shop", item="名牌包", amount_fen=99_900)
    result = registry.execute(
        "pay_merchant", params, token=_token("pay_merchant", params), ctx=ctx
    )
    assert result.ok is False
    assert result.error_code == "MERCHANT_NOT_ALLOWED"
    assert _balance(ctx.conn) == DEBIT_BALANCE


def test_pay_merchant_commits_an_existing_hold(ctx: ToolContext) -> None:
    """带 hold_id 时走第 2 阶段：有效的 commit，过期的回 HOLD_EXPIRED。"""
    reserved = _params("reserve_funds", amount_fen=30_000, purpose="买蛋糕")
    held = registry.execute(
        "reserve_funds", reserved, token=_token("reserve_funds", reserved), ctx=ctx
    )
    params = _params("pay_merchant", merchant="cake_shop", hold_id=held.data["hold_id"])
    result = registry.execute(
        "pay_merchant", params, token=_token("pay_merchant", params), ctx=ctx
    )
    assert result.ok is True
    assert result.data["status"] == "committed"
    assert _balance(ctx.conn) == DEBIT_BALANCE - 30_000
    # 同样这笔预授权再 commit 一次：幂等，不会再扣一次钱
    repeat = registry.execute(
        "pay_merchant", params, token=_token("pay_merchant", params), ctx=ctx
    )
    assert repeat.ok is True
    assert _balance(ctx.conn) == DEBIT_BALANCE - 30_000


def test_pay_merchant_rejects_expired_and_unknown_hold(ctx: ToolContext) -> None:
    """错误分支：预授权过期是 HOLD_EXPIRED，压根没有这个预授权是 HOLD_NOT_FOUND。"""
    with db.tx(ctx.conn):
        ctx.conn.execute(
            "INSERT INTO holds (id, account_id, payee_id, amount_fen, note, status,"
            " idempotency_key, session_id, created_ms, expires_ms)"
            " VALUES ('h_old', ?, ?, 10_000, '过期了', 'held', 'k_old', 's_tools', ?, ?)",
            (DEBIT_ID, PAYEE_LIJING, FROZEN_MS - 10_000, FROZEN_MS - 1_000),
        )
    expired = _params("pay_merchant", merchant="cake_shop", hold_id="h_old")
    old = registry.execute(
        "pay_merchant", expired, token=_token("pay_merchant", expired), ctx=ctx
    )
    assert old.ok is False
    assert old.error_code == "HOLD_EXPIRED"
    ghost = _params("pay_merchant", merchant="cake_shop", hold_id="h_ghost")
    none = registry.execute(
        "pay_merchant", ghost, token=_token("pay_merchant", ghost), ctx=ctx
    )
    assert none.ok is False
    assert none.error_code == "HOLD_NOT_FOUND"
    assert _balance(ctx.conn) == DEBIT_BALANCE


@pytest.mark.parametrize("tool", WRITE_TOOL_NAMES)
def test_every_write_tool_refuses_without_capability_token(tool: str, ctx: ToolContext) -> None:
    """四个写工具一条用例盖完：没有令牌一律 MissingCapability，连库都不碰。"""
    before = _balance(ctx.conn)
    with pytest.raises(MissingCapability) as excinfo:
        registry.execute(tool, MINIMAL_PARAMS[tool], token=None, ctx=ctx)
    assert excinfo.value.code == "MISSING_CAPABILITY"
    assert excinfo.value.fields.get("tool") == tool
    assert _balance(ctx.conn) == before



def test_transfer_commits_in_one_call_and_extends_the_audit_chain(ctx: ToolContext) -> None:
    """两阶段资金都在工具内部走完：一次调用即 committed，落一条流水且审计链仍然自洽。"""
    params = _params("transfer", payee_id=PAYEE_LIJING, amount_fen=10_000)
    result = registry.execute("transfer", params, token=_token("transfer", params), ctx=ctx)
    assert result.ok is True
    assert result.data["status"] == "committed"
    assert result.data["balance_after_fen"] == DEBIT_BALANCE - 10_000
    assert "settle_error" not in result.data
    hold_id = str(result.data["hold_id"])
    hold = funds.fetch_hold(ctx.conn, hold_id)
    assert hold is not None and hold.status == "committed"
    rows = ctx.conn.execute(
        "SELECT direction, amount_fen, category FROM transactions WHERE hold_id = ?",
        (hold_id,),
    ).fetchall()
    settled = [(row["direction"], row["amount_fen"], row["category"]) for row in rows]
    assert settled == [("out", 10_000, "转账")]
    assert audit.verify_chain(ctx.conn) is True
