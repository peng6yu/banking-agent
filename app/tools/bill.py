"""账单分析工具。

对应文档：docs/06_工具层与场景实现规格.md 6.3 analyze_bill。
返回结构固定（有没有异常、异常几条都一样），三条异常规则：
拆单（同商户 60 分钟内 ≥3 笔且单笔 500—1000 元）、凌晨境外（00:00—05:00
境外且单笔 ≥500 元）、订阅涨价（连续两月涨 ≥30%）。
并列一律金额降序、金额相同按名字典序，保证同样输入永远同样输出。
"""

from __future__ import annotations

import sqlite3
import time
from typing import Any, Final

from app import timefmt
from app.clock import ValidationError
from app.models import ToolResult
from app.money import INTERNAL_CATEGORIES, fen_to_yuan_str
from app.tools.base import ToolContext, ok_result, primary_account
from app.tools.queries import fetch_transactions

TOP_MERCHANT_COUNT: Final[int] = 5

# 异常检测阈值（docs/06 6.3 analyze_bill 分支表）
SPLIT_WINDOW_MS: Final[int] = 60 * 60 * 1000
SPLIT_MIN_FEN: Final[int] = 50_000
SPLIT_MAX_FEN: Final[int] = 100_000
SPLIT_MIN_COUNT: Final[int] = 3
OVERSEAS_MIN_FEN: Final[int] = 50_000
OVERSEAS_NIGHT_START_HOUR: Final[int] = 0
OVERSEAS_NIGHT_END_HOUR: Final[int] = 5
SUBSCRIPTION_HIKE_RATIO: Final[float] = 0.30
ANOMALY_KINDS: Final[tuple[str, ...]] = (
    "split_orders", "odd_hour_overseas", "subscription_hike",
)
OVERSEAS_MARKERS: Final[tuple[str, ...]] = (
    "境外", "overseas", "香港", "澳门", "台湾", "日本", "东京", "大阪", "韩国", "首尔",
    "新加坡", "美国", "纽约", "洛杉矶", "英国", "伦敦", "法国", "巴黎", "德国", "柏林",
    "意大利", "米兰", "加拿大", "多伦多", "澳洲", "悉尼", "迪拜", "阿联酋", "瑞士", "苏黎世",
)


def analyze_bill(ctx: ToolContext, params: dict[str, Any], token: str | None = None) -> ToolResult:
    """账单分析：固定返回结构 + 三条异常检测规则，命中与否都按同一形状返回。"""
    period = str(params.get("period", "month"))
    year = int(params["year"])
    month = params.get("month")
    if period == "month" and month is None:
        raise ValidationError("按月分析要告诉我几月", year=year, period=period)
    start_ms, end_ms = _period_range(period, year, month)
    account = primary_account(ctx.conn, ctx.account_id)
    rows = fetch_transactions(ctx.conn, account["id"], start_ms, end_ms, None, None, None)
    baseline = _hike_baseline(ctx.conn, str(account["id"]), period, year, month)
    data = _bill_summary(rows, period, year, month, start_ms, end_ms, str(account["tail"]))
    data["anomalies"] = _detect_anomalies(rows, baseline)
    data["summary"] = _summary_text(data)
    return ok_result(data)
def _local(ts_ms: int) -> time.struct_time:
    """流水时间戳一律按本机时区拆解，测试和种子数据用同一套拆法。"""
    return time.localtime(ts_ms / 1000)


