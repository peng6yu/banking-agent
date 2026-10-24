"""订阅推断与续费窗口的测试（阶段 6 P0-7）。

改动前的问题：list_subs 只读 subs 表。也就是说，只有"已经在本行登记过代扣"的
订阅才看得见；用户真正的会员订阅大多不在表里，页面就显示"没有订阅"。
这一组钉住三件事：
1. 能从流水里认出周期性扣款，并且每一笔证据都能点回去；
2. 认不准的时候宁可不报：金额差太多、间隔不规律、转账/理财、模拟流水都不算；
3. 推断结果 != 代扣记录：接口必须同时给出两者的边界说明，界面不许把它当事实。
"""

from __future__ import annotations

import time
from typing import Any

import pytest

from app import audit, subscriptions
from app.tools import registry
from app.tools.base import ToolContext
from tests.tool_fixtures import DEBIT_ID, USER_ID, bank, ctx

__all__ = ["bank", "ctx"]

DAY_MS = 86_400_000
NOW = int(time.time() * 1000)


def _tx(
    conn,
    tx_id: str,
    ts_ms: int,
    amount_fen: int,
    counterparty: str,
    *,
    category: str = "订阅",
    channel: str = "app",
    direction: str = "out",
    reversal_of: str | None = None,
) -> None:
    conn.execute(
        "INSERT INTO transactions (id, account_id, direction, amount_fen, counterparty,"
        " category, note, ts_ms, balance_after_fen, channel, reversal_of)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (tx_id, DEBIT_ID, direction, amount_fen, counterparty, category, "",
         ts_ms, 1_000_000, channel, reversal_of),
    )


def _monthly_series(conn, merchant: str, amounts: list[int], *, start_days_ago: int = 100,
                    category: str = "订阅", channel: str = "app") -> None:
    """按月造一串支出：每笔间隔 30 天。"""
    for index, amount in enumerate(amounts):
        _tx(
            conn,
            f"t_{merchant}_{index}",
            NOW - (start_days_ago - index * 30) * DAY_MS,
            amount,
            merchant,
            category=category,
            channel=channel,
        )


# ---------------------------------------------------------------- 能认出来的情况


def test_three_monthly_charges_are_inferred_with_evidence(ctx: ToolContext) -> None:
    _monthly_series(ctx.conn, "云端笔记", [1_800, 1_800, 1_800])
    result = subscriptions.detect_recurring(ctx.conn, DEBIT_ID, as_of=NOW)
    item = next(i for i in result["items"] if i["merchant"] == "云端笔记")
    assert item["period"] == "monthly"
    assert item["occurrences"] == 3
    assert item["avg_amount_fen"] == 1_800
    assert item["confidence"] in {"medium", "high"}
    assert "云端笔记" in item["basis"] and "30 天" in item["basis"]
    assert item["uncertainty"].strip()
    assert [e["transaction_id"] for e in item["evidence"]] == [
        "t_云端笔记_0", "t_云端笔记_1", "t_云端笔记_2",
    ]
    assert all(e["amount_yuan"] for e in item["evidence"])


def test_four_stable_charges_are_high_confidence(ctx: ToolContext) -> None:
    _monthly_series(ctx.conn, "音乐会员", [1_500, 1_500, 1_500, 1_500])
    result = subscriptions.detect_recurring(ctx.conn, DEBIT_ID, as_of=NOW)
    item = next(i for i in result["items"] if i["merchant"] == "音乐会员")
    assert item["confidence"] == "high"
    assert item["confidence_label"] == "高"


def test_a_yearly_charge_can_be_inferred_too(ctx: ToolContext) -> None:
    conn = ctx.conn
    _tx(conn, "t_year_0", NOW - 760 * DAY_MS, 29_900, "域名续费", category="生活缴费")
    _tx(conn, "t_year_1", NOW - 395 * DAY_MS, 29_900, "域名续费", category="生活缴费")
    _tx(conn, "t_year_2", NOW - 30 * DAY_MS, 29_900, "域名续费", category="生活缴费")
    result = subscriptions.detect_recurring(conn, DEBIT_ID, lookback_months=30, as_of=NOW)
    item = next(i for i in result["items"] if i["merchant"] == "域名续费")
    assert item["period"] == "yearly"
    assert item["period_label"] == "每年"
    assert "一年" in item["uncertainty"] or "年" in item["basis"]


# ---------------------------------------------------------------- 宁可不报的情况


def test_two_occurrences_are_not_enough(ctx: ToolContext) -> None:
    _monthly_series(ctx.conn, "搬家公司", [50_000, 50_000])
    result = subscriptions.detect_recurring(ctx.conn, DEBIT_ID, as_of=NOW)
    assert all(i["merchant"] != "搬家公司" for i in result["items"])


def test_amounts_that_swing_too_much_are_skipped(ctx: ToolContext) -> None:
    """同一个商户每月金额差 40%：更像按用量计费，不该报成订阅。"""
    _monthly_series(ctx.conn, "打车", [2_000, 2_800, 3_900], category="交通")
    result = subscriptions.detect_recurring(ctx.conn, DEBIT_ID, as_of=NOW)
    assert all(i["merchant"] != "打车" for i in result["items"])


def test_irregular_gaps_are_skipped(ctx: ToolContext) -> None:
    """三次支出但间隔是 5 天、70 天：不像订阅，跳过。"""
    _tx(ctx.conn, "t_irr_0", NOW - 80 * DAY_MS, 3_000, "偶尔买", category="购物")
    _tx(ctx.conn, "t_irr_1", NOW - 75 * DAY_MS, 3_000, "偶尔买", category="购物")
    _tx(ctx.conn, "t_irr_2", NOW - 5 * DAY_MS, 3_000, "偶尔买", category="购物")
    result = subscriptions.detect_recurring(ctx.conn, DEBIT_ID, as_of=NOW)
    assert all(i["merchant"] != "偶尔买" for i in result["items"])


