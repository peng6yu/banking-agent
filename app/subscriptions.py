"""从流水里推断"疑似订阅"，以及算下一次续费提醒。

为什么要单独一个模块：银行的代扣记录（subs 表）只知道"我这边有授权"，
真实世界大量订阅并不走本行代扣，或者用户根本没在本行登记。所以
list_subs 只能列已知的，剩下的要靠流水推断。

三条硬边界，代码里照着写，界面上也要照着写：
1. 推断不是事实。命中只说明"这几笔看起来像周期性扣款"，可能与会员订阅无关
   （比如每月固定话费、房租、保险），所以每条候选都要带证据和不确定性说明；
2. 取消订阅只能停银行侧，不能替用户在商户那里退订（见 CANCEL_BOUNDARY_TEXT）；
3. 续费提醒是"打开页面时按 next_charge_ms 算出来的"，不是推送。没有后台推送
   渠道就是没有，不能写成"已提醒你"。

全部算法确定性：只按时间与金额排序聚合，不用随机、不依赖当前时间以外的输入。
"""

from __future__ import annotations

import time
from typing import Any, Final

import app.audit as audit
import app.db as db
import app.timefmt as timefmt
from app.clock import ValidationError, now_ms
from app.money import fen_to_yuan_str

#: 一次推断看多少个月。窗口越长越容易识别，也越容易把"换工作后固定转账"算进来。
LOOKBACK_MONTHS: Final[int] = 6
#: 至少出现几次才值得提示。2 次太容易撞上巧合（比如两次搬家）。
MIN_OCCURRENCES: Final[int] = 3
#: 相邻两笔的间隔落在这些区间才算月付 / 年付。
MONTHLY_GAP_DAYS: Final[tuple[int, int]] = (25, 35)
YEARLY_GAP_DAYS: Final[tuple[int, int]] = (350, 380)
#: 金额波动容忍度：最高一笔 / 最低一笔 <= 1.25 就算"差不多同一笔钱"。
AMOUNT_SPREAD: Final[float] = 1.25
#: 不算订阅的分类：转账是人对人，理财是本人内部划转。
EXCLUDED_CATEGORIES: Final[tuple[str, ...]] = ("转账", "理财")
#: 续费提醒默认提前几天。
UPCOMING_DAYS: Final[int] = 7
MAX_CANDIDATES: Final[int] = 10

CONFIDENCE_LABELS: Final[dict[str, str]] = {
    "high": "高",
    "medium": "中",
    "low": "低",
}
INFERRED_NOTE: Final[str] = (
    "这些是从流水里猜出来的，不是本行的代扣授权记录：可能是会员订阅，"
    "也可能只是每月固定的房租、话费或保险。点开能看到每一笔流水，自己核对之后再决定要不要处理。"
)
UPCOMING_NOTE: Final[str] = (
    "续费提醒是按代扣记录里的下次扣费日期算的，打开页面时才计算，不会推送给你；"
    "想提前处理，可以先停掉银行侧代扣。"
)

#: 停掉银行侧代扣之后必须一直说清的那句边界：停的只是本行代扣，商户会员还在。
STOPPED_BOUNDARY_TEXT: Final[str] = (
    "只停了银行侧周期性代扣，商户会员需要你自己去商户端取消"
)

_DAY_MS: Final[int] = 86_400_000


def _month_start(ts_ms: int, months_back: int = 0) -> int:
    st = time.localtime(ts_ms / 1000)
    month = st.tm_mon - months_back
    year = st.tm_year
    while month <= 0:
        month += 12
        year -= 1
    return int(time.mktime((year, month, 1, 0, 0, 0, 0, -1, -1)) * 1000)


def _period_text(period: str) -> str:
    """周期口径中文：monthly → 每月，yearly → 每年。"""
    return "每月" if str(period) == "monthly" else "每年"


def _status_text(status: str) -> str:
    """订阅状态中文：active → 生效中，其余（只会有 cancelled）→ 已停止（银行侧）。"""
    return "生效中" if str(status) == "active" else "已停止（银行侧）"


def _days_between(earlier_ms: int, later_ms: int) -> float:
    return round((later_ms - earlier_ms) / _DAY_MS, 1)


