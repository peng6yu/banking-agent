"""理财只读工具：产品清单与风险评估（场景三的上半段）。

对应文档：docs/06 工具层与场景实现规格.md 6.5 场景三、docs/03 基金与持仓表。

单位约定（整数分/厘，避免浮点误差）：
- nav_micro：基金净值 × 1_000_000；
- shares_milli：持有份额 × 1_000；
- amount_fen（分）→ shares_milli = amount_fen × 10^7 // nav_micro；
- 市值（分）= shares_milli × nav_micro // 10_000_000。

这两个只读工具只查表、只算账，不碰余额也不碰持仓，更不判断该不该买：
风控结论由 policy 给（docs/06 6.7），这里只把事实和提示摆给用户看。
"""

from __future__ import annotations

import sqlite3
from typing import Any, Final

from app.clock import ValidationError
from app.models import ToolResult
from app.money import fen_to_yuan_str
from app.tools.base import ToolContext, fail_result, ok_result, primary_account

NAV_SCALE: Final[int] = 1_000_000
SHARE_SCALE: Final[int] = 1_000
SHARES_NUMERATOR: Final[int] = 10_000_000
MAX_LIMIT: Final[int] = 50

CATEGORY_LABELS: Final[dict[str, str]] = {
    "money": "货币型",
    "bond": "债券型",
    "mixed": "混合型",
    "equity": "股票型",
}
RISK_LABELS: Final[dict[int, str]] = {
    1: "R1 谨慎型",
    2: "R2 稳健型",
    3: "R3 平衡型",
    4: "R4 进取型",
    5: "R5 激进型",
}
# 低风险产品的单笔提示线（分）：R1/R2 买 5 万以上、R3 及以上买 10 万以上，
# 只是在评估里提醒一句「金额相对这个风险等级偏大」，不是拦截。
CONCENTRATION_FEN: Final[dict[int, int]] = {1: 500_000, 2: 500_000, 3: 1_000_000, 4: 2_000_000, 5: 2_000_000}


def shares_for(amount_fen: int, nav_micro: int) -> int:
    """这笔钱能买到多少份（单位 0.001 份，向下取整，宁可少算不买多）。"""
    if nav_micro <= 0:
        raise ValidationError("基金净值异常，请稍后再看", nav_micro=nav_micro)
    return amount_fen * SHARES_NUMERATOR // nav_micro


def market_value_fen(shares_milli: int, nav_micro: int) -> int:
    """这些份额现在值多少钱（分）。"""
    return shares_milli * nav_micro // SHARES_NUMERATOR


def fetch_product(conn: sqlite3.Connection, code: str) -> sqlite3.Row | None:
    row = conn.execute("SELECT * FROM fund_products WHERE code = ?", (str(code),)).fetchone()
    return row


def holding_of(conn: sqlite3.Connection, user_id: str, code: str) -> sqlite3.Row | None:
    row = conn.execute(
        "SELECT * FROM fund_holdings WHERE user_id = ? AND product_code = ?",
        (str(user_id), str(code)),
    ).fetchone()
    return row


def risk_label(risk_level: int) -> str:
    return RISK_LABELS.get(int(risk_level), f"R{int(risk_level)}")


def list_products(
    ctx: ToolContext, params: dict[str, Any], token: str | None = None
) -> ToolResult:
    """基金产品清单：可按风险等级和类型筛选，顺便带上「我持有哪些」。"""
    where = ["1 = 1"]
    args: list[Any] = []
    risk = params.get("risk_level")
    if risk is not None:
        where.append("risk_level = ?")
        args.append(int(risk))
    category = str(params.get("category") or "")
    if category:
        if category not in CATEGORY_LABELS:
            return fail_result(
                "BAD_CATEGORY",
                f"没有 {category} 这一类产品。",
                {"allowed_categories": sorted(CATEGORY_LABELS)},
            )
        where.append("category = ?")
        args.append(category)
    rows = ctx.conn.execute(
        "SELECT * FROM fund_products WHERE " + " AND ".join(where) + " ORDER BY risk_level, code LIMIT ?",
        (*args, MAX_LIMIT),
    ).fetchall()
    account = primary_account(ctx.conn, ctx.account_id)
    held = _holdings(ctx.conn, str(account["user_id"]))
    products = [_product_view(row, held.get(str(row["code"]))) for row in rows]
    return ok_result(
        {
            "products": products,
            "count": len(products),
            "empty": not products,
            "held_count": sum(1 for item in products if item["holding_shares_milli"] > 0),
            "summary": f"共 {len(products)} 只在售基金，其中你持有 {sum(1 for item in products if item['holding_shares_milli'] > 0)} 只。",
        }
    )