def test_transfers_and_investments_are_never_subscriptions(ctx: ToolContext) -> None:
    """每月固定给家里人转钱、每月固定买基金，都不是商户订阅。"""
    _monthly_series(ctx.conn, "妈妈", [300_000, 300_000, 300_000], category="转账")
    _monthly_series(ctx.conn, "现金添利货币A", [100_000, 100_000, 100_000], category="理财")
    result = subscriptions.detect_recurring(ctx.conn, DEBIT_ID, as_of=NOW)
    merchants = {i["merchant"] for i in result["items"]}
    assert "妈妈" not in merchants
    assert "现金添利货币A" not in merchants


def test_simulated_demo_transactions_are_ignored(ctx: ToolContext) -> None:
    """演示用的模拟流水（channel=simulated）不许被当成真实订阅。"""
    _monthly_series(ctx.conn, "演示商户", [9_900, 9_900, 9_900], channel="simulated")
    result = subscriptions.detect_recurring(ctx.conn, DEBIT_ID, as_of=NOW)
    assert all(i["merchant"] != "演示商户" for i in result["items"])


def test_reversed_transactions_are_ignored(ctx: ToolContext) -> None:
    """冲正过的流水不算消费，不能拿它推订阅。"""
    _monthly_series(ctx.conn, "退款商户", [7_000, 7_000, 7_000])
    ctx.conn.execute(
        "UPDATE transactions SET reversed_by = 't_rev' WHERE id = 't_退款商户_1'"
    )
    result = subscriptions.detect_recurring(ctx.conn, DEBIT_ID, as_of=NOW)
    assert all(i["merchant"] != "退款商户" for i in result["items"])


def test_the_window_only_looks_at_recent_months(ctx: ToolContext) -> None:
    """半年前的老记录不进窗口：跟着 lookback_months 走。"""
    _monthly_series(ctx.conn, "去年会员", [9_900, 9_900, 9_900], start_days_ago=400)
    result = subscriptions.detect_recurring(ctx.conn, DEBIT_ID, lookback_months=3, as_of=NOW)
    assert all(i["merchant"] != "去年会员" for i in result["items"])


# ---------------------------------------------------------------- 续费窗口


def test_upcoming_lists_active_subs_within_the_window(ctx: ToolContext) -> None:
    soon = ctx.conn.execute("SELECT * FROM subs WHERE status = 'active'").fetchone()
    assert soon is not None
    ctx.conn.execute(
        "UPDATE subs SET next_charge_ms = ? WHERE id = ?", (NOW + 2 * DAY_MS, soon["id"])
    )
    result = subscriptions.upcoming(ctx.conn, DEBIT_ID, as_of=NOW)
    item = next(i for i in result["items"] if i["sub_id"] == str(soon["id"]))
    assert item["days_left"] == pytest.approx(2.0)
    assert "不推送" in result["note"] or "不会推送" in result["note"]
    assert "商户会员" in result["action"]


def test_upcoming_skips_charges_beyond_the_window(ctx: ToolContext) -> None:
    row = ctx.conn.execute("SELECT * FROM subs WHERE status = 'active'").fetchone()
    assert row is not None
    ctx.conn.execute(
        "UPDATE subs SET next_charge_ms = ? WHERE id = ?", (NOW + 40 * DAY_MS, row["id"])
    )
    result = subscriptions.upcoming(ctx.conn, DEBIT_ID, as_of=NOW)
    assert all(i["sub_id"] != str(row["id"]) for i in result["items"])


def test_upcoming_skips_cancelled_subs(ctx: ToolContext) -> None:
    row = ctx.conn.execute("SELECT * FROM subs WHERE status = 'active'").fetchone()
    assert row is not None
    ctx.conn.execute(
        "UPDATE subs SET status = 'cancelled', next_charge_ms = ? WHERE id = ?",
        (NOW + 1 * DAY_MS, row["id"]),
    )
    result = subscriptions.upcoming(ctx.conn, DEBIT_ID, as_of=NOW)
    assert all(i["sub_id"] != str(row["id"]) for i in result["items"])


# ---------------------------------------------------------------- 工具与边界文案


def test_list_subs_reports_both_sources_and_the_boundary(ctx: ToolContext) -> None:
    result = registry.execute("list_subs", {}, ctx=ctx)
    assert result.ok is True
    data = result.data
    assert data["count"] >= 1
    assert "inferred" in data and "upcoming" in data
    assert data["inferred"]["count"] == len(data["inferred"]["items"])
    assert "不是本行的代扣授权" in data["inferred"]["note"]
    assert "inferred" in data["scope"]
    assert "商户那边" in data["scope"]["cancel_boundary"]


def test_inference_never_writes_to_the_subs_table(ctx: ToolContext) -> None:
    """推断是纯读：跑十次也不会在 subs 表里多出一行，也不会写审计。"""
    before_subs = int(ctx.conn.execute("SELECT COUNT(*) AS n FROM subs").fetchone()["n"])
    before_audit = int(ctx.conn.execute("SELECT COUNT(*) AS n FROM audit_log").fetchone()["n"])
    for _ in range(10):
        subscriptions.detect_recurring(ctx.conn, DEBIT_ID, as_of=NOW)
    assert int(ctx.conn.execute("SELECT COUNT(*) AS n FROM subs").fetchone()["n"]) == before_subs
    assert int(ctx.conn.execute("SELECT COUNT(*) AS n FROM audit_log").fetchone()["n"]) == before_audit
