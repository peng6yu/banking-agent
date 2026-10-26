"""确定性储蓄计算模块的单测（阶段 5）：公式、口径与全部边界。

这里只测 app/proactive.py 的纯函数：输入交易行，输出整数分。任何一条断言都不该
依赖真实时钟或模型——金额算错比提醒晚发严重得多，所以边界要一条条钉住。
"""

from __future__ import annotations

import time
from datetime import date

import pytest

from app import proactive
from app.clock import ValidationError

BASE = date(2026, 10, 23)


def _ms(year: int, month: int, day: int, hour: int = 12) -> int:
    """本地时区的某天中午，避免跨时区把日期推到前一天。"""
    return int(time.mktime((year, month, day, hour, 0, 0, 0, 0, -1)) * 1000)


def _tx(
    tx_id: str,
    direction: str,
    amount_fen: int,
    category: str,
    ts_ms: int,
    *,
    counterparty: str = "商户",
    account_id: str = "a_1",
    note: str = "",
    reversal_of: str | None = None,
    reversed_by: str | None = None,
) -> dict[str, object]:
    return {
        "id": tx_id,
        "account_id": account_id,
        "direction": direction,
        "amount_fen": amount_fen,
        "counterparty": counterparty,
        "category": category,
        "note": note,
        "ts_ms": ts_ms,
        "reversal_of": reversal_of,
        "reversed_by": reversed_by,
    }


# 三个完整月（7/8/9）的收入与必要支出，外加一笔可选消费和一笔当月支出。
HISTORY = [
    _tx("t_in7", "in", 1_280_000, "工资", _ms(2026, 7, 10), counterparty="汇丰银行代发"),
    _tx("t_es7", "out", 400_000, "餐饮", _ms(2026, 7, 15)),
    _tx("t_in8", "in", 1_280_000, "工资", _ms(2026, 8, 10), counterparty="汇丰银行代发"),
    _tx("t_es8", "out", 400_000, "餐饮", _ms(2026, 8, 15)),
    _tx("t_in9", "in", 1_280_000, "工资", _ms(2026, 9, 10), counterparty="汇丰银行代发"),
    _tx("t_es9", "out", 400_000, "餐饮", _ms(2026, 9, 15)),
    _tx("t_shop9", "out", 200_000, "购物", _ms(2026, 9, 20)),
    # 当月（10 月）还没走完，不能压低基准。
    _tx("t_cur", "out", 999_999, "餐饮", _ms(2026, 10, 5)),
]


# ---------------------------------------------------------------- 周期与日期


def test_periods_remaining_counts_natural_months_including_current() -> None:
    assert proactive.periods_remaining(date(2026, 12, 31), BASE) == 3
    assert proactive.periods_remaining(date(2026, 10, 31), BASE) == 1
    assert proactive.periods_remaining(date(2026, 11, 30), BASE) == 2
    assert proactive.periods_remaining(date(2027, 1, 1), BASE) == 4
    # 目标日期已过：0 表示"没有下一期了"，而不是"不用存"。
    assert proactive.periods_remaining(date(2026, 10, 22), BASE) == 0


def test_days_remaining_is_calendar_days() -> None:
    assert proactive.days_remaining(date(2026, 12, 31), BASE) == 69
    assert proactive.days_remaining(date(2026, 10, 23), BASE) == 0
    assert proactive.days_remaining(date(2026, 10, 1), BASE) == -22


def test_recent_months_crosses_year_boundary_and_excludes_current() -> None:
    assert proactive.recent_months(date(2026, 2, 15), 3) == ["2025-11", "2025-12", "2026-01"]
    assert proactive.recent_months(date(2026, 10, 23), 1) == ["2026-09"]


def test_month_key_uses_local_time() -> None:
    assert proactive.month_key(_ms(2026, 9, 30, 23)) == "2026-09"
    assert proactive.month_key(_ms(2026, 10, 1, 1)) == "2026-10"


# ---------------------------------------------------------------- 每期要求


def test_required_per_period_rounds_up_never_down() -> None:
    assert proactive.required_per_period(2_500_000, 3) == 833_334
    assert proactive.required_per_period(1, 3) == 1
    assert proactive.required_per_period(100, 4) == 25
    assert proactive.required_per_period(0, 3) == 0
    # 已到期：缺口退化成"现在就要一次性补齐"。
    assert proactive.required_per_period(10, 0) == 10
    assert proactive.required_per_period(10, -1) == 10


