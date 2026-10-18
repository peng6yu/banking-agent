"""工具层分支表测试（docs/06 6.3）。

覆盖三件事：只读工具在正常与各种错误分支上的返回形状、写工具没有令牌一律拒绝、
analyze_bill 的三条异常规则各自命中与不命中，以及并列排序的可复现性。
"""

from __future__ import annotations

import time
from dataclasses import replace
from typing import Any

import pytest

from app import clock, db, funds
from app.clock import MissingCapability, ValidationError
from app.models import Action
from app.security import token as token_mod
from app.tools import registry
from app.tools.base import ToolContext
from app.tools.registry import TOOL_NAMES, WRITE_TOOL_NAMES

SECRET = "d" * 64
USER_ID = "u_test"
DEBIT_ID = "a_debit"
CREDIT_ID = "a_credit"
DEBIT_BALANCE = 1_286_050
PAYEE_LIJING = "p_lij"
SUB_ACTIVE = "sb_active"
SUB_CANCELLED = "sb_cancelled"

FROZEN_MS = int(time.mktime((2026, 10, 18, 12, 0, 0, 0, -1, -1)) * 1000)
MINIMAL_PARAMS: dict[str, dict[str, Any]] = {
    "transfer": {"payee_id": PAYEE_LIJING, "amount_fen": 30_000},
    "cancel_subscription": {"sub_id": SUB_ACTIVE},
    "reserve_funds": {"amount_fen": 50_000},
    "pay_merchant": {"merchant": "cake_shop", "item": "生日蛋糕", "amount_fen": 88_800},
}


def _ms(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> int:
    return int(time.mktime((year, month, day, hour, minute, 0, 0, -1, -1)) * 1000)


def _tx(
    conn,
    tx_id: str,
    ts_ms: int,
    amount_fen: int,
    counterparty: str,
    category: str,
    *,
    account_id: str = DEBIT_ID,
    direction: str = "out",
    channel: str = "app",
    note: str = "",
) -> None:
    conn.execute(
        "INSERT INTO transactions"
        " (id, account_id, direction, amount_fen, counterparty, category, note,"
        " ts_ms, balance_after_fen, channel)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?)",
        (tx_id, account_id, direction, amount_fen, counterparty, category, note, ts_ms, channel),
    )


def _seed(conn) -> None:
    """一份专供工具测试的小账本：异常样本和反例都摆好了。"""
    conn.execute(
        "INSERT INTO users (id, name, phone, created_ms) VALUES (?, ?, ?, ?)",
        (USER_ID, "张三", "13800000001", FROZEN_MS),
    )
    conn.executemany(
        "INSERT INTO accounts (id, user_id, name, tail, type, balance_fen, credit_limit_fen)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            (DEBIT_ID, USER_ID, "工资储蓄卡", "1234", "debit", DEBIT_BALANCE, 0),
            (CREDIT_ID, USER_ID, "环球信用卡", "5678", "credit", 50_000, 2_000_000),
        ],
    )
    conn.executemany(
        "INSERT INTO cards (id, account_id, tail, kind, status, per_tx_limit_fen)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        [
            ("c_1", DEBIT_ID, "1234", "physical", "active", 500_000),
            ("c_2", CREDIT_ID, "5678", "virtual", "active", 200_000),
        ],
    )
    conn.executemany(
        "INSERT INTO payees (id, user_id, name, bank, card_tail, alias) VALUES (?, ?, ?, ?, ?, ?)",
        [
            (PAYEE_LIJING, USER_ID, "李静", "招行", "8821", "房东"),
            ("p_zhang_a", USER_ID, "张三", "工行", "4417", "同事A"),
            ("p_zhang_b", USER_ID, "张三", "中行", "6620", "同事B"),
            ("p_wang", USER_ID, "王强", "建行", "3095", "老妈"),
        ],
    )
    conn.executemany(
        "INSERT INTO subs (id, account_id, merchant, amount_fen, period, next_charge_ms, status)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            (SUB_ACTIVE, DEBIT_ID, "爱奇艺会员", 2_500, "monthly", _ms(2026, 11, 5), "active"),
            (SUB_CANCELLED, DEBIT_ID, "百度网盘", 1_200, "monthly", _ms(2026, 11, 8), "cancelled"),
        ],
    )
    _seed_anomalies(conn)
    _seed_ties(conn)


