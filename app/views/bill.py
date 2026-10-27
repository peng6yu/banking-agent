"""账单分析：本月收支、主要类别、异常与趋势。

只读：本模块不写库、不发令牌、不调执行器。
"""

from __future__ import annotations

import sqlite3
from typing import Any

from app import proactive
from app import timefmt
from app.clock import ValidationError, now_ms
from app.subscriptions import CONFIDENCE_LABELS
from app.tools import bill, bill_acks, queries
from app.tools.base import primary_account
from app.tools.bill import ANOMALY_KINDS

from app.views.money import _accounts, _ctx, _enrich_anomalies, _trend
from app.views.labels import ANOMALY_RULES, DEFAULT_TREND_MONTHS


def resolve_account(conn: sqlite3.Connection, account_id: str | None) -> tuple[str, dict[str, Any]]:
    """账单看哪张卡：没指定就看主储蓄卡，指定了就必须真实存在。

    账单是「某一张卡」的流水视图，不是全账户聚合：储蓄卡看花了多少，信用卡看
    刷了多少、还了多少、还剩多少额度。切错卡比报错更糟，所以找不到就直接 400。
    """
    if account_id:
        row = conn.execute(
            "SELECT * FROM accounts WHERE id = ?", (str(account_id),)
        ).fetchone()
        if row is None:
            raise ValidationError(
                "找不到这个账户，可能已经被销户或者编号不对。",
                reason="ACCOUNT_NOT_FOUND",
                field="account_id",
            )
    else:
        row = primary_account(conn)
    return str(row["id"]), queries.account_view(row)


def _scope_note(account: dict[str, Any]) -> str:
    """说清这一屏是哪个口径，尤其是信用卡和储蓄卡不一样的那部分。"""
    if str(account.get("type")) == "credit":
        return (
            "这张信用卡的口径：支出是刷卡、收入是还款；可用额度 = 总额度 − 已用，"
            "刷信用卡不动储蓄卡余额，还款才动。"
        )
    return "这张储蓄卡的口径：支出/收入都是账面真实进出，不含账户互转与理财。"


def _acked_block(conn: sqlite3.Connection, account_id: str) -> dict[str, Any]:
    """「已核实」清单：核实过的流水不消失，只是不再算待核实；这里给一个撤销入口。

    只按账户列（不按月）：核实是一个收尾动作，不是账期内的统计，跨月也要看得到。
    """
    items: list[dict[str, Any]] = []
    for row in bill_acks.acked_rows(conn, account_id):
        rule = ANOMALY_RULES.get(str(row.get("kind") or ""), {})
        item = dict(row)
        item["rule_id"] = rule.get("rule_id", "")
        item["rule_text"] = rule.get("rule_text", "")
        item["confidence_label"] = CONFIDENCE_LABELS.get(rule.get("confidence", "low"), "低")
        items.append(item)
    return {
        "items": items,
        "count": len(items),
        "note": "核实只免打扰：流水与余额都不动。点「恢复成待核实」就重新挂回首页与账单页。",
    }


def bill_view(
    conn: sqlite3.Connection,
    *,
    period: str,
    year: int,
    month: int | None,
    account_id: str | None = None,
) -> dict[str, Any]:
    """账单分析：统计 + 分类 + 趋势 + 异常（每条带依据、证据与后续动作）。"""
    stamp = now_ms()
    account_id, account = resolve_account(conn, account_id)
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
    anomalies = _enrich_anomalies(conn, list(data.get("anomalies") or []), account_id)
    return {
        "ok": True,
        "generated_text": timefmt.maybe_minute_text(stamp),
        "period": period,
        "year": int(year),
        "month": month if period == "month" else None,
        "range": data.get("range"),
        # 账单是「某一张卡」的视图：把当前卡和可切换的账户一并带出去，
        # 前端不必再打一次 /api/business/management 才知道有哪些卡。
        "account": account,
        "accounts": _accounts(conn),
        "scope_note": _scope_note(account),
        # 主口径：钱真的离开净资产。账户互转/理财单独放在 internal 里交代。
        "spend_out_fen": data.get("spend_out_fen", 0),
        "spend_out_yuan": data.get("spend_out_yuan", "0.00"),
        "spend_in_fen": data.get("spend_in_fen", 0),
        "spend_in_yuan": data.get("spend_in_yuan", "0.00"),
        "net_fen": data.get("net_fen", 0),
        "net_yuan": data.get("net_yuan", "0.00"),
        "out_count": data.get("out_count", 0),
        "in_count": data.get("in_count", 0),
        # 副口径：把内部搬运也算进来，用来解释差额。
        "net_all_fen": data.get("net_all_fen", 0),
        "net_all_yuan": data.get("net_all_yuan", "0.00"),
        "internal": data.get("internal") or {
            "out_fen": 0, "out_yuan": "0.00", "in_fen": 0, "in_yuan": "0.00",
            "count": 0, "items": [], "note": "",
        },
        "by_category": data.get("by_category") or [],
        "summary": data.get("summary"),
        "trend": _trend(conn, account_id, DEFAULT_TREND_MONTHS),
        "anomalies": {
            "items": anomalies,
            "count": len(anomalies),
            "state": "发现异常（待核实）" if anomalies else "本期没有命中任何规则",
            "disclaimer": "三条规则都是启发式提示，不是欺诈认定；命中后请自己核对流水。",
            "rules": [
                {
                    "kind": kind,
                    "rule_id": ANOMALY_RULES.get(kind, {}).get("rule_id", ""),
                    "rule_text": ANOMALY_RULES.get(kind, {}).get("rule_text", ""),
                    "confidence": ANOMALY_RULES.get(kind, {}).get("confidence", "low"),
                    "confidence_label": CONFIDENCE_LABELS.get(
                        ANOMALY_RULES.get(kind, {}).get("confidence", "low"), "低"
                    ),
                }
                for kind in ANOMALY_KINDS
            ],
        },
        # 核实过的流水：不消失，只是不算待核实；这里给撤销入口。
        "acked": _acked_block(conn, account_id),
        "method": {"currency": "CNY"},
    }