# ---------------------------------------------------------------- 现金流口径


def test_analyze_cashflow_uses_complete_months_only() -> None:
    cash = proactive.analyze_cashflow(HISTORY, base=BASE, lookback_months=6)

    assert cash["months"] == ["2026-04", "2026-05", "2026-06", "2026-07", "2026-08", "2026-09"]
    assert cash["months_with_data"] == ["2026-07", "2026-08", "2026-09"]
    assert cash["months_with_data_count"] == 3
    assert cash["has_history"] is True
    assert cash["income_total_fen"] == 3_840_000
    assert cash["essential_total_fen"] == 1_200_000
    assert cash["spend_total_fen"] == 1_400_000
    assert cash["avg_income_fen"] == 1_280_000
    assert cash["avg_essential_fen"] == 400_000
    assert cash["avg_spend_fen"] == 466_667  # 1,400,000 / 3 四舍五入
    assert cash["projected_period_balance_fen"] == 880_000
    assert cash["currency"] == "CNY"


def test_analyze_cashflow_without_history_is_unknown_not_zero_confidence() -> None:
    cash = proactive.analyze_cashflow([], base=BASE)
    assert cash["has_history"] is False
    assert cash["months_with_data_count"] == 0
    assert cash["avg_income_fen"] == 0
    assert cash["avg_essential_fen"] == 0
    assert cash["projected_period_balance_fen"] == 0


def test_analyze_cashflow_ignores_internal_transfers_and_bad_amounts() -> None:
    rows = [
        _tx("t1", "in", 5_000_000, "账户互转", _ms(2026, 9, 3)),
        _tx("t2", "out", 5_000_000, "理财", _ms(2026, 9, 4)),
        _tx("t3", "out", 0, "餐饮", _ms(2026, 9, 5)),
        _tx("t4", "out", -400_000, "餐饮", _ms(2026, 9, 6)),
        _tx("t5", "in", 1_000_000, "工资", _ms(2026, 9, 7)),
    ]
    cash = proactive.analyze_cashflow(rows, base=BASE)
    # 内部搬运不算收支；0 与负数不污染平均值。
    assert cash["income_total_fen"] == 1_000_000
    assert cash["essential_total_fen"] == 0
    assert cash["spend_total_fen"] == 0
    assert cash["avg_income_fen"] == 1_000_000
    assert cash["projected_period_balance_fen"] == 1_000_000


def test_analyze_cashflow_clamps_lookback_window() -> None:
    assert proactive.analyze_cashflow([], base=BASE, lookback_months=0)["lookback_months"] == 1
    assert proactive.analyze_cashflow([], base=BASE, lookback_months=99)["lookback_months"] == 24


def test_dedupe_drops_reversal_pairs_and_exact_duplicates() -> None:
    ts = _ms(2026, 9, 12)
    rows = [
        _tx("t_orig", "out", 400_000, "餐饮", ts, reversed_by="t_rev"),
        _tx("t_rev", "in", 400_000, "餐饮", ts, reversal_of="t_orig"),
        _tx("t_keep", "out", 400_000, "餐饮", ts, counterparty="海底捞"),
        _tx("t_dup", "out", 400_000, "餐饮", ts, counterparty="海底捞"),
    ]
    kept, stats = proactive.dedupe_transactions(rows)
    assert [row["id"] for row in kept] == ["t_keep"]
    assert stats == {
        "input_count": 4,
        "reversal_original": 1,
        "reversal_entry": 1,
        "duplicate": 1,
    }


def test_refunded_and_duplicated_rows_do_not_inflate_spending() -> None:
    ts = _ms(2026, 9, 12)
    rows = [
        _tx("t_big", "out", 900_000, "餐饮", ts, reversed_by="t_back"),
        _tx("t_back", "in", 900_000, "餐饮", ts, reversal_of="t_big"),
        _tx("t_spend", "out", 400_000, "餐饮", ts, counterparty="海底捞"),
        _tx("t_spend_dup", "out", 400_000, "餐饮", ts, counterparty="海底捞"),
    ]
    cash = proactive.analyze_cashflow(rows, base=BASE)
    assert cash["spend_total_fen"] == 400_000
    assert cash["essential_total_fen"] == 400_000