def _overseas(
    conn,
    tx_id: str,
    when: int,
    amount_fen: int,
    counterparty: str,
    category: str = "购物",
) -> None:
    """境外流水的快捷写法，channel 固定 overseas。"""
    _tx(conn, tx_id, when, amount_fen, counterparty, category, channel="overseas")


def _seed_anomalies(conn) -> None:
    """命中样本 + 反例：拆单、凌晨境外、订阅涨价三条规则各配一个不该命中的。"""
    for index, minute in enumerate((20 * 60, 20 * 60 + 15, 20 * 60 + 30)):
        _tx(
            conn, f"t_split{index}", _ms(2026, 10, 5, 0, minute), 99_900,
            "优衣库旗舰店", "购物", channel="pos",
        )
    for index, minute in enumerate((9 * 60, 9 * 60 + 10, 9 * 60 + 20)):
        _tx(conn, f"t_small{index}", _ms(2026, 10, 6, 0, minute), 30_000, "楼下小卖部", "购物")
    for index, hour in enumerate((8, 9, 11)):
        _tx(conn, f"t_far{index}", _ms(2026, 10, 7, hour), 99_900, "星巴克", "餐饮")
    _overseas(conn, "t_ovs", _ms(2026, 10, 8, 2, 14), 288_000, "Apple Store 香港")
    _overseas(conn, "t_ovs_day", _ms(2026, 10, 9, 14, 0), 468_000, "巴黎 LV 专卖店")
    _tx(conn, "t_ovs_local", _ms(2026, 10, 10, 3, 0), 288_000, "麦当劳", "餐饮")
    _overseas(conn, "t_ovs_small", _ms(2026, 10, 11, 2, 0), 12_000, "星巴克香港店", "餐饮")
    _tx(conn, "t_sub_a", _ms(2026, 8, 5), 2_500, "爱奇艺会员", "订阅")
    _tx(conn, "t_sub_b", _ms(2026, 9, 5), 2_500, "爱奇艺会员", "订阅")
    _tx(conn, "t_sub_c", _ms(2026, 10, 5), 3_500, "爱奇艺会员", "订阅")
    _tx(conn, "t_sub_d", _ms(2026, 9, 6), 1_200, "百度网盘", "订阅")
    _tx(conn, "t_sub_e", _ms(2026, 10, 6), 1_250, "百度网盘", "订阅")
    _tx(conn, "t_sub_f", _ms(2026, 8, 7), 5_000, "京东商城", "订阅")
    _tx(conn, "t_sub_g", _ms(2026, 10, 7), 7_000, "京东商城", "订阅")


def _seed_ties(conn) -> None:
    """3 月放两笔金额完全相同、分类不同的支出，专测并列时的排序可复现。"""
    _tx(conn, "t_tie_a", _ms(2026, 3, 10, 20, 0), 20_000, "腾讯视频", "娱乐")
    _tx(conn, "t_tie_b", _ms(2026, 3, 10, 21, 0), 20_000, "海底捞", "餐饮")


@pytest.fixture()
def bank(tmp_path):
    """每条用例一套干净账本 + 冻结时间，跑完解冻。"""
    clock.freeze(FROZEN_MS)
    conn = db.connect(str(tmp_path / "tools.db"))
    db.init_db(conn)
    _seed(conn)
    yield conn
    conn.close()
    clock.unfreeze()


@pytest.fixture()
def ctx(bank) -> ToolContext:
    return ToolContext(
        conn=bank, session_id="s_tools", node_id="n1", account_id=DEBIT_ID, secret=SECRET
    )