def _rows(conn, account_id: str, since_ms: int) -> list[Any]:
    return conn.execute(
        "SELECT * FROM transactions"
        " WHERE account_id = ? AND direction = 'out' AND ts_ms >= ?"
        " AND reversal_of IS NULL AND reversed_by IS NULL"
        " ORDER BY counterparty, ts_ms, id",
        (account_id, since_ms),
    ).fetchall()


def _cadence(gaps: list[float]) -> str | None:
    lo, hi = MONTHLY_GAP_DAYS
    if all(lo <= gap <= hi for gap in gaps):
        return "monthly"
    lo_y, hi_y = YEARLY_GAP_DAYS
    if all(lo_y <= gap <= hi_y for gap in gaps):
        return "yearly"
    return None


def _confidence(rows: list[Any], gaps: list[float], spread: float) -> str:
    if len(rows) >= 4 and spread <= 1.10 and max(gaps) - min(gaps) <= 4:
        return "high"
    if len(rows) >= 3 and spread <= AMOUNT_SPREAD:
        return "medium"
    return "low"


def _candidate(rows: list[Any]) -> dict[str, Any] | None:
    amounts = [int(row["amount_fen"]) for row in rows]
    low, high = min(amounts), max(amounts)
    if low <= 0:
        return None
    spread = round(high / low, 3)
    if spread > AMOUNT_SPREAD:
        return None
    times = [int(row["ts_ms"]) for row in rows]
    gaps = [_days_between(times[i], times[i + 1]) for i in range(len(times) - 1)]
    period = _cadence(gaps)
    if period is None:
        return None
    confidence = _confidence(rows, gaps, spread)
    avg_fen = round(sum(amounts) / len(amounts))
    merchant = str(rows[0]["counterparty"])
    gap_text = f"{min(gaps):g}—{max(gaps):g} 天" if len(gaps) > 1 else f"{gaps[0]:g} 天"
    period_label = "每月" if period == "monthly" else "每年"
    spread_text = "金额基本一致" if spread <= 1.02 else f"金额在 {fen_to_yuan_str(low)}—{fen_to_yuan_str(high)} 元之间浮动"
    return {
        "merchant": merchant,
        "period": period,
        "period_label": period_label,
        "occurrences": len(rows),
        "avg_amount_fen": avg_fen,
        "avg_amount_yuan": fen_to_yuan_str(avg_fen),
        "last_amount_fen": amounts[-1],
        "last_amount_yuan": fen_to_yuan_str(amounts[-1]),
        "first_ms": times[0],
        "first_text": timefmt.date_text(times[0]),
        "last_ms": times[-1],
        "last_text": timefmt.date_text(times[-1]),
        "interval_days_min": min(gaps),
        "interval_days_max": max(gaps),
        "confidence": confidence,
        "confidence_label": CONFIDENCE_LABELS[confidence],
        "basis": (
            f"{merchant} 在 {timefmt.date_text(times[0])} 到 {timefmt.date_text(times[-1])} 之间共 {len(rows)} 笔，"
            f"间隔 {gap_text}（{period_label}），{spread_text}。"
        ),
        "uncertainty": (
            "只是时间与金额规律相符，不能证明这是订阅；固定房租、话费、保险也会这样。"
            if period == "monthly"
            else "一年才出现一次的支出样本很少，可能是续保或会员年费，也可能是一次性消费。"
        ),
        "evidence": [
            {
                "transaction_id": str(row["id"]),
                "ts_ms": int(row["ts_ms"]),
                "time_text": timefmt.date_text(int(row["ts_ms"])),
                "amount_fen": int(row["amount_fen"]),
                "amount_yuan": fen_to_yuan_str(int(row["amount_fen"])),
                "category": str(row["category"]),
                "note": row["note"],
            }
            for row in rows
        ],
    }


#: 流水渠道的中文说法：逐笔核对时要说清楚每一笔是怎么进账的。
CHANNEL_LABELS: Final[dict[str, str]] = {
    "app": "手机银行",
    "auto": "自动扣款",
    "bank": "柜面",
    "credit": "信用卡",
    "debit": "借记卡",
    "manual": "手动补录",
    "overseas": "境外",
    "pos": "刷卡",
    "simulated": "演示注入",
}