def estimate_risk(
    ctx: ToolContext, params: dict[str, Any], token: str | None = None
) -> ToolResult:
    """买之前算一笔：这只基金多险、这笔钱买得到多少、能买到多少份。"""
    code = str(params.get("product_code") or "")
    product = fetch_product(ctx.conn, code)
    if product is None:
        return fail_result("PRODUCT_NOT_FOUND", f"没有找到这只基金（{code}）。")
    amount_fen = int(params.get("amount_fen") or 0)
    if amount_fen <= 0:
        return fail_result("BAD_AMOUNT", "请告诉我你想投多少钱，金额要大于 0。")
    nav_micro = int(product["nav_micro"])
    min_buy_fen = int(product["min_buy_fen"])
    level = int(product["risk_level"])
    account = primary_account(ctx.conn, ctx.account_id)
    held = holding_of(ctx.conn, str(account["user_id"]), code)
    shares_milli = shares_for(amount_fen, nav_micro)
    warnings = _warnings(level, amount_fen, min_buy_fen, shares_milli)
    return ok_result(
        {
            "product_code": str(product["code"]),
            "product_name": str(product["name"]),
            "category": str(product["category"]),
            "category_label": CATEGORY_LABELS.get(str(product["category"]), str(product["category"])),
            "risk_level": level,
            "risk_label": risk_label(level),
            "amount_fen": amount_fen,
            "amount_yuan": fen_to_yuan_str(amount_fen),
            "min_buy_fen": min_buy_fen,
            "min_buy_yuan": fen_to_yuan_str(min_buy_fen),
            "meets_minimum": amount_fen >= min_buy_fen,
            "nav_micro": nav_micro,
            "nav_text": f"{nav_micro / NAV_SCALE:.4f}",
            "shares_milli": shares_milli,
            "shares_text": f"{shares_milli / SHARE_SCALE:.3f}",
            "market_value_fen": market_value_fen(shares_milli, nav_micro),
            "market_value_yuan": fen_to_yuan_str(market_value_fen(shares_milli, nav_micro)),
            "holding_shares_milli": int(held["shares_milli"]) if held else 0,
            "holding_market_value_fen": market_value_fen(int(held["shares_milli"]), nav_micro) if held else 0,
            "warnings": warnings,
            "advice": _advice(level, amount_fen, min_buy_fen),
            "summary": f"{product['name']}（{risk_label(level)}），投 {fen_to_yuan_str(amount_fen)} "
            f"大约买到 {shares_milli / SHARE_SCALE:.3f} 份。",
        }
    )


def _warnings(level: int, amount_fen: int, min_buy_fen: int, shares_milli: int) -> list[str]:
    tips: list[str] = []
    if amount_fen < min_buy_fen:
        tips.append(f"低于起购金额 {fen_to_yuan_str(min_buy_fen)}，这笔钱买不了这只基金。")
    if amount_fen > CONCENTRATION_FEN.get(level, 1_000_000):
        tips.append(f"{risk_label(level)}产品单笔投 {fen_to_yuan_str(amount_fen)} 偏集中，可以考虑分批。")
    if shares_milli <= 0:
        tips.append("这笔钱按当前净值买不到整 0.001 份，请加一点金额。")
    if level >= 4:
        tips.append("高风险等级产品净值波动大，短期亏损是可能的。")
    return tips


def _advice(level: int, amount_fen: int, min_buy_fen: int) -> str:
    if amount_fen < min_buy_fen:
        return f"这只基金起购 {fen_to_yuan_str(min_buy_fen)}，可以换个产品或者补到起购线以上。"
    if level <= 2:
        return "属于低波动的稳健品种，适合放一部分闲钱；别影响日常周转。"
    if level == 3:
        return "攻守兼备，短期会有波动，建议半年以上不用的钱再放进来。"
    return "高风险高波动，只用亏得起的钱参与。"


def _holdings(conn: sqlite3.Connection, user_id: str) -> dict[str, sqlite3.Row]:
    rows = conn.execute("SELECT * FROM fund_holdings WHERE user_id = ?", (user_id,)).fetchall()
    return {str(row["product_code"]): row for row in rows}


def _product_view(row: sqlite3.Row, held: sqlite3.Row | None) -> dict[str, Any]:
    """产品 + 我的持仓，合成一条返回给模型看的视图。"""
    code = str(row["code"])
    nav_micro = int(row["nav_micro"])
    shares = int(held["shares_milli"]) if held else 0
    value_fen = market_value_fen(shares, nav_micro)
    return {
        "code": code,
        "name": str(row["name"]),
        "category": str(row["category"]),
        "category_label": CATEGORY_LABELS.get(str(row["category"]), str(row["category"])),
        "risk_level": int(row["risk_level"]),
        "risk_label": risk_label(int(row["risk_level"])),
        "min_buy_fen": int(row["min_buy_fen"]),
        "min_buy_yuan": fen_to_yuan_str(int(row["min_buy_fen"])),
        "nav_micro": nav_micro,
        "nav_text": f"{nav_micro / NAV_SCALE:.4f}",
        "nav_updated_ms": int(row["nav_updated_ms"]),
        "description": str(row["description"]),
        "holding_shares_milli": shares,
        "holding_shares_text": f"{shares / SHARE_SCALE:.3f}",
        "holding_cost_fen": int(held["cost_fen"]) if held else 0,
        "holding_market_value_fen": value_fen,
        "holding_market_value_yuan": fen_to_yuan_str(value_fen),
    }
