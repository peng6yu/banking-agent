"""理财四工具的分支表测试：产品清单、风险评估、申购、赎回（docs/06 6.5 场景三）。

这个文件守住三条底线：
1. 只读两工具（list_products / estimate_risk）只看不碰，一分钱都不许动；
2. 写两工具（buy_fund / redeem_fund）与 transfer 完全同构 —— 没有令牌一律拒绝，
   有了令牌才走两阶段资金，余额只经 app/funds 变动；
3. 申购要有份额、赎回不能赎出负持仓，每一步都留在审计链上。
"""

from __future__ import annotations

import sqlite3
from typing import Any

import pytest

from app import audit
from app.clock import MissingCapability
from app.models import Action
from app.policy.engine import decide
from app.policy.rules import PolicyContext
from app.tools import registry
from app.tools.base import ToolContext
from tests.tool_fixtures import (
    DEBIT_BALANCE,
    DEBIT_ID,
    USER_ID,
    _balance,
    _token,
    bank,
    ctx,
)

__all__ = ["bank", "ctx"]

MONEY_CODE = "F00001"  # 现金添利货币A：R1，起购 1 元
BOND_CODE = "F00002"  # 稳盈债券A：R2，起购 100 元，存量 284548 milli 份
MIXED_CODE = "F00003"  # 均衡配置混合A：R3，起购 100 元
EQUITY_CODE = "F00004"  # 沪深精选指数A：R4，起购 500 元
GLOBAL_CODE = "F00005"  # 全球科技QDII：R5，起购 1000 元
FUND_TOOLS = ("list_products", "estimate_risk", "buy_fund", "redeem_fund")