def merchant_transactions(
    conn,
    account_id: str,
    merchant: str,
    *,
    lookback_months: int = LOOKBACK_MONTHS,
    as_of: int | None = None,
    limit: int = 12,
) -> list[dict[str, Any]]:
    """某个商户的逐笔支出流水（只读）。

    为什么单独一个函数：用户问"帮我核实一下这几笔"时，用户要看的是**每一笔**
    的日期、金额、备注原文，而不是一句"3 笔、间隔 30 天、金额基本一致"的结论。
    结论是推断，逐笔是事实；两者必须分开呈现，用户才能自己判断。

    这里不做任何周期性判断（那是 detect_recurring 的事），也不看用户定性过
    什么——定性只影响"要不要继续当成待办"，不影响"这笔流水确实存在"。
    """
    name = str(merchant or "").strip()
    if not name:
        raise ValidationError("要看哪个商户的流水？")
    at = int(as_of) if as_of is not None else now_ms()
    since = _month_start(at, lookback_months)
    rows = [
        row
        for row in _rows(conn, account_id, since)
        if str(row["counterparty"]) == name
    ]
    rows.sort(key=lambda row: (int(row["ts_ms"]), str(row["id"])), reverse=True)
    out: list[dict[str, Any]] = []
    for row in rows[: max(1, limit)]:
        channel = str(row["channel"])
        out.append(
            {
                "transaction_id": str(row["id"]),
                "ts_ms": int(row["ts_ms"]),
                "time_text": timefmt.date_text(int(row["ts_ms"])),
                "amount_fen": int(row["amount_fen"]),
                "amount_yuan": fen_to_yuan_str(int(row["amount_fen"])),
                "category": str(row["category"]),
                "channel": channel,
                "channel_label": CHANNEL_LABELS.get(channel, channel),
                "note": row["note"],
            }
        )
    return out


def merchants_with_transactions(
    conn, account_id: str, *, lookback_months: int = LOOKBACK_MONTHS, as_of: int | None = None
) -> list[dict[str, Any]]:
    """窗口内出现过支出的商户名与笔数（只读）：用户只说了商户名时用来对名字。

    只按流水说话，不掺杂"像不像订阅"——所以一个从没被推断成订阅的商户
    （比如只在窗口里买了一本书）也能在这里被找到并逐笔核对。
    """
    at = int(as_of) if as_of is not None else now_ms()
    since = _month_start(at, lookback_months)
    counts: dict[str, int] = {}
    for row in _rows(conn, account_id, since):
        name = str(row["counterparty"])
        counts[name] = counts.get(name, 0) + 1
    return [
        {"merchant": name, "count": count}
        for name, count in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    ]


def detect_recurring(
    conn,
    account_id: str,
    *,
    lookback_months: int = LOOKBACK_MONTHS,
    as_of: int | None = None,
    user_id: str | None = None,
) -> dict[str, Any]:
    """按商户分组找周期性支出，返回候选清单（永不改库）。

    两类商户会被排除在待办之外，理由都是「用户已经处理完了」：
    1. user_id 已经给过结论的：不论他说「不是我订的」还是「确实是我订的」，
       这条推断都不再待办——退订误报和确认订阅都算处理完了，继续挂在这里
       只会让列表永远清不掉；
    2. subs 表里 status='cancelled' 的：停掉银行侧代扣就等于用户已经把这条
       代扣处理掉了，不该还挂在「疑似订阅」里像没管过。注意只排除已停止的，
       active 的照旧留在推断结果里——用户可能还想核对、或者把它停掉。
    """
    as_of = int(as_of if as_of is not None else time.time() * 1000)
    since = _month_start(as_of, lookback_months)
    groups: dict[str, list[Any]] = {}
    for row in _rows(conn, account_id, since):
        if str(row["category"]) in EXCLUDED_CATEGORIES:
            continue
        if str(row["channel"]) == "simulated":
            continue
        groups.setdefault(str(row["counterparty"]), []).append(row)

    settled = (
        {
            merchant
            for merchant, item in verdicts(conn, str(user_id)).items()
            if item.get("verdict") in VERDICT_ACTIONS
        }
        if user_id
        else set()
    )
    # 已经停掉银行侧代扣的商户：用户处理完了，不该再当待办。
    # 这里刻意不排除 status='active' 的商户——那才是还需要用户决定的那批。
    stopped = {
        str(row["merchant"])
        for row in conn.execute(
            "SELECT DISTINCT merchant FROM subs WHERE account_id = ? AND status = 'cancelled'",
            (account_id,),
        ).fetchall()
    }
    candidates: list[dict[str, Any]] = []
    for merchant in sorted(groups):
        if merchant in settled or merchant in stopped:
            continue
        rows = groups[merchant]
        if len(rows) < MIN_OCCURRENCES:
            continue
        item = _candidate(rows)
        if item is None:
            continue
        candidates.append(item)

    order = {"high": 0, "medium": 1, "low": 2}
    candidates.sort(key=lambda c: (order[c["confidence"]], -c["occurrences"], c["merchant"]))
    candidates = candidates[:MAX_CANDIDATES]
    return {
        "items": candidates,
        "count": len(candidates),
        "lookback_months": lookback_months,
        "as_of_ms": as_of,
        "note": INFERRED_NOTE,
        "boundary": "推断结果不是本行代扣授权，也不是商户会员状态；处理前请先核对每一笔流水。",
    }


