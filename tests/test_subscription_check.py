"""逐笔核实疑似订阅：不能只给一句「像订阅」，要把每一笔流水念出来。

用户的要求（2026-10-25）：核实到底干了啥、要真核实；逐笔核实而不是一起核实。
这一组钉住：

1. 「核实」命中的是逐笔视图（`subscription_check`），每笔都带时间/金额/渠道；
2. 聊天里说「是我订的 / 不是我订的」和业务管理页按钮同源，只写一行定性；
3. 核实本身是只读的：不动钱、不建计划、不写定性；
4. 同一结论重复说不会重复记账，改口是覆盖不是追加；
5. 不在候选里、也没有历史定性的商户，不会凭空写一条结论。

种子数据本身能推断出 3 条周期性扣款，所以断言一律相对基线、用自定义商户名。
"""

from __future__ import annotations

from typing import Any

import pytest

from app import audit, subscriptions
from app.clock import ValidationError, now_ms
from app.planner import assist
from tests.tool_fixtures import DEBIT_ID, USER_ID, bank, ctx

__all__ = ["bank", "ctx"]

M = "测试商户甲"
M2 = "测试商户乙"
DAY_MS = 86_400_000


def _monthly(conn, merchant: str, amounts: list[int], *, channel: str = "auto") -> None:
    now = now_ms()
    for index, amount in enumerate(amounts):
        conn.execute(
            "INSERT INTO transactions (id, account_id, direction, amount_fen, counterparty,"
            " category, note, ts_ms, balance_after_fen, channel, reversal_of)"
            " VALUES (?, ?, 'out', ?, ?, '订阅', '自动续费', ?, ?, ?, NULL)",
            (
                f"t_{merchant}_{index}",
                DEBIT_ID,
                amount,
                merchant,
                now - (70 - index * 30) * DAY_MS,
                1_000_000,
                channel,
            ),
        )


def _balance(conn) -> int:
    return int(conn.execute("SELECT balance_fen FROM accounts WHERE id = ?", (DEBIT_ID,)).fetchone()[0])


def _plan_count(conn) -> int:
    return int(conn.execute("SELECT COUNT(*) FROM plans").fetchone()[0])


def _verdict_rows(conn) -> list[Any]:
    return [
        (str(row["merchant"]), str(row["verdict"]))
        for row in conn.execute(
            "SELECT merchant, verdict FROM subscription_verdicts ORDER BY merchant"
        ).fetchall()
    ]


def _audit_count(conn, event: str) -> int:
    return int(
        conn.execute("SELECT COUNT(*) FROM audit_log WHERE event_type = ?", (event,)).fetchone()[0]
    )


def _merchants(conn) -> set[str]:
    return {
        str(item["merchant"])
        for item in subscriptions.detect_recurring(conn, DEBIT_ID, user_id=USER_ID)["items"]
    }


def _check(conn, merchant: str = M) -> dict[str, Any]:
    out = assist.handle(conn, f"帮我核实一下「{merchant}」这几笔是不是订阅")
    assert out is not None and out["action"] == "subscription_check", out
    return out


@pytest.fixture()
def subs(bank):
    _monthly(bank, M, [1999, 1999, 1999])
    _monthly(bank, M2, [1500, 1500, 1500])
    assert {M, M2} <= _merchants(bank)
    return bank


def test_detect_routes_subscription_phrases_and_leaves_queries_alone() -> None:
    assert assist.detect(f"帮我核实一下「{M}」这几笔是不是订阅") == "subscription"
    assert assist.detect(f"「{M}」不是我订的") == "subscription"
    assert assist.detect(f"「{M}」确实是我订的") == "subscription"
    assert assist.detect("我有哪些疑似订阅") == "subscription"
    # 查余额/账单不能被订阅快路抢走
    assert assist.detect("查一下我储蓄卡尾号1234的余额") is None
    assert assist.detect("看看我这个月花了多少") is None


def test_check_reads_every_transaction_with_time_amount_and_channel(subs) -> None:
    out = _check(subs)
    rows = out["data"]["transactions"]
    assert out["data"]["merchant"] == M
    assert len(rows) == 3
    assert all(row["amount_yuan"] == "19.99" for row in rows)
    # 渠道要翻成人话，不能把 auto 这种内部值甩给用户
    assert {row["channel_label"] for row in rows} == {"自动扣款"}
    # 每一笔都要在正文里念出来，且带序号
    for index, row in enumerate(rows, start=1):
        assert f"{index}) {row['ts_text']}" in out["summary"]
        assert row["amount_yuan"] in out["summary"]
    assert "逐笔" in out["summary"]
    # 边界必须说清：流水原文不是商户订单
    assert "不是商户订单" in out["summary"]
    # 候选的推断信息要一起给出来（period/interval/把握度）
    candidate = out["data"]["candidate"]
    assert candidate is not None and candidate["merchant"] == M
    assert "把握度" in out["summary"]


