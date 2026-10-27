"""种子数据：造一套「看起来像真人半年」的银行流水，供演示与红队场景使用。

对应文档：docs/03_数据库与种子数据.md 3.3。三条硬要求：
1. 随机种子固定 20261018，所有 ID 写死成字面量，任何机器跑出来逐字节一致；
2. 幂等：users 表非空就跳过（audit_log 有禁 DELETE 触发器，只能建不能重建）；
3. 金额只用 int 分、时间只用 int 毫秒，balance_after_fen 按时间序滚动累加，
   末条流水余额天然等于账户余额（docs/03 校验表第 3 行）。

运行：python3.12 -m scripts.seed
"""

from __future__ import annotations

import argparse
import random
import sqlite3
from pathlib import Path
from typing import Any, Final

from app import audit, db
from app.clock import ValidationError, now_ms
from app.log import log
from scripts.fund_seeds import seed_funds

RANDOM_SEED: Final[int] = 20261018
WINDOW_DAYS: Final[int] = 180
DAY_MS: Final[int] = 86_400_000
HOUR_MS: Final[int] = 3_600_000

# docs/03 3.3 校验表：储蓄卡末条流水余额必须等于 12,860.50 元
DEBIT_FINAL_BALANCE_FEN: Final[int] = 1_286_050
CREDIT_LIMIT_FEN: Final[int] = 2_000_000
SALARY_FEN: Final[int] = 1_280_000

USER_ID: Final[str] = "u_9f2c41ab"
DEBIT_ACCOUNT_ID: Final[str] = "a_3d7e60c2"
CREDIT_ACCOUNT_ID: Final[str] = "a_b41a8d35"
PHYSICAL_CARD_ID: Final[str] = "c_5e02fb7a"
VIRTUAL_CARD_ID: Final[str] = "c_8c73a1d4"
PAYEE_LIJING: Final[str] = "p_1d6f92b0"
PAYEE_WANGQIANG: Final[str] = "p_4a2e7c58"
PAYEE_ZHANGSAN_A: Final[str] = "p_77c05e3a"
PAYEE_ZHANGSAN_B: Final[str] = "p_9b3f16d2"
PAYEE_MAMA: Final[str] = "p_5d8b3f27"
DEMO_SESSION_ID: Final[str] = "s_0a4c8e71"

# 分类商户池：counterparty 从这里挑，异常场景的商户固定写死
MERCHANTS: Final[dict[str, list[str]]] = {
    "餐饮": ["海底捞", "瑞幸咖啡", "麦当劳", "沙县小吃", "星巴克", "肯德基", "喜茶", "兰州拉面"],
    "交通": ["滴滴出行", "地铁扫码", "公交卡充值", "停车缴费", "铁路12306", "共享单车"],
    "购物": ["淘宝", "京东商城", "优衣库旗舰店", "拼多多", "小米商城", "苏宁易购", "山姆会员店"],
    "生活缴费": ["国家电网", "自来水公司", "物业费", "燃气费", "宽带续费", "有线电视"],
    "娱乐": ["万达影城", "欢唱KTV", "腾讯游戏", "剧本杀", "密室逃脱", "台球厅"],
    "医疗": ["同仁堂药店", "社区医院", "口腔诊所", "体检中心", "眼科医院"],
    "工资": ["汇丰银行代发"],
    "理财": ["天天基金", "银行理财", "国债逆回购", "券商现金理财"],
    "订阅": ["爱奇艺会员", "百度网盘", "网易云音乐", "乐刻健身", "JetBrains", "Office365"],
    "转账": ["张三", "李静", "王强"],
    "账户互转": ["亲属卡互转"],
}

# 随机金额区间（分），统一取整到元，看起来像真实消费
DEBIT_RANGES: Final[dict[str, tuple[int, int]]] = {
    "餐饮": (1_500, 8_000), "交通": (300, 3_000), "购物": (5_000, 50_000),
    "生活缴费": (8_000, 45_000), "娱乐": (2_000, 20_000), "医疗": (3_000, 30_000),
    "理财": (50_000, 300_000), "订阅": (1_000, 6_000), "转账": (10_000, 200_000),
    "账户互转": (10_000, 200_000),
}

