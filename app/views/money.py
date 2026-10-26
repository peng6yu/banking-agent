"""视图用的读数积木·金额与流水（第 5 层）：账户、流水、趋势、账单块。

只做三件事：查（只读）、算（整数分）、拼（可 JSON 化的 dict）。不做裁决——
能不能确认、能不能验证是 policy 与 planner 的事，这里只把已经裁好的事实带出去。
"""

from __future__ import annotations

import sqlite3
import time
from datetime import date
from typing import Any

from app import funds
from app import proactive
from app import timefmt
from app.tools import bill
from app.tools import queries
from app.money import fen_to_yuan_str
from app.tools.base import ToolContext

from app.views.labels import ANOMALY_RULES, DEFAULT_TREND_MONTHS, MAX_TREND_MONTHS


# ---------------------------------------------------------------- 小工具


def _ctx(conn: sqlite3.Connection, account_id: str | None = None) -> ToolContext:
    """只读工具的执行上下文：没有会话、没有密钥，也不该有。"""
    return ToolContext(
        conn=conn, session_id="view", node_id="view", task_id="view",
        account_id=account_id, secret=None,
    )


def _date_text(day: date | str | None) -> str | None:
    if day is None:
        return None
    return day.isoformat() if isinstance(day, date) else str(day)


def _start_of_month(day: date) -> int:
    return int(time.mktime((day.year, day.month, 1, 0, 0, 0, 0, 0, -1)) * 1000)


def _month_start_ms(year: int, month: int) -> int:
    """左闭右开窗口的左端；mktime 会把 month=13 归一化到次年 1 月。"""
    return int(time.mktime((year, month, 1, 0, 0, 0, 0, 0, -1)) * 1000)