def _token(tool: str, params: dict[str, Any], amount_fen: int | None = None) -> str:
    """按工具真实参数签一张黄牌令牌，params 必须一字不差。

    金额默认从 params 里取，否则令牌里的 amount_fen 与工具构造的 Action 对不上，
    令牌校验第 7 步会判 AMOUNT_MISMATCH。
    """
    if amount_fen is None and "amount_fen" in params:
        amount_fen = int(params["amount_fen"])
    action = Action(tool=tool, params=params, account_id=DEBIT_ID, amount_fen=amount_fen)
    return token_mod.issue(action, secret=SECRET)


def _balance(conn, account_id: str = DEBIT_ID) -> int:
    row = conn.execute("SELECT balance_fen FROM accounts WHERE id = ?", (account_id,)).fetchone()
    return int(row["balance_fen"])


# ---------------------------------------------------------------- 注册表本身

def test_tool_names_match_authoritative_table() -> None:
    """docs/06 6.1 的权威表：名字与顺序都不能改。"""
    assert TOOL_NAMES == (
        "query_balance",
        "query_transactions",
        "analyze_bill",
        "list_subs",
        "list_cards",
        "resolve_payee",
        "transfer",
        "cancel_subscription",
        "reserve_funds",
        "pay_merchant",
    )
    assert set(TOOL_NAMES) == set(registry.TOOLS)
    assert len(registry.tool_schemas()) == 10


def test_registry_flags_and_schemas_are_consistent() -> None:
    """每个 params 都 additionalProperties=False；金额参数必须是带下限的整数。"""
    for spec in registry.TOOL_SPECS:
        assert spec.params["additionalProperties"] is False
        assert spec.params["type"] == "object"
        for key, prop in spec.params["properties"].items():
            if key.endswith("_fen"):
                assert prop["type"] == "integer", f"{spec.name}.{key}"
                assert "minimum" in prop, f"{spec.name}.{key}"
    assert registry.WRITE_TOOL_NAMES == (
        "transfer",
        "cancel_subscription",
        "reserve_funds",
        "pay_merchant",
    )
    assert set(registry.READONLY_TOOL_NAMES) | set(registry.WRITE_TOOL_NAMES) == set(TOOL_NAMES)


def test_readonly_and_write_split_matches_policy_rules() -> None:
    """工具层的只读/写划分要和 policy 认的那份完全一致，否则安全网会漏。"""
    from app.policy import rules as policy_rules

    assert set(registry.READONLY_TOOL_NAMES) == set(policy_rules.READONLY_TOOLS)
    assert set(registry.WRITE_TOOL_NAMES) == set(
        policy_rules.SPEND_TOOLS | policy_rules.CONFIRM_ONLY_TOOLS
    )


def test_tool_schemas_never_leak_handler() -> None:
    for schema in registry.tool_schemas():
        assert set(schema) == {"name", "description", "params"}
        assert "handler" not in schema["params"]


def test_unknown_tool_and_bad_params_are_rejected(ctx: ToolContext) -> None:
    """注册表外的名字是 ToolNotFound；参数多字段/缺字段/类型错/金额为 0 都要拦。"""
    from app.clock import ToolNotFound

    with pytest.raises(ToolNotFound):
        registry.get_tool("query_balance_v2")
    with pytest.raises(ValidationError):
        registry.execute("transfer", {"payee_id": "p_lij", "amount_fen": 1, "extra": 1}, ctx=ctx)
    with pytest.raises(ValidationError):
        registry.execute("transfer", {"payee_id": "p_lij"}, ctx=ctx)
    with pytest.raises(ValidationError):
        registry.execute("transfer", {"payee_id": "p_lij", "amount_fen": "3000"}, ctx=ctx)
    with pytest.raises(ValidationError):
        registry.execute("transfer", {"payee_id": "p_lij", "amount_fen": 0}, ctx=ctx)
    with pytest.raises(ValidationError):
        registry.execute("resolve_payee", {"name": 3}, ctx=ctx)


# ---------------------------------------------------------------- 只读工具

