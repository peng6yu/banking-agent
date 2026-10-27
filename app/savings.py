"""储蓄计划：每期真的把一笔钱从储蓄卡划进目标，取消时原路划回去。

用户的定义（原话）：「计划是指的是把计划每个周期把钱划出来一部分达成目标，
取消就划回去，也是需要动钱的。」所以这一层不是"记个数"，而是真的动钱：

- 划走：储蓄卡余额减少，目标里增加，流水里留一条「内部搬运 · 目标名」；
- 划回：取消计划时把划过的钱一次性打回原卡，流水里留一条反向记录；
- 划不走：余额不够就跳过这一期，写一条 skipped 审计，界面上说清"这期没攒上"。

纪律（和预约转账同一套）：
1. 没有用户勾的授权（authorized_ms）绝不自动划，cadence 为空就只是算数；
2. 金额是确定性算出来的（缺口 ÷ 剩余期数，向上取整到元），模型不参与；
3. 每期一笔都进审计、都对应一条流水，用户点开能追到钱去哪了；
4. 单期上限 MAX_PERIOD_FEN，超过就得自己手动存，不让后台一次划走一大笔。

分类用 money.INTERNAL_CATEGORIES 里的「储蓄」：它不属于收支，所以账单的
「花了多少」不会被自己的储蓄计划污染，但流水明细里照样看得见。
"""

from __future__ import annotations

import sqlite3
from typing import Any

import app.audit as audit
import app.db as db
import app.funds as funds
from app.clock import BankError, InsufficientFunds, NotFound, ValidationError, now_ms
from app.ids import new_id
from app.money import fen_to_yuan_str
from app.savings_view import (
    CADENCE_LABELS,
    CADENCES,
    DAY_MS,
    DEPOSIT_CATEGORY,
    MAX_PERIOD_FEN,
    MIN_PERIOD_FEN,
    SOURCES,
    STATE_LABELS,
    _deposit_view,
    _val,
    cadence_label,
    deposits_for,
    goal_state,
    period_ms,
    periods_left,
    savings_goal_view,
    state_label,
    suggest_amount,
)

def _load_goal(conn: sqlite3.Connection, goal_id: str, user_id: str | None = None) -> Any:
    sql = "SELECT * FROM savings_goals WHERE id = ?"
    args: list[Any] = [str(goal_id)]
    if user_id:
        sql += " AND user_id = ?"
        args.append(str(user_id))
    row = conn.execute(sql, args).fetchone()
    if row is None:
        raise NotFound("没有这个储蓄计划，可能已经被取消了。", goal_id=str(goal_id))
    return row


def _load_account(conn: sqlite3.Connection, account_id: str, user_id: str) -> Any:
    row = conn.execute("SELECT * FROM accounts WHERE id = ?", (str(account_id),)).fetchone()
    if row is None or str(row["user_id"]) != str(user_id):
        raise ValidationError("这张卡我没找到，换一张储蓄卡试试。", account_id=str(account_id))
    if str(row["type"]) != "debit":
        raise ValidationError(
            "储蓄计划只从储蓄卡划钱：信用卡的钱本来就是银行的，得先还款。",
            account_id=str(account_id),
        )
    if str(_val(row, "status", "active")) != "active":
        raise ValidationError("这张卡现在不是正常状态，先解冻再来划。", account_id=str(account_id))
    return row


def _check_amount(amount: int, row: Any) -> int:
    value = int(amount)
    if value < MIN_PERIOD_FEN:
        raise ValidationError("一次至少划 1 元，这么点钱来回划没有意义。", amount_fen=value)
    if value > MAX_PERIOD_FEN:
        raise ValidationError(
            "一期最多划 5,000 元。要一次存更多，直接跟我说「给换一台相机存 8000」，我逐笔跟你确认。",
            amount_fen=value,
            max_fen=MAX_PERIOD_FEN,
        )
    gap = max(0, int(_val(row, "target_fen", 0)) - int(_val(row, "saved_fen", 0)))
    if gap and value > gap:
        raise ValidationError(
            "这一笔比还差的钱多：只差 " + fen_to_yuan_str(gap) + " 元就到目标了。",
            amount_fen=value,
            gap_fen=gap,
        )
    return value


