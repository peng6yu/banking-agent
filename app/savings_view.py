"""储蓄计划的读模型：状态说法、这一期该划多少、划款记录。

写入（真的动钱）在 app/savings.py；这里只读库里的既有事实，不写一行。
拆开是因为这个文件被视图与 API 共用，而 action 那份要 import funds。
"""

from __future__ import annotations

import sqlite3
from datetime import date
from typing import Any, Final

from app import timefmt
from app.clock import ValidationError, now_ms
from app.money import fen_to_yuan_str

CADENCES: Final[tuple[str, ...]] = ("monthly", "weekly")
CADENCE_LABELS: Final[dict[str, str]] = {"monthly": "每月", "weekly": "每周"}
_PERIOD_DAYS: Final[dict[str, int]] = {"monthly": 30, "weekly": 7}
DAY_MS: Final[int] = 86_400_000
# 一期最多划 5,000 元：再多就不是"每期攒一点"，而是搬一大笔，
# 那种事该用户自己在对话里逐笔确认，不能交给后台定时器。
MAX_PERIOD_FEN: Final[int] = 500_000
MIN_PERIOD_FEN: Final[int] = 100
DEPOSIT_CATEGORY: Final[str] = "储蓄"
SOURCES: Final[tuple[str, ...]] = ("cycle", "manual")
STATE_LABELS: Final[dict[str, str]] = {
    "draft": "还没授权",
    "active": "每期自动划",
    "paused": "已暂停",
    "achieved": "已达成",
    "cancelled": "已取消",
}


def _val(row: Any, key: str, default: Any = None) -> Any:
    """sqlite3.Row 没有 .get()，这里统一成「读不到就给默认值」。"""
    try:
        value = row[key]
    except (IndexError, KeyError):
        return default
    return default if value is None else value


def period_ms(cadence: str) -> int:
    days = _PERIOD_DAYS.get(str(cadence))
    if days is None:
        raise ValidationError("这个节奏我不认识，只能选每月或每周。", cadence=str(cadence))
    return days * DAY_MS


def cadence_label(cadence: str) -> str:
    return CADENCE_LABELS.get(str(cadence), "")


def goal_state(row: Any) -> str:
    """五种状态：draft（没选卡/没授权，后台绝不自动划）/ active / paused / achieved / cancelled。"""
    status = str(_val(row, "status", ""))
    if status == "cancelled":
        return "cancelled"
    if status == "achieved":
        return "achieved"
    if str(_val(row, "paused_reason", "")):
        return "paused"
    if not str(_val(row, "cadence", "")) or not str(_val(row, "account_id", "")):
        return "draft"
    if not int(_val(row, "authorized_ms", 0)):
        return "draft"
    return "active"


def state_label(state: str) -> str:
    return STATE_LABELS.get(str(state), "")


