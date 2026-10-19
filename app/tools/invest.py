"""两个理财写工具：申购、赎回（场景三的下半段）。

对应文档：docs/06 工具层与场景实现规格.md 6.5 场景三、docs/05 资金两阶段协议。

与 transfer 完全同构的三条硬规矩：
1. handler 第一句就是 require_token，没有令牌当场 MissingCapability；
2. 余额只经 app/funds 动（申购走 initiate+commit 出账，赎回走 initiate+redeem 入账），
   工具层绝不直接 UPDATE accounts；
3. 工具不判断该不该买，红黄绿灯是人脸 + 动态口令的事，由 policy 判（docs/06 6.7）。

份额账本在 fund_holdings 表：申购加份额加成本，赎回减份额按比例减成本，
赎完最后一笔直接删行（表上有 CHECK(shares_milli > 0)，库里不允许 0 份僵尸行）。
"""

from __future__ import annotations

import sqlite3
from typing import Any, Final

# 模块级导入（不用 from 包 import 模块）：tests/test_imports.py 用 ast 按模块名登记依赖，
# 包级导入会被它漏掉，允许依赖表就会与代码现状对不上。
import app.db as db  # noqa: PLR0402
import app.funds as funds  # noqa: PLR0402
import app.tools.products as products  # noqa: PLR0402
from app.clock import BankError, InsufficientFunds
from app.log import log
from app.models import ToolResult
from app.money import fen_to_yuan_str
from app.tools.base import (
    ToolContext,
    build_action,
    ensure_system_payee,
    fail_result,
    ok_result,
    primary_account,
    require_token,
)

BOUGHT_TEXT: Final[str] = "申购成功，钱已经变成基金份额了。"
REDEEMED_TEXT: Final[str] = "赎回成功，钱已经回到你的卡里。"
UNSETTLED_TEXT: Final[str] = "钱已经锁定，但结算没成功，稍后我会重试。"


def buy_fund(ctx: ToolContext, params: dict[str, Any], token: str | None = None) -> ToolResult:
    """申购：一次调用把两阶段资金走完——建 hold 之后立刻 commit，然后记份额。"""
    require_token("buy_fund", params, token)
    account = primary_account(ctx.conn, ctx.account_id)
    product = products.fetch_product(ctx.conn, str(params.get("product_code") or ""))
    if product is None:
        return fail_result("PRODUCT_NOT_FOUND", f"没有找到这只基金（{params.get('product_code')}）。")
    amount_fen = int(params["amount_fen"])
    min_buy_fen = int(product["min_buy_fen"])
    if amount_fen < min_buy_fen:
        return fail_result(
            "BELOW_MIN_BUY",
            f"这只基金起购 {fen_to_yuan_str(min_buy_fen)}，还差一点。",
            {"min_buy_fen": min_buy_fen, "min_buy_yuan": fen_to_yuan_str(min_buy_fen),
             "gap_fen": min_buy_fen - amount_fen, "product_code": str(product["code"])},
        )
    nav_micro = int(product["nav_micro"])
    shares_milli = products.shares_for(amount_fen, nav_micro)
    if shares_milli <= 0:
        return fail_result("AMOUNT_TOO_SMALL", "这笔钱按当前净值还买不到 0.001 份，请再加一点。")
    ensure_system_payee(ctx.conn, account)
    action = build_action("buy_fund", params, str(account["id"]), amount_fen)
    try:
        hold = funds.initiate(
            ctx.conn, action, session_id=ctx.session_key(), node_id=ctx.node_key(),
            token=token, secret=ctx.secret, payee_id=funds.SYSTEM_PAYEE_ID,
        )
    except InsufficientFunds as exc:
        return _insufficient(exc)
    data = _base_view(product, amount_fen, shares_milli, nav_micro, hold.hold_id, hold.status)
    settled = _commit(ctx, hold.hold_id, str(product["name"]), data)
    if not settled:
        return ok_result(data)
    _add_shares(ctx.conn, str(account["user_id"]), str(product["code"]), shares_milli, amount_fen)
    _fill_holding(ctx.conn, str(account["user_id"]), str(product["code"]), data)
    data["message"] = BOUGHT_TEXT
    return ok_result(data)