def authorize(
    conn: sqlite3.Connection,
    *,
    goal_id: str,
    account_id: str,
    cadence: str = "monthly",
    auto_fen: int = 0,
    user_id: str | None = None,
    now: int | None = None,
    session_id: str | None = None,
    task_id: str | None = None,
) -> dict[str, Any]:
    """用户勾了「每期自动划」：记住从哪张卡、什么节奏、每期多少，从下一期开始执行。"""
    if str(cadence) not in CADENCES:
        raise ValidationError("节奏只能是每月或每周。", cadence=str(cadence))
    row = _load_goal(conn, goal_id, user_id)
    if goal_state(row) == "cancelled":
        raise ValidationError("这个计划已经取消了，恢复它才能重新授权。", goal_id=str(goal_id))
    owner = str(user_id or _val(row, "user_id", ""))
    _load_account(conn, account_id, owner)
    fixed = int(auto_fen or 0)
    if fixed < 0:
        raise ValidationError("每期金额不能是负数。", auto_fen=fixed)
    if fixed and fixed > MAX_PERIOD_FEN:
        raise ValidationError("一期最多划 5,000 元。", auto_fen=fixed, max_fen=MAX_PERIOD_FEN)
    stamp = now_ms() if now is None else int(now)
    due = stamp + period_ms(cadence)
    with db.tx(conn):
        conn.execute(
            "UPDATE savings_goals SET cadence = ?, account_id = ?, auto_fen = ?,"
            " authorized_ms = ?, next_due_ms = ?, paused_reason = '', updated_ms = ?"
            " WHERE id = ?",
            (str(cadence), str(account_id), fixed, stamp, due, stamp, str(goal_id)),
        )
        seq = audit.append(
            conn,
            "savings_goal.updated",
            {
                "goal_id": str(goal_id),
                "changes": {
                    "cadence": [str(_val(row, "cadence", "")), str(cadence)],
                    "account_id": [str(_val(row, "account_id", "")), str(account_id)],
                    "auto_fen": [int(_val(row, "auto_fen", 0)), fixed],
                },
                "authorized_ms": stamp,
                "next_due_ms": due,
                "updated_ms": stamp,
            },
            session_id=session_id,
            task_id=task_id,
        )
    fresh = _load_goal(conn, goal_id, owner)
    return {"goal": savings_goal_view(conn, fresh, now=stamp), "audit_seq": seq}


def deposit(
    conn: sqlite3.Connection,
    *,
    goal_id: str,
    user_id: str | None = None,
    amount_fen: int | None = None,
    source: str = "manual",
    due_ms: int | None = None,
    now: int | None = None,
    session_id: str | None = None,
    task_id: str | None = None,
) -> dict[str, Any]:
    """划一笔进目标：真的改余额、真的留流水，不是改个数字。"""
    src = str(source)
    if src not in SOURCES:
        raise ValidationError("划款来源只能是 cycle 或 manual。", source=src)
    row = _load_goal(conn, goal_id, user_id)
    state = goal_state(row)
    if state == "cancelled":
        raise ValidationError("这个计划已经取消了。想继续存，先点「恢复这个计划」。", goal_id=str(goal_id))
    if state == "achieved":
        raise ValidationError("这个计划已经存够了，不用再划。要加码就先把目标金额调高。", goal_id=str(goal_id))
    account_id = str(_val(row, "account_id", ""))
    if not account_id:
        raise ValidationError("这个计划还没选从哪张卡划钱，先选一张储蓄卡。", goal_id=str(goal_id))
    owner = str(user_id or _val(row, "user_id", ""))
    _load_account(conn, account_id, owner)
    stamp = now_ms() if now is None else int(now)
    raw_amount = suggest_amount(row, now=stamp) if amount_fen is None else int(amount_fen)
    amount = _check_amount(raw_amount, row)
    due = int(due_ms or 0)
    if not due:
        due = int(_val(row, "next_due_ms", 0)) if src == "cycle" else stamp
    due = due or stamp
    with db.tx(conn):
        if src == "cycle":
            existing = _find_cycle(conn, str(goal_id), due)
            if existing is not None:
                return {
                    "deposit": _deposit_view(existing),
                    "repeated": True,
                    "goal": savings_goal_view(conn, row, now=stamp),
                }
        written = _record_deposit(
            conn, row=row, account_id=account_id, amount=amount, due=due, src=src,
            stamp=stamp, owner=owner, session_id=session_id, task_id=task_id,
        )
    return {
        "deposit": {**_deposit_view(written), "balance_after_yuan": fen_to_yuan_str(written["balance_after_fen"])},
        "repeated": False,
        "goal": savings_goal_view(conn, _load_goal(conn, goal_id), now=stamp),
        "audit_seq": written["audit_seq"],
    }