def test_query_balance_without_tail_lists_all_and_puts_primary_first(ctx: ToolContext) -> None:
    """正常分支：不传尾号回全部账户，主账户排第一，金额带分和人话两种写法。"""
    result = registry.execute("query_balance", {}, ctx=ctx)
    assert result.ok is True
    assert result.data["count"] == 2
    assert result.data["primary_tail"] == "1234"
    assert [item["tail"] for item in result.data["accounts"]] == ["1234", "5678"]
    assert result.data["accounts"][0]["balance_fen"] == DEBIT_BALANCE
    assert result.data["accounts"][0]["balance_yuan"] == "12,860.50"


def test_query_balance_by_tail_and_missing_tail(ctx: ToolContext) -> None:
    """传尾号只回一张卡；尾号不存在是 ACCOUNT_NOT_FOUND，不是异常。"""
    hit = registry.execute("query_balance", {"account_tail": "5678"}, ctx=ctx)
    assert hit.ok is True
    assert hit.data["count"] == 1
    assert hit.data["accounts"][0]["type"] == "credit"
    miss = registry.execute("query_balance", {"account_tail": "9999"}, ctx=ctx)
    assert miss.ok is False
    assert miss.error_code == "ACCOUNT_NOT_FOUND"
    assert miss.error_text


def test_query_transactions_normal_and_limit(ctx: ToolContext) -> None:
    """正常分支：默认回看 30 天，条数受 limit 控制，倒序返回。"""
    result = registry.execute("query_transactions", {}, ctx=ctx)
    assert result.ok is True
    assert result.data["days"] == 30
    assert result.data["count"] == 16
    assert "empty" not in result.data
    limited = registry.execute("query_transactions", {"limit": 3}, ctx=ctx)
    assert limited.data["count"] == 3
    stamps = [item["ts_ms"] for item in limited.data["items"]]
    assert stamps == sorted(stamps, reverse=True)


def test_query_transactions_empty_result_is_not_an_error(ctx: ToolContext) -> None:
    """查不到就是查不到：ok 仍然为 True，靠 data.empty 表达，别抛异常。"""
    result = registry.execute("query_transactions", {"category": "工资"}, ctx=ctx)
    assert result.ok is True
    assert result.error_code is None
    assert result.data["empty"] is True
    assert result.data["items"] == []


def test_query_transactions_rejects_unknown_category(ctx: ToolContext) -> None:
    """分类不在闭集里，回 BAD_CATEGORY 并把合法分类一并带回去。"""
    result = registry.execute("query_transactions", {"category": "购物车"}, ctx=ctx)
    assert result.ok is False
    assert result.error_code == "BAD_CATEGORY"
    assert "订阅" in result.data["valid_categories"]


def test_query_transactions_truncates_long_window(ctx: ToolContext) -> None:
    """要查超过一年就按 365 天截断，并且明说被截断了。"""
    result = registry.execute("query_transactions", {"days": 400}, ctx=ctx)
    assert result.ok is True
    assert result.data["days"] == 365
    assert result.data["truncated"] is True


def test_query_transactions_filters_by_amount_and_category(ctx: ToolContext) -> None:
    """金额区间与分类过滤要能叠加着用。"""
    big = registry.execute("query_transactions", {"min_fen": 100_000}, ctx=ctx)
    assert big.ok is True
    assert all(item["amount_fen"] >= 100_000 for item in big.data["items"])
    subs = registry.execute("query_transactions", {"category": "订阅", "days": 200}, ctx=ctx)
    assert subs.data["count"] == 7


def test_analyze_bill_returns_fixed_shape_and_sums(ctx: ToolContext) -> None:
    """正常分支：结构固定，合计与分类占比算得对。"""
    result = registry.execute(
        "analyze_bill", {"period": "month", "year": 2026, "month": 10}, ctx=ctx
    )
    assert result.ok is True
    data = result.data
    assert {
        "period", "year", "month", "range", "account_tail",
        "total_out_fen", "total_in_fen", "total_out_yuan", "total_in_yuan",
        "out_count", "in_count", "by_category", "top_merchants", "anomalies", "summary",
    } <= set(data)
    assert data["total_out_fen"] == 1_757_150
    assert data["total_in_fen"] == 0
    assert [item["category"] for item in data["by_category"]] == ["购物", "餐饮", "订阅"]
    assert data["by_category"][0]["amount_fen"] == 1_145_700
    assert data["by_category"][0]["pct"] == 65.2
    assert data["out_count"] == 16
    assert sum(item["amount_fen"] for item in data["by_category"]) == data["total_out_fen"]


