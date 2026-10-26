"""注册表与只读工具的分支表测试（docs/06 6.1/6.2）。

只读工具可以被安全地直接调用：没有令牌、没有业务判断、没有写库路径；analyze_bill
的三条异常规则命中与不命中、并列排序的可复现性也在这里验证。
写工具的分支表在 tests/test_tools_write.py。
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from app.clock import ValidationError
from app.tools import registry
from app.tools.base import ToolContext
from app.tools.registry import TOOL_NAMES
from tests.tool_fixtures import (
    DEBIT_BALANCE,
    PAYEE_LIJING,
    SUB_ACTIVE,
    _ms,
    _tx,
    bank,
    ctx,
)

__all__ = ["bank", "ctx"]

# ---------------------------------------------------------------- 注册表本身

#: docs/06 6.1 权威表里的十个基础工具，名字与顺序都不能改
DOCS_06_TOOL_NAMES = (
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
#: 场景三理财新增的四个工具，追加在基础表之后
FUND_TOOL_NAMES = ("list_products", "estimate_risk", "buy_fund", "redeem_fund")
#: 场景四卡片管理新增的五个写工具，再追加在理财之后
#: 场景一补齐：定时转账三件套与 AA 拆分
SCHEDULE_TOOL_NAMES = ("schedule_transfer", "list_scheduled", "cancel_scheduled")
SPLIT_TOOL_NAMES = ("split_transfer",)
#: 场景四补齐：销户与改卡密码
CARD_CLOSE_TOOL_NAMES = ("close_card", "change_card_pin")
#: 回退冲正
REVERSAL_TOOL_NAMES = ("reverse_transfer",)
#: 阶段 5 新增，追加到权威表尾部：储蓄目标/计划/提醒三只读 + 四个黄灯写工具
PROACTIVE_TOOL_NAMES = (
    "list_savings_goals",
    "compute_savings_plan",
    "list_proactive_alerts",
    "create_savings_goal",
    "update_savings_goal",
    "resolve_proactive_alert",
    "run_proactive_monitor",
)
#: 阶段 6 投资者适当性：查画像（只读）+ 提交问卷（黄灯写，不动钱但要令牌）
SUITABILITY_TOOL_NAMES = ("get_suitability", "assess_suitability")
CARD_TOOL_NAMES = (
    "apply_virtual_card",
    "set_card_limit",
    "freeze_card",
    "unfreeze_card",
    "report_card_loss",
)


def test_tool_names_match_authoritative_table() -> None:
    """docs/06 6.1 的权威表：名字与顺序都不能改；理财四件套与卡片五件套依次跟在后面。"""
    assert TOOL_NAMES == (
        DOCS_06_TOOL_NAMES
        + FUND_TOOL_NAMES
        + CARD_TOOL_NAMES
        + SCHEDULE_TOOL_NAMES
        + SPLIT_TOOL_NAMES
        + CARD_CLOSE_TOOL_NAMES
        + REVERSAL_TOOL_NAMES
        + PROACTIVE_TOOL_NAMES
        + SUITABILITY_TOOL_NAMES
    )
    assert set(TOOL_NAMES) == set(registry.TOOLS)
    assert len(registry.tool_schemas()) == len(TOOL_NAMES)


def test_registry_flags_and_schemas_are_consistent() -> None:
    """每个 params 都 additionalProperties=False；金额参数必须是带下限的整数。"""
    for spec in registry.TOOL_SPECS:
        assert spec.params["additionalProperties"] is False
        assert spec.params["type"] == "object"
        for key, prop in spec.params["properties"].items():
            if key.endswith("_fen"):
                assert prop["type"] == "integer", f"{spec.name}.{key}"
                assert "minimum" in prop, f"{spec.name}.{key}"
    # 写标记只认权威表里的 writes 字段，不另维护一份名单
    assert registry.WRITE_TOOL_NAMES == tuple(
        spec.name for spec in registry.TOOL_SPECS if spec.writes
    )
    assert registry.READONLY_TOOL_NAMES == tuple(
        spec.name for spec in registry.TOOL_SPECS if not spec.writes
    )
    assert set(registry.READONLY_TOOL_NAMES) | set(registry.WRITE_TOOL_NAMES) == set(TOOL_NAMES)


def test_readonly_and_write_split_matches_policy_rules() -> None:
    """工具层的只读/写划分要和 policy 认的那份完全一致，否则安全网会漏。

    理财的只读两件套（list_products / estimate_risk）已登记进 policy 的
    READONLY_TOOLS：查理财列表走 R01 绿灯，不再被 R03 当成幻觉工具拦停。
    """
    from app.policy import rules as policy_rules

    policy_write = (
        policy_rules.SPEND_TOOLS
        | policy_rules.CONFIRM_ONLY_TOOLS
        | policy_rules.INVEST_TOOLS
        | policy_rules.SENSITIVE_TOOLS
    )
    # 工具层每个写工具都必须被策略层某个写集合认领，否则它就没有规则管。
    registered_write = set(policy_write) & set(registry.TOOL_NAMES)
    assert set(registry.WRITE_TOOL_NAMES) == registered_write
    # 策略层留名但工具层暂无 handler 的敏感工具（R09 会直接拒，走不到执行）：
    # 这份差额是显式已知的，新增未实现工具要同步改这里，不能悄悄变大。
    assert set(policy_write) - set(registry.TOOL_NAMES) == {
        "change_transfer_limit",
        "unbind_card",
        "update_contact",
        "reset_password",
    }
    assert set(policy_rules.READONLY_TOOLS) == set(registry.READONLY_TOOL_NAMES)
    assert set(registry.READONLY_TOOL_NAMES) - set(policy_rules.READONLY_TOOLS) == set()
    assert not set(registry.READONLY_TOOL_NAMES) & set(registry.WRITE_TOOL_NAMES)


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
        "spend_out_fen", "spend_in_fen", "spend_out_yuan", "spend_in_yuan",
        "net_fen", "net_yuan", "internal",
        "out_count", "in_count", "by_category", "top_merchants", "anomalies", "summary",
    } <= set(data)
    assert data["spend_out_fen"] == 1_757_150
    assert data["spend_in_fen"] == 0
    assert [item["category"] for item in data["by_category"]] == ["购物", "餐饮", "订阅"]
    assert data["by_category"][0]["amount_fen"] == 1_145_700
    assert data["by_category"][0]["pct"] == 65.2
    assert data["out_count"] == 16
    assert sum(item["amount_fen"] for item in data["by_category"]) == data["spend_out_fen"]
    # 内部搬运单列：这份账本没有账户互转/理财，所以 internal 是空的、两口径相等。
    assert data["internal"]["out_fen"] == 0
    assert data["total_out_fen"] == data["spend_out_fen"]


def test_a_transfer_out_counts_as_spending_but_moving_money_does_not(ctx: ToolContext, bank) -> None:
    """转给别人的钱是花掉了；在自己卡之间搬（账户互转/理财）不是花掉。

    同一个「支出」全站只认一个定义：钱有没有真的离开你的净资产。这条用例把它钉住，
    免得哪天有人把「转账」整类又算回内部搬运里（或者反过来把互转算成支出）。
    """
    before = registry.execute(
        "analyze_bill", {"period": "month", "year": 2026, "month": 10}, ctx=ctx
    ).data
    _tx(bank, "t_out_gift", _ms(2026, 10, 5, 10), 50_000, "张三", "转账")
    _tx(bank, "t_self_move", _ms(2026, 10, 5, 11), 70_000, "亲属卡互转", "账户互转")
    after = registry.execute(
        "analyze_bill", {"period": "month", "year": 2026, "month": 10}, ctx=ctx
    ).data
    assert after["spend_out_fen"] == before["spend_out_fen"] + 50_000
    assert after["internal"]["out_fen"] == 70_000
    assert after["total_out_fen"] == after["spend_out_fen"] + 70_000
    assert after["net_fen"] == before["net_fen"] - 50_000
    assert after["out_count"] == before["out_count"] + 1
    assert [item["category"] for item in after["by_category"] if item["category"] == "账户互转"] == []
    assert [item["category"] for item in after["by_category"] if item["category"] == "转账"] == ["转账"]


def test_analyze_bill_detects_all_three_anomalies(ctx: ToolContext) -> None:
    """三条规则同时命中：拆单、凌晨境外、订阅涨价。"""
    data = registry.execute(
        "analyze_bill", {"period": "month", "year": 2026, "month": 10}, ctx=ctx
    ).data
    kinds = [item["kind"] for item in data["anomalies"]]
    assert kinds == ["split_orders", "odd_hour_overseas", "subscription_hike"]
    split, night, hike = data["anomalies"]
    # 必需字段是集合下限（界面渲染依赖），新增证据字段不得改变这个契约
    assert {"kind", "title", "detail", "amount_fen"} <= set(split)
    # 证据字段：用户要能看到"到底是哪几笔流水"构成了这条异常
    assert split["merchant"] == "优衣库旗舰店"
    assert split["occurrence_count"] == len(split["tx_ids"]) >= 3
    assert split["window_end_ms"] - split["window_start_ms"] <= 60 * 60 * 1000
    assert night["occurrence_count"] == 1 and len(night["tx_ids"]) == 1
    assert night["window_start_ms"] == night["window_end_ms"]
    assert hike["comparison"]["hike_pct"] >= 30
    assert hike["window_start_ms"] is None and hike["window_end_ms"] is None
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
    assert data["spend_out_fen"] == 0
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
    assert year.data["spend_out_fen"] > 0
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


def test_resolve_payee_lists_both_same_name_candidates(ctx: ToolContext) -> None:
    """没说卡尾号时，两个同名「张三」（尾号 4417 / 6620）都要摆出来让用户选。"""
    dup = registry.execute("resolve_payee", {"name": "张三"}, ctx=ctx)
    assert dup.ok is True
    assert dup.data["count"] == 2
    assert [item["card_tail"] for item in dup.data["candidates"]] == ["4417", "6620"]


def test_resolve_payee_narrows_same_name_by_card_tail(ctx: ToolContext) -> None:
    """同名收款人靠卡尾号收敛：说了尾号就只留那一位，对不上就一个都不留。

    给了尾号就必须直接收敛成一个，不能在同名人之间反复反问；尾号对不上时
    一个都不留——用户点名的尾号必须真的对上，不能悄悄换成另一个同名的人。
    """
    dup = registry.execute("resolve_payee", {"name": "张三"}, ctx=ctx)
    assert dup.data["count"] == 2

    picked = registry.execute("resolve_payee", {"name": "张三", "card_tail": "4417"}, ctx=ctx)
    assert picked.ok is True
    assert picked.data["count"] == 1
    only = picked.data["candidates"][0]
    assert only["card_tail"] == "4417"
    assert only["payee_id"] == dup.data["candidates"][0]["payee_id"]
    assert only["payee_id"] != dup.data["candidates"][1]["payee_id"]

    other = registry.execute("resolve_payee", {"name": "张三", "card_tail": "6620"}, ctx=ctx)
    assert other.data["count"] == 1
    assert other.data["candidates"][0]["payee_id"] == dup.data["candidates"][1]["payee_id"]

    mismatch = registry.execute("resolve_payee", {"name": "张三", "card_tail": "9999"}, ctx=ctx)
    assert mismatch.ok is True
    assert mismatch.data["count"] == 0
    assert mismatch.data["candidates"] == []


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
    """不传尾号列名下所有卡；传了按账户过滤；尾号不存在是 ACCOUNT_NOT_FOUND。

    曾经不传尾号只看主账户（储蓄卡），用户问「我有几张卡」得到「1 张」而名下还有
    一张信用卡——回答本身没错，但不是用户问的那件事。
    """
    all_cards = registry.execute("list_cards", {}, ctx=ctx)
    assert all_cards.ok is True
    assert [card["kind"] for card in all_cards.data["cards"]] == ["physical", "virtual"]
    assert [card["account_tail"] for card in all_cards.data["cards"]] == ["1234", "5678"]
    assert all_cards.data["count"] == 2
    credit = registry.execute("list_cards", {"account_tail": "5678"}, ctx=ctx)
    assert credit.data["cards"][0]["kind"] == "virtual"
    assert credit.data["cards"][0]["per_tx_limit_fen"] == 200_000
    miss = registry.execute("list_cards", {"account_tail": "9999"}, ctx=ctx)
    assert miss.ok is False
    assert miss.error_code == "ACCOUNT_NOT_FOUND"


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