def _record_deposit(
    conn: sqlite3.Connection,
    *,
    row: Any,
    account_id: str,
    amount: int,
    due: int,
    src: str,
    stamp: int,
    owner: str,
    session_id: str | None,
    task_id: str | None,
) -> dict[str, Any]:
    """事务里的那一段：出账 → 记一期 → 改目标 → 写审计。调用方已包事务。"""
    goal_id = str(_val(row, "id", ""))
    name = str(_val(row, "name", "")) or "储蓄计划"
    found = conn.execute("SELECT balance_fen FROM accounts WHERE id = ?", (account_id,)).fetchone()
    balance_before = int(found[0]) if found else 0
    leg = funds.post_internal_leg(
        conn,
        account_id=account_id,
        direction="out",
        amount_fen=amount,
        counterparty=name,
        category=DEPOSIT_CATEGORY,
        note="储蓄计划 · " + name,
        stamp_ms=stamp,
    )
    deposit_id = new_id("sd")
    conn.execute(
        "INSERT INTO savings_deposits"
        " (id, goal_id, account_id, amount_fen, tx_id, due_ms, created_ms, status, source)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, 'deposited', ?)",
        (deposit_id, goal_id, account_id, amount, leg["tx_id"], due, stamp, src),
    )
    target = int(_val(row, "target_fen", 0))
    saved_after = int(_val(row, "saved_fen", 0)) + amount
    reached = saved_after >= target
    cadence = str(_val(row, "cadence", "")) or "monthly"
    next_due = due + period_ms(cadence) if src == "cycle" else int(_val(row, "next_due_ms", 0))
    conn.execute(
        "UPDATE savings_goals SET saved_fen = ?, last_run_ms = ?, last_result = 'deposited',"
        " next_due_ms = ?, status = ?, paused_reason = ?, updated_ms = ? WHERE id = ?",
        (saved_after, stamp, 0 if reached else next_due, "achieved" if reached else "active", "",
         stamp, goal_id),
    )
    seq = audit.append(
        conn,
        "savings.deposited",
        {
            "goal_id": goal_id,
            "user_id": owner,
            "account_id": account_id,
            "amount_fen": amount,
            "due_ms": due,
            "source": src,
            "tx_id": leg["tx_id"],
            "balance_before_fen": balance_before,
            "balance_after_fen": leg["balance_after_fen"],
            "saved_after_fen": saved_after,
            "target_fen": target,
        },
        session_id=session_id,
        task_id=task_id,
    )
    return {
        "id": deposit_id,
        "goal_id": goal_id,
        "account_id": account_id,
        "amount_fen": amount,
        "tx_id": leg["tx_id"],
        "due_ms": due,
        "created_ms": stamp,
        "source": src,
        "status": "deposited",
        "refund_tx_id": None,
        "refunded_ms": 0,
        "balance_after_fen": leg["balance_after_fen"],
        "audit_seq": seq,
    }


def _find_cycle(conn: sqlite3.Connection, goal_id: str, due_ms: int) -> Any:
    return conn.execute(
        "SELECT * FROM savings_deposits WHERE goal_id = ? AND due_ms = ? AND source = 'cycle'",
        (goal_id, int(due_ms)),
    ).fetchone()