def periods_left(target_date: str, *, cadence: str, now: int | None = None) -> int:
    """到目标日还剩几期：今天已过期就当 1 期（逼一次「这钱还存不存」的对话）。"""
    try:
        target = date.fromisoformat(str(target_date))
    except (TypeError, ValueError):
        return 1
    stamp = now_ms() if now is None else int(now)
    days = (target - date.fromtimestamp(stamp / 1000)).days
    if days <= 0:
        return 1
    per = _PERIOD_DAYS.get(str(cadence), 30)
    return max(1, -(-days // per))


def _round_up_to_yuan(fen: int) -> int:
    return -(-int(fen) // MIN_PERIOD_FEN) * MIN_PERIOD_FEN


def suggest_amount(row: Any, *, now: int | None = None) -> int:
    """这一期该划多少：锁了固定值就用它，否则「缺口 ÷ 剩余期数」向上取整到元。"""
    target = int(_val(row, "target_fen", 0))
    saved = int(_val(row, "saved_fen", 0))
    gap = max(0, target - saved)
    if gap <= 0:
        return 0
    fixed = int(_val(row, "auto_fen", 0))
    if fixed > 0:
        return min(fixed, gap)
    left = periods_left(
        str(_val(row, "target_date", "")), cadence=str(_val(row, "cadence", "")) or "monthly", now=now
    )
    return min(_round_up_to_yuan(-(-gap // left)), gap)


def _deposit_view(row: Any) -> dict[str, Any]:
    amount = int(_val(row, "amount_fen", 0))
    return {
        "deposit_id": str(_val(row, "id", "")),
        "amount_fen": amount,
        "amount_yuan": fen_to_yuan_str(amount),
        "tx_id": str(_val(row, "tx_id", "")),
        "due_ms": int(_val(row, "due_ms", 0)),
        "due_text": timefmt.minute_text(int(_val(row, "due_ms", 0))),
        "created_ms": int(_val(row, "created_ms", 0)),
        "created_text": timefmt.minute_text(int(_val(row, "created_ms", 0))),
        "source": str(_val(row, "source", "")),
        "status": str(_val(row, "status", "")),
    }


def deposits_for(conn: sqlite3.Connection, goal_id: str, *, limit: int = 5) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM savings_deposits WHERE goal_id = ? ORDER BY created_ms DESC LIMIT ?",
        (str(goal_id), int(limit)),
    ).fetchall()
    return [
        {
            **_deposit_view(row),
            "refunded_ms": int(_val(row, "refunded_ms", 0)),
            "refunded_text": timefmt.maybe_minute_text(int(_val(row, "refunded_ms", 0))) or "",
        }
        for row in rows
    ]


def savings_goal_view(conn: sqlite3.Connection, row: Any, *, now: int | None = None) -> dict[str, Any]:
    """一个计划的完整现状：状态、节奏、这一期划多少、下次什么时候、划了几笔、退了多少。"""
    stamp = now_ms() if now is None else int(now)
    goal_id = str(_val(row, "id", ""))
    state = goal_state(row)
    cadence = str(_val(row, "cadence", ""))
    target = int(_val(row, "target_fen", 0))
    saved = int(_val(row, "saved_fen", 0))
    next_due = int(_val(row, "next_due_ms", 0))
    deposited_count = 0
    deposited_sum = 0
    found = conn.execute(
        "SELECT COUNT(*) AS n, COALESCE(SUM(amount_fen), 0) AS total FROM savings_deposits"
        " WHERE goal_id = ? AND status = 'deposited'",
        (goal_id,),
    ).fetchone()
    if found is not None:
        deposited_count = int(found["n"])
        deposited_sum = int(found["total"])
    account_id = str(_val(row, "account_id", ""))
    account_label = ""
    if account_id:
        found_account = conn.execute(
            "SELECT name, tail FROM accounts WHERE id = ?", (account_id,)
        ).fetchone()
        if found_account is not None:
            account_label = str(found_account["name"]) + " 尾号 " + str(found_account["tail"])
    fixed = int(_val(row, "auto_fen", 0))
    amount = suggest_amount(row, now=stamp)
    return {
        "goal_id": goal_id,
        "name": str(_val(row, "name", "")),
        "state": state,
        "state_label": state_label(state),
        "status": str(_val(row, "status", "")),
        "cadence": cadence,
        "cadence_label": cadence_label(cadence),
        "account_id": account_id,
        "account_label": account_label,
        "authorized": bool(int(_val(row, "authorized_ms", 0))),
        "authorized_ms": int(_val(row, "authorized_ms", 0)),
        "auto_fen": fixed,
        "auto_yuan": fen_to_yuan_str(fixed) if fixed else "",
        "amount_fen": amount,
        "amount_yuan": fen_to_yuan_str(amount),
        "target_fen": target,
        "target_yuan": fen_to_yuan_str(target),
        "target_date": str(_val(row, "target_date", "")),
        "saved_fen": saved,
        "saved_yuan": fen_to_yuan_str(saved),
        "remaining_fen": max(0, target - saved),
        "remaining_yuan": fen_to_yuan_str(max(0, target - saved)),
        "progress_pct": 0 if target <= 0 else min(100, int(round(saved * 100 / target))),
        "periods_remaining": periods_left(str(_val(row, "target_date", "")), cadence=cadence or "monthly", now=stamp),
        "next_due_ms": next_due,
        "next_due_text": timefmt.maybe_minute_text(next_due) or "",
        "deposited_fen": deposited_sum,
        "deposited_yuan": fen_to_yuan_str(deposited_sum),
        "deposit_count": deposited_count,
        "refunded_fen": int(_val(row, "refunded_fen", 0)),
        "refunded_yuan": fen_to_yuan_str(int(_val(row, "refunded_fen", 0))),
        "paused_reason": str(_val(row, "paused_reason", "")),
        "last_run_ms": int(_val(row, "last_run_ms", 0)),
        "last_run_text": timefmt.maybe_minute_text(int(_val(row, "last_run_ms", 0))) or "",
        "last_result": str(_val(row, "last_result", "")),
        "closed_ms": int(_val(row, "closed_ms", 0)),
        "closed_text": timefmt.maybe_minute_text(int(_val(row, "closed_ms", 0))) or "",
        "deposits": deposits_for(conn, goal_id),
    }


