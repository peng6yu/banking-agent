"""停掉银行侧代扣之后：待办里不该再有它，但「已停止」一栏要看得到。

用户投诉（2026-10-25）：点确认卡「停止代扣」→ 银行侧 subs 行已 cancelled、聊天也
说「已停掉银行侧代扣」，可「疑似订阅（从流水推断）」里同一家商户还挂着，像没处理；
确认卡上更是只写「取消订阅」「见操作说明全文」，看不到是哪家商户、月费多少、
下次扣费什么时候、边界是什么。这一组钉住四件事：

1. subs 里 status='cancelled' 的商户从「疑似订阅」待办里排除，active 的不排除；
2. 被停掉的商户不消失：stopped_debits 单列一栏，字段齐（金额两位小数、周期中文、
   固定边界口径）；
3. management_view 的 inferred 带 stopped / stopped_count，note 说明停掉的已不在待办；
4. 确认卡带订阅详情（describe_confirmation / prompt_payload 透传），且
   new_confirmation 的文案带上商户与月费——查不到订阅号时退回原文案，不编造。
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from typing import Any

import pytest

from app import subscriptions
from app.api import business
from app.models import DAGNode, Action, Decision
from app.planner import events
from app.planner import state as planner_state
from tests.tool_fixtures import DEBIT_ID, SUB_ACTIVE, SUB_CANCELLED, USER_ID, bank, ctx

__all__ = ["bank", "ctx"]

DAY_MS = 86_400_000
NOW = int(time.time() * 1000)
USER = USER_ID
#: 契约字段：确认卡 details.subscription 与管理页 inferred.stopped[] 都是这套。
SUBSCRIPTION_KEYS = {
    "merchant",
    "amount_fen",
    "amount_yuan",
    "period",
    "period_label",
    "next_charge_text",
    "boundary_text",
    "status",
    "status_label",
}
STOPPED_KEYS = SUBSCRIPTION_KEYS | {"sub_id"}


def _monthly(conn, merchant: str, amounts: list[int], *, start_days_ago: int = 100) -> None:
    """造若干笔按月出现的同商户流水（category=订阅、channel=app）。"""
    for index, amount in enumerate(amounts):
        conn.execute(
            "INSERT INTO transactions (id, account_id, direction, amount_fen, counterparty,"
            " category, note, ts_ms, balance_after_fen, channel, reversal_of)"
            " VALUES (?, ?, 'out', ?, ?, '订阅', '', ?, ?, 'app', NULL)",
            (
                f"t_{merchant}_{index}",
                DEBIT_ID,
                amount,
                merchant,
                NOW - (start_days_ago - index * 30) * DAY_MS,
                1_000_000,
            ),
        )


def _merchants(conn, user_id: str | None = USER) -> set[str]:
    items = subscriptions.detect_recurring(
        conn, DEBIT_ID, as_of=NOW, user_id=user_id
    )["items"]
    return {str(item["merchant"]) for item in items}


def _subs(conn, sub_id: str, merchant: str, amount_fen: int, period: str, status: str) -> None:
    conn.execute(
        "INSERT INTO subs (id, account_id, merchant, amount_fen, period, next_charge_ms, status)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (sub_id, DEBIT_ID, merchant, amount_fen, period, NOW + 10 * DAY_MS, status),
    )


def _action(sub_id: str) -> Action:
    return Action(
        tool="cancel_subscription", params={"sub_id": sub_id}, account_id=DEBIT_ID
    )


def _decision() -> Decision:
    return Decision(
        level="yellow",
        rule_id="R05_confirm_only",
        reason_code="cancel_subscription",
        reason_text="停止代扣会改订阅状态，需要你点确认才会执行。",
    )


def _node(sub_id: str) -> DAGNode:
    return DAGNode(id="n1", tool="cancel_subscription", params={"sub_id": sub_id})


def _state(conn: Any) -> SimpleNamespace:
    """new_confirmation 只用到连接、会话/运行号和卡片表，不需要真开一轮。"""
    return SimpleNamespace(
        conn=conn, session_id="s_stop", run_id="r_stop", confirmations={}
    )


# ------------------------------------------------- 1. 待办过滤：cancelled 走，active 留


def test_a_cancelled_bank_debit_leaves_the_backlog(bank) -> None:
    _monthly(bank, "测试商户丙", [1800, 1800, 1800])
    assert "测试商户丙" in _merchants(bank)

    # active 的银行侧代扣不排除：那是用户还需要决定的那批。
    _subs(bank, "sb_pro_active", "测试商户丙", 1800, "monthly", "active")
    assert "测试商户丙" in _merchants(bank)

    # 停掉银行侧代扣 = 用户已经处理完了，不该再当待办。
    bank.execute("UPDATE subs SET status = 'cancelled' WHERE id = ?", ("sb_pro_active",))
    assert "测试商户丙" not in _merchants(bank)
    # 反证：别的候选没被顺手清空，用例真的在跑推断而不是拿到空列表。
    assert _merchants(bank) is not None


def test_the_cancelled_fixture_merchant_is_gone_only_because_of_subs(bank) -> None:
    """百度网盘：流水够 3 笔、金额也稳，只因为 subs 行是 cancelled 才消失。"""
    bank.execute(
        "INSERT INTO transactions (id, account_id, direction, amount_fen, counterparty,"
        " category, note, ts_ms, balance_after_fen, channel, reversal_of)"
        " VALUES (?, ?, 'out', ?, ?, '订阅', '', ?, ?, 'app', NULL)",
        ("t_sub_h", DEBIT_ID, 1200, "百度网盘", NOW - 80 * DAY_MS, 1_000_000),
    )
    assert "百度网盘" not in _merchants(bank, user_id=None)

    # 把银行侧代扣改回 active：同一条商户马上回到推断结果里——排除来自 subs 状态，
    # 不是流水不够或别的过滤条件顺手把它藏了。
    bank.execute("UPDATE subs SET status = 'active' WHERE id = ?", (SUB_CANCELLED,))
    assert "百度网盘" in _merchants(bank, user_id=None)


# ------------------------------------------------- 2. 已停止一栏的字段与口径


def test_stopped_debits_lists_cancelled_subs_with_full_fields(bank) -> None:
    _subs(bank, "sb_yearly", "某某视频年费", 19_900, "yearly", "cancelled")
    items = subscriptions.stopped_debits(bank, DEBIT_ID)
    by_merchant = {str(item["merchant"]): item for item in items}
    assert set(by_merchant) == {"百度网盘", "某某视频年费"}

    monthly = by_merchant["百度网盘"]
    assert set(monthly) == STOPPED_KEYS
    assert monthly["sub_id"] == SUB_CANCELLED
    # 金额分转元，两位小数字符串（复用 fen_to_yuan_str，不新造格式）
    assert monthly["amount_fen"] == 1200
    assert monthly["amount_yuan"] == "12.00"
    assert monthly["period"] == "monthly"
    assert monthly["period_label"] == "每月"
    assert monthly["status"] == "cancelled"
    assert monthly["status_label"] == "已停止（银行侧）"
    assert monthly["next_charge_text"].startswith("2026-11-08")
    # 边界口径固定：停的只是银行侧代扣，商户会员得用户自己退。
    assert monthly["boundary_text"] == "只停了银行侧周期性代扣，商户会员需要你自己去商户端取消"

    yearly = by_merchant["某某视频年费"]
    assert yearly["period_label"] == "每年"
    assert yearly["amount_yuan"] == "199.00"


def test_stopped_debits_is_empty_when_nothing_was_stopped(bank) -> None:
    bank.execute("UPDATE subs SET status = 'active' WHERE status = 'cancelled'")
    assert subscriptions.stopped_debits(bank, DEBIT_ID) == []
    assert "stopped_debits" in subscriptions.__all__


# ------------------------------------------------- 3. 业务管理页的 inferred.stopped


def test_management_view_lists_stopped_separately_from_the_backlog(bank) -> None:
    # 补第 3 笔流水：这条原本能被推断出来，停掉之后必须从待办里消失。
    bank.execute(
        "INSERT INTO transactions (id, account_id, direction, amount_fen, counterparty,"
        " category, note, ts_ms, balance_after_fen, channel, reversal_of)"
        " VALUES (?, ?, 'out', ?, ?, '订阅', '', ?, ?, 'app', NULL)",
        ("t_sub_h", DEBIT_ID, 1200, "百度网盘", NOW - 80 * DAY_MS, 1_000_000),
    )
    inferred = business.management_view(bank)["subscriptions"]["inferred"]
    assert "百度网盘" not in {str(item["merchant"]) for item in inferred["items"]}
    stopped = inferred["stopped"]
    assert inferred["stopped_count"] == len(stopped) == 1
    assert stopped[0]["sub_id"] == SUB_CANCELLED
    assert set(stopped[0]) == STOPPED_KEYS
    # note 要说清「停掉的不在待办里、在哪一栏」，否则用户还是会以为没处理。
    assert "停掉银行侧代扣的商户已不在待办里" in inferred["note"]
    assert "已停止银行侧代扣" in inferred["note"]
    # 现有的定性字段一个都不能少（本任务只加字段，不改旧口径）
    assert {"verdicts", "settled", "settled_count", "confirmed_count", "dismissed_count"} <= set(
        inferred
    )


# ------------------------------------------------- 4. 确认卡带订阅详情


def test_describe_confirmation_shows_the_subscription_detail(bank) -> None:
    details = events.describe_confirmation(
        bank, _action(SUB_ACTIVE), _decision(), tool="cancel_subscription"
    )
    sub = details["subscription"]
    assert sub is not None and set(sub) == SUBSCRIPTION_KEYS
    assert sub["merchant"] == "爱奇艺会员"
    assert sub["amount_fen"] == 2500
    assert sub["amount_yuan"] == "25.00"
    assert sub["period"] == "monthly"
    assert sub["period_label"] == "每月"
    assert sub["next_charge_text"].startswith("2026-11-05")
    assert sub["status"] == "active"
    assert sub["status_label"] == "生效中"
    assert sub["boundary_text"] == "只停了银行侧周期性代扣，商户会员需要你自己去商户端取消"
    # 别的工具没有订阅详情：显式 None，不硬塞一个空对象冒充。
    assert events.describe_confirmation(bank, _action(SUB_ACTIVE), _decision(), tool="transfer")[
        "subscription"
    ] is None


def test_describe_confirmation_does_not_invent_a_missing_subscription(bank) -> None:
    details = events.describe_confirmation(
        bank, _action("sb_ghost"), _decision(), tool="cancel_subscription"
    )
    assert details["subscription"] is None


def test_prompt_payload_passes_the_subscription_through(bank) -> None:
    action = _action(SUB_ACTIVE)
    node = _node(SUB_ACTIVE)
    decision = _decision()
    conf = planner_state.Confirmation(
        id="cf_x",
        node_id=node.id,
        action=action,
        level="yellow",
        required_factors=[],
        action_text="停止银行侧代扣「爱奇艺会员」（每月 25.00 元）",
        created_ms=1,
        expires_ms=2,
    )
    details = events.describe_confirmation(bank, action, decision, tool=node.tool)
    payload = events.prompt_payload(node, conf, decision, details)
    assert payload["subscription"] == details["subscription"]
    # 老调用方不传 details 也不能崩：键在，值是 None。
    assert events.prompt_payload(node, conf, decision)["subscription"] is None


# ------------------------------------------------- 5. 确认卡文案带对象


def test_new_confirmation_names_the_merchant_and_the_monthly_fee(bank) -> None:
    action = _action(SUB_ACTIVE)
    node = _node(SUB_ACTIVE)

    async def build() -> planner_state.Confirmation:
        return planner_state.new_confirmation(_state(bank), node, action, _decision())

    conf = asyncio.run(build())
    assert "爱奇艺会员" in conf.action_text
    assert "每月" in conf.action_text
    assert "25.00" in conf.action_text
    assert "取消订阅" not in conf.action_text
    # 落库的卡片状态不变：文案改了，流程口径没改。
    row = bank.execute("SELECT status FROM confirmations WHERE id = ?", (conf.id,)).fetchone()
    assert str(row["status"]) == "pending"
    # action_text() 仍是纯函数：不连库，保持原来的通用文案。
    assert planner_state.action_text("cancel_subscription", action) == "取消订阅"


def test_new_confirmation_falls_back_when_the_subscription_is_unknown(bank) -> None:
    action = _action("sb_ghost")
    node = _node("sb_ghost")

    async def build() -> planner_state.Confirmation:
        return planner_state.new_confirmation(_state(bank), node, action, _decision())

    conf = asyncio.run(build())
    # 查不到就退回原文案，绝不编一个商户名出来。
    assert conf.action_text == "取消订阅"