def upcoming(
    conn, account_id: str, *, within_days: int = UPCOMING_DAYS, as_of: int | None = None
) -> dict[str, Any]:
    """未来 within_days 天内会扣费的已知订阅（只读，按 next_charge_ms 算）。"""
    as_of = int(as_of if as_of is not None else time.time() * 1000)
    until = as_of + within_days * _DAY_MS
    rows = conn.execute(
        "SELECT * FROM subs WHERE account_id = ? AND status = 'active'"
        " AND next_charge_ms >= ? AND next_charge_ms <= ? ORDER BY next_charge_ms, id",
        (account_id, as_of - _DAY_MS, until),
    ).fetchall()
    items = [
        {
            "sub_id": str(row["id"]),
            "merchant": str(row["merchant"]),
            "amount_fen": int(row["amount_fen"]),
            "amount_yuan": fen_to_yuan_str(int(row["amount_fen"])),
            "period": str(row["period"]),
            "next_charge_ms": int(row["next_charge_ms"]),
            "next_charge_text": timefmt.date_text(int(row["next_charge_ms"])),
            "days_left": round((int(row["next_charge_ms"]) - as_of) / _DAY_MS, 1),
        }
        for row in rows
    ]
    return {
        "items": items,
        "count": len(items),
        "within_days": within_days,
        "note": UPCOMING_NOTE,
        "action": "想停就在「业务管理 · 订阅」里停掉银行侧代扣；停不了商户会员本身。",
    }


def stopped_debits(conn, account_id: str) -> list[dict[str, Any]]:
    """本账户下已经停掉银行侧代扣的商户（只读，按商户名排序）。

    停掉银行侧代扣等于用户已经处理完了：这些商户不再出现在「疑似订阅」待办里，
    改成列在「已停止银行侧代扣」一栏，让用户看得到自己处理过什么、还差哪一步。

    boundary_text 是固定口径，不随记录变化：停的只是本行的周期性代扣，商户那边的
    会员/订阅本身还在，得用户自己去商户端取消。别把它写成「已取消订阅」。
    """
    rows = conn.execute(
        "SELECT * FROM subs WHERE account_id = ? AND status = 'cancelled'"
        " ORDER BY merchant, id",
        (account_id,),
    ).fetchall()
    items: list[dict[str, Any]] = []
    for row in rows:
        amount_fen = int(row["amount_fen"])
        period = str(row["period"])
        status = str(row["status"])
        items.append(
            {
                "sub_id": str(row["id"]),
                "merchant": str(row["merchant"]),
                "amount_fen": amount_fen,
                "amount_yuan": fen_to_yuan_str(amount_fen),
                "period": period,
                "period_label": _period_text(period),
                "next_charge_text": timefmt.date_text(int(row["next_charge_ms"])),
                "status": status,
                "status_label": _status_text(status),
                "boundary_text": STOPPED_BOUNDARY_TEXT,
            }
        )
    return items


#: 用户对「疑似订阅」只能做两种定性。没有第三种：不能替用户在商户退订，
#: 也不能把推断直接写成已确认。
VERDICT_NOT_SUBSCRIPTION: Final[str] = "not_subscription"
VERDICT_CONFIRMED: Final[str] = "confirmed"
VERDICT_ACTIONS: Final[tuple[str, ...]] = (VERDICT_NOT_SUBSCRIPTION, VERDICT_CONFIRMED)
VERDICT_LABELS: Final[dict[str, str]] = {
    VERDICT_NOT_SUBSCRIPTION: "不是我订的",
    VERDICT_CONFIRMED: "确实是我订的",
}