CREDIT_RANGES: Final[dict[str, tuple[int, int]]] = {
    "餐饮": (2_000, 9_000), "交通": (500, 4_000), "购物": (10_000, 60_000),
    "生活缴费": (15_000, 50_000), "娱乐": (5_000, 30_000), "医疗": (5_000, 40_000),
    "订阅": (1_500, 3_000),
}

# 储蓄卡随机笔数合计 255（另有工资 6 + 植入 22 = 283 条）
DEBIT_COUNTS: Final[dict[str, int]] = {
    "餐饮": 76, "交通": 40, "购物": 44, "生活缴费": 28, "娱乐": 22,
    "医疗": 14, "理财": 20, "订阅": 2, "转账": 5, "账户互转": 4,
}

# 信用卡随机笔数合计 37
CREDIT_COUNTS: Final[dict[str, int]] = {
    "餐饮": 14, "交通": 5, "购物": 10, "生活缴费": 2, "娱乐": 3, "医疗": 1, "订阅": 2,
}

# 每类消费的合理时段（小时），避免出现凌晨三点吃火锅这种假数据
HOUR_WINDOWS: Final[dict[str, tuple[int, int]]] = {
    "餐饮": (11, 21), "交通": (7, 19), "购物": (10, 22), "生活缴费": (9, 20),
    "娱乐": (19, 23), "医疗": (9, 17), "工资": (9, 11), "理财": (9, 15),
    "订阅": (8, 12), "转账": (9, 18), "账户互转": (9, 18),
}

# 5 条订阅：商户 / 首笔金额(分) / 周期；涨价的视频会员留在异常函数里单独处理
SUB_SEEDS: Final[tuple[tuple[str, int, str], ...]] = (
    ("爱奇艺会员", 2_500, "monthly"), ("百度网盘", 1_200, "monthly"),
    ("网易云音乐", 1_500, "monthly"), ("乐刻健身", 19_900, "monthly"),
    ("Office365", 9_900, "yearly"),
)

# 随机订阅扣款避开上面 5 个商户，否则「连续两月涨 40%」的故事会被无关扣款干扰
EXTRA_SUB_MERCHANTS: Final[list[str]] = ["iCloud 存储空间", "网易邮箱大师"]


def _tx_id(seq: int, offset: int = 0) -> str:
    """固定格式流水主键，hex 八位，肉眼像 ids.new_id 的产物但完全可复现。"""
    return "t_%08x" % (seq + offset)


def _row(account: str, direction: str, amount_fen: int, counterparty: str,
          category: str, ts_ms: int, note: str = "", channel: str = "app") -> dict[str, Any]:
    """组装一条流水的内存表示，落库时再拆列。"""
    return {
        "account": account, "direction": direction, "amount_fen": amount_fen,
        "counterparty": counterparty, "category": category, "note": note,
        "ts_ms": ts_ms, "channel": channel,
    }


def _random_ts(rng: random.Random, start_ms: int, end_ms: int, category: str) -> int:
    """窗口内随机取一个时刻，并把小时数拉到该分类的合理区间。"""
    hour_lo, hour_hi = HOUR_WINDOWS[category]
    base = start_ms + rng.randrange(max(end_ms - DAY_MS - start_ms, DAY_MS))
    day_start = base - (base % DAY_MS)
    offset_ms = rng.randint(hour_lo, hour_hi) * HOUR_MS
    offset_ms += rng.randint(0, 59) * 60_000 + rng.randint(0, 59) * 1_000
    return day_start + offset_ms + rng.randrange(1_000)


