"""理财种子：在售基金与既有持仓（场景三：产品推荐、风险评估、申购赎回）。

对应文档：docs/03_数据库与种子数据.md 3.3 的理财部分。

数字全部写死成字面量：净值用 nav_micro（百万分之一元），份额用 shares_milli（千分之一份），
这样任意机器跑出来逐字节一致；holder 只是演示用的既有持仓，申购/赎回会在这两张表上产生变化。
"""

from __future__ import annotations

import sqlite3
from typing import Any, Final

DAY_MS: Final[int] = 86_400_000

# (code, name, category, risk_level, min_buy_fen, nav_micro, description)
PRODUCTS: Final[tuple[tuple[str, str, str, int, int, int, str], ...]] = (
    ("F00001", "现金添利货币A", "money", 1, 100, 1_000_000, "随存随取，波动最小，适合放随时要用的钱。"),
    ("F00002", "稳盈债券A", "bond", 2, 10_000, 1_054_300, "以国债和政策性金融债为主，回撤小、收益稳。"),
    ("F00003", "均衡配置混合A", "mixed", 3, 10_000, 1_231_800, "股债搭配，攻守兼备，中等波动。"),
    ("F00004", "沪深精选指数A", "equity", 4, 50_000, 987_600, "跟踪沪深300，短期波动明显，可能亏损本金。"),
    ("F00005", "全球科技QDII", "equity", 5, 100_000, 2_417_500, "集中押注海外科技股，风险最高，只适合闲钱。"),
)

# (product_code, shares_milli, cost_fen)：既有持仓，份额按建仓时的净值折算
HOLDINGS: Final[tuple[tuple[str, int, int], ...]] = (
    ("F00002", 284_548, 30_000),
    ("F00003", 405_910, 50_000),
)


def _product_rows(now_ms_value: int) -> list[dict[str, Any]]:
    """把 PRODUCTS 摊成 fund_products 的插入行，净值更新时间统一钉在播种当天零点。"""
    nav_ms = now_ms_value - now_ms_value % DAY_MS
    return [
        {"code": code, "name": name, "category": category, "risk_level": risk,
         "min_buy_fen": min_buy, "nav_micro": nav, "nav_updated_ms": nav_ms,
         "description": description}
        for code, name, category, risk, min_buy, nav, description in PRODUCTS
    ]


def seed_funds(conn: sqlite3.Connection, user_id: str) -> tuple[int, int]:
    """幂等地写入在售基金与既有持仓；调用方负责开启事务。"""
    products = _product_rows(int(conn.execute("SELECT MAX(ts_ms) AS m FROM transactions")
                                .fetchone()["m"] or 0))
    for row in products:
        conn.execute(
            "INSERT OR IGNORE INTO fund_products"
            " (code, name, category, risk_level, min_buy_fen, nav_micro, nav_updated_ms,"
            "  description) VALUES (:code, :name, :category, :risk_level, :min_buy_fen,"
            "  :nav_micro, :nav_updated_ms, :description)",
            row,
        )
    for code, shares_milli, cost_fen in HOLDINGS:
        conn.execute(
            "INSERT OR IGNORE INTO fund_holdings (user_id, product_code, shares_milli, cost_fen)"
            " VALUES (?, ?, ?, ?)",
            (user_id, code, shares_milli, cost_fen),
        )
    return len(PRODUCTS), len(HOLDINGS)