def test_analyze_bill_detects_all_three_anomalies(ctx: ToolContext) -> None:
    """三条规则同时命中：拆单、凌晨境外、订阅涨价。"""
    data = registry.execute(
        "analyze_bill", {"period": "month", "year": 2026, "month": 10}, ctx=ctx
    ).data
    kinds = [item["kind"] for item in data["anomalies"]]
    assert kinds == ["split_orders", "odd_hour_overseas", "subscription_hike"]
    split, night, hike = data["anomalies"]
    assert set(split) == {"kind", "title", "detail", "amount_fen"}
    assert split["amount_fen"] == 299_700
    assert "优衣库旗舰店" in split["detail"]
    assert night["amount_fen"] == 288_000
    assert "香港" in night["detail"]
    assert hike["amount_fen"] == 1_000


def test_analyze_bill_ignores_near_miss_samples(ctx: ToolContext) -> None:
    """反例不该命中：金额不到 500 元、跨了 60 分钟、白天境外、深夜境内、
    境外小额、订阅涨幅不到 30%、订阅跳月。"""
    data = registry.execute(
        "analyze_bill", {"period": "month", "year": 2026, "month": 10}, ctx=ctx
    ).data
    assert [item["kind"] for item in data["anomalies"]].count("split_orders") == 1
    assert [item["kind"] for item in data["anomalies"]].count("odd_hour_overseas") == 1
    assert [item["kind"] for item in data["anomalies"]].count("subscription_hike") == 1
    details = " ".join(item["detail"] for item in data["anomalies"])
    for never in ("楼下小卖部", "星巴克", "麦当劳", "百度网盘", "京东商城", "巴黎"):
        assert never not in details


def test_analyze_bill_empty_month_is_ok_with_zeros(ctx: ToolContext) -> None:
    """没有流水的月份照样返回固定结构，金额全 0 并给一句人话。"""
    data = registry.execute(
        "analyze_bill", {"period": "month", "year": 2026, "month": 1}, ctx=ctx
    ).data
    assert data["total_out_fen"] == 0
    assert data["by_category"] == []
    assert data["top_merchants"] == []
    assert data["anomalies"] == []
    assert data["summary"]


def test_analyze_bill_tie_order_is_reproducible(ctx: ToolContext) -> None:
    """并列排序：金额降序，金额相同按分类名字典序，且重复调用结果一致。"""
    first = registry.execute(
        "analyze_bill", {"period": "month", "year": 2026, "month": 3}, ctx=ctx
    ).data
    second = registry.execute(
        "analyze_bill", {"period": "month", "year": 2026, "month": 3}, ctx=ctx
    ).data
    assert [item["amount_fen"] for item in first["by_category"]] == [20_000, 20_000]
    assert [item["category"] for item in first["by_category"]] == ["娱乐", "餐饮"]
    assert first["by_category"] == second["by_category"]
    assert first["top_merchants"] == second["top_merchants"]


def test_analyze_bill_year_period_and_missing_month(ctx: ToolContext) -> None:
    """按年分析不需要 month；按月分析不给 month 是参数错误。"""
    year = registry.execute("analyze_bill", {"period": "year", "year": 2026}, ctx=ctx)
    assert year.ok is True
    assert year.data["month"] is None
    assert year.data["total_out_fen"] > 0
    with pytest.raises(ValidationError):
        registry.execute("analyze_bill", {"period": "month", "year": 2026}, ctx=ctx)