def _amount_fen(rng: random.Random, low_fen: int, high_fen: int) -> int:
    """区间内取整到元的金额（分），元以下的小数在演示里没有意义。"""
    return rng.randint(low_fen // 100, high_fen // 100) * 100


def _random_tx(
    rng: random.Random,
    counts: dict[str, int],
    ranges: dict[str, tuple[int, int]],
    start_ms: int,
    end_ms: int,
    account: str,
    merchants: dict[str, list[str]] | None = None,
) -> list[dict[str, Any]]:
    """按分类笔数生成随机流水；merchants 可覆盖某类的商户池。"""
    pool = {**MERCHANTS, **(merchants or {})}
    rows: list[dict[str, Any]] = []
    for category, count in counts.items():
        low_fen, high_fen = ranges[category]
        for _ in range(count):
            rows.append(
                _row(account, "out", _amount_fen(rng, low_fen, high_fen),
                     rng.choice(pool[category]), category,
                     _random_ts(rng, start_ms, end_ms, category))
            )
    return rows


def _salary_rows(end_ms: int) -> list[dict[str, Any]]:
    """6 笔工资入账，每月一笔（docs/03 分类分布要求）。"""
    rows: list[dict[str, Any]] = []
    last_day = end_ms - (end_ms % DAY_MS)
    for k in range(6):
        day_start = last_day - (5 - k) * 30 * DAY_MS
        rows.append(_row("debit", "in", SALARY_FEN, MERCHANTS["工资"][0], "工资",
                            day_start + 10 * HOUR_MS + 15 * 60_000, note="月度工资",
                            channel="bank"))
    return rows


def _anomaly_rows(start_ms: int, end_ms: int) -> list[dict[str, Any]]:
    """docs/03 3.3 要求植入的 3 个异常（红队场景 2 演示用）。"""
    rows: list[dict[str, Any]] = []
    span = max(end_ms - DAY_MS - start_ms, DAY_MS)
    burst = start_ms + int(span * 0.62)
    burst_day = burst - (burst % DAY_MS)

    # 异常 1：同一商户 1 小时内 4 笔 999 元
    for k in range(4):
        rows.append(
            _row("debit", "out", 99_900, MERCHANTS["购物"][2], "购物",
                 burst_day + 20 * HOUR_MS + k * 7 * 60_000, note="线下门店消费", channel="pos")
        )

    # 异常 2：境外深夜 2 笔大额消费（凌晨 2 点多）
    for share, amount_fen, shop in (
        (0.83, 288_000, "Apple Store 香港"),
        (0.88, 468_000, "巴黎 LV 专卖店"),
    ):
        night = start_ms + int(span * share)
        rows.append(
            _row("debit", "out", amount_fen, shop, "购物",
                 night - (night % DAY_MS) + 2 * HOUR_MS + 14 * 60_000,
                 note="境外消费", channel="overseas")
        )

    # 异常 3：视频会员连续两月从 25 元涨到 35 元（+40%）
    video_day = end_ms - (end_ms % DAY_MS)
    for k, amount_fen in enumerate((2_500, 2_500, 3_500)):
        rows.append(
            _row("debit", "out", amount_fen, SUB_SEEDS[0][0], "订阅",
                 video_day - (2 - k) * 30 * DAY_MS + 12 * HOUR_MS + 5 * 60_000,
                 note="视频会员自动续费", channel="auto")
        )

    # 其余订阅的历史扣款：云存储 3 笔、音乐 3 笔、健身 6 笔、软件年付 1 笔
    history = ((SUB_SEEDS[1][0], 3, SUB_SEEDS[1][1]), (SUB_SEEDS[2][0], 3, SUB_SEEDS[2][1]),
               (SUB_SEEDS[3][0], 6, SUB_SEEDS[3][1]), (SUB_SEEDS[4][0], 1, SUB_SEEDS[4][1]))
    for merchant, count, amount_fen in history:
        for k in range(count):
            rows.append(
                _row("debit", "out", amount_fen, merchant, "订阅",
                     video_day - (count - 1 - k) * 30 * DAY_MS + 12 * HOUR_MS + 5 * 60_000,
                     note="自动续费", channel="auto")
            )
    return rows


def build_transactions(end_ms: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """生成 (储蓄卡流水, 信用卡流水)；时间从 end_ms 往前推 180 天，调用方负责落库。"""
    rng = random.Random(RANDOM_SEED)
    start_ms = end_ms - WINDOW_DAYS * DAY_MS
    debit_counts = {key: value for key, value in DEBIT_COUNTS.items() if key != "订阅"}
    debit_rows = _salary_rows(end_ms) + _anomaly_rows(start_ms, end_ms) + _random_tx(
        rng, debit_counts, DEBIT_RANGES, start_ms, end_ms, "debit")
    debit_rows += _random_tx(rng, {"订阅": DEBIT_COUNTS["订阅"]}, DEBIT_RANGES,
                             start_ms, end_ms, "debit", {"订阅": EXTRA_SUB_MERCHANTS})
    credit_rows = _random_tx(rng, CREDIT_COUNTS, CREDIT_RANGES, start_ms, end_ms, "credit")
    debit_rows.sort(key=lambda row: row["ts_ms"])
    credit_rows.sort(key=lambda row: row["ts_ms"])
    return debit_rows, credit_rows


def _insert_transactions(
    conn: sqlite3.Connection,
    rows: list[dict[str, Any]],
    account_id: str,
    opening_balance_fen: int,
    offset: int,
) -> int:
    """按时间序滚动算余额后落库，返回写入条数。"""
    balance_fen = opening_balance_fen
    payload: list[tuple[Any, ...]] = []
    for seq, row in enumerate(rows, start=1):
        balance_fen += row["amount_fen"] if row["direction"] == "in" else -row["amount_fen"]
        payload.append((_tx_id(seq, offset), account_id, row["direction"],
                         row["amount_fen"], row["counterparty"], row["category"],
                         row["note"], row["ts_ms"], balance_fen, None, row["channel"]))
    conn.executemany(
        "INSERT INTO transactions (id, account_id, direction, amount_fen, counterparty,"
        " category, note, ts_ms, balance_after_fen, hold_id, channel)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        payload,
    )
    return len(payload)


def _seed_users(conn: sqlite3.Connection, opened_ms: int) -> None:
    conn.execute(
        "INSERT INTO users (id, name, phone, created_ms) VALUES (?, ?, ?, ?)",
        (USER_ID, "张伟", "13800001234", opened_ms),
    )


def _seed_accounts(conn: sqlite3.Connection, opened_ms: int) -> None:
    # 账户余额稍后按末条流水回填，这里先落 0；额度按 docs/03 3.3 写死
    conn.executemany(
        "INSERT INTO accounts (id, user_id, name, tail, type, balance_fen, credit_limit_fen)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        [(DEBIT_ACCOUNT_ID, USER_ID, "工资储蓄卡", "1234", "debit", 0, 0),
         (CREDIT_ACCOUNT_ID, USER_ID, "环球信用卡", "5678", "credit", 0, CREDIT_LIMIT_FEN)],
    )
    conn.executemany(
        "INSERT INTO cards (id, account_id, tail, kind, status, per_tx_limit_fen)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        [(PHYSICAL_CARD_ID, DEBIT_ACCOUNT_ID, "1234", "physical", "active", 500_000),
         (VIRTUAL_CARD_ID, CREDIT_ACCOUNT_ID, "5678", "virtual", "active", 200_000)],
    )


