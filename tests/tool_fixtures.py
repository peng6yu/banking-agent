"""工具层测试的公共夹具：临时账本、种子数据、令牌与余额辅助。

只读用例与写工具用例各占一个文件（docs/08 的 400 行上限），两边共用这里的常量、
fixture 和辅助函数，保证同一套账本、同一张令牌在两侧行为一致。
"""

from __future__ import annotations

import time
from typing import Any

import pytest

from app import clock, db
from app.models import Action
from app.security import token as token_mod
from app.tools.base import ToolContext
from scripts.fund_seeds import seed_funds

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
    "buy_fund": {"product_code": "F00003", "amount_fen": 20_000},
    "redeem_fund": {"product_code": "F00002", "amount_fen": 10_000},
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
    seed_funds(conn, USER_ID)


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