def redeem_fund(ctx: ToolContext, params: dict[str, Any], token: str | None = None) -> ToolResult:
    """赎回：先冻结这笔回款，再由 funds.redeem 打回卡里，然后扣份额。

    回款是往卡里进的钱，所以建 hold 时不做余额校验（check_balance=False），
    不能要求用户卡里先有这笔钱才准赎回。
    """
    require_token("redeem_fund", params, token)
    account = primary_account(ctx.conn, ctx.account_id)
    code = str(params.get("product_code") or "")
    product = products.fetch_product(ctx.conn, code)
    if product is None:
        return fail_result("PRODUCT_NOT_FOUND", f"没有找到这只基金（{code}）。")
    nav_micro = int(product["nav_micro"])
    held = products.holding_of(ctx.conn, str(account["user_id"]), code)
    held_shares = int(held["shares_milli"]) if held else 0
    amount_fen = int(params["amount_fen"])
    shares_milli = products.shares_for(amount_fen, nav_micro)
    data = _base_view(product, amount_fen, shares_milli, nav_micro, "", "")
    data["holding_shares_milli"] = held_shares
    data["holding_market_value_fen"] = products.market_value_fen(held_shares, nav_micro)
    if shares_milli <= 0:
        return fail_result("AMOUNT_TOO_SMALL", "这笔金额太小，赎不回整 0.001 份。")
    if shares_milli > held_shares:
        return fail_result(
            "REDEEM_EXCEEDS_HOLDING",
            f"你手上只有 {held_shares / products.SHARE_SCALE:.3f} 份，赎不了这么多。",
            {"held_shares_milli": held_shares, "requested_shares_milli": shares_milli,
             "held_market_value_fen": data["holding_market_value_fen"], "product_code": code},
        )
    ensure_system_payee(ctx.conn, account)
    action = build_action("redeem_fund", params, str(account["id"]), amount_fen)
    try:
        hold = funds.initiate(
            ctx.conn, action, session_id=ctx.session_key(), node_id=ctx.node_key(),
            token=token, secret=ctx.secret, payee_id=funds.SYSTEM_PAYEE_ID, check_balance=False,
        )
    except BankError as exc:
        return fail_result(exc.code, exc.user_text)
    data["hold_id"] = hold.hold_id
    if not _redeem(ctx, hold.hold_id, str(product["name"]), data):
        return ok_result(data)
    _sell_shares(ctx.conn, str(account["user_id"]), code, shares_milli)
    _fill_holding(ctx.conn, str(account["user_id"]), code, data)
    data["message"] = REDEEMED_TEXT
    return ok_result(data)


def _commit(ctx: ToolContext, hold_id: str, counterparty: str, data: dict[str, Any]) -> bool:
    """第二阶段出账：申购的钱交到基金手里。失败不上抛，钱仍锁在 hold 里。"""
    try:
        outcome = funds.commit(ctx.conn, hold_id, counterparty=counterparty)
    except BankError as exc:
        log.warning("buy_fund.commit_failed hold=%s code=%s", hold_id, exc.code)
        data["settle_error"] = exc.code
        data["message"] = UNSETTLED_TEXT
        return False
    data["status"] = "committed"
    data["balance_after_fen"] = int(outcome.balance_after_fen)
    data["idempotent"] = bool(outcome.idempotent)
    return True