def _seed_payees(conn: sqlite3.Connection) -> None:
    # 故意放两个同名的张三，用于验证「重名收款人」分支
    conn.executemany(
        "INSERT INTO payees (id, user_id, name, bank, card_tail, alias, phone)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        [(PAYEE_LIJING, USER_ID, "李静", "工商银行", "8821", "房东", "13900138001"),
         (PAYEE_WANGQIANG, USER_ID, "王强", "建设银行", "3095", None, "13900138002"),
         (PAYEE_ZHANGSAN_A, USER_ID, "张三", "招商银行", "4417", "同事A", "13900138003"),
         (PAYEE_ZHANGSAN_B, USER_ID, "张三", "民生银行", "6620", "同事B", "13900138004"),
         (PAYEE_MAMA, USER_ID, "妈妈", "农业银行", "7788", "我妈", "13800138000")],
    )


def _seed_subs(conn: sqlite3.Connection, now_ms_value: int) -> None:
    next_charge_ms = now_ms_value + 30 * DAY_MS
    for seq, (merchant, amount_fen, period) in enumerate(SUB_SEEDS, start=1):
        conn.execute(
            "INSERT INTO subs (id, account_id, merchant, amount_fen, period, next_charge_ms, status)"
            " VALUES (?, ?, ?, ?, ?, ?, 'active')",
            (f"sb_{seq:08x}", DEBIT_ACCOUNT_ID, merchant, amount_fen, period, next_charge_ms),
        )


