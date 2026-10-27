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
from app.subscriptions import CONFIDENCE_LABELS
from app.tools import bill
from app.tools import queries
from app.money import fen_to_yuan_str
from app.tools.base import ToolContext, primary_account

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


def _anomaly_prompt(view: dict[str, Any]) -> str:
    """按钮发出的那句话要带这一笔的身份：时间、商户、金额、流水号。

    以前是 labels 里一句固定口吻（「帮我核实一下凌晨那笔境外消费…」），卡片上明明有
    商户和流水号，点下去却只剩一句「那笔」——服务端只能靠猜；同一个月有两笔凌晨境外时，
    必然猜错。这里按实例拼，服务端就能精确到行。
    """
    kind = str(view.get("kind") or "")
    merchant = str(view.get("merchant") or view.get("title") or "这笔消费")
    tx_ids = [str(i) for i in (view.get("evidence") or {}).get("tx_ids") or []]
    tail = f"，流水号 {'、'.join(tx_ids)}" if tx_ids else ""
    when = str(view.get("window_start_text") or "")
    if not when:
        rows = (view.get("evidence") or {}).get("transactions") or []
        when = str(rows[0].get("ts_text") or "") if rows else "最近"
    if kind == "subscription_hike":
        comparison = view.get("comparison") or {}
        current = comparison.get("current_yuan") or view.get("amount_yuan")
        previous = comparison.get("previous_yuan")
        extra = f"（上月 {previous} 元）" if previous else ""
        return f"帮我核实一下 {merchant} 这个月的扣费：{current} 元{extra}{tail}，是不是涨价了。"
    if kind == "split_orders":
        count = view.get("occurrence_count") or len(tx_ids)
        return (
            f"帮我核实一下 {merchant} {when} 这几笔是不是拆单："
            f"共 {count} 笔、合计 {view.get('amount_yuan')} 元{tail}。"
        )
    return (
        f"帮我核实一下 {when} 的这笔消费：{merchant} {view.get('amount_yuan')} 元"
        f"{tail}，是不是我自己刷的。"
    )


def _anomaly_actions(view: dict[str, Any], account_id: str) -> list[dict[str, Any]]:
    """规则自带动作照发；只有「交给助手」那类，把口吻换成这一笔的具体事实。

    末尾再补一条「我已核实」：规则命中只是线索，用户看过之后必须能把它从
    待办里拿掉，否则同一条会永远挂在首页。
    """
    rule = ANOMALY_RULES.get(str(view.get("kind") or ""), {})
    actions: list[dict[str, Any]] = []
    for raw in rule.get("actions", []):
        action = dict(raw)
        if str(action.get("kind")) == "prompt":
            action["prompt"] = _anomaly_prompt(view)
        actions.append(action)
    # 两档结论：自己刷的 / 不是我刷的。「不是我刷的」不是一句口头结论——它是冻结卡、
    # 找银行争议的起点，所以必须单独落库，不能被「我已核实」一档吞掉。
    base = {
        "rule_kind": str(view.get("kind") or ""),
        "account_id": account_id,
        "tx_ids": list((view.get("evidence") or {}).get("tx_ids") or []),
    }
    actions.append(
        {
            **base,
            "id": "ack_mine",
            "label": "是我自己刷的",
            "kind": "ack_anomaly",
            "verdict": "mine",
            "note": "点完就从首页收件箱、账单页和明细标红里消失；流水本身不动。",
        }
    )
    actions.append(
        {
            **base,
            "id": "ack_not_mine",
            "label": "不是我刷的",
            "kind": "ack_anomaly",
            "verdict": "not_mine",
            "note": "记下结论后先临时冻结这张卡、保留流水去找银行；钱和流水都不会动。",
        }
    )
    return actions


def _anomaly_view(
    item: dict[str, Any],
    index: int,
    tx_map: dict[str, dict[str, Any]],
    account_id: str = "",
) -> dict[str, Any]:
    """把工具层的一条命中，补成前端能直接渲染、且用户能自己复核的卡片。"""
    kind = str(item.get("kind", ""))
    rule = ANOMALY_RULES.get(kind, {})
    amount_fen = int(item.get("amount_fen") or 0)
    tx_ids = [str(i) for i in (item.get("tx_ids") or [])]
    view = {
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
            # 页面上只出现中文把握度；原始枚举留着给前端分组用。
            "confidence_label": CONFIDENCE_LABELS.get(rule.get("confidence", "low"), "低"),
        },
        "possible_false_positive": rule.get("possible_false_positive", ""),
        "evidence": {
            "tx_ids": tx_ids,
            "transactions": [tx_map[i] for i in tx_ids if i in tx_map],
        },
        "actions": [],
    }
    if kind == "subscription_hike":
        # 涨价异常在检测层记的是差额（10 元）；卡片上要显示的是这个月实际扣了多少，
        # 差额写成副标——否则「10.00 待核实」会让人以为这月只扣了 10 元。
        comparison = view.get("comparison") or {}
        if isinstance(comparison.get("current_fen"), int):
            view["amount_fen"] = comparison["current_fen"]
            view["amount_yuan"] = comparison["current_yuan"]
            view["amount_note"] = f"上月 {comparison.get('previous_yuan')} 元"
    view["actions"] = _anomaly_actions(view, account_id)
    return view


def _enrich_anomalies(
    conn: sqlite3.Connection,
    items: list[dict[str, Any]],
    account_id: str = "",
) -> list[dict[str, Any]]:
    ids: list[str] = []
    for item in items:
        ids.extend(str(i) for i in (item.get("tx_ids") or []))
    tx_map = _tx_map(conn, ids)
    return [
        _anomaly_view(item, index, tx_map, account_id)
        for index, item in enumerate(items)
    ]


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
    effective = str(account_id or primary_account(conn)["id"])
    anomalies = _enrich_anomalies(conn, list(data.get("anomalies") or []), effective)
    data["anomalies"] = anomalies
    data["anomaly_count"] = len(anomalies)
    return data