def _redeem(ctx: ToolContext, hold_id: str, counterparty: str, data: dict[str, Any]) -> bool:
    """第二阶段入账：赎回的钱打回卡里。失败不上抛，等 hold 到期作废。"""
    try:
        balance_after_fen = funds.redeem(ctx.conn, hold_id, counterparty=counterparty)
    except BankError as exc:
        log.warning("redeem_fund.redeem_failed hold=%s code=%s", hold_id, exc.code)
        data["settle_error"] = exc.code
        data["message"] = UNSETTLED_TEXT
        return False
    data["status"] = "committed"
    data["balance_after_fen"] = int(balance_after_fen)
    return True


def _add_shares(
    conn: sqlite3.Connection, user_id: str, code: str, shares_milli: int, cost_fen: int
) -> None:
    """申购记份额：原来没有就新建，有就加上（份额与成本同时累加）。"""
    with db.tx(conn):
        conn.execute(
            "INSERT INTO fund_holdings (user_id, product_code, shares_milli, cost_fen)"
            " VALUES (?, ?, ?, ?)"
            " ON CONFLICT(user_id, product_code) DO UPDATE SET"
            " shares_milli = shares_milli + excluded.shares_milli,"
            " cost_fen = cost_fen + excluded.cost_fen",
            (user_id, code, shares_milli, cost_fen),
        )


def _sell_shares(conn: sqlite3.Connection, user_id: str, code: str, shares_milli: int) -> None:
    """赎回扣份额：成本按剩余份额比例留，成本会因取整略少于原值。"""
    row = products.holding_of(conn, user_id, code)
    if row is None:
        return
    left = int(row["shares_milli"]) - shares_milli
    with db.tx(conn):
        if left > 0:
            cost_left = int(row["cost_fen"]) * left // int(row["shares_milli"])
            conn.execute(
                "UPDATE fund_holdings SET shares_milli = ?, cost_fen = ?"
                " WHERE user_id = ? AND product_code = ?",
                (left, cost_left, user_id, code),
            )
        else:
            conn.execute(
                "DELETE FROM fund_holdings WHERE user_id = ? AND product_code = ?", (user_id, code)
            )


def _fill_holding(conn: sqlite3.Connection, user_id: str, code: str, data: dict[str, Any]) -> None:
    """把「我现在持有多少」回填到返回值里，planner 和前端都读这两个字段。"""
    held = products.holding_of(conn, user_id, code)
    shares = int(held["shares_milli"]) if held else 0
    value_fen = products.market_value_fen(shares, int(data["nav_micro"]))
    data["holding_shares_milli"] = shares
    data["holding_shares_text"] = f"{shares / products.SHARE_SCALE:.3f}"
    data["holding_market_value_fen"] = value_fen
    data["holding_market_value_yuan"] = fen_to_yuan_str(value_fen)
    data["holding_cost_fen"] = int(held["cost_fen"]) if held else 0


def _base_view(
    product: sqlite3.Row,
    amount_fen: int,
    shares_milli: int,
    nav_micro: int,
    hold_id: str,
    status: str,
) -> dict[str, Any]:
    return {
        "product_code": str(product["code"]),
        "product_name": str(product["name"]),
        "risk_label": products.risk_label(int(product["risk_level"])),
        "amount_fen": amount_fen,
        "amount_yuan": fen_to_yuan_str(amount_fen),
        "nav_micro": nav_micro,
        "nav_text": f"{nav_micro / products.NAV_SCALE:.4f}",
        "shares_milli": shares_milli,
        "shares_text": f"{shares_milli / products.SHARE_SCALE:.3f}",
        "market_value_fen": products.market_value_fen(shares_milli, nav_micro),
        "hold_id": hold_id,
        "status": status,
    }


def _insufficient(exc: InsufficientFunds) -> ToolResult:
    balance = int(exc.fields.get("balance_fen") or 0)
    amount = int(exc.fields.get("amount_fen") or 0)
    return fail_result(
        "INSUFFICIENT_FUNDS",
        f"卡里只有 {fen_to_yuan_str(balance)}，这笔要 {fen_to_yuan_str(amount)}。",
        {"balance_fen": balance, "amount_fen": amount, "short_fen": max(amount - balance, 0)},
    )