# ---------------------------------------------------------------- 储蓄计划


def test_compute_plan_feasible_path() -> None:
    plan = proactive.compute_savings_plan(
        target_fen=3_000_000,
        target_date="2026-12-31",
        saved_fen=500_000,
        transactions=HISTORY,
        base=BASE,
    )
    assert plan["currency"] == "CNY"
    assert plan["target_fen"] == 3_000_000
    assert plan["saved_fen"] == 500_000
    assert plan["remaining_fen"] == 2_500_000
    assert plan["periods_remaining"] == 3
    assert plan["period_unit"] == "month"
    assert plan["required_per_period_fen"] == 833_334
    assert plan["progress_pct"] == 16
    assert plan["avg_income_fen"] == 1_280_000
    assert plan["avg_essential_fen"] == 400_000
    assert plan["projected_period_balance_fen"] == 880_000
    assert plan["period_gap_fen"] == 0  # 833,334 < 880,000，没有缺口
    assert plan["period_surplus_fen"] == 46_666  # 880,000 - 833,334，盈余是另一个概念
    assert plan["total_gap_fen"] == 0
    assert plan["feasibility"] == proactive.FEASIBLE
    assert plan["warnings"] == []
    assert plan["assumptions"]  # 假设必须显式给出来，模型只负责解释
    # 全程整数分：不许出现 float。
    for key in (
        "target_fen",
        "saved_fen",
        "remaining_fen",
        "required_per_period_fen",
        "period_gap_fen",
        "period_surplus_fen",
        "total_gap_fen",
        "avg_income_fen",
        "avg_essential_fen",
        "avg_spend_fen",
        "projected_period_balance_fen",
        "days_remaining",
        "periods_remaining",
        "progress_pct",
    ):
        assert isinstance(plan[key], int) and not isinstance(plan[key], bool), key


def test_compute_plan_achieved_goal() -> None:
    plan = proactive.compute_savings_plan(
        target_fen=3_000_000,
        target_date="2026-12-31",
        saved_fen=3_000_000,
        transactions=HISTORY,
        base=BASE,
    )
    assert plan["remaining_fen"] == 0
    assert plan["required_per_period_fen"] == 0
    assert plan["progress_pct"] == 100
    assert plan["feasibility"] == proactive.ACHIEVED
    assert "GOAL_ACHIEVED" in plan["warnings"]


def test_compute_plan_past_target_date_is_expired() -> None:
    plan = proactive.compute_savings_plan(
        target_fen=3_000_000,
        target_date="2026-10-01",
        saved_fen=1_000_000,
        transactions=HISTORY,
        base=BASE,
    )
    assert plan["days_remaining"] == -22
    assert plan["periods_remaining"] == 0
    # 已经到期：缺口退化成一次性补齐，绝不显示"每期 0 元"。
    assert plan["required_per_period_fen"] == 2_000_000
    assert plan["feasibility"] == proactive.EXPIRED
    assert "TARGET_DATE_PASSED" in plan["warnings"]


def test_compute_plan_without_history_is_unknown() -> None:
    plan = proactive.compute_savings_plan(
        target_fen=3_000_000,
        target_date="2026-12-31",
        saved_fen=0,
        transactions=[],
        base=BASE,
    )
    assert plan["feasibility"] == proactive.UNKNOWN
    assert plan["has_history"] is False
    assert "NO_TRANSACTION_HISTORY" in plan["warnings"]
    assert plan["projected_period_balance_fen"] == 0


def test_compute_plan_cashflow_not_positive() -> None:
    rows = [
        _tx("t_in", "in", 500_000, "工资", _ms(2026, 9, 10)),
        _tx("t_es", "out", 900_000, "餐饮", _ms(2026, 9, 12)),
    ]
    plan = proactive.compute_savings_plan(
        target_fen=3_000_000,
        target_date="2026-12-31",
        saved_fen=0,
        transactions=rows,
        base=BASE,
    )
    assert plan["projected_period_balance_fen"] == -400_000
    assert plan["feasibility"] == proactive.INFEASIBLE
    assert "CASHFLOW_NOT_POSITIVE" in plan["warnings"]