def refund_goal(
    conn: sqlite3.Connection,
    *,
    goal_id: str,
    user_id: str | None = None,
    reason: str = "",
    now: int | None = None,
    session_id: str | None = None,
    task_id: str | None = None,
) -> dict[str, Any]:
    """取消计划：把划过的钱一次性原路打回储蓄卡，然后关掉这个计划。"""
    row = _load_goal(conn, goal_id, user_id)
    if goal_state(row) == "cancelled":
        raise ValidationError("这个计划已经取消过了，钱也早就划回去了。", goal_id=str(goal_id))
    rows = conn.execute(
        "SELECT * FROM savings_deposits WHERE goal_id = ? AND status = 'deposited'"
        " ORDER BY created_ms DESC",
        (str(goal_id),),
    ).fetchall()
    total = sum(int(r["amount_fen"]) for r in rows)
    account_id = str(_val(row, "account_id", "")) or (str(rows[0]["account_id"]) if rows else "")
    stamp = now_ms() if now is None else int(now)
    name = str(_val(row, "name", "")) or "储蓄计划"
    tx_id = ""
    balance_after: int | None = None
    with db.tx(conn):
        if total > 0 and account_id:
            leg = funds.post_internal_leg(
                conn,
                account_id=account_id,
                direction="in",
                amount_fen=total,
                counterparty=name,
                category=DEPOSIT_CATEGORY,
                note="计划取消 · 划回 " + name,
                stamp_ms=stamp,
            )
            tx_id = str(leg["tx_id"])
            balance_after = int(leg["balance_after_fen"])
            conn.execute(
                "UPDATE savings_deposits SET status = 'refunded', refund_tx_id = ?, refunded_ms = ?"
                " WHERE goal_id = ? AND status = 'deposited'",
                (tx_id, stamp, str(goal_id)),
            )
        conn.execute(
            "UPDATE savings_goals SET saved_fen = 0, status = 'cancelled',"
            " refunded_fen = refunded_fen + ?, next_due_ms = 0, paused_reason = '',"
            " closed_ms = ?, last_result = 'refunded', updated_ms = ? WHERE id = ?",
            (total, stamp, stamp, str(goal_id)),
        )
        seq = audit.append(
            conn,
            "savings.refunded",
            {
                "goal_id": str(goal_id),
                "user_id": str(_val(row, "user_id", "")),
                "account_id": account_id,
                "amount_fen": total,
                "deposit_count": len(rows),
                "tx_id": tx_id,
                "balance_after_fen": balance_after,
                "reason": str(reason or ""),
                "closed_ms": stamp,
            },
            session_id=session_id,
            task_id=task_id,
        )
    fresh = _load_goal(conn, goal_id)
    return {
        "refunded_fen": total,
        "refunded_yuan": fen_to_yuan_str(total),
        "deposit_count": len(rows),
        "tx_id": tx_id,
        "balance_after_yuan": None if balance_after is None else fen_to_yuan_str(balance_after),
        "goal": savings_goal_view(conn, fresh, now=stamp),
        "audit_seq": seq,
    }


def pause_goal(
    conn: sqlite3.Connection,
    *,
    goal_id: str,
    user_id: str | None = None,
    reason: str = "user",
    now: int | None = None,
) -> dict[str, Any]:
    """暂停：钱不动，只是不再按周期划；已经划进去的钱原地留着。"""
    row = _load_goal(conn, goal_id, user_id)
    state = goal_state(row)
    if state in ("cancelled", "achieved"):
        raise ValidationError("这个计划已经结束了，没有可暂停的。", goal_id=str(goal_id))
    if state == "paused":
        return {"goal": savings_goal_view(conn, row), "already": True}
    stamp = now_ms() if now is None else int(now)
    with db.tx(conn):
        conn.execute(
            "UPDATE savings_goals SET paused_reason = ?, updated_ms = ? WHERE id = ?",
            (str(reason or "user"), stamp, str(goal_id)),
        )
        audit.append(
            conn,
            "savings_goal.updated",
            {"goal_id": str(goal_id), "changes": {"paused_reason": ["", str(reason or "user")]},
             "updated_ms": stamp},
        )
    return {"goal": savings_goal_view(conn, _load_goal(conn, goal_id), now=stamp), "already": False}


