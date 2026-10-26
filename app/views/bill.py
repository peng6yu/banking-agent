"""账单分析：本月收支、主要类别、异常与趋势。

只读：本模块不写库、不发令牌、不调执行器。
"""

from __future__ import annotations

import sqlite3
from typing import Any

from app import proactive
from app import timefmt
from app.tools import bill
from app.tools import queries
from app.clock import now_ms
from app.tools.base import primary_account
from app.tools.bill import ANOMALY_KINDS

from app.views.money import _ctx, _enrich_anomalies, _trend
from app.views.labels import ANOMALY_RULES, DEFAULT_TREND_MONTHS


def bill_view(
    conn: sqlite3.Connection, *, period: str, year: int, month: int | None
) -> dict[str, Any]:
    """账单分析：统计 + 分类 + 趋势 + 异常（每条带依据、证据与后续动作）。"""
    stamp = now_ms()
    account = primary_account(conn)
    account_id = str(account["id"])
    today = proactive.local_date()
    period = (period or "month").strip().lower()
    if period not in ("month", "year"):
        period = "month"
    if period == "month":
        month = int(month or today.month)
        year = int(year or today.year)
    data = bill.analyze_bill(
        _ctx(conn, account_id),
        {"period": period, "year": int(year), **({"month": month} if period == "month" else {})},
    ).data
    anomalies = _enrich_anomalies(conn, list(data.get("anomalies") or []))
    return {
        "generated_ms": stamp,
        "generated_text": timefmt.maybe_minute_text(stamp),
        "period": period,
        "year": int(year),
        "month": month if period == "month" else None,
        "range": data.get("range"),
        "account_tail": data.get("account_tail"),
        "total_out_fen": data.get("total_out_fen", 0),
        "total_out_yuan": data.get("total_out_yuan", "0.00"),
        "total_in_fen": data.get("total_in_fen", 0),
        "total_in_yuan": data.get("total_in_yuan", "0.00"),
        "out_count": data.get("out_count", 0),
        "in_count": data.get("in_count", 0),
        "by_category": data.get("by_category") or [],
        "top_merchants": data.get("top_merchants") or [],
        "summary": data.get("summary"),
        "trend": _trend(conn, account_id, DEFAULT_TREND_MONTHS),
        "anomalies": {
            "items": anomalies,
            "count": len(anomalies),
            "state": "发现异常（待核实）",
            "disclaimer": "三条规则都是启发式提示，不是欺诈认定；命中后请自己核对流水。",
            "rules": [
                {
                    "kind": kind,
                    "rule_id": ANOMALY_RULES.get(kind, {}).get("rule_id", ""),
                    "rule_text": ANOMALY_RULES.get(kind, {}).get("rule_text", ""),
                    "confidence": ANOMALY_RULES.get(kind, {}).get("confidence", "low"),
                }
                for kind in ANOMALY_KINDS
            ],
        },
        "method": {
            "currency": "CNY",
            "money_unit": "所有金额以整数分计算与返回，同时给 yuan 字符串",
            "categories": list(queries.CATEGORIES),
            "internal_transfer_note": "转账/理财属于账户内部搬运，收支统计与趋势里不计入。",
            "anomaly_window": "拆单与凌晨境外只扫当前账期；订阅涨价会带上一个自然月的基线。",
        },
    }