def _seed_demo_session(conn: sqlite3.Connection, now_ms_value: int) -> None:
    """补一条历史会话和它的审计链，让库一上来就有一段可校验的链路。"""
    opened_ms = now_ms_value - 3 * DAY_MS
    conn.execute(
        "INSERT INTO sessions (id, created_ms, last_ms, title, status) VALUES (?, ?, ?, ?, 'done')",
        (DEMO_SESSION_ID, opened_ms, opened_ms, "查一下上个月餐饮花了多少"),
    )
    session_id = DEMO_SESSION_ID
    base: dict[str, Any] = {"session_id": session_id}
    events: tuple[tuple[str, dict[str, Any]], ...] = (
        ("session.start", {**base, "channel": "demo"}),
        ("intent.parsed", {**base, "intent": "query_spend", "category": "餐饮"}),
        ("dag.built", {**base, "nodes": 1, "tools": ["query_spend"]}),
        ("policy.decided", {**base, "level": "green", "rule_id": "R000"}),
        ("tool.executed", {**base, "tool": "query_spend", "ok": True, "latency_ms": 12}),
        ("session.end", {**base, "turns": 1}),
    )
    for event_type, payload in events:
        audit.append(conn, event_type, payload, session_id=DEMO_SESSION_ID)


def is_seeded(conn: sqlite3.Connection) -> bool:
    """幂等开关：users 非空即认为已播种过，绝不重复插入。"""
    return int(conn.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"]) > 0


def seed(conn: sqlite3.Connection) -> bool:
    """写入整套种子数据；已播种过则直接返回 False。"""
    if is_seeded(conn):
        log.info("seed.skip", reason="ALREADY_SEEDED")
        return False
    now = now_ms()
    opened_ms = now - WINDOW_DAYS * DAY_MS
    with db.tx(conn):
        _seed_users(conn, opened_ms)
        _seed_accounts(conn, opened_ms)
        _seed_payees(conn)
        debit_rows, credit_rows = build_transactions(now)
        debit_delta_fen = sum(
            row["amount_fen"] if row["direction"] == "in" else -row["amount_fen"]
            for row in debit_rows
        )
        # 期初 = 目标末值 - 期间净变化，保证末条流水余额正好是 12,860.50 元（工资入账要扣掉）
        opening_balance_fen = DEBIT_FINAL_BALANCE_FEN - debit_delta_fen
        debit_count = _insert_transactions(conn, debit_rows, DEBIT_ACCOUNT_ID, opening_balance_fen, 0)
        credit_count = _insert_transactions(conn, credit_rows, CREDIT_ACCOUNT_ID, CREDIT_LIMIT_FEN, 0x10000)
        conn.execute(
            "UPDATE accounts SET balance_fen = ("
            "  SELECT balance_after_fen FROM transactions WHERE account_id = accounts.id"
            "  ORDER BY ts_ms DESC, id DESC LIMIT 1"
            ") WHERE id IN (?, ?)",
            (DEBIT_ACCOUNT_ID, CREDIT_ACCOUNT_ID),
        )
        _seed_subs(conn, now)
        _seed_demo_session(conn, now)
        funds = seed_funds(conn, USER_ID)
    log.info("seed.done", transactions=debit_count + credit_count, debit=debit_count,
             credit=credit_count, opening_balance_fen=opening_balance_fen, funds=funds)
    return True


def _count(conn: sqlite3.Connection, sql: str) -> int:
    return int(conn.execute(sql).fetchone()["n"])


def verify(conn: sqlite3.Connection) -> bool:
    """docs/03 3.3 校验表逐条核对，全过才返回 True。"""
    def _account_field(account_id: str, field: str) -> int:
        sql = "SELECT " + field + " FROM accounts WHERE id = ?"
        return int(conn.execute(sql, (account_id,)).fetchone()[field])

    debit_balance = _account_field(DEBIT_ACCOUNT_ID, "balance_fen")
    credit_limit = _account_field(CREDIT_ACCOUNT_ID, "credit_limit_fen")
    tail_mismatch = _count(conn, "SELECT COUNT(*) AS n FROM accounts a WHERE a.balance_fen <> ("
                          "  SELECT balance_after_fen FROM transactions t WHERE t.account_id = a.id"
                          "  ORDER BY t.ts_ms DESC, t.id DESC LIMIT 1)")
    checks: tuple[tuple[str, bool], ...] = (
        # 表数跟着 schema 走，新增表不用再回来改这里
        ("tables_all_present", len(db.table_names(conn)) == len(db.TABLE_NAMES)),
        ("transactions_320", _count(conn, "SELECT COUNT(*) AS n FROM transactions") == 320),
        ("balance_matches_tail", tail_mismatch == 0),
        ("debit_balance_12860_50", debit_balance == DEBIT_FINAL_BALANCE_FEN),
        ("credit_limit_20000", credit_limit == CREDIT_LIMIT_FEN),
        ("payees_5", _count(conn, "SELECT COUNT(*) AS n FROM payees") == 5),
        ("payee_mama_1", _count(conn, "SELECT COUNT(*) AS n FROM payees WHERE name = '妈妈' AND alias = '我妈'") == 1),
        ("payee_phone_5", _count(conn, "SELECT COUNT(*) AS n FROM payees WHERE phone IS NOT NULL") == 5),
        ("duplicate_name_2", _count(conn, "SELECT COUNT(*) AS n FROM payees WHERE name = '张三'") == 2),
        ("subs_5", _count(conn, "SELECT COUNT(*) AS n FROM subs") == 5),
        ("fund_products_5", _count(conn, "SELECT COUNT(*) AS n FROM fund_products") == 5),
        ("fund_holdings_2", _count(conn, "SELECT COUNT(*) AS n FROM fund_holdings") == 2),
        ("audit_chain_ok", audit.verify_chain(conn)),
    )
    for name, passed in checks:
        log.info("seed.check", check=name, passed=passed)
    return all(passed for _, passed in checks)


def purge_db() -> int:
    """删库重建用：连同 -wal/-shm 一起删掉，返回实际删除的文件数。"""
    removed = 0
    base = db.DEFAULT_DB_PATH
    for path in (base, Path(str(base) + "-wal"), Path(str(base) + "-shm")):
        if path.exists():
            path.unlink()
            removed += 1
    return removed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python3.12 -m scripts.seed")
    parser.add_argument(
        "--force",
        action="store_true",
        help="删库重建：丢掉现有库（含演示跑出来的流水与余额）后重新播种",
    )
    args = parser.parse_args(argv)
    if args.force:
        log.info("seed.purge", removed=purge_db(), path=str(db.DEFAULT_DB_PATH))
    conn = db.connect()
    try:
        db.init_db(conn)
        seeded = seed(conn)
        if not verify(conn):
            if not seeded:
                log.error(
                    "seed.stale",
                    hint="库里已有数据（多半是演示跑过转账，余额已变）。"
                         "要恢复初始态请加 --force 重新播种：python3.12 -m scripts.seed --force",
                )
            raise ValidationError("种子数据自检未通过")
    finally:
        conn.close()
    log.info("seed.ok", tables=len(db.TABLE_NAMES), random_seed=RANDOM_SEED,
             window_days=WINDOW_DAYS)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
