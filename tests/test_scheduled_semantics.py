"""预约转账的业务语义：跨用户不可读、到点失败必须进审计链。

为什么单独一个文件：
- 越权读（别人的预约出现在我的列表里）和"失败没留痕"都不是资金路径，
  但都属于业务语义错误，放在 test_scheduled.py 的资金用例里容易被忽略。
- 这两个用例只碰 scheduled 工具与审计链，不需要模型、不需要 HTTP。
"""

from __future__ import annotations

from typing import Any

import pytest

import app.audit as audit
import app.tools.scheduled as scheduled
from tests.tool_fixtures import DEBIT_ID, FROZEN_MS, PAYEE_LIJING, SECRET, bank, ctx

__all__ = ["bank", "ctx"]

DUE = FROZEN_MS + 86_400_000
OTHER_USER = "u_other"
OTHER_ACCOUNT = "a_other"
OTHER_PAYEE = "p_other"


def _add_other_user(conn) -> None:
    """另一个用户名下的一套账户 + 收款人，用来验证列表不会串号。"""
    conn.execute(
        "INSERT INTO users (id, name, phone, created_ms) VALUES (?, ?, ?, ?)",
        (OTHER_USER, "李四", "13900000002", FROZEN_MS),
    )
    conn.execute(
        "INSERT INTO accounts (id, user_id, name, tail, type, balance_fen)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (OTHER_ACCOUNT, OTHER_USER, "别人的储蓄卡", "9999", "debit", 500_000),
    )
    conn.execute(
        "INSERT INTO payees (id, user_id, name, bank, card_tail) VALUES (?, ?, ?, ?, ?)",
        (OTHER_PAYEE, OTHER_USER, "王五", "建设银行", "8888"),
    )


def _schedule(conn, sid: str, *, account_id: str, payee_id: str, user_id: str,
              amount_fen: int = 30_000, due_ms: int = DUE) -> None:
    conn.execute(
        "INSERT INTO scheduled_transfers"
        " (id, user_id, account_id, payee_id, amount_fen, note, due_ms, recurrence,"
        " status, created_ms, idempotency_key)"
        " VALUES (?, ?, ?, ?, ?, '', ?, 'once', 'scheduled', ?, ?)",
        (sid, user_id, account_id, payee_id, amount_fen, due_ms, FROZEN_MS, "ik_" + sid),
    )


def test_list_scheduled_only_returns_own_rows(ctx) -> None:
    """列表必须按当前用户过滤：早先的 SQL 没有 user 条件，会列出别人的预约。"""
    _add_other_user(ctx.conn)
    _schedule(ctx.conn, "s_mine", account_id=DEBIT_ID, payee_id=PAYEE_LIJING, user_id="u_test")
    _schedule(ctx.conn, "s_other", account_id=OTHER_ACCOUNT, payee_id=OTHER_PAYEE, user_id=OTHER_USER)

    result = scheduled.list_scheduled(ctx, {})

    assert result.ok is True
    ids = [item["scheduled_id"] for item in result.data["items"]]
    assert ids == ["s_mine"], f"只应看到自己的预约，实际 {ids}"
    assert result.data["count"] == 1


def test_list_scheduled_empty_for_user_without_rows(ctx) -> None:
    """没有预约时给空列表和一句人话，而不是报错。"""
    result = scheduled.list_scheduled(ctx, {})
    assert result.ok is True
    assert result.data["items"] == []
    assert "没有" in result.data["summary"]


def test_failed_execution_is_written_to_audit_chain(ctx, monkeypatch: pytest.MonkeyPatch) -> None:
    """到点执行失败：状态置 failed，同时必须有一条 scheduled.failed 审计。"""
    _schedule(ctx.conn, "s_fail", account_id=DEBIT_ID, payee_id=PAYEE_LIJING, user_id="u_test")

    def _boom(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("token service down")

    monkeypatch.setattr(scheduled.token_mod, "issue_audited", _boom)

    done = scheduled.execute_due(ctx.conn, now=DUE + 1_000, secret=SECRET)

    assert [item["status"] for item in done] == ["failed"]
    row = ctx.conn.execute(
        "SELECT status FROM scheduled_transfers WHERE id = 's_fail'"
    ).fetchone()
    assert str(row["status"]) == "failed"

    rows = ctx.conn.execute(
        "SELECT event_type, payload_json FROM audit_log WHERE event_type = 'scheduled.failed'"
    ).fetchall()
    assert len(rows) == 1, "失败必须留痕，否则事后查不出为什么没转"
    assert "token service down" in str(rows[0]["payload_json"])
    assert audit.verify_chain(ctx.conn) is True