def _tx_count(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT COUNT(*) AS n FROM transactions").fetchone()["n"])


def _hold_count(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT COUNT(*) AS n FROM holds").fetchone()["n"])


def _holding(conn: sqlite3.Connection, code: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM fund_holdings WHERE user_id = ? AND product_code = ?",
        (USER_ID, code),
    ).fetchone()


def _audit_types(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute("SELECT event_type FROM audit_log ORDER BY seq").fetchall()
    return [str(row["event_type"]) for row in rows]


def _run(tool: str, params: dict[str, Any], tool_ctx: ToolContext, token: str | None = None):
    return registry.execute(tool, params, token=token, ctx=tool_ctx)


def test_the_four_fund_tools_are_registered_and_callable() -> None:
    """四个工具都在注册表里，名字与写属性都对，参数表不许塞多余字段。"""
    for name in FUND_TOOLS:
        assert name in registry.TOOL_NAMES
        spec = registry.get_tool(name)
        assert spec.name == name
        assert spec.params["additionalProperties"] is False
    assert registry.get_tool("list_products").writes is False
    assert registry.get_tool("estimate_risk").writes is False
    assert registry.get_tool("buy_fund").writes is True
    assert registry.get_tool("redeem_fund").writes is True
    assert {item["name"] for item in registry.tool_schemas()} >= set(FUND_TOOLS)


def test_list_products_lists_every_product_with_risk_and_nav(ctx: ToolContext) -> None:
    """产品清单要带风险等级、起购金额和净值，同时一分钱都不许动。"""
    before_tx = _tx_count(ctx.conn)
    result = _run("list_products", {}, ctx)
    assert result.ok is True
    assert result.data["count"] == 5
    assert result.data["empty"] is False
    for item in result.data["products"]:
        assert item["risk_level"] in (1, 2, 3, 4, 5)
        assert item["risk_label"].startswith("R")
        assert item["nav_micro"] > 0
        assert item["min_buy_fen"] > 0
    assert _balance(ctx.conn) == DEBIT_BALANCE
    assert _tx_count(ctx.conn) == before_tx


def test_list_products_filters_by_risk_level_and_category(ctx: ToolContext) -> None:
    """按风险等级和类型筛选都生效。"""
    low = _run("list_products", {"risk_level": 1}, ctx)
    assert low.ok is True
    assert [item["code"] for item in low.data["products"]] == [MONEY_CODE]
    equity = _run("list_products", {"category": "equity"}, ctx)
    assert equity.ok is True
    assert {item["code"] for item in equity.data["products"]} == {EQUITY_CODE, GLOBAL_CODE}
    assert all(item["category"] == "equity" for item in equity.data["products"])


def test_list_products_rejects_an_unknown_category(ctx: ToolContext) -> None:
    """类型不存在要明确报错，不能悄悄返回空清单。"""
    result = _run("list_products", {"category": "gold"}, ctx)
    assert result.ok is False
    assert result.error_code == "BAD_CATEGORY"
    assert "money" in result.data["allowed_categories"]


def test_estimate_risk_advises_without_touching_money(ctx: ToolContext) -> None:
    """风险评估只出结论：等级、起购线、能买多少份、有什么提示，一分钱都不动。"""
    before_tx = _tx_count(ctx.conn)
    result = _run("estimate_risk", {"product_code": GLOBAL_CODE, "amount_fen": 200_000}, ctx)
    assert result.ok is True
    assert result.data["risk_level"] == 5
    assert result.data["risk_label"].startswith("R5")
    assert result.data["meets_minimum"] is True
    assert result.data["shares_milli"] > 0
    assert result.data["advice"]
    assert result.data["warnings"]
    assert _balance(ctx.conn) == DEBIT_BALANCE
    assert _tx_count(ctx.conn) == before_tx
    assert _hold_count(ctx.conn) == 0


def test_estimate_risk_flags_below_minimum_and_unknown_product(ctx: ToolContext) -> None:
    """低于起购线要提示；不存在的基金码要报 PRODUCT_NOT_FOUND。"""
    low = _run("estimate_risk", {"product_code": EQUITY_CODE, "amount_fen": 1_000}, ctx)
    assert low.ok is True
    assert low.data["meets_minimum"] is False
    assert any("起购" in tip for tip in low.data["warnings"])
    missing = _run("estimate_risk", {"product_code": "F99999", "amount_fen": 1_000}, ctx)
    assert missing.ok is False
    assert missing.error_code == "PRODUCT_NOT_FOUND"


def test_buy_fund_below_the_minimum_is_refused_and_changes_nothing(ctx: ToolContext) -> None:
    """低于起购门槛：一律拒绝，且不建 hold、不扣钱、不留持仓。"""
    before_tx = _tx_count(ctx.conn)
    before_hold = _hold_count(ctx.conn)
    params = {"product_code": EQUITY_CODE, "amount_fen": 20_000}
    result = _run("buy_fund", params, ctx, _token("buy_fund", params))
    assert result.ok is False
    assert result.error_code == "BELOW_MIN_BUY"
    assert result.data["min_buy_fen"] == 50_000
    assert result.data["gap_fen"] == 30_000
    assert _balance(ctx.conn) == DEBIT_BALANCE
    assert _tx_count(ctx.conn) == before_tx
    assert _hold_count(ctx.conn) == before_hold
    assert _holding(ctx.conn, EQUITY_CODE) is None


def test_buy_fund_creates_shares_and_keeps_the_audit_chain(ctx: ToolContext) -> None:
    """正常申购：扣钱、记份额、留下审计链。"""
    params = {"product_code": MONEY_CODE, "amount_fen": 20_000}
    result = _run("buy_fund", params, ctx, _token("buy_fund", params))
    assert result.ok is True
    assert result.data["status"] == "committed"
    assert result.data["shares_milli"] > 0
    assert result.data["balance_after_fen"] == DEBIT_BALANCE - 20_000
    assert _balance(ctx.conn) == DEBIT_BALANCE - 20_000
    holding = _holding(ctx.conn, MONEY_CODE)
    assert holding is not None
    assert int(holding["shares_milli"]) == result.data["shares_milli"]
    assert int(holding["cost_fen"]) == 20_000
    assert result.data["holding_market_value_fen"] > 0
    types = _audit_types(ctx.conn)
    assert "transfer.initiated" in types
    assert "transfer.committed" in types
    assert audit.verify_chain(ctx.conn) is True


def test_redeem_fund_beyond_the_holding_is_refused(ctx: ToolContext) -> None:
    """赎得比持有多：拒绝，且库里不许出现负份额。"""
    before_tx = _tx_count(ctx.conn)
    params = {"product_code": BOND_CODE, "amount_fen": 50_000}
    result = _run("redeem_fund", params, ctx, _token("redeem_fund", params))
    assert result.ok is False
    assert result.error_code == "REDEEM_EXCEEDS_HOLDING"
    assert result.data["held_shares_milli"] == 284_548
    assert result.data["requested_shares_milli"] > result.data["held_shares_milli"]
    holding = _holding(ctx.conn, BOND_CODE)
    assert int(holding["shares_milli"]) == 284_548
    assert _balance(ctx.conn) == DEBIT_BALANCE
    assert _tx_count(ctx.conn) == before_tx
    rows = ctx.conn.execute("SELECT shares_milli FROM fund_holdings").fetchall()
    assert all(int(row["shares_milli"]) > 0 for row in rows)


def test_redeem_fund_returns_money_to_the_card(ctx: ToolContext) -> None:
    """正常赎回：钱回卡、份额减少、成本按比例留。"""
    params = {"product_code": BOND_CODE, "amount_fen": 10_000}
    result = _run("redeem_fund", params, ctx, _token("redeem_fund", params))
    assert result.ok is True
    assert result.data["status"] == "committed"
    assert result.data["balance_after_fen"] == DEBIT_BALANCE + 10_000
    assert _balance(ctx.conn) == DEBIT_BALANCE + 10_000
    holding = _holding(ctx.conn, BOND_CODE)
    assert int(holding["shares_milli"]) == 284_548 - 94_849
    assert int(holding["cost_fen"]) < 30_000
    assert "transfer.committed" in _audit_types(ctx.conn)
    assert audit.verify_chain(ctx.conn) is True


@pytest.mark.parametrize(
    "tool,params",
    [
        ("buy_fund", {"product_code": MIXED_CODE, "amount_fen": 20_000}),
        ("redeem_fund", {"product_code": BOND_CODE, "amount_fen": 10_000}),
    ],
)
def test_fund_write_tools_refuse_without_a_capability_token(
    ctx: ToolContext, tool: str, params: dict[str, Any]
) -> None:
    """没有令牌：当场 MissingCapability，余额与持仓都不许动。"""
    before_tx = _tx_count(ctx.conn)
    before_hold = _hold_count(ctx.conn)
    with pytest.raises(MissingCapability):
        _run(tool, params, ctx)
    assert _balance(ctx.conn) == DEBIT_BALANCE
    assert _tx_count(ctx.conn) == before_tx
    assert _hold_count(ctx.conn) == before_hold
    assert int(_holding(ctx.conn, BOND_CODE)["shares_milli"]) == 284_548


@pytest.mark.parametrize("tool", ["buy_fund", "redeem_fund"])
def test_policy_keeps_fund_operations_red_with_face_and_otp(tool: str) -> None:
    """理财是红灯：规则 R10，验证因子必须同时含人脸和动态口令。"""
    action = Action(
        tool=tool,
        params={"product_code": MIXED_CODE, "amount_fen": 20_000},
        account_id=DEBIT_ID,
        amount_fen=20_000,
    )
    decision = decide(action, PolicyContext())
    assert decision.level == "red"
    assert decision.rule_id == "R10_invest_red"
    assert set(decision.required_factors) == {"face", "otp"}


def test_the_fund_seed_data_is_idempotent(conn: sqlite3.Connection) -> None:
    """种子数据可重复执行：基金五只、既有持仓两条，第二次跑不再写。"""
    from scripts.seed import seed

    assert seed(conn) is True
    products = int(conn.execute("SELECT COUNT(*) AS n FROM fund_products").fetchone()["n"])
    holdings = int(conn.execute("SELECT COUNT(*) AS n FROM fund_holdings").fetchone()["n"])
    assert products == 5
    assert holdings == 2
    assert seed(conn) is False
    assert int(conn.execute("SELECT COUNT(*) AS n FROM fund_products").fetchone()["n"]) == products
    assert int(conn.execute("SELECT COUNT(*) AS n FROM fund_holdings").fetchone()["n"]) == holdings
