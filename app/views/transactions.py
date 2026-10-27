"""流水明细：一张卡在一段时间里的完整流水（分页 + 过滤）。只读。

账单页的「相关流水」只列命中规则的证据流水，不是对账单。用户想按卡、按月、
按方向/分类/关键词翻自己的账时得能看到全量，并且知道这一屏合起来是多少钱，
所以这里补的是那个缺口：完整流水 + 合计 + 分页，口径与账单页同一张卡。
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Any

from app import timefmt
from app.clock import ValidationError, now_ms
from app.money import INTERNAL_CATEGORIES, fen_to_yuan_str
from app.subscriptions import CONFIDENCE_LABELS
from app.tools import queries
from app.tools.bill import anomalies_for_month
from app.views.bill import resolve_account
from app.views.labels import ANOMALY_RULES
from app.views.money import _accounts

DEFAULT_LIMIT: int = 30
MAX_LIMIT: int = 100
PERIODS: tuple[str, ...] = ("month", "year")
DIRECTIONS: tuple[str, ...] = ("in", "out")
NOTE: str = (
    "只列这张卡在这个时间段里的流水；账户互转与理财也在里面，"
    "它们只是不算收支（账单页的合计与这里的方向合计口径一致）。"
)
ANOMALY_NOTE: str = (
    "标红的是命中账单规则的流水，点一下能看是哪条规则、凭什么命中；"
    "规则命中只说明值得核实，不是欺诈认定。"
)


def _ledger_month_start(year: int, month: int) -> int:
    return int(datetime(year, month, 1).timestamp() * 1000)


def _range(period: str, year: int, month: int | None) -> tuple[int, int, str]:
    """返回 [start, end) 与一句人话标签；都用本地时区，和账单页同口径。"""
    if period == "year":
        return _ledger_month_start(year, 1), _ledger_month_start(year + 1, 1), f"{year} 年全年"
    assert month is not None
    start = _ledger_month_start(year, month)
    end = _ledger_month_start(year + 1, 1) if month == 12 else _ledger_month_start(year, month + 1)
    return start, end, f"{year} 年 {month} 月"


def _like(text: str) -> str:
    """LIKE 通配符转义：用户输入里的 % 和 _ 不能当通配符跑。"""
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _where(account_id: str, start_ms: int, end_ms: int, *, direction: str | None,
           category: str | None, q: str) -> tuple[str, list[Any]]:
    sql = " WHERE account_id = ? AND ts_ms >= ? AND ts_ms < ?"
    args: list[Any] = [account_id, start_ms, end_ms]
    if direction:
        sql += " AND direction = ?"
        args.append(direction)
    if category:
        sql += " AND category = ?"
        args.append(category)
    if q:
        pattern = f"%{_like(q)}%"
        sql += " AND (counterparty LIKE ? ESCAPE '\\' OR note LIKE ? ESCAPE '\\')"
        args.extend([pattern, pattern])
    return sql, args


def _totals(conn: sqlite3.Connection, where: str, args: list[Any]) -> dict[str, int]:
    """合计给两套：主口径（账户互转/理财不算收支，与账单页一致）和内部搬运那部分。"""
    marks = ", ".join("?" for _ in INTERNAL_CATEGORIES)
    sums = {"in": 0, "out": 0, "internal_in": 0, "internal_out": 0}
    for row in conn.execute(
        "SELECT direction, SUM(amount_fen) AS total,"
        f" SUM(CASE WHEN category IN ({marks}) THEN amount_fen ELSE 0 END) AS internal"
        " FROM transactions" + where + " GROUP BY direction",
        [*INTERNAL_CATEGORIES, *args],
    ).fetchall():
        side = "in" if str(row["direction"]) == "in" else "out"
        sums[side] = int(row["total"] or 0)
        sums["internal_" + side] = int(row["internal"] or 0)
    return sums


def _scope(conn: sqlite3.Connection, period: str, year: int | None,
           month: int | None, stamp: int) -> tuple[str, int, int | None, int, int, str]:
    """校验时间范围，返回归一后的 (period, year, month, start, end, label)。"""
    today = datetime.fromtimestamp(stamp / 1000)
    period = (period or "month").strip().lower()
    if period not in PERIODS:
        raise ValidationError("时间范围只能选本月或本年。", reason="BAD_PERIOD", field="period")
    year = int(year or today.year)
    if period == "year":
        month = None
    else:
        month = int(month or today.month)
        if not 1 <= month <= 12:
            raise ValidationError("月份得在 1 到 12 之间。", reason="BAD_MONTH", field="month")
    start_ms, end_ms, label = _range(period, year, month)
    return period, year, month, start_ms, end_ms, label


def _flag_brief(item: dict[str, Any], index: int) -> dict[str, Any]:
    """把工具层一条命中压成「这一笔属于哪条异常」的小卡片，逐笔挂在流水上。"""
    kind = str(item.get("kind") or "")
    rule = ANOMALY_RULES.get(kind, {})
    confidence = str(rule.get("confidence", "low"))
    amount_fen = int(item.get("amount_fen") or 0)
    return {
        "id": f"{kind or 'anomaly'}-{index}",
        "kind": kind,
        "title": str(item.get("title") or ""),
        "detail": str(item.get("detail") or ""),
        "amount_fen": amount_fen,
        "amount_yuan": fen_to_yuan_str(amount_fen),
        "tx_count": len(item.get("tx_ids") or []),
        "rule_id": str(rule.get("rule_id", "")),
        "rule_text": str(rule.get("rule_text", "")),
        "confidence": confidence,
        "confidence_label": CONFIDENCE_LABELS.get(confidence, "低"),
        "possible_false_positive": str(rule.get("possible_false_positive", "")),
    }


def _anomaly_marks(
    conn: sqlite3.Connection, account_id: str, rows: list[sqlite3.Row]
) -> dict[str, dict[str, Any]]:
    """这一页流水里，哪些笔命中了异常：按笔所在月份现算一次，逐笔挂标记。

    月份只取这一页出现过的，最多两三个月；检测逻辑与账单页共用
    bill.anomalies_for_month，避免「账单页说异常、明细里不标红」。
    """
    months: set[tuple[int, int]] = set()
    for row in rows:
        tm = datetime.fromtimestamp(int(row["ts_ms"]) / 1000)
        months.add((tm.year, tm.month))
    marks: dict[str, dict[str, Any]] = {}
    for year, month in sorted(months):
        found = anomalies_for_month(conn, account_id, year, month)
        for index, item in enumerate(found):
            brief = _flag_brief(item, index)
            for tx_id in item.get("tx_ids") or []:
                marks.setdefault(str(tx_id), brief)
    return marks


def _page_data(
    conn: sqlite3.Connection,
    account_id: str,
    where: str,
    args: list[Any],
    limit: int,
    offset: int,
) -> tuple[int, dict[str, int], list[dict[str, Any]]]:
    """取这一页要用到的三样：总数、合计、逐笔（带「属于哪条异常」的标记）。"""
    total = int(conn.execute(
        "SELECT COUNT(*) AS n FROM transactions" + where, args
    ).fetchone()["n"])
    sums = _totals(conn, where, args)
    rows = conn.execute(
        "SELECT * FROM transactions" + where + " ORDER BY ts_ms DESC, id DESC LIMIT ? OFFSET ?",
        [*args, limit, offset],
    ).fetchall()
    items = [queries.tx_view(row) for row in rows]
    marks = _anomaly_marks(conn, account_id, rows)
    for item in items:
        item["anomaly"] = marks.get(str(item["transaction_id"]))
    return total, sums, items


def transactions_view(
    conn: sqlite3.Connection,
    *,
    account_id: str | None = None,
    period: str = "month",
    year: int | None = None,
    month: int | None = None,
    direction: str | None = None,
    category: str | None = None,
    q: str = "",
    limit: int = DEFAULT_LIMIT,
    offset: int = 0,
) -> dict[str, Any]:
    """按卡 + 时间段翻流水：命中多少笔、合计多少、这一页是哪些。"""
    stamp = now_ms()
    period, year, month, start_ms, end_ms, label = _scope(conn, period, year, month, stamp)
    direction = (direction or "").strip().lower() or None
    if direction is not None and direction not in DIRECTIONS:
        raise ValidationError("方向只能选收入或支出。", reason="BAD_DIRECTION", field="direction")
    category = (category or "").strip() or None
    q = (q or "").strip()
    limit = max(1, min(int(limit or DEFAULT_LIMIT), MAX_LIMIT))
    offset = max(0, int(offset or 0))

    account_id, account = resolve_account(conn, account_id)
    where, args = _where(account_id, start_ms, end_ms,
                         direction=direction, category=category, q=q)
    total, sums, items = _page_data(conn, account_id, where, args, limit, offset)
    in_fen = int(sums["in"]) - int(sums["internal_in"])
    out_fen = int(sums["out"]) - int(sums["internal_out"])
    net_fen = in_fen - out_fen
    internal_out_fen = int(sums["internal_out"])
    internal_in_fen = int(sums["internal_in"])
    return {
        "ok": True,
        "generated_text": timefmt.maybe_minute_text(stamp),
        "period": period,
        "year": year,
        "month": month,
        "range": {
            "label": label,
            "start_ms": start_ms,
            "start_text": timefmt.stamp_text(start_ms),
            "end_ms": end_ms,
            "end_text": timefmt.stamp_text(end_ms),
        },
        "account": account,
        "accounts": _accounts(conn),
        "filters": {
            "direction": direction or "",
            "category": category or "",
            "q": q,
            "limit": limit,
            "offset": offset,
        },
        "items": items,
        "count": len(items),
        "anomaly_count": sum(1 for item in items if item.get("anomaly")),
        "anomaly_note": ANOMALY_NOTE,
        "total": total,
        "has_more": offset + len(items) < total,
        "out_fen": out_fen,
        "out_yuan": fen_to_yuan_str(out_fen),
        "in_fen": in_fen,
        "in_yuan": fen_to_yuan_str(in_fen),
        "net_fen": net_fen,
        "net_yuan": fen_to_yuan_str(net_fen),
        "internal_out_fen": internal_out_fen,
        "internal_out_yuan": fen_to_yuan_str(internal_out_fen),
        "internal_in_fen": internal_in_fen,
        "internal_in_yuan": fen_to_yuan_str(internal_in_fen),
        "note": NOTE,
    }
