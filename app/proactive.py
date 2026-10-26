"""主动财务管理：储蓄目标、确定性测算、监测规则与提醒。

对应文档：docs/改造实施基线.md 9.2（阶段 5 接入建议）。

这一层只做三件事：
1. 用纯函数算数——当前累计储蓄、剩余目标、剩余周期、阶段储蓄要求、历史平均
   收入、必要支出、预计周期结余、目标可行性、预算偏差，全部 int 分；
2. 把「目标 / 监测规则 / 提醒 / 监测运行」四个实体落库（SQL 只在这里写）；
3. 手动触发一次监测：读交易与目标，产出带证据的提醒。

边界写死在代码里：模型不参与算金额；这里不转账、不申购、不改余额——任何
"动钱" 的动作都不在本模块的写入路径上。
"""

from __future__ import annotations

import json
import os
import time
from datetime import date
from typing import Any, Final

import app.audit as audit
import app.db as db
from app import timefmt
from app.clock import ValidationError, now_ms
from app.ids import new_id
from app.log import log
from app.money import INTERNAL_CATEGORIES, fen_to_yuan_str

# 币种固定 CNY：产品只支持人民币账户，换币种属于另一个需求。
CURRENCY: Final[str] = "CNY"

# 必要支出分类：吃、行、水电、看病、订阅。购物/娱乐算可选消费，不进"必要支出"。
ESSENTIAL_CATEGORIES: Final[tuple[str, ...]] = (
    "餐饮",
    "交通",
    "生活缴费",
    "医疗",
    "订阅",
)

# 转账与理财是账户之间的内部搬运：既不是收入，也不是消费，统计时排除。
# 常量本体在 app/money.py（工具层也用它），这里只是按老名字再导出一遍。

LOOKBACK_MONTHS: Final[int] = 6
MIN_LOOKBACK_MONTHS: Final[int] = 1
MAX_LOOKBACK_MONTHS: Final[int] = 24

# 单笔上限 100 万，目标金额允许到 1 亿（竞赛演示够用，且仍在 int 安全区）。
MAX_TARGET_FEN: Final[int] = 100_000_000_00

# 后台监测的节流：同一个目标最多每 6 小时被节拍器唤起一次。
# 为什么不是每分钟：目标是月度口径，检查得再密也不会多出信息，只会多出重复提醒。
MONITOR_INTERVAL_ENV: Final[str] = "PROACTIVE_MONITOR_MS"
DEFAULT_MONITOR_INTERVAL_MS: Final[int] = 6 * 60 * 60 * 1000
MIN_MONITOR_INTERVAL_MS: Final[int] = 60 * 1000

# 监测的触发来源，写进 monitor_runs.trigger 与审计：手动点和后台跑必须能分开看。
TRIGGER_MANUAL: Final[str] = "manual"
TRIGGER_SCHEDULER: Final[str] = "scheduler"
MONITOR_TRIGGERS: Final[tuple[str, ...]] = (TRIGGER_MANUAL, TRIGGER_SCHEDULER)

DEFAULT_PROGRESS_TOLERANCE: Final[int] = 10
MAX_PROGRESS_TOLERANCE: Final[int] = 100

GOAL_STATUSES: Final[tuple[str, ...]] = ("active", "achieved", "cancelled")
RULE_KINDS: Final[tuple[str, ...]] = ("progress", "budget")
RULE_PERIODS: Final[tuple[str, ...]] = ("monthly", "weekly")
ALERT_STATUSES: Final[tuple[str, ...]] = ("open", "ignored", "resolved")
ALERT_ACTIONS: Final[tuple[str, ...]] = ("ignore", "resolve")
ALERT_ACTION_IGNORE: Final[str] = "ignore"
ALERT_ACTION_RESOLVE: Final[str] = "resolve"

DEFAULT_LIMIT: Final[int] = 20
MAX_LIMIT: Final[int] = 200

# 提醒原因码：稳定字符串，便于去重与前端映射。
REASON_PROGRESS_BEHIND: Final[str] = "progress_behind"
REASON_BUDGET_OVERSPENT: Final[str] = "budget_overspent"
REASON_CASHFLOW_INSUFFICIENT: Final[str] = "cashflow_insufficient"

# 可行性结论码。
FEASIBLE: Final[str] = "feasible"
INFEASIBLE: Final[str] = "infeasible"
ACHIEVED: Final[str] = "achieved"
EXPIRED: Final[str] = "expired"
UNKNOWN: Final[str] = "unknown"


# ---------------------------------------------------------------- 基础工具


def _get(row: Any, key: str, default: Any = None) -> Any:
    """同时支持 sqlite3.Row 与 dict；缺列返回 default 而不是抛错。"""
    try:
        return row[key]
    except (IndexError, KeyError, TypeError):
        return default


def _int_field(row: Any, key: str, default: int = 0) -> int:
    value = _get(row, key, default)
    return default if value is None else int(value)