def verdicts(conn, user_id: str) -> dict[str, dict[str, Any]]:
    """用户对疑似订阅的定性结论：merchant -> 结论（按用户隔离）。"""
    rows = conn.execute(
        "SELECT * FROM subscription_verdicts WHERE user_id = ? ORDER BY updated_ms DESC",
        (str(user_id),),
    ).fetchall()
    return {
        str(row["merchant"]): {
            "merchant": str(row["merchant"]),
            "verdict": str(row["verdict"]),
            "verdict_label": VERDICT_LABELS.get(str(row["verdict"]), str(row["verdict"])),
            "note": str(row["note"] or ""),
            "updated_ms": int(row["updated_ms"]),
            "updated_text": timefmt.date_text(int(row["updated_ms"])),
        }
        for row in rows
    }


def record_verdict(
    conn, *, user_id: str, merchant: str, verdict: str, note: str = ""
) -> dict[str, Any]:
    """记下用户对一条疑似订阅的定性，返回结论视图（同时写审计）。

    这**不是**取消订阅：确认是订阅只意味着以后不再当误报；真要退订只能停
    银行侧代扣，商户那边的会员得用户自己去退（见 CANCEL_BOUNDARY_TEXT 所在
    upcoming() 的 action 文案）。
    """
    act = str(verdict or "").strip().lower()
    if act not in VERDICT_ACTIONS:
        raise ValidationError("这个定性不认识", verdict=verdict, allowed=list(VERDICT_ACTIONS))
    name = str(merchant or "").strip()
    if not name:
        raise ValidationError("要指明是哪家商户", merchant=merchant)
    stamp = now_ms()
    with db.tx(conn):
        conn.execute(
            "INSERT INTO subscription_verdicts (user_id, merchant, verdict, note, created_ms, updated_ms)"
            " VALUES (?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(user_id, merchant) DO UPDATE SET"
            " verdict = excluded.verdict, note = excluded.note, updated_ms = excluded.updated_ms",
            (str(user_id), name, act, str(note or "")[:200], stamp, stamp),
        )
        audit.append(
            conn,
            "subscription.verdict",
            {"user_id": str(user_id), "merchant": name, "verdict": act},
        )
    return {
        "merchant": name,
        "verdict": act,
        "verdict_label": VERDICT_LABELS[act],
        "note": str(note or "")[:200],
        "updated_ms": stamp,
        "updated_text": timefmt.date_text(stamp),
    }


def clear_verdict(conn, *, user_id: str, merchant: str) -> dict[str, Any]:
    """撤掉之前的定性，让这条重新回到「疑似订阅」待办（同时写审计）。

    定性是用户自己下的，就必须能自己收回：点错了、或者商户后来改成了年费，
    都不该被一条旧结论永久钉住。
    """
    name = str(merchant or "").strip()
    if not name:
        raise ValidationError("要指明是哪家商户", merchant=merchant)
    with db.tx(conn):
        cur = conn.execute(
            "DELETE FROM subscription_verdicts WHERE user_id = ? AND merchant = ?",
            (str(user_id), name),
        )
        removed = int(cur.rowcount or 0)
        audit.append(
            conn,
            "subscription.verdict_cleared",
            {"user_id": str(user_id), "merchant": name, "removed": removed},
        )
    return {"merchant": name, "cleared": bool(removed)}


__all__ = [
    "CONFIDENCE_LABELS",
    "INFERRED_NOTE",
    "LOOKBACK_MONTHS",
    "MIN_OCCURRENCES",
    "UPCOMING_DAYS",
    "UPCOMING_NOTE",
    "VERDICT_ACTIONS",
    "VERDICT_CONFIRMED",
    "VERDICT_LABELS",
    "VERDICT_NOT_SUBSCRIPTION",
    "CHANNEL_LABELS",
    "clear_verdict",
    "detect_recurring",
    "merchant_transactions",
    "merchants_with_transactions",
    "record_verdict",
    "stopped_debits",
    "upcoming",
    "verdicts",
]