def resume_goal(
    conn: sqlite3.Connection,
    *,
    goal_id: str,
    user_id: str | None = None,
    now: int | None = None,
) -> dict[str, Any]:
    """继续：把下次划款排在下一期，不补之前停掉的那些期。"""
    row = _load_goal(conn, goal_id, user_id)
    state = goal_state(row)
    if state == "cancelled":
        raise ValidationError("这个计划已经取消了，得先恢复它。", goal_id=str(goal_id))
    if state == "achieved":
        raise ValidationError("这个计划已经存够了，不用再继续。", goal_id=str(goal_id))
    if state != "paused":
        return {"goal": savings_goal_view(conn, row), "already": True}
    stamp = now_ms() if now is None else int(now)
    cadence = str(_val(row, "cadence", "")) or "monthly"
    due = stamp + period_ms(cadence)
    with db.tx(conn):
        conn.execute(
            "UPDATE savings_goals SET paused_reason = '', next_due_ms = ?, updated_ms = ? WHERE id = ?",
            (due, stamp, str(goal_id)),
        )
        audit.append(
            conn,
            "savings_goal.updated",
            {"goal_id": str(goal_id), "changes": {"paused_reason": ["user", ""]},
             "next_due_ms": due, "updated_ms": stamp},
        )
    return {"goal": savings_goal_view(conn, _load_goal(conn, goal_id), now=stamp), "already": False}


def _skip_cycle(conn: sqlite3.Connection, row: Any, stamp: int, want_fen: int, balance: int) -> None:
    """余额不够：顺延一期，钱不动，但要留痕，界面得说清「这期没攒上」。"""
    goal_id = str(_val(row, "id", ""))
    due = int(_val(row, "next_due_ms", 0)) or stamp
    cadence = str(_val(row, "cadence", "")) or "monthly"
    with db.tx(conn):
        conn.execute(
            "UPDATE savings_goals SET next_due_ms = ?, last_run_ms = ?, last_result = 'insufficient',"
            " updated_ms = ? WHERE id = ?",
            (due + period_ms(cadence), stamp, stamp, goal_id),
        )
        audit.append(
            conn,
            "savings.skipped",
            {
                "goal_id": goal_id,
                "user_id": str(_val(row, "user_id", "")),
                "due_ms": due,
                "want_fen": int(want_fen),
                "balance_fen": int(balance),
                "reason": "insufficient_funds",
            },
        )


def run_due_goals(conn: sqlite3.Connection, *, now: int | None = None, limit: int = 20) -> dict[str, Any]:
    """后台节拍器进的一步：把到点的计划各划一笔。一期最多划一次，靠唯一索引兜底。"""
    stamp = now_ms() if now is None else int(now)
    rows = conn.execute(
        "SELECT * FROM savings_goals WHERE status = 'active' AND cadence != ''"
        " AND authorized_ms > 0 AND paused_reason = '' AND next_due_ms > 0 AND next_due_ms <= ?"
        " ORDER BY next_due_ms LIMIT ?",
        (stamp, int(limit)),
    ).fetchall()
    deposited = 0
    skipped = 0
    results: list[dict[str, Any]] = []
    for row in rows:
        goal_id = str(_val(row, "id", ""))
        account_id = str(_val(row, "account_id", ""))
        balance = 0
        if account_id:
            found = conn.execute(
                "SELECT balance_fen FROM accounts WHERE id = ?", (account_id,)
            ).fetchone()
            balance = int(found[0]) if found else 0
        want = suggest_amount(row, now=stamp)
        if want <= 0:
            continue
        try:
            outcome = deposit(conn, goal_id=goal_id, source="cycle", due_ms=int(_val(row, "next_due_ms", 0)), now=stamp)
        except InsufficientFunds:
            _skip_cycle(conn, row, stamp, want, balance)
            skipped += 1
            results.append({"goal_id": goal_id, "result": "insufficient", "want_fen": want})
            continue
        except BankError as exc:
            results.append({"goal_id": goal_id, "result": "error", "error": exc.user_text})
            continue
        deposited += 1
        results.append({"goal_id": goal_id, "result": "deposited", "amount_fen": want})
    return {"ran": len(rows), "deposited": deposited, "skipped": skipped, "results": results}



__all__ = [
    "CADENCES", "CADENCE_LABELS", "DAY_MS", "DEPOSIT_CATEGORY", "MAX_PERIOD_FEN",
    "MIN_PERIOD_FEN", "STATE_LABELS", "authorize", "cadence_label", "deposit",
    "deposits_for", "goal_state", "pause_goal", "period_ms", "periods_left",
    "refund_goal", "resume_goal", "run_due_goals", "savings_goal_view",
    "state_label", "suggest_amount",
]
