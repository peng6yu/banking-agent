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

_DAY_MS: Final[int] = 86_400_000


def _month_start(ts_ms: int, months_back: int = 0) -> int:
    st = time.localtime(ts_ms / 1000)
    month = st.tm_mon - months_back
    year = st.tm_year
    while month <= 0:
        month += 12
        year -= 1
    return int(time.mktime((year, month, 1, 0, 0, 0, 0, -1, -1)) * 1000)


def _time_text(ts_ms: int) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(ts_ms / 1000))


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
        "first_text": _time_text(times[0]),
        "last_ms": times[-1],
        "last_text": _time_text(times[-1]),
        "interval_days_min": min(gaps),
        "interval_days_max": max(gaps),
        "confidence": confidence,
        "confidence_label": CONFIDENCE_LABELS[confidence],
        "basis": (
            f"{merchant} 在 {_time_text(times[0])} 到 {_time_text(times[-1])} 之间共 {len(rows)} 笔，"
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
                "time_text": _time_text(int(row["ts_ms"])),
                "amount_fen": int(row["amount_fen"]),
                "amount_yuan": fen_to_yuan_str(int(row["amount_fen"])),
                "category": str(row["category"]),
                "note": row["note"],
            }
            for row in rows
        ],
    }


def detect_recurring(
    conn,
    account_id: str,
    *,
    lookback_months: int = LOOKBACK_MONTHS,
    as_of: int | None = None,
) -> dict[str, Any]:
    """按商户分组找周期性支出，返回候选清单（永不改库）。"""
    as_of = int(as_of if as_of is not None else time.time() * 1000)
    since = _month_start(as_of, lookback_months)
    groups: dict[str, list[Any]] = {}
    for row in _rows(conn, account_id, since):
        if str(row["category"]) in EXCLUDED_CATEGORIES:
            continue
        if str(row["channel"]) == "simulated":
            continue
        groups.setdefault(str(row["counterparty"]), []).append(row)

    candidates: list[dict[str, Any]] = []
    for merchant in sorted(groups):
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
            "next_charge_text": _time_text(int(row["next_charge_ms"])),
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


__all__ = [
    "CONFIDENCE_LABELS",
    "INFERRED_NOTE",
    "LOOKBACK_MONTHS",
    "MIN_OCCURRENCES",
    "UPCOMING_DAYS",
    "UPCOMING_NOTE",
    "detect_recurring",
    "upcoming",
]