def test_resolve_payee_disambiguates_duplicates_by_card_tail(ctx: ToolContext) -> None:
    """同名收款人必须带卡尾号；备注名和模糊名也要能查到；查不到回空集合。"""
    dup = registry.execute("resolve_payee", {"name": "张三"}, ctx=ctx)
    assert dup.ok is True
    assert dup.data["count"] == 2
    assert [item["card_tail"] for item in dup.data["candidates"]] == ["4417", "6620"]
    assert all(item["card_tail"] for item in dup.data["candidates"])
    alias = registry.execute("resolve_payee", {"name": "房东"}, ctx=ctx)
    assert alias.data["candidates"][0]["payee_id"] == PAYEE_LIJING
    fuzzy = registry.execute("resolve_payee", {"name": "王"}, ctx=ctx)
    assert fuzzy.data["candidates"][0]["name"] == "王强"
    none = registry.execute("resolve_payee", {"name": "查无此人"}, ctx=ctx)
    assert none.ok is True
    assert none.data["candidates"] == []


def test_list_subs_only_active_by_default(ctx: ToolContext) -> None:
    """默认只列生效中的；include_cancelled=True 才带上已取消的。"""
    active = registry.execute("list_subs", {}, ctx=ctx)
    assert active.ok is True
    assert active.data["count"] == 1
    assert active.data["subs"][0]["sub_id"] == SUB_ACTIVE
    assert active.data["monthly_total_fen"] == 2_500
    both = registry.execute("list_subs", {"include_cancelled": True}, ctx=ctx)
    assert both.data["count"] == 2
    assert both.data["monthly_total_fen"] == 2_500


def test_list_cards_by_account_and_missing_tail(ctx: ToolContext) -> None:
    """按账户过滤；尾号不存在是 ACCOUNT_NOT_FOUND。"""
    all_cards = registry.execute("list_cards", {}, ctx=ctx)
    assert all_cards.ok is True
    assert [card["kind"] for card in all_cards.data["cards"]] == ["physical"]
    credit = registry.execute("list_cards", {"account_tail": "5678"}, ctx=ctx)
    assert credit.data["cards"][0]["kind"] == "virtual"
    assert credit.data["cards"][0]["per_tx_limit_fen"] == 200_000
    miss = registry.execute("list_cards", {"account_tail": "9999"}, ctx=ctx)
    assert miss.ok is False
    assert miss.error_code == "ACCOUNT_NOT_FOUND"


def _params(tool: str, **extra: Any) -> dict[str, Any]:
    """按 MINIMAL_PARAMS 起手再补参数，保证和签令牌时用的参数一字不差。"""
    return {**MINIMAL_PARAMS[tool], **extra}


def test_transfer_holds_money_without_deducting(ctx: ToolContext) -> None:
    """正常分支：只建预授权，钱还没扣，走的是 funds 的两阶段资金。"""
    params = _params("transfer", payee_id=PAYEE_LIJING, amount_fen=10_000, note="十月房租")
    result = registry.execute("transfer", params, token=_token("transfer", params), ctx=ctx)
    assert result.ok is True
    assert result.data["status"] == "held"
    assert result.data["hold_id"]
    assert result.data["amount_fen"] == 10_000
    assert _balance(ctx.conn) == DEBIT_BALANCE
    hold = funds.fetch_hold(ctx.conn, result.data["hold_id"])
    assert hold is not None
    assert hold.status == "held"
    assert hold.expires_ms == FROZEN_MS + funds.HOLD_TTL_MS


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


def test_list_subs_rejects_unknown_account(ctx: ToolContext) -> None:
    """错误分支：工具层不做业务判断，但账户不存在时只能报 ValidationError。"""
    ghost = replace(ctx, account_id="a_ghost")
    with pytest.raises(ValidationError):
        registry.execute("list_subs", {}, ctx=ghost)


def test_list_cards_rejects_unknown_account(ctx: ToolContext) -> None:
    """错误分支：账户不存在时 list_cards 同样只能抛 ValidationError。"""
    ghost = replace(ctx, account_id="a_ghost")
    with pytest.raises(ValidationError):
        registry.execute("list_cards", {}, ctx=ghost)


def test_resolve_payee_rejects_blank_name(ctx: ToolContext) -> None:
    """错误分支：空姓名在参数校验就被挡下，不会拿着空串去查库。"""
    with pytest.raises(ValidationError):
        registry.execute("resolve_payee", {"name": ""}, ctx=ctx)