def _require_int(
    value: Any,
    field: str,
    *,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int:
    """金额/数量一律整数：bool 和 str 都不收，浮点直接拒绝（不许 float 算钱）。"""
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValidationError(f"{field} 必须是整数", field=field, value=repr(value))
    if minimum is not None and value < minimum:
        raise ValidationError(f"{field} 不能小于 {minimum}", field=field, value=value)
    if maximum is not None and value > maximum:
        raise ValidationError(f"{field} 不能大于 {maximum}", field=field, value=value)
    return int(value)


def _require_text(value: Any, field: str, *, max_len: int = 40, default: str = "") -> str:
    if value is None:
        return default
    if not isinstance(value, str):
        raise ValidationError(f"{field} 必须是字符串", field=field)
    text = value.strip()
    if len(text) > max_len:
        raise ValidationError(f"{field} 太长", field=field, max_len=max_len)
    return text


def _require_choice(value: Any, field: str, choices: tuple[str, ...]) -> str:
    text = str(value)
    if text not in choices:
        raise ValidationError(f"{field} 取值不合法", field=field, allowed=list(choices))
    return text


def parse_target_date(value: Any) -> date:
    """目标日期严格 YYYY-MM-DD：日期不是时刻，不引入时区推日。"""
    if not isinstance(value, str):
        raise ValidationError("目标日期必须是 YYYY-MM-DD 字符串", target_date=repr(value))
    text = value.strip()
    if len(text) != 10 or text[4] != "-" or text[7] != "-":
        raise ValidationError("目标日期必须是 YYYY-MM-DD", target_date=text)
    try:
        return date.fromisoformat(text)
    except ValueError as exc:
        raise ValidationError("目标日期不是合法日期", target_date=text) from exc


def local_date(ts_ms_value: int | None = None) -> date:
    """毫秒时间戳 -> 本地日历日（与账单分析用的是同一套本地时区口径）。"""
    stamp = now_ms() if ts_ms_value is None else int(ts_ms_value)
    tm = time.localtime(stamp / 1000)
    return date(tm.tm_year, tm.tm_mon, tm.tm_mday)


def month_key(ts_ms_value: int) -> str:
    tm = time.localtime(int(ts_ms_value) / 1000)
    return f"{tm.tm_year:04d}-{tm.tm_mon:02d}"


def _period_day_start_ms(day: date) -> int:
    return int(time.mktime((day.year, day.month, day.day, 0, 0, 0, 0, 0, -1)) * 1000)


def _month_start(day: date) -> date:
    return date(day.year, day.month, 1)


def recent_months(base: date, count: int) -> list[str]:
    """最近 count 个「完整」自然月（不含 base 所在月），按时间升序。"""
    year, month = base.year, base.month
    keys: list[str] = []
    for _ in range(count):
        month -= 1
        if month == 0:
            year -= 1
            month = 12
        keys.append(f"{year:04d}-{month:02d}")
    return list(reversed(keys))


def days_remaining(target_date: date, base: date | None = None) -> int:
    today = local_date() if base is None else base
    return (target_date - today).days


def periods_remaining(target_date: date, base: date | None = None) -> int:
    """剩余计划周期数（按自然月，含当月）。

    2026-10-23 -> 2026-12-31 得 3（10、11、12 三个月）；到了目标月得 1；
    目标日期已过得 0——0 的含义是"没有下一期了"，不是"不需要存"。
    """
    today = local_date() if base is None else base
    if target_date < today:
        return 0
    return (target_date.year - today.year) * 12 + (target_date.month - today.month) + 1


def required_per_period(remaining_fen: int, periods: int) -> int:
    """每期至少要存多少：向上取整到分，不足一分按一分，绝不向下取整放过缺口。"""
    if remaining_fen <= 0:
        return 0
    if periods <= 0:
        # 已经到期：缺口只能一次性补齐，"每期"退化成"现在就要"。
        return int(remaining_fen)
    return (int(remaining_fen) + periods - 1) // periods


def _div_round(total: int, count: int) -> int:
    """整数四舍五入除法（非负），避免 float 参与算钱。"""
    if count <= 0:
        return 0
    return (int(total) + count // 2) // count


# ---------------------------------------------------------------- 交易口径


def dedupe_transactions(rows: Any) -> tuple[list[Any], dict[str, int]]:
    """冲正对与完全重复流水去重，返回 (保留行, 计数)。

    - 被冲正的原交易（reversed_by 非空）不再计入；
    - 冲正本身（reversal_of 非空）只是把原交易抹掉，也不再计入；
    - 同账户/方向/金额/对方/时刻/分类完全相同的行只算第一笔（重复导入/重放）。
    """
    kept: list[Any] = []
    seen: set[tuple[Any, ...]] = set()
    stats = {"input_count": 0, "reversal_original": 0, "reversal_entry": 0, "duplicate": 0}
    for row in rows or ():
        stats["input_count"] += 1
        if _get(row, "reversed_by"):
            stats["reversal_original"] += 1
            continue
        if _get(row, "reversal_of"):
            stats["reversal_entry"] += 1
            continue
        key = (
            _get(row, "account_id"),
            _get(row, "direction"),
            _get(row, "amount_fen"),
            _get(row, "counterparty"),
            _get(row, "ts_ms"),
            _get(row, "category"),
        )
        if key in seen:
            stats["duplicate"] += 1
            continue
        seen.add(key)
        kept.append(row)
    return kept, stats


def _is_income(row: Any) -> bool:
    return str(_get(row, "direction", "")) == "in"


def _is_spend(row: Any) -> bool:
    return str(_get(row, "direction", "")) == "out"


def _is_internal(row: Any) -> bool:
    return str(_get(row, "category", "")) in INTERNAL_CATEGORIES


def analyze_cashflow(
    transactions: Any,
    *,
    base: date | None = None,
    lookback_months: int = LOOKBACK_MONTHS,
) -> dict[str, Any]:
    """历史收入 / 必要支出 / 预计周期结余（纯函数，int 分）。

    口径：
    - 只统计最近 N 个「完整」自然月，当月未走完不计入（避免半个月压低基准）；
    - 转账与理财内部搬运不计入收入/支出；
    - 冲正对与重复流水先去掉；
    - 平均值按"有交易的月份数"取整除法，四舍五入到分。
    """
    today = local_date() if base is None else base
    months = max(MIN_LOOKBACK_MONTHS, min(int(lookback_months), MAX_LOOKBACK_MONTHS))
    window = recent_months(today, months)
    kept, stats = dedupe_transactions(transactions)
    window_set = set(window)

    income_by_month = {key: 0 for key in window}
    essential_by_month = {key: 0 for key in window}
    spend_by_month = {key: 0 for key in window}
    for row in kept:
        key = month_key(_int_field(row, "ts_ms"))
        if key not in window_set or _is_internal(row):
            continue
        amount = _int_field(row, "amount_fen")
        if amount <= 0:
            continue
        if _is_income(row):
            income_by_month[key] += amount
        elif _is_spend(row):
            spend_by_month[key] += amount
            if str(_get(row, "category", "")) in ESSENTIAL_CATEGORIES:
                essential_by_month[key] += amount

    active_months = [
        key
        for key in window
        if income_by_month[key] or spend_by_month[key] or essential_by_month[key]
    ]
    income_total = sum(income_by_month.values())
    essential_total = sum(essential_by_month.values())
    spend_total = sum(spend_by_month.values())
    divisor = len(active_months)

    avg_income = _div_round(income_total, divisor)
    avg_essential = _div_round(essential_total, divisor)
    avg_spend = _div_round(spend_total, divisor)
    return {
        "currency": CURRENCY,
        "lookback_months": months,
        "months": window,
        "months_with_data": active_months,
        "months_with_data_count": divisor,
        "has_history": divisor > 0,
        "income_by_month": income_by_month,
        "essential_by_month": essential_by_month,
        "spend_by_month": spend_by_month,
        "income_total_fen": income_total,
        "essential_total_fen": essential_total,
        "spend_total_fen": spend_total,
        "avg_income_fen": avg_income,
        "avg_essential_fen": avg_essential,
        "avg_spend_fen": avg_spend,
        "projected_period_balance_fen": avg_income - avg_essential,
        "dedupe": stats,
    }


# ---------------------------------------------------------------- 计划测算


def compute_savings_plan(
    *,
    target_fen: int,
    target_date: str | date,
    saved_fen: int = 0,
    transactions: Any = (),
    base: date | None = None,
    lookback_months: int = LOOKBACK_MONTHS,
    currency: str = CURRENCY,
) -> dict[str, Any]:
    """储蓄计划：全部输出结构化数字 + 假设说明，模型只负责解释。

    输入校验失败抛 ValidationError（400），不做任何猜测性修补。

    缺口口径（两者互斥，至少一个为 0）：
    - period_gap_fen = max(0, 阶段储蓄要求 - 预计周期结余)，缺口不会为负；
    - period_surplus_fen = max(0, 预计周期结余 - 阶段储蓄要求)；
    - total_gap_fen = period_gap_fen * 剩余周期数，仅在确有缺口时为正。
    """
    if str(currency) != CURRENCY:
        raise ValidationError("只支持人民币目标", currency=str(currency))
    target = _require_int(target_fen, "target_fen", minimum=1, maximum=MAX_TARGET_FEN)
    saved = _require_int(saved_fen, "saved_fen", minimum=0)
    due = target_date if isinstance(target_date, date) else parse_target_date(target_date)
    today = local_date() if base is None else base

    cash = analyze_cashflow(transactions, base=today, lookback_months=lookback_months)
    remaining = max(0, target - saved)
    periods = periods_remaining(due, today)
    days = (due - today).days
    per_period = required_per_period(remaining, periods)
    progress_pct = min(100, saved * 100 // target) if target else 0

    balance = cash["projected_period_balance_fen"]
    if remaining <= 0:
        period_gap = 0
        period_surplus = 0
    else:
        period_gap = max(0, per_period - balance)
        period_surplus = max(0, balance - per_period)
    total_gap = period_gap * periods if periods > 0 and period_gap > 0 else 0

    warnings: list[str] = []
    if remaining == 0:
        feasibility = ACHIEVED
        warnings.append("GOAL_ACHIEVED")
    elif days < 0:
        feasibility = EXPIRED
        warnings.append("TARGET_DATE_PASSED")
    elif not cash["has_history"]:
        feasibility = UNKNOWN
        warnings.append("NO_TRANSACTION_HISTORY")
    elif balance <= 0:
        feasibility = INFEASIBLE
        warnings.append("CASHFLOW_NOT_POSITIVE")
    elif per_period > balance:
        feasibility = INFEASIBLE
        warnings.append("REQUIRED_EXCEEDS_SURPLUS")
    else:
        feasibility = FEASIBLE

    return {
        "currency": CURRENCY,
        "target_fen": target,
        "target_yuan": fen_to_yuan_str(target),
        "target_date": due.isoformat(),
        "today": today.isoformat(),
        "days_remaining": days,
        "period_unit": "month",
        "periods_remaining": periods,
        "saved_fen": saved,
        "saved_yuan": fen_to_yuan_str(saved),
        "remaining_fen": remaining,
        "remaining_yuan": fen_to_yuan_str(remaining),
        "progress_pct": progress_pct,
        "required_per_period_fen": per_period,
        "required_per_period_yuan": fen_to_yuan_str(per_period),
        "period_gap_fen": period_gap,
        "period_gap_yuan": fen_to_yuan_str(period_gap),
        "period_surplus_fen": period_surplus,
        "period_surplus_yuan": fen_to_yuan_str(period_surplus),
        "total_gap_fen": total_gap,
        "total_gap_yuan": fen_to_yuan_str(total_gap),
        "avg_income_fen": cash["avg_income_fen"],
        "avg_income_yuan": fen_to_yuan_str(cash["avg_income_fen"]),
        "avg_essential_fen": cash["avg_essential_fen"],
        "avg_essential_yuan": fen_to_yuan_str(cash["avg_essential_fen"]),
        "avg_spend_fen": cash["avg_spend_fen"],
        "projected_period_balance_fen": balance,
        "projected_period_balance_yuan": fen_to_yuan_str(balance),
        "feasibility": feasibility,
        "has_history": cash["has_history"],
        "months_analyzed": cash["months_with_data_count"],
        "warnings": warnings,
        "assumptions": [
            "金额一律以整数分计算，币种固定 CNY。",
            f"历史基准取最近 {cash['months_with_data_count']} 个有流水的完整自然月"
            f"（窗口 {cash['lookback_months']} 个月，当月未走完不计入）。",
            "转账与理财内部搬运不计收入也不计支出；冲正对与重复流水已去重。",
            "预计周期结余 = 历史平均收入 - 历史必要支出（餐饮/交通/生活缴费/医疗/订阅），"
            "可选消费不计入，结论偏乐观。",
            "阶段储蓄要求按剩余自然月向上取整到分。",
            "缺口（period_gap）与盈余（period_surplus）互斥，缺口不会为负数。",
        ],
    }


def budget_deviation(*, budget_fen: int, actual_fen: int, period: str = "monthly") -> dict[str, Any]:
    """预算偏差：超了多少钱、用了百分之几（全整数，不出现 float）。"""
    budget = _require_int(budget_fen, "budget_fen", minimum=1)
    actual = _require_int(actual_fen, "actual_fen", minimum=0)
    _require_choice(period, "period", RULE_PERIODS)
    deviation = actual - budget
    return {
        "currency": CURRENCY,
        "period": period,
        "budget_fen": budget,
        "budget_yuan": fen_to_yuan_str(budget),
        "actual_fen": actual,
        "actual_yuan": fen_to_yuan_str(actual),
        "deviation_fen": deviation,
        "deviation_yuan": fen_to_yuan_str(deviation),
        "over_budget": deviation > 0,
        "over_fen": max(0, deviation),
        "remaining_fen": max(0, budget - actual),
        "usage_pct": actual * 100 // budget,
    }


# ---------------------------------------------------------------- 仓储：目标


def default_user_id(conn) -> str:
    """演示库是单用户；没有用户就没有财务主体，直接 400 而不是猜。"""
    row = conn.execute("SELECT id FROM users ORDER BY created_ms, id LIMIT 1").fetchone()
    if row is None:
        raise ValidationError("还没有用户数据，无法管理储蓄目标", reason="NO_USERS")
    return str(row["id"])


def _goal_rows(conn, *, user_id: str | None = None, status: str | None = None, limit: int = DEFAULT_LIMIT):
    clause = ""
    args: list[Any] = []
    if user_id:
        clause += " AND user_id = ?"
        args.append(user_id)
    if status:
        clause += " AND status = ?"
        args.append(status)
    args.append(max(1, min(int(limit), MAX_LIMIT)))
    sql = (
        "SELECT * FROM savings_goals WHERE 1=1" + clause +
        " ORDER BY created_ms DESC, id LIMIT ?"
    )
    return list(conn.execute(sql, args).fetchall())


def list_goals(
    conn,
    *,
    user_id: str | None = None,
    status: str | None = None,
    limit: int = DEFAULT_LIMIT,
) -> list[dict[str, Any]]:
    """目标列表 + 进度：交易只读一次，多个目标共用同一份现金流基准。"""
    rows = _goal_rows(conn, user_id=user_id, status=status, limit=limit)
    rows_by_user: dict[str, list[Any]] = {}
    views: list[dict[str, Any]] = []
    for row in rows:
        owner = str(row["user_id"])
        if owner not in rows_by_user:
            rows_by_user[owner] = transactions_for_user(conn, owner)
        views.append(goal_view(conn, row, transactions=rows_by_user[owner]))
    return views


def get_goal(conn, goal_id: str, *, user_id: str | None = None):
    if user_id:
        return conn.execute(
            "SELECT * FROM savings_goals WHERE id = ? AND user_id = ?", (str(goal_id), user_id)
        ).fetchone()
    return conn.execute("SELECT * FROM savings_goals WHERE id = ?", (str(goal_id),)).fetchone()


def transactions_for_user(conn, user_id: str, *, limit: int = 5000) -> list[Any]:
    """某个用户名下所有账户的流水（只读，最多最近 5000 条，足够算 24 个月基准）。"""
    rows = conn.execute(
        "SELECT t.* FROM transactions t JOIN accounts a ON a.id = t.account_id"
        " WHERE a.user_id = ? ORDER BY t.ts_ms DESC, t.id DESC LIMIT ?",
        (str(user_id), max(1, int(limit))),
    ).fetchall()
    return list(reversed(rows))


def rules_for_goal(conn, goal_id: str, *, enabled_only: bool = False) -> list[Any]:
    sql = "SELECT * FROM monitor_rules WHERE goal_id = ?"
    if enabled_only:
        sql += " AND enabled = 1"
    sql += " ORDER BY kind, id"
    return list(conn.execute(sql, (str(goal_id),)).fetchall())


#: kind -> 阈值所在的列。列名只从这张白名单取，不拼外部输入。
_RULE_THRESHOLD_COLUMNS: Final[dict[str, str]] = {
    "budget": "threshold_fen",
    "progress": "threshold_pp",
}


def _rule_view(row) -> dict[str, Any]:
    """规则的对外形状：分与百分点分列给，调用方不用按 kind 猜单位。"""
    return {
        "rule_id": str(_get(row, "id")),
        "kind": str(_get(row, "kind")),
        "threshold_fen": int(_get(row, "threshold_fen")) if _get(row, "threshold_fen") is not None else None,
        "threshold_pp": int(_get(row, "threshold_pp")) if _get(row, "threshold_pp") is not None else None,
        "period": str(_get(row, "period")),
        "enabled": bool(_get(row, "enabled", 0)),
    }


def _insert_rule(conn, goal_id: str, kind: str, threshold: int, period: str, created: int) -> str:
    rule_id = new_id("mr")
    column = _RULE_THRESHOLD_COLUMNS[kind]
    conn.execute(
        f"INSERT INTO monitor_rules (id, goal_id, kind, {column}, period, enabled, created_ms)"
        " VALUES (?, ?, ?, ?, ?, 1, ?)",
        (rule_id, goal_id, kind, int(threshold), period, int(created)),
    )
    return rule_id


def _set_rule_threshold(
    conn, rule_id: str, threshold: int, *, kind: str, period: str | None = None
) -> None:
    column = _RULE_THRESHOLD_COLUMNS[kind]
    if period:
        conn.execute(
            f"UPDATE monitor_rules SET {column} = ?, period = ? WHERE id = ?",
            (int(threshold), period, rule_id),
        )
    else:
        conn.execute(
            f"UPDATE monitor_rules SET {column} = ? WHERE id = ?", (int(threshold), rule_id)
        )


def count_open_alerts(conn, goal_id: str) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM proactive_alerts WHERE goal_id = ? AND status = 'open'",
        (str(goal_id),),
    ).fetchone()
    return int(row["n"]) if row is not None else 0


def goal_view(
    conn,
    row,
    *,
    transactions: Any = None,
    base: date | None = None,
) -> dict[str, Any]:
    """目标的对外形状：静态字段 + 计划摘要 + 规则 + 未处理提醒数。"""
    goal_id = str(row["id"])
    if transactions is None:
        transactions = transactions_for_user(conn, str(row["user_id"]))
    plan = compute_savings_plan(
        target_fen=int(row["target_fen"]),
        target_date=str(row["target_date"]),
        saved_fen=int(row["saved_fen"]),
        transactions=transactions,
        base=base,
    )
    rules = rules_for_goal(conn, goal_id)
    today = base or local_date()
    plan_view = {
        "currency": plan["currency"],
        "period_unit": plan["period_unit"],
        "periods_remaining": plan["periods_remaining"],
        "required_per_period_fen": plan["required_per_period_fen"],
        "avg_income_fen": plan["avg_income_fen"],
        "avg_essential_fen": plan["avg_essential_fen"],
        "avg_spend_fen": plan["avg_spend_fen"],
        "projected_period_balance_fen": plan["projected_period_balance_fen"],
        "period_gap_fen": plan["period_gap_fen"],
        "period_surplus_fen": plan["period_surplus_fen"],
        "total_gap_fen": plan["total_gap_fen"],
        "months_analyzed": plan["months_analyzed"],
        "has_history": plan["has_history"],
        "feasibility": plan["feasibility"],
        "warnings": plan["warnings"],
        "assumptions": plan["assumptions"],
    }
    budget_rule = next((rule for rule in rules if str(rule["kind"]) == "budget"), None)
    budget_view: dict[str, Any] | None = None
    if budget_rule is not None:
        cadence = str(budget_rule["period"])
        window_start = _period_start(cadence, today)
        window_end = _period_end(cadence, today)
        kept, _ = dedupe_transactions(transactions)
        actual_fen = _spend_in_window(kept, window_start, window_end)
        deviation = budget_deviation(
            budget_fen=int(budget_rule["threshold_fen"]), actual_fen=actual_fen, period=cadence
        )
        budget_view = {
            "rule_id": str(budget_rule["id"]),
            "period": cadence,
            "period_label": _period_label(cadence, today),
            "budget_fen": int(budget_rule["threshold_fen"]),
            "budget_yuan": deviation["budget_yuan"],
            "actual_fen": actual_fen,
            "actual_yuan": deviation["actual_yuan"],
            "deviation_fen": deviation["deviation_fen"],
            "deviation_yuan": deviation["deviation_yuan"],
            "over_budget": deviation["over_budget"],
            "over_fen": deviation["over_fen"],
            "remaining_fen": deviation["remaining_fen"],
            "usage_pct": deviation["usage_pct"],
            "enabled": bool(budget_rule["enabled"]),
        }
    return {
        "goal_id": goal_id,
        "user_id": str(row["user_id"]),
        "name": str(row["name"]),
        "target_fen": int(row["target_fen"]),
        "target_yuan": fen_to_yuan_str(int(row["target_fen"])),
        "target_date": str(row["target_date"]),
        "saved_fen": int(row["saved_fen"]),
        "saved_yuan": fen_to_yuan_str(int(row["saved_fen"])),
        "status": str(row["status"]),
        "created_ms": int(row["created_ms"]),
        "updated_ms": int(row["updated_ms"]),
        "progress_pct": plan["progress_pct"],
        "remaining_fen": plan["remaining_fen"],
        "remaining_yuan": plan["remaining_yuan"],
        "periods_remaining": plan["periods_remaining"],
        "required_per_period_fen": plan["required_per_period_fen"],
        "required_per_period_yuan": plan["required_per_period_yuan"],
        "feasibility": plan["feasibility"],
        "days_remaining": plan["days_remaining"],
        "open_alert_count": count_open_alerts(conn, goal_id),
        "monitor_rules": [_rule_view(rule) for rule in rules],
        "plan": plan_view,
        "budget": budget_view,
    }


def create_goal(
    conn,
    *,
    user_id: str | None = None,
    name: Any = "",
    target_fen: Any,
    target_date: Any,
    saved_fen: Any = 0,
    budget_fen: Any = None,
    progress_tolerance: Any = None,
    period: str = "monthly",
    session_id: str | None = None,
    task_id: str | None = None,
) -> dict[str, Any]:
    """创建目标：同时建默认监测规则（进度必建，预算给了才建）。写审计。"""
    owner = str(user_id) if user_id else default_user_id(conn)
    user_row = conn.execute("SELECT id FROM users WHERE id = ?", (owner,)).fetchone()
    if user_row is None:
        raise ValidationError("用户不存在", user_id=owner)
    target = _require_int(target_fen, "target_fen", minimum=1, maximum=MAX_TARGET_FEN)
    saved = _require_int(saved_fen, "saved_fen", minimum=0)
    if saved > target:
        raise ValidationError("已存金额不能超过目标金额", saved_fen=saved, target_fen=target)
    due = parse_target_date(target_date)
    tolerance = DEFAULT_PROGRESS_TOLERANCE
    if progress_tolerance is not None:
        tolerance = _require_int(
            progress_tolerance, "progress_tolerance", minimum=1, maximum=MAX_PROGRESS_TOLERANCE
        )
    budget = None
    if budget_fen is not None:
        budget = _require_int(budget_fen, "budget_fen", minimum=1)
    cadence = _require_choice(period, "period", RULE_PERIODS)
    title = _require_text(name, "name", default="储蓄目标") or "储蓄目标"
    stamp = now_ms()
    goal_id = new_id("sg")

    with db.tx(conn):
        conn.execute(
            "INSERT INTO savings_goals"
            " (id, user_id, name, target_fen, target_date, saved_fen, status, created_ms, updated_ms)"
            " VALUES (?, ?, ?, ?, ?, ?, 'active', ?, ?)",
            (goal_id, owner, title, target, due.isoformat(), saved, stamp, stamp),
        )
        rule_ids = [_insert_rule(conn, goal_id, "progress", tolerance, cadence, stamp)]
        if budget is not None:
            rule_ids.append(_insert_rule(conn, goal_id, "budget", budget, cadence, stamp))
        seq = audit.append(
            conn,
            "savings_goal.created",
            {
                "goal_id": goal_id,
                "user_id": owner,
                "name": title,
                "target_fen": target,
                "target_date": due.isoformat(),
                "saved_fen": saved,
                "budget_fen": budget,
                "rule_ids": rule_ids,
                "created_ms": stamp,
            },
            session_id=session_id,
            task_id=task_id,
        )

    fresh = get_goal(conn, goal_id)
    return {"goal": goal_view(conn, fresh), "audit_seq": seq, "rule_ids": rule_ids}


def update_goal(
    conn,
    goal_id: str,
    *,
    user_id: str | None = None,
    name: Any = None,
    target_fen: Any = None,
    target_date: Any = None,
    saved_fen: Any = None,
    status: Any = None,
    budget_fen: Any = None,
    progress_tolerance: Any = None,
    monitor_enabled: Any = None,
    session_id: str | None = None,
    task_id: str | None = None,
) -> dict[str, Any] | None:
    """调整目标：金额/日期/已存/状态/预算/监测开关；返回 None 表示目标不存在。"""
    row = get_goal(conn, goal_id, user_id=user_id)
    if row is None:
        return None

    changes: dict[str, list[Any]] = {}
    new: dict[str, Any] = {}
    if name is not None:
        title = _require_text(name, "name")
        if not title:
            raise ValidationError("目标名称不能为空", field="name")
        new["name"] = title
    if target_fen is not None:
        new["target_fen"] = _require_int(
            target_fen, "target_fen", minimum=1, maximum=MAX_TARGET_FEN
        )
    if target_date is not None:
        new["target_date"] = parse_target_date(target_date).isoformat()
    if saved_fen is not None:
        new["saved_fen"] = _require_int(saved_fen, "saved_fen", minimum=0)
    if status is not None:
        new["status"] = _require_choice(status, "status", GOAL_STATUSES)

    target_after = int(new.get("target_fen", row["target_fen"]))
    saved_after = int(new.get("saved_fen", row["saved_fen"]))
    if saved_after > target_after:
        raise ValidationError("已存金额不能超过目标金额", saved_fen=saved_after, target_fen=target_after)

    # 没显式改状态时按金额自动收口：存够即达成，调高目标后重新激活。
    if "status" not in new:
        current = str(row["status"])
        if current == "active" and saved_after >= target_after:
            new["status"] = "achieved"
        elif current == "achieved" and saved_after < target_after:
            new["status"] = "active"

    for field, value in new.items():
        old = _get(row, field)
        if old != value:
            changes[field] = [old if not isinstance(old, bool) else int(old), value]

    want_monitor = None
    if monitor_enabled is not None:
        if not isinstance(monitor_enabled, bool):
            raise ValidationError("monitor_enabled 必须是布尔值", field="monitor_enabled")
        want_monitor = monitor_enabled

    rule_changes: dict[str, Any] = {}
    rule_rows = {str(_get(r, "kind")): r for r in rules_for_goal(conn, goal_id)}
    tolerance = None
    if progress_tolerance is not None:
        tolerance = _require_int(
            progress_tolerance, "progress_tolerance", minimum=1, maximum=MAX_PROGRESS_TOLERANCE
        )
    budget = None
    if budget_fen is not None:
        budget = _require_int(budget_fen, "budget_fen", minimum=1)

    if not changes and tolerance is None and budget is None and want_monitor is None:
        raise ValidationError("没有要更新的字段", goal_id=str(goal_id))

    stamp = now_ms()
    with db.tx(conn):
        if changes:
            conn.execute(
                "UPDATE savings_goals SET name = ?, target_fen = ?, target_date = ?,"
                " saved_fen = ?, status = ?, updated_ms = ? WHERE id = ?",
                (
                    str(new.get("name", row["name"])),
                    int(new.get("target_fen", row["target_fen"])),
                    str(new.get("target_date", row["target_date"])),
                    int(new.get("saved_fen", row["saved_fen"])),
                    str(new.get("status", row["status"])),
                    stamp,
                    str(goal_id),
                ),
            )
        else:
            conn.execute(
                "UPDATE savings_goals SET updated_ms = ? WHERE id = ?", (stamp, str(goal_id))
            )

        if tolerance is not None:
            rule = rule_rows.get("progress")
            if rule is None:
                rule_changes["progress"] = _insert_rule(
                    conn, str(goal_id), "progress", tolerance, "monthly", stamp
                )
            elif int(_get(rule, "threshold_pp")) != tolerance:
                _set_rule_threshold(
                    conn, str(_get(rule, "id")), tolerance, kind="progress"
                )
                rule_changes["progress_threshold"] = [
                    int(_get(rule, "threshold_pp")), tolerance
                ]
        if budget is not None:
            rule = rule_rows.get("budget")
            if rule is None:
                rule_changes["budget"] = _insert_rule(
                    conn, str(goal_id), "budget", budget, "monthly", stamp
                )
            elif int(_get(rule, "threshold_fen")) != budget:
                _set_rule_threshold(conn, str(_get(rule, "id")), budget, kind="budget")
                rule_changes["budget_threshold"] = [int(_get(rule, "threshold_fen")), budget]
        if want_monitor is not None:
            conn.execute(
                "UPDATE monitor_rules SET enabled = ? WHERE goal_id = ?",
                (1 if want_monitor else 0, str(goal_id)),
            )
            rule_changes["monitor_enabled"] = want_monitor

        seq = audit.append(
            conn,
            "savings_goal.updated",
            {
                "goal_id": str(goal_id),
                "changes": changes,
                "rule_changes": rule_changes,
                "updated_ms": stamp,
            },
            session_id=session_id,
            task_id=task_id,
        )

    fresh = get_goal(conn, goal_id)
    return {
        "goal": goal_view(conn, fresh),
        "changes": changes,
        "rule_changes": rule_changes,
        "audit_seq": seq,
    }


# ---------------------------------------------------------------- 仓储：提醒


def _loads_evidence(text: Any) -> dict[str, Any]:
    try:
        loaded = json.loads(str(text))
    except (TypeError, ValueError):
        return {"raw": str(text)}
    return loaded if isinstance(loaded, dict) else {"value": loaded}


def alert_view(row) -> dict[str, Any]:
    resolved = _get(row, "resolved_ms")
    return {
        "alert_id": str(row["id"]),
        "goal_id": str(_get(row, "goal_id")) if _get(row, "goal_id") else None,
        "rule_id": str(_get(row, "rule_id")) if _get(row, "rule_id") else None,
        "reason": str(row["reason"]),
        "evidence": _loads_evidence(_get(row, "evidence_json")),
        "suggestion": str(_get(row, "suggestion", "")),
        "created_ms": int(row["created_ms"]),
        "status": str(row["status"]),
        "resolved_ms": int(resolved) if resolved is not None else None,
        "user_id": str(_get(row, "user_id", "")) or None,
    }


def list_alerts(
    conn,
    *,
    user_id: str | None = None,
    status: str = "open",
    goal_id: str | None = None,
    limit: int = DEFAULT_LIMIT,
) -> list[dict[str, Any]]:
    """提醒列表：默认只看未处理的；status='all' 才把已忽略/已处理的也带上。"""
    clause = ""
    args: list[Any] = []
    if status and status != "all":
        clause += " AND a.status = ?"
        args.append(_require_choice(status, "status", ALERT_STATUSES))
    if goal_id:
        clause += " AND a.goal_id = ?"
        args.append(str(goal_id))
    if user_id:
        # 两种归属都要认：目标提醒挂在 goal 上，计划提醒没有 goal，只能靠 a.user_id。
        clause += " AND (a.user_id = ? OR g.user_id = ?)"
        args.extend([str(user_id), str(user_id)])
    args.append(max(1, min(int(limit), MAX_LIMIT)))
    sql = (
        "SELECT a.* FROM proactive_alerts a LEFT JOIN savings_goals g ON g.id = a.goal_id"
        " WHERE 1=1" + clause + " ORDER BY a.created_ms DESC, a.id DESC LIMIT ?"
    )
    return [alert_view(row) for row in conn.execute(sql, args).fetchall()]


def get_alert(conn, alert_id: str):
    return conn.execute(
        "SELECT * FROM proactive_alerts WHERE id = ?", (str(alert_id),)
    ).fetchone()


def resolve_alert(
    conn,
    alert_id: str,
    action: Any,
    *,
    user_id: str | None = None,
    session_id: str | None = None,
    task_id: str | None = None,
) -> dict[str, Any] | None:
    """处理提醒：ignore=不再跟进，resolve=已解决。返回 None 表示提醒不存在。

    只改 proactive_alerts 一行，不产生任何资金动作；重复处理幂等。
    """
    act = _require_choice(action, "action", ALERT_ACTIONS)
    row = get_alert(conn, alert_id)
    if row is None:
        return None
    if user_id:
        owner = conn.execute(
            "SELECT a.user_id AS direct, g.user_id AS via_goal FROM proactive_alerts a"
            " LEFT JOIN savings_goals g ON g.id = a.goal_id WHERE a.id = ?",
            (str(alert_id),),
        ).fetchone()
        if owner is None:
            return None
        names = {str(value) for value in (owner["direct"], owner["via_goal"]) if value}
        if names and str(user_id) not in names:
            return None
    target_status = "ignored" if act == "ignore" else "resolved"
    if str(row["status"]) != "open":
        # 幂等分支也返回统一形状：{"alert": ..., "audit_seq": None, "idempotent": True}。
        # 之前这里直接返回扁平 view，API 与工具层都按 resolved["alert"] 取值，会 KeyError。
        return {"alert": alert_view(row), "audit_seq": None, "idempotent": True}

    stamp = now_ms()
    with db.tx(conn):
        conn.execute(
            "UPDATE proactive_alerts SET status = ?, resolved_ms = ?"
            " WHERE id = ? AND status = 'open'",
            (target_status, stamp, str(alert_id)),
        )
        seq = audit.append(
            conn,
            "proactive.alert_resolved",
            {
                "alert_id": str(alert_id),
                "goal_id": str(_get(row, "goal_id")) if _get(row, "goal_id") else None,
                "action": act,
                "status": target_status,
                "resolved_ms": stamp,
            },
            session_id=session_id,
            task_id=task_id,
        )
    fresh = get_alert(conn, alert_id)
    view = alert_view(fresh)
    view["idempotent"] = False
    return {"alert": view, "audit_seq": seq}


# ---------------------------------------------------------------- 监测


def _period_label(period: str, day: date) -> str:
    if period == "weekly":
        iso = day.isocalendar()
        return f"{iso[0]:04d}-W{iso[1]:02d}"
    return f"{day.year:04d}-{day.month:02d}"


def _period_start(period: str, day: date) -> date:
    if period == "weekly":
        return day.fromordinal(day.toordinal() - day.weekday())
    return _month_start(day)


def _spend_in_window(kept: list[Any], start: date, end: date) -> int:
    start_ms = _period_day_start_ms(start)
    end_ms = _period_day_start_ms(end)
    total = 0
    for row in kept:
        stamp = _int_field(row, "ts_ms")
        if stamp < start_ms or stamp >= end_ms:
            continue
        if _is_internal(row) or not _is_spend(row):
            continue
        total += _int_field(row, "amount_fen")
    return total


def _period_end(period: str, day: date) -> date:
    start = _period_start(period, day)
    if period == "weekly":
        return date.fromordinal(start.toordinal() + 7)
    if start.month == 12:
        return date(start.year + 1, 1, 1)
    return date(start.year, start.month + 1, 1)


def create_alert(
    conn,
    *,
    goal_id: str | None,
    reason: str,
    evidence: dict[str, Any],
    suggestion: str = "",
    rule_id: str | None = None,
    user_id: str | None = None,
    now: int | None = None,
) -> str:
    """插一条待处理提醒，返回 alert_id（调用方负责事务）。

    给 app/plans.py 复用：计划提醒和「到期要你确认的资金动作」都落在 proactive_alerts 里，
    这样界面上「提醒」只有一个口径，不会出现两个来源各说各的。
    """
    stamp = now_ms() if now is None else int(now)
    alert_id = new_id("pa")
    owner = str(user_id or "")
    if not owner and goal_id:
        row = conn.execute(
            "SELECT user_id FROM savings_goals WHERE id = ?", (str(goal_id),)
        ).fetchone()
        owner = str(row["user_id"]) if row is not None else ""
    conn.execute(
        "INSERT INTO proactive_alerts"
        " (id, goal_id, rule_id, reason, evidence_json, suggestion, created_ms, status,"
        "  resolved_ms, user_id)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, 'open', NULL, ?)",
        (
            alert_id,
            goal_id,
            rule_id,
            reason,
            json.dumps(evidence or {}, ensure_ascii=False, sort_keys=True),
            suggestion,
            stamp,
            owner,
        ),
    )
    return alert_id


def _alert_exists(conn, goal_id: str, rule_id: str | None, reason: str, since_ms: int) -> bool:
    sql = (
        "SELECT 1 FROM proactive_alerts WHERE goal_id = ? AND reason = ? AND created_ms >= ?"
    )
    args: list[Any] = [str(goal_id), reason, int(since_ms)]
    if rule_id is None:
        sql += " AND rule_id IS NULL"
    else:
        sql += " AND rule_id = ?"
        args.append(str(rule_id))
    return conn.execute(sql + " LIMIT 1", args).fetchone() is not None


def _suggestion_for_progress(plan: dict[str, Any], periods: int) -> str:
    if periods <= 0:
        return (
            f"目标日期已过，还差 {plan['remaining_yuan']} 元；"
            "建议调整目标日期或把缺口拆成一次性补充计划。"
        )
    return (
        f"建议接下来每月至少存 {plan['required_per_period_yuan']} 元，"
        f"共 {periods} 期；当前每月预计可结余 "
        f"{plan['projected_period_balance_yuan']} 元。"
    )


def _candidate_alerts(
    conn,
    goal_row,
    rules: list[Any],
    *,
    today: date,
    transactions: Any,
    now_ms_value: int,
) -> list[dict[str, Any]]:
    """算出一轮监测应该产出的提醒（纯计算 + 一次去重查询，不写库）。"""
    goal_id = str(goal_row["id"])
    plan = compute_savings_plan(
        target_fen=int(goal_row["target_fen"]),
        target_date=str(goal_row["target_date"]),
        saved_fen=int(goal_row["saved_fen"]),
        transactions=transactions,
        base=today,
    )
    kept, _stats = dedupe_transactions(transactions)
    created_ms = _int_field(goal_row, "created_ms")
    target_day = parse_target_date(str(goal_row["target_date"]))
    evaluated_ms = int(now_ms_value)
    created_day = local_date(created_ms)
    total_days = max(1, (target_day - created_day).days)
    elapsed_days = max(0, min((today - created_day).days, total_days))
    expected_saved = int(goal_row["target_fen"]) * elapsed_days // total_days
    expected_pct = min(100, expected_saved * 100 // max(1, int(goal_row["target_fen"])))
    candidates: list[dict[str, Any]] = []

    for rule in rules:
        if not int(_get(rule, "enabled", 0)):
            continue
        kind = str(_get(rule, "kind"))
        period = str(_get(rule, "period", "monthly"))
        # 两个单位不再共用一列：progress 读百分点，budget 读分。
        threshold = int(_get(rule, "threshold_pp" if kind == "progress" else "threshold_fen", 0) or 0)
        label = _period_label(period, today)
        period_start = _period_day_start_ms(_period_start(period, today))
        if kind == "progress":
            lag = expected_pct - int(plan["progress_pct"])
            if plan["remaining_fen"] <= 0 or plan["days_remaining"] < 0:
                continue
            if lag <= threshold:
                continue
            rule_id = str(_get(rule, "id"))
            if _alert_exists(conn, goal_id, rule_id, REASON_PROGRESS_BEHIND, period_start):
                continue
            candidates.append(
                {
                    "goal_id": goal_id,
                    "rule_id": rule_id,
                    "reason": REASON_PROGRESS_BEHIND,
                    "suggestion": _suggestion_for_progress(plan, int(plan["periods_remaining"])),
                    "evidence": {
                        "kind": "progress",
                        "period": period,
                        "period_label": label,
                        "expected_pct": expected_pct,
                        "actual_pct": int(plan["progress_pct"]),
                        "lag_pp": lag,
                        "threshold_pp": threshold,
                        "expected_saved_fen": expected_saved,
                        "saved_fen": int(plan["saved_fen"]),
                        "target_fen": int(plan["target_fen"]),
                        "remaining_fen": int(plan["remaining_fen"]),
                        "periods_remaining": int(plan["periods_remaining"]),
                        "required_per_period_fen": int(plan["required_per_period_fen"]),
                        "avg_income_fen": int(plan["avg_income_fen"]),
                        "avg_essential_fen": int(plan["avg_essential_fen"]),
                        "projected_period_balance_fen": int(
                            plan["projected_period_balance_fen"]
                        ),
                        "feasibility": str(plan["feasibility"]),
                        "evaluated_ms": evaluated_ms,
                        "dedupe_key": f"progress:{rule_id}:{label}",
                    },
                }
            )
        elif kind == "budget":
            start = _period_start(period, today)
            end = _period_end(period, today)
            actual = _spend_in_window(kept, start, end)
            deviation = budget_deviation(
                budget_fen=threshold, actual_fen=actual, period=period
            )
            if not deviation["over_budget"]:
                continue
            rule_id = str(_get(rule, "id"))
            if _alert_exists(conn, goal_id, rule_id, REASON_BUDGET_OVERSPENT, period_start):
                continue
            candidates.append(
                {
                    "goal_id": goal_id,
                    "rule_id": rule_id,
                    "reason": REASON_BUDGET_OVERSPENT,
                    "suggestion": (
                        f"{label} 已支出 {deviation['actual_yuan']} 元，"
                        f"超过预算 {deviation['budget_yuan']} 元"
                        f"（超 {fen_to_yuan_str(deviation['over_fen'])} 元）。"
                    ),
                    "evidence": {
                        "kind": "budget",
                        "period": period,
                        "period_label": label,
                        "budget_fen": deviation["budget_fen"],
                        "actual_fen": deviation["actual_fen"],
                        "deviation_fen": deviation["deviation_fen"],
                        "over_fen": deviation["over_fen"],
                        "usage_pct": deviation["usage_pct"],
                        "evaluated_ms": evaluated_ms,
                        "dedupe_key": f"budget:{rule_id}:{label}",
                    },
                }
            )

    # 目标层面的现金流提醒：必要时一次性给出"这个目标按当前收支做不到"。
    if (
        plan["remaining_fen"] > 0
        and plan["days_remaining"] >= 0
        and plan["feasibility"] == INFEASIBLE
        and plan["has_history"]
    ):
        period_start = _period_day_start_ms(_month_start(today))
        if not _alert_exists(conn, goal_id, None, REASON_CASHFLOW_INSUFFICIENT, period_start):
            label = _period_label("monthly", today)
            candidates.append(
                {
                    "goal_id": goal_id,
                    "rule_id": None,
                    "reason": REASON_CASHFLOW_INSUFFICIENT,
                    "suggestion": (
                        f"按最近几个月的平均收入与必要支出，每月可结余 "
                        f"{plan['projected_period_balance_yuan']} 元，低于目标要求的 "
                        f"{plan['required_per_period_yuan']} 元；"
                        "建议延长目标日期、下调目标金额，或先压缩可选消费。"
                    ),
                    "evidence": {
                        "kind": "goal",
                        "period": "monthly",
                        "period_label": label,
                        "avg_income_fen": int(plan["avg_income_fen"]),
                        "avg_essential_fen": int(plan["avg_essential_fen"]),
                        "projected_period_balance_fen": int(
                            plan["projected_period_balance_fen"]
                        ),
                        "required_per_period_fen": int(plan["required_per_period_fen"]),
                        "period_gap_fen": int(plan["period_gap_fen"]),
                        "remaining_fen": int(plan["remaining_fen"]),
                        "feasibility": str(plan["feasibility"]),
                        "evaluated_ms": evaluated_ms,
                        "dedupe_key": f"cashflow:{goal_id}:{label}",
                    },
                }
            )
    return candidates


def monitor_now(conn) -> int:
    """监测基准时刻：单独抽出来，测试可以 freeze 时钟得到稳定结果。"""
    return now_ms()


def run_monitor(
    conn,
    *,
    user_id: str | None = None,
    goal_id: str | None = None,
    session_id: str | None = None,
    task_id: str | None = None,
    trigger: str = TRIGGER_MANUAL,
) -> dict[str, Any]:
    """跑一次监测（可重复）：读目标与交易 → 产出提醒 → 记运行与审计。

    trigger 区分「用户点按钮」和「后台节拍器」，两者都走同一条路径，
    但审计与 monitor_runs 里必须能分开看到底是谁触发的。

    不写 accounts/transactions：只新增 proactive_alerts 与 monitor_runs 两类行。
    失败时回滚整轮提醒并落一条 status='failed' 的运行记录，不假装成功。
    """
    stamp = monitor_now(conn)
    today = local_date(stamp)
    run_id = new_id("mnr")
    owner = str(user_id) if user_id else None
    goals = [
        row
        for row in _goal_rows(conn, user_id=owner, status="active", limit=MAX_LIMIT)
        if goal_id is None or str(row["id"]) == str(goal_id)
    ]
    if goal_id is not None and not goals:
        not_found_finished = monitor_now(conn)
        return {
            "run_id": run_id,
            "status": "failed",
            "started_ms": stamp,
            "started_text": timefmt.maybe_minute_text(stamp),
            "finished_ms": not_found_finished,
            "finished_text": timefmt.maybe_minute_text(not_found_finished),
            "goals_checked": 0,
            "rules_checked": 0,
            "alerts_created": 0,
            "alerts": [],
            "detail": "GOAL_NOT_FOUND",
        }

    rules_checked = 0
    skipped_expired = 0
    try:
        candidates: list[dict[str, Any]] = []
        for row in goals:
            if days_remaining(parse_target_date(str(row["target_date"])), today) < 0:
                # 目标日期已过：进度提醒只会制造焦虑，交给用户调整目标，不反复提醒。
                skipped_expired += 1
                continue
            rules = rules_for_goal(conn, str(row["id"]), enabled_only=True)
            rules_checked += len(rules)
            tx_rows = transactions_for_user(conn, str(row["user_id"]))
            candidates.extend(
                _candidate_alerts(
                    conn, row, rules, today=today, transactions=tx_rows, now_ms_value=stamp
                )
            )

        created: list[dict[str, Any]] = []
        with db.tx(conn):
            for candidate in candidates:
                alert_id = new_id("pa")
                conn.execute(
                    "INSERT INTO proactive_alerts"
                    " (id, goal_id, rule_id, reason, evidence_json, suggestion, created_ms,"
                    "  status, resolved_ms)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, 'open', NULL)",
                    (
                        alert_id,
                        candidate["goal_id"],
                        candidate["rule_id"],
                        candidate["reason"],
                        json.dumps(candidate["evidence"], ensure_ascii=False, sort_keys=True),
                        candidate["suggestion"],
                        stamp,
                    ),
                )
                audit.append(
                    conn,
                    "proactive.alert_created",
                    {
                        "alert_id": alert_id,
                        "goal_id": candidate["goal_id"],
                        "rule_id": candidate["rule_id"],
                        "reason": candidate["reason"],
                        "created_ms": stamp,
                    },
                    session_id=session_id,
                    task_id=task_id,
                )
                created.append(
                    {
                        "alert_id": alert_id,
                        "goal_id": candidate["goal_id"],
                        "rule_id": candidate["rule_id"],
                        "reason": candidate["reason"],
                        "suggestion": candidate["suggestion"],
                        "evidence": candidate["evidence"],
                    }
                )
            finished = monitor_now(conn)
            conn.execute(
                "INSERT INTO monitor_runs"
                " (id, started_ms, finished_ms, status, alerts_created, detail, trigger)"
                " VALUES (?, ?, ?, 'ok', ?, ?, ?)",
                (
                    run_id,
                    stamp,
                    finished,
                    len(created),
                    f"goals={len(goals)};rules={rules_checked};"
                    f"expired_skipped={skipped_expired}",
                    trigger,
                ),
            )
            seq = audit.append(
                conn,
                "proactive.monitor_run",
                {
                    "run_id": run_id,
                    "status": "ok",
                    "goals_checked": len(goals),
                    "rules_checked": rules_checked,
                    "alerts_created": len(created),
                    "trigger": trigger,
                },
                session_id=session_id,
                task_id=task_id,
            )
        return {
            "run_id": run_id,
            "status": "ok",
            "started_ms": stamp,
            "started_text": timefmt.maybe_minute_text(stamp),
            "finished_ms": finished,
            "finished_text": timefmt.maybe_minute_text(finished),
            "goals_checked": len(goals),
            "rules_checked": rules_checked,
            "alerts_created": len(created),
            "alerts": created,
            "detail": f"expired_skipped={skipped_expired}",
            "audit_seq": seq,
        }
    except Exception as exc:  # noqa: BLE001 - 监测失败要落记录，不能吞掉也不能炸穿 API
        detail = f"{type(exc).__name__}: {exc}"[:300]
        finished = monitor_now(conn)
        # 失败记录必须先落库、且**独立于审计**：审计写失败本身也是失败原因之一
        # （tests/test_security_proactive.py 的 P6 就是这么造的），
        # 把它俩放同一个事务里，审计一炸连「这次监测失败过」都留不下来。
        try:
            with db.tx(conn):
                conn.execute(
                    "INSERT INTO monitor_runs"
                    " (id, started_ms, finished_ms, status, alerts_created, detail, trigger)"
                    " VALUES (?, ?, ?, 'failed', 0, ?, ?)",
                    (run_id, stamp, finished, detail, trigger),
                )
        except Exception:  # noqa: BLE001 - 落不下失败记录只能记日志，不能再上抛
            log.warn("monitor.run_record_failed", run_id=run_id, detail=detail)
        try:
            with db.tx(conn):
                audit.append(
                    conn,
                    "proactive.monitor_run",
                    {
                        "run_id": run_id,
                        "status": "failed",
                        "goals_checked": len(goals),
                        "rules_checked": rules_checked,
                        "alerts_created": 0,
                        "detail": detail,
                        "trigger": trigger,
                    },
                    session_id=session_id,
                    task_id=task_id,
                )
        except Exception:  # noqa: BLE001 - 审计不可用不影响这次失败已被记录
            log.warn("monitor.failure_audit_failed", run_id=run_id)
        return {
            "run_id": run_id,
            "status": "failed",
            "started_ms": stamp,
            "started_text": timefmt.maybe_minute_text(stamp),
            "finished_ms": finished,
            "finished_text": timefmt.maybe_minute_text(finished),
            "goals_checked": len(goals),
            "rules_checked": rules_checked,
            "alerts_created": 0,
            "alerts": [],
            "detail": detail,
        }


def list_runs(conn, limit: int = DEFAULT_LIMIT) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM monitor_runs ORDER BY started_ms DESC, id DESC LIMIT ?",
        (max(1, min(int(limit), MAX_LIMIT)),),
    ).fetchall()
    return [
        {
            "run_id": str(row["id"]),
            "started_ms": int(row["started_ms"]),
            "finished_ms": int(row["finished_ms"]) if row["finished_ms"] is not None else None,
            "status": str(row["status"]),
            "alerts_created": int(row["alerts_created"]),
            "detail": str(row["detail"]),
            "trigger": str(row["trigger"]),
            "trigger_label": "后台节拍" if str(row["trigger"]) == TRIGGER_SCHEDULER else "手动触发",
        }
        for row in rows
    ]


def monitor_interval_ms() -> int:
    """两次自动监测之间的最小间隔；非法值退回默认，最小 1 分钟。"""
    raw = os.getenv(MONITOR_INTERVAL_ENV, "").strip()
    if not raw:
        return DEFAULT_MONITOR_INTERVAL_MS
    try:
        value = int(raw)
    except ValueError:
        log.warn("proactive.bad_monitor_interval", raw=raw)
        return DEFAULT_MONITOR_INTERVAL_MS
    return max(value, MIN_MONITOR_INTERVAL_MS)


def run_due_monitors(
    conn,
    *,
    now: int | None = None,
    interval_ms: int | None = None,
) -> dict[str, Any]:
    """后台节拍器调用：只监测「用户显式打开监测开关」的目标。

    三条边界，缺一条就会变成偷偷摸摸的管家：
    1. 没打开监测的目标一律不碰（monitor_rules.enabled 必须有一个是 1）；
    2. 同一个目标按 PROACTIVE_MONITOR_MS 节流，last_monitored_ms 落库，
       所以进程重启也不会把同一小时重复算一遍；
    3. 只写提醒与运行记录，不动钱。
    """
    stamp = int(now) if now is not None else monitor_now(conn)
    interval = monitor_interval_ms() if interval_ms is None else max(int(interval_ms), 0)
    goals = _goal_rows(conn, status="active", limit=MAX_LIMIT)
    due: list[str] = []
    skipped_no_rule = 0
    skipped_throttled = 0
    for row in goals:
        enabled = [
            rule for rule in rules_for_goal(conn, str(row["id"]), enabled_only=True)
        ]
        if not enabled:
            skipped_no_rule += 1
            continue
        last = int(_int_field(row, "last_monitored_ms", 0))
        if last and stamp - last < interval:
            skipped_throttled += 1
            continue
        due.append(str(row["id"]))

    runs: list[dict[str, Any]] = []
    failed = 0
    for goal_id in due:
        try:
            run = run_monitor(conn, goal_id=goal_id, trigger=TRIGGER_SCHEDULER)
        except Exception as exc:  # noqa: BLE001 - 一个目标炸了不能拖垮整轮
            failed += 1
            log.warn(
                "proactive.monitor_failed",
                goal_id=goal_id,
                detail=f"{type(exc).__name__}: {exc}"[:200],
            )
            continue
        runs.append(run)
        if run.get("status") == "ok":
            with db.tx(conn):
                conn.execute(
                    "UPDATE savings_goals SET last_monitored_ms = ? WHERE id = ?",
                    (stamp, goal_id),
                )
        else:
            failed += 1

    return {
        "checked": len(due),
        "runs": runs,
        "alerts_created": sum(int(r.get("alerts_created") or 0) for r in runs),
        "failed": failed,
        "skipped_no_rule": skipped_no_rule,
        "skipped_throttled": skipped_throttled,
        "trigger": TRIGGER_SCHEDULER,
        "interval_ms": interval,
        "now_ms": stamp,
        "note": "只监测用户打开监测开关的目标；同一目标按间隔节流；不动钱。",
    }


__all__ = [
    "ACHIEVED",
    "ALERT_ACTIONS",
    "ALERT_ACTION_IGNORE",
    "ALERT_ACTION_RESOLVE",
    "ALERT_STATUSES",
    "CURRENCY",
    "DEFAULT_PROGRESS_TOLERANCE",
    "ESSENTIAL_CATEGORIES",
    "EXPIRED",
    "FEASIBLE",
    "GOAL_STATUSES",
    "INFEASIBLE",
    "INTERNAL_CATEGORIES",
    "LOOKBACK_MONTHS",
    "MAX_LIMIT",
    "MAX_TARGET_FEN",
    "REASON_BUDGET_OVERSPENT",
    "REASON_CASHFLOW_INSUFFICIENT",
    "REASON_PROGRESS_BEHIND",
    "RULE_KINDS",
    "RULE_PERIODS",
    "UNKNOWN",
    "alert_view",
    "analyze_cashflow",
    "budget_deviation",
    "compute_savings_plan",
    "count_open_alerts",
    "create_alert",
    "create_goal",
    "days_remaining",
    "dedupe_transactions",
    "default_user_id",
    "get_alert",
    "get_goal",
    "goal_view",
    "list_alerts",
    "list_goals",
    "list_runs",
    "local_date",
    "monitor_interval_ms",
    "monitor_now",
    "run_due_monitors",
    "MONITOR_INTERVAL_ENV",
    "TRIGGER_MANUAL",
    "TRIGGER_SCHEDULER",
    "parse_target_date",
    "periods_remaining",
    "recent_months",
    "required_per_period",
    "resolve_alert",
    "rules_for_goal",
    "run_monitor",
    "transactions_for_user",
    "update_goal",
]