def _ms(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> int:
    """把本地时间拼成毫秒时间戳；跨年跨月交给 mktime 归一化。"""
    return int(time.mktime((year, month, day, hour, minute, 0, 0, -1, -1)) * 1000)


def _month_key(ts_ms: int) -> tuple[int, int]:
    parts = _local(ts_ms)
    return int(parts.tm_year), int(parts.tm_mon)


def _month_label(key: tuple[int, int]) -> str:
    return f"{key[0]} 年 {key[1]} 月"


def _next_month(key: tuple[int, int]) -> tuple[int, int]:
    return (key[0] + 1, 1) if key[1] == 12 else (key[0], key[1] + 1)


def _range_label(period: str, year: int, month: Any) -> str:
    return _month_label((int(year), int(month))) if period == "month" else f"{int(year)} 年"


def _period_range(period: str, year: int, month: Any) -> tuple[int, int]:
    """把 period/year/month 变成左闭右开的时间窗 [start_ms, end_ms)。"""
    if period == "year":
        return _ms(int(year), 1, 1), _ms(int(year) + 1, 1, 1)
    mon = int(month or 1)
    return _ms(int(year), mon, 1), _ms(int(year), mon + 1, 1)


def _hike_baseline(
    conn: sqlite3.Connection, account_id: str, period: str, year: int, month: Any
) -> list[sqlite3.Row]:
    """按月分析时额外取上一个自然月的订阅流水，用来判断「连续两月」的第一跳。"""
    if period != "month":
        return []
    prev_key = (int(year) - 1, 12) if int(month or 1) == 1 else (int(year), int(month) - 1)
    start_ms = _ms(prev_key[0], prev_key[1], 1)
    end_ms = _ms(int(year), int(month), 1)
    return fetch_transactions(conn, account_id, start_ms, end_ms, "订阅", None, None)


def _bill_summary(
    rows: list[sqlite3.Row],
    period: str,
    year: int,
    month: Any,
    start_ms: int,
    end_ms: int,
    account_tail: str,
) -> dict[str, Any]:
    """账单主体结构：无论有没有流水，字段一律齐全。"""
    out_rows = [row for row in rows if str(row["direction"]) == "out"]
    in_rows = [row for row in rows if str(row["direction"]) == "in"]
    total_out = sum(int(row["amount_fen"]) for row in out_rows)
    total_in = sum(int(row["amount_fen"]) for row in in_rows)
    # 转账/理财是账户内部搬运，两个方向都要单列：前端要把「全部结余」和
    # 「不含内部搬运的结余」摆在一起说明，否则总览（排除）与账单（不排除）
    # 会各报一个数，用户不知道该信哪个。
    internal_out = sum(
        int(row["amount_fen"]) for row in out_rows
        if str(row["category"]) in INTERNAL_CATEGORIES
    )
    internal_in = sum(
        int(row["amount_fen"]) for row in in_rows
        if str(row["category"]) in INTERNAL_CATEGORIES
    )
    net_all = total_in - total_out
    net_external = (total_in - internal_in) - (total_out - internal_out)
    return {
        "period": period,
        "year": int(year),
        "month": int(month) if period == "month" else None,
        "range": {
            "start_ms": start_ms,
            "end_ms": end_ms,
            "label": _range_label(period, year, month),
        },
        "account_tail": account_tail,
        "total_out_fen": total_out,
        "total_in_fen": total_in,
        "total_out_yuan": fen_to_yuan_str(total_out),
        "total_in_yuan": fen_to_yuan_str(total_in),
        "internal_out_fen": internal_out,
        "internal_in_fen": internal_in,
        "net_all_fen": net_all,
        "net_all_yuan": fen_to_yuan_str(net_all),
        "net_external_fen": net_external,
        "net_external_yuan": fen_to_yuan_str(net_external),
        "out_count": len(out_rows),
        "in_count": len(in_rows),
        "by_category": _by_category(out_rows, total_out),
        "top_merchants": _top_merchants(out_rows),
        "anomalies": [],
    }


def _by_category(out_rows: list[sqlite3.Row], total_out: int) -> list[dict[str, Any]]:
    """分类汇总；并列时按金额降序、金额相同按分类名字典序，保证可复现。"""
    buckets: dict[str, tuple[int, int]] = {}
    for row in out_rows:
        category = str(row["category"])
        amount, count = buckets.get(category, (0, 0))
        buckets[category] = (amount + int(row["amount_fen"]), count + 1)
    items = [
        {
            "category": category,
            "amount_fen": amount,
            "amount_yuan": fen_to_yuan_str(amount),
            "count": count,
            "pct": round(amount / total_out * 100, 1) if total_out > 0 else 0.0,
        }
        for category, (amount, count) in buckets.items()
    ]
    return sorted(items, key=lambda item: (-item["amount_fen"], item["category"]))


def _top_merchants(
    out_rows: list[sqlite3.Row], limit: int = TOP_MERCHANT_COUNT
) -> list[dict[str, Any]]:
    """商户 Top N，并列时按金额降序、金额相同按商户名字典序。"""
    buckets: dict[str, tuple[int, int]] = {}
    for row in out_rows:
        merchant = str(row["counterparty"])
        amount, count = buckets.get(merchant, (0, 0))
        buckets[merchant] = (amount + int(row["amount_fen"]), count + 1)
    items = [
        {"merchant": merchant, "amount_fen": amount, "count": count}
        for merchant, (amount, count) in buckets.items()
    ]
    return sorted(items, key=lambda item: (-item["amount_fen"], item["merchant"]))[:limit]


def _summary_text(data: dict[str, Any]) -> str:
    """一句话结论；空账期也照常说一句人话。"""
    label = str(data["range"]["label"])
    if not data["by_category"]:
        return f"{label}没有任何支出记录。"
    top = data["by_category"][0]
    return (
        f"{label}一共支出 {data['total_out_yuan']} 元，共 {data['out_count']} 笔，"
        f"花得最多的是{top['category']}（{top['pct']}%）。"
    )


def _detect_anomalies(
    rows: list[sqlite3.Row], baseline: list[sqlite3.Row]
) -> list[dict[str, Any]]:
    """三条规则全跑一遍，命中就往固定结构里塞，顺序按规则编号再按金额降序。"""
    found: list[dict[str, Any]] = []
    found.extend(_split_order_anomalies(rows))
    found.extend(_odd_hour_anomalies(rows))
    found.extend(_subscription_hike_anomalies([*baseline, *rows]))
    order = {kind: index for index, kind in enumerate(ANOMALY_KINDS)}
    return sorted(
        found,
        key=lambda item: (order.get(str(item["kind"]), 99), -int(item["amount_fen"])),
    )


def _split_order_anomalies(rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
    """规则一：同一家商户 60 分钟内 ≥3 笔、且单笔落在 500—1000 元，疑似拆单。"""
    spends = sorted(
        (
            row
            for row in rows
            if str(row["direction"]) == "out"
            and SPLIT_MIN_FEN <= int(row["amount_fen"]) <= SPLIT_MAX_FEN
        ),
        key=lambda row: int(row["ts_ms"]),
    )
    groups: dict[str, list[sqlite3.Row]] = {}
    for row in spends:
        groups.setdefault(str(row["counterparty"]), []).append(row)
    found: list[dict[str, Any]] = []
    for merchant in sorted(groups):
        for window in _windows(groups[merchant], SPLIT_WINDOW_MS, SPLIT_MIN_COUNT):
            total = sum(int(row["amount_fen"]) for row in window)
            window_start_ms = int(window[0]["ts_ms"])
            window_end_ms = int(window[-1]["ts_ms"])
            found.append(
                {
                    "kind": "split_orders",
                    "title": f"{merchant} 疑似拆单",
                    "detail": (
                        f"{timefmt.minute_text(window_start_ms)} 到 "
                        f"{timefmt.minute_text(window_end_ms)}，在 {merchant} "
                        f"连着付了 {len(window)} 笔，单笔都在 500—1000 元，"
                        f"合计 {fen_to_yuan_str(total)} 元。"
                    ),
                    "amount_fen": total,
                    # 证据字段：前端要能列出"相关流水"让人自己复核，
                    # 只给一句结论等于让人只能信，不能查。
                    "merchant": merchant,
                    # transactions.id 是文本主键（形如 t_000000ec），不是整数行号；
        # 这里必须原样保留字符串，否则下游按 id 回查流水会直接崩。
        "tx_ids": [str(row["id"]) for row in window],
                    "occurrence_count": len(window),
                    "window_start_ms": window_start_ms,
                    "window_end_ms": window_end_ms,
                }
            )
    return found


def _windows(
    group: list[sqlite3.Row], window_ms: int, min_count: int
) -> list[list[sqlite3.Row]]:
    """滑窗切成互不重叠的时间片，只留下笔数够的窗口，避免同一段被重复报。"""
    windows: list[list[sqlite3.Row]] = []
    left = 0
    for right, row in enumerate(group):
        while int(row["ts_ms"]) - int(group[left]["ts_ms"]) > window_ms:
            left += 1
        if right - left + 1 >= min_count:
            windows.append(group[left : right + 1])
            left = right + 1
    return windows


def _odd_hour_anomalies(rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
    """规则二：凌晨 0—5 点、境外、单笔 ≥500 元的支出，夜里人在境外。"""
    found: list[dict[str, Any]] = []
    for row in rows:
        if not _is_night_overseas(row):
            continue
        amount_fen = int(row["amount_fen"])
        found.append(
            {
                "kind": "odd_hour_overseas",
                "title": f"{row['counterparty']} 凌晨境外消费",
                "detail": (
                    f"{timefmt.minute_text(int(row['ts_ms']))} 这笔 {fen_to_yuan_str(amount_fen)} 元"
                    f"发生在 0 点到 5 点之间，商户 {row['counterparty']} 属于境外。"
                ),
                "amount_fen": amount_fen,
                "merchant": str(row["counterparty"]),
                "tx_ids": [str(row["id"])],
                "occurrence_count": 1,
                "window_start_ms": int(row["ts_ms"]),
                "window_end_ms": int(row["ts_ms"]),
            }
        )
    return found


def _is_night_overseas(row: sqlite3.Row) -> bool:
    if str(row["direction"]) != "out" or int(row["amount_fen"]) < OVERSEAS_MIN_FEN:
        return False
    hour = int(_local(int(row["ts_ms"])).tm_hour)
    if not OVERSEAS_NIGHT_START_HOUR <= hour < OVERSEAS_NIGHT_END_HOUR:
        return False
    return _is_overseas(row)


def _is_overseas(row: sqlite3.Row) -> bool:
    """境外判定：渠道标记说了算，商户名或备注里带境外地名也算。"""
    if str(row["channel"]).lower() == "overseas":
        return True
    text = f"{row['counterparty']} {row['note']}".lower()
    return any(marker.lower() in text for marker in OVERSEAS_MARKERS)


def _subscription_hike_anomalies(rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
    """规则三：同一订阅连续两个自然月金额上涨 ≥30%。"""
    totals: dict[tuple[str, tuple[int, int]], int] = {}
    ids: dict[tuple[str, tuple[int, int]], list[str]] = {}
    for row in rows:
        if str(row["category"]) != "订阅" or str(row["direction"]) != "out":
            continue
        key = (str(row["counterparty"]), _month_key(int(row["ts_ms"])))
        totals[key] = totals.get(key, 0) + int(row["amount_fen"])
        ids.setdefault(key, []).append(str(row["id"]))
    found: list[dict[str, Any]] = []
    for (merchant, prev_key), prev_amount in sorted(totals.items()):
        cur_amount = totals.get((merchant, _next_month(prev_key)))
        if cur_amount is None or prev_amount <= 0:
            continue
        ratio = (cur_amount - prev_amount) / prev_amount
        if ratio < SUBSCRIPTION_HIKE_RATIO:
            continue
        found.append(
            {
                "kind": "subscription_hike",
                "title": f"{merchant} 订阅涨价 {round(ratio * 100)}%",
                "detail": (
                    f"{_month_label(prev_key)} 到 {_month_label(_next_month(prev_key))}，"
                    f"{merchant} 从 {fen_to_yuan_str(prev_amount)} 元涨到 "
                    f"{fen_to_yuan_str(cur_amount)} 元，涨幅 {round(ratio * 100)}%。"
                ),
                "amount_fen": cur_amount - prev_amount,
                "merchant": merchant,
                "tx_ids": [*ids.get((merchant, prev_key), []),
                           *ids.get((merchant, _next_month(prev_key)), [])],
                "occurrence_count": 2,
                # 订阅涨价按自然月比较，没有"毫秒窗口"，显式给 null 比编一个时间更诚实
                "window_start_ms": None,
                "window_end_ms": None,
                "comparison": {
                    "previous_month": _month_label(prev_key),
                    "current_month": _month_label(_next_month(prev_key)),
                    "previous_fen": prev_amount,
                    "current_fen": cur_amount,
                    "hike_pct": round(ratio * 100),
                },
            }
        )
    return found