def _accounts(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """全部账户；储蓄卡在前。信用卡不推算"可用额度"，那需要真实账单口径。"""
    rows = conn.execute(
        "SELECT * FROM accounts"
        " ORDER BY CASE type WHEN 'debit' THEN 0 ELSE 1 END, id"
    ).fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        view = queries.account_view(row)
        if view["type"] == "debit":
            # 可支配金额不是账面余额：在途 hold 和生日预算预留都不能再花。
            # 查询接口与资金写操作读同一个 funds.available_fen，避免两套口径。
            available = funds.available_fen(conn, str(row["id"]))
            view["available_fen"] = available
            view["available_yuan"] = fen_to_yuan_str(available)
            held = funds.held_fen(conn, str(row["id"]))
            reserved = funds.reserved_budget_fen(conn, str(row["id"]))
            view["held_fen"] = held
            view["held_yuan"] = fen_to_yuan_str(held)
            view["reserved_budget_fen"] = reserved
            view["reserved_budget_yuan"] = fen_to_yuan_str(reserved)
        else:
            view["available_fen"] = None
            view["available_yuan"] = None
            view["available_note"] = "可用额度要按当期账单口径算，这里不推算。"
        out.append(view)
    return out


def _tx_map(conn: sqlite3.Connection, ids: list[str]) -> dict[str, dict[str, Any]]:
    """按 id 取流水视图（异常卡片要能列出"相关流水"）。

    transactions.id 是文本主键（t_000000ec 这种），全程按字符串传递，
    不要为了排序或去重把它转成整数。
    """
    clean = sorted({str(i) for i in ids if i is not None})
    if not clean:
        return {}
    marks = ",".join("?" * len(clean))
    rows = conn.execute(
        f"SELECT * FROM transactions WHERE id IN ({marks})", clean
    ).fetchall()
    return {str(row["id"]): queries.tx_view(row) for row in rows}


def _comparison_view(raw: Any) -> dict[str, Any] | None:
    """异常里的「对比」：分和元都给，前端不用自己做除法。"""
    if not isinstance(raw, dict):
        return None
    out = dict(raw)
    for key in ("previous", "current"):
        fen = raw.get(f"{key}_fen")
        if isinstance(fen, int):
            out[f"{key}_yuan"] = fen_to_yuan_str(fen)
    return out


def _anomaly_view(
    item: dict[str, Any], index: int, tx_map: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    """把工具层的一条命中，补成前端能直接渲染、且用户能自己复核的卡片。"""
    kind = str(item.get("kind", ""))
    rule = ANOMALY_RULES.get(kind, {})
    amount_fen = int(item.get("amount_fen") or 0)
    tx_ids = [str(i) for i in (item.get("tx_ids") or [])]
    return {
        "id": f"{kind or 'anomaly'}-{index}",
        # 状态只有「发现异常」：规则命中不是欺诈认定，不能写成「已确认问题」。
        "status": "发现异常",
        "status_note": "规则命中只表示值得核实，需要你自己或客服确认。",
        "kind": kind,
        "title": str(item.get("title") or ""),
        "detail": str(item.get("detail") or ""),
        "amount_fen": amount_fen,
        "amount_yuan": fen_to_yuan_str(amount_fen),
        "merchant": item.get("merchant"),
        "occurrence_count": item.get("occurrence_count"),
        "window_start_ms": item.get("window_start_ms"),
        "window_start_text": timefmt.maybe_minute_text(item.get("window_start_ms")),
        "window_end_ms": item.get("window_end_ms"),
        "window_end_text": timefmt.maybe_minute_text(item.get("window_end_ms")),
        "window_text": (
            f"{timefmt.maybe_minute_text(item.get('window_start_ms'))} ~ {timefmt.maybe_minute_text(item.get('window_end_ms'))}"
            if item.get("window_start_ms") and item.get("window_end_ms")
            else None
        ),
        "comparison": _comparison_view(item.get("comparison")),
        "rule": {
            "id": rule.get("rule_id", ""),
            "text": rule.get("rule_text", ""),
            "severity": rule.get("severity", "info"),
            "confidence": rule.get("confidence", "low"),
        },
        "possible_false_positive": rule.get("possible_false_positive", ""),
        "evidence": {
            "tx_ids": tx_ids,
            "transactions": [tx_map[i] for i in tx_ids if i in tx_map],
        },
        "actions": list(rule.get("actions", [])),
    }


def _enrich_anomalies(
    conn: sqlite3.Connection, items: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    ids: list[str] = []
    for item in items:
        ids.extend(str(i) for i in (item.get("tx_ids") or []))
    tx_map = _tx_map(conn, ids)
    return [_anomaly_view(item, index, tx_map) for index, item in enumerate(items)]


def _trend(
    conn: sqlite3.Connection, account_id: str, months: int = DEFAULT_TREND_MONTHS
) -> list[dict[str, Any]]:
    """最近 N 个自然月的收支（含当月，当月标 partial）。

    口径：账户互转（本人卡之间、亲属卡）与理财申购赎回是钱在自己名下换地方，
    不计入收支——否则"钱从储蓄卡转到信用卡"会被算成一次收入加一次支出，趋势图立刻失真。
    对外转账（转给张三）是真花钱，照常计入支出。
    """
    months = max(1, min(int(months), MAX_TREND_MONTHS))
    today = proactive.local_date()
    keys: list[tuple[int, int]] = []
    year, month = today.year, today.month
    for _ in range(months):
        keys.append((year, month))
        month -= 1
        if month == 0:
            year, month = year - 1, 12
    keys.reverse()
    start_ms = _month_start_ms(*keys[0])
    end_ms = _month_start_ms(today.year, today.month + 1)
    rows = conn.execute(
        "SELECT direction, amount_fen, category, ts_ms FROM transactions"
        " WHERE account_id = ? AND ts_ms >= ? AND ts_ms < ?",
        (account_id, start_ms, end_ms),
    ).fetchall()
    buckets: dict[tuple[int, int], dict[str, int]] = {
        key: {"income_fen": 0, "expense_fen": 0, "count": 0} for key in keys
    }
    for row in rows:
        if str(row["category"]) in proactive.INTERNAL_CATEGORIES:
            continue
        tm = time.localtime(int(row["ts_ms"]) / 1000)
        key = (tm.tm_year, tm.tm_mon)
        bucket = buckets.get(key)
        if bucket is None:
            continue
        amount = int(row["amount_fen"])
        if str(row["direction"]) == "in":
            bucket["income_fen"] += amount
        else:
            bucket["expense_fen"] += amount
        bucket["count"] += 1
    out: list[dict[str, Any]] = []
    for key in keys:
        bucket = buckets[key]
        net = bucket["income_fen"] - bucket["expense_fen"]
        out.append(
            {
                "month": f"{key[0]:04d}-{key[1]:02d}",
                "label": f"{key[1]} 月",
                "income_fen": bucket["income_fen"],
                "income_yuan": fen_to_yuan_str(bucket["income_fen"]),
                "expense_yuan": fen_to_yuan_str(bucket["expense_fen"]),
                "net_fen": net,
                # 亏损月要带负号：以前这里取了 abs()，趋势表会把 -1,311.00 显示成
                # 1,311.00，和同页红色的 -1,311.00 并存，同一屏两个结余。
                "net_yuan": fen_to_yuan_str(net),
                "net_positive": net >= 0,
                "transaction_count": bucket["count"],
                "partial": key == (today.year, today.month),
            }
        )
    return out


def _bill_block(
    conn: sqlite3.Connection, account_id: str | None, year: int, month: int
) -> dict[str, Any]:
    """当期账单：直接复用 analyze_bill，避免首页和账单页各算一套数字。"""
    ctx = _ctx(conn, account_id)
    result = bill.analyze_bill(ctx, {"period": "month", "year": int(year), "month": int(month)})
    data = dict(result.data or {})
    anomalies = _enrich_anomalies(conn, list(data.get("anomalies") or []))
    data["anomalies"] = anomalies
    data["anomaly_count"] = len(anomalies)
    return data