def test_compute_plan_required_exceeds_surplus() -> None:
    plan = proactive.compute_savings_plan(
        target_fen=12_000_000,
        target_date="2026-12-31",
        saved_fen=0,
        transactions=HISTORY,
        base=BASE,
    )
    assert plan["required_per_period_fen"] == 4_000_000
    assert plan["projected_period_balance_fen"] == 880_000
    assert plan["period_gap_fen"] == 3_120_000
    assert plan["period_surplus_fen"] == 0  # 有缺口时盈余必为 0
    assert plan["total_gap_fen"] == 9_360_000
    assert plan["feasibility"] == proactive.INFEASIBLE
    assert "REQUIRED_EXCEEDS_SURPLUS" in plan["warnings"]


@pytest.mark.parametrize("bad_target", [0, -1, True, 3_000_000.0, "3000000", None])
def test_compute_plan_rejects_bad_target_amount(bad_target: object) -> None:
    with pytest.raises(ValidationError):
        proactive.compute_savings_plan(
            target_fen=bad_target,  # type: ignore[arg-type]
            target_date="2026-12-31",
            saved_fen=0,
            transactions=HISTORY,
            base=BASE,
        )


@pytest.mark.parametrize("bad_saved", [-1, 1.5, "100", True])
def test_compute_plan_rejects_bad_saved_amount(bad_saved: object) -> None:
    with pytest.raises(ValidationError):
        proactive.compute_savings_plan(
            target_fen=3_000_000,
            target_date="2026-12-31",
            saved_fen=bad_saved,  # type: ignore[arg-type]
            transactions=HISTORY,
            base=BASE,
        )


@pytest.mark.parametrize("bad_date", ["2026/12/31", "2026-2-3", "2026-13-01", 20261231, None])
def test_compute_plan_rejects_bad_target_date(bad_date: object) -> None:
    with pytest.raises(ValidationError):
        proactive.compute_savings_plan(
            target_fen=3_000_000,
            target_date=bad_date,  # type: ignore[arg-type]
            saved_fen=0,
            transactions=HISTORY,
            base=BASE,
        )


def test_compute_plan_currency_is_fixed_to_cny() -> None:
    with pytest.raises(ValidationError):
        proactive.compute_savings_plan(
            target_fen=3_000_000,
            target_date="2026-12-31",
            currency="USD",
            base=BASE,
        )


def test_target_amount_upper_bound() -> None:
    with pytest.raises(ValidationError):
        proactive.compute_savings_plan(
            target_fen=proactive.MAX_TARGET_FEN + 1,
            target_date="2026-12-31",
            base=BASE,
        )


# ---------------------------------------------------------------- 预算偏差


def test_budget_deviation_over_budget() -> None:
    deviation = proactive.budget_deviation(budget_fen=400_000, actual_fen=520_000)
    assert deviation["deviation_fen"] == 120_000
    assert deviation["over_budget"] is True
    assert deviation["over_fen"] == 120_000
    assert deviation["remaining_fen"] == 0
    assert deviation["usage_pct"] == 130
    assert deviation["period"] == "monthly"


def test_budget_deviation_under_budget() -> None:
    deviation = proactive.budget_deviation(budget_fen=400_000, actual_fen=300_000)
    assert deviation["deviation_fen"] == -100_000
    assert deviation["over_budget"] is False
    assert deviation["over_fen"] == 0
    assert deviation["remaining_fen"] == 100_000
    assert deviation["usage_pct"] == 75


def test_budget_deviation_weekly_period() -> None:
    deviation = proactive.budget_deviation(budget_fen=100_000, actual_fen=100_000, period="weekly")
    assert deviation["over_budget"] is False
    assert deviation["usage_pct"] == 100


@pytest.mark.parametrize(
    ("budget", "actual", "period"),
    [(0, 100, "monthly"), (-1, 100, "monthly"), (100, -1, "monthly"), (100, 100, "daily")],
)
def test_budget_deviation_rejects_bad_inputs(budget: int, actual: int, period: str) -> None:
    with pytest.raises(ValidationError):
        proactive.budget_deviation(budget_fen=budget, actual_fen=actual, period=period)