def test_check_offers_both_verdict_choices(subs) -> None:
    out = _check(subs)
    values = [str(choice["value"]) for choice in out["choices"]]
    assert f"「{M}」不是我订的" in values
    assert f"「{M}」确实是我订的" in values


def test_check_is_read_only(subs) -> None:
    balance = _balance(subs)
    plans = _plan_count(subs)
    verdicts = _verdict_rows(subs)
    _check(subs)
    assert _balance(subs) == balance
    assert _plan_count(subs) == plans
    assert _verdict_rows(subs) == verdicts
    assert _audit_count(subs, "subscription.verdict") == 0


def test_chat_verdict_records_and_removes_from_backlog(subs) -> None:
    balance = _balance(subs)
    plans = _plan_count(subs)
    out = assist.handle(subs, f"「{M}」不是我订的")
    assert out is not None and out["action"] == "subscription_verdict"
    assert _verdict_rows(subs) == [(M, subscriptions.VERDICT_NOT_SUBSCRIPTION)]
    assert M not in _merchants(subs)
    assert _audit_count(subs, "subscription.verdict") == 1
    # 定性只写结论：不动钱、不建计划
    assert _balance(subs) == balance
    assert _plan_count(subs) == plans


def test_confirmed_verdict_also_removes_it(subs) -> None:
    out = assist.handle(subs, f"「{M}」确实是我订的")
    assert out is not None and out["action"] == "subscription_verdict"
    assert _verdict_rows(subs) == [(M, subscriptions.VERDICT_CONFIRMED)]
    assert M not in _merchants(subs)


def test_repeating_the_same_verdict_does_not_double_write(subs) -> None:
    assist.handle(subs, f"「{M}」不是我订的")
    out = assist.handle(subs, f"「{M}」不是我订的")
    assert out is not None
    assert _verdict_rows(subs) == [(M, subscriptions.VERDICT_NOT_SUBSCRIPTION)]
    assert _audit_count(subs, "subscription.verdict") == 1
    assert "已经定过性" in out["summary"]


def test_changing_the_verdict_overwrites_the_single_row(subs) -> None:
    assist.handle(subs, f"「{M}」不是我订的")
    assist.handle(subs, f"「{M}」确实是我订的")
    assert _verdict_rows(subs) == [(M, subscriptions.VERDICT_CONFIRMED)]
    assert _audit_count(subs, "subscription.verdict") == 2
    assert M not in _merchants(subs)


def test_settled_merchant_can_still_be_checked_line_by_line(subs) -> None:
    assist.handle(subs, f"「{M}」确实是我订的")
    out = _check(subs)
    assert out["data"]["candidate"] is None
    assert len(out["data"]["transactions"]) == 3
    assert "定过性" in out["summary"]


def test_unknown_merchant_is_not_given_a_verdict(subs) -> None:
    out = assist.handle(subs, "「从来没出现过的商户」不是我订的")
    assert out is not None
    assert _verdict_rows(subs) == []
    assert _audit_count(subs, "subscription.verdict") == 0


def test_list_shows_candidates_as_choices(subs) -> None:
    out = assist.handle(subs, "我有哪些疑似订阅")
    assert out is not None
    labels = " ".join(str(choice["label"]) for choice in out["choices"])
    assert M in labels and M2 in labels
    assert "核对" in labels


def test_merchant_helpers_reject_empty_and_list_merchants(subs) -> None:
    with pytest.raises(ValidationError):
        subscriptions.merchant_transactions(subs, DEBIT_ID, "")
    names = {str(item["merchant"]) for item in subscriptions.merchants_with_transactions(subs, DEBIT_ID)}
    assert {M, M2} <= names
    rows = subscriptions.merchant_transactions(subs, DEBIT_ID, M, limit=2)
    assert len(rows) == 2
    assert rows[0]["ts_ms"] >= rows[1]["ts_ms"]


def test_audit_chain_stays_valid_after_chat_verdict(subs) -> None:
    assist.handle(subs, f"「{M}」不是我订的")
    assert audit.verify_chain(subs) is True
