"""手动补录与模拟注入：往本行流水里加一笔，并同步演示余额。

这两条是同一个口径的两个入口，都只写 transactions + accounts：
- 同账户、同参数、同一个本地自然日里重复提交只算一次（幂等），返回已有流水；
- 支出不许超过余额（收入不封顶）；
- 都落一条审计（transaction.manual / proactive.simulated_expense）。

为什么放在用例层而不是领域层：这是「演示库没有真实渠道」时的替代写口，没有
策略/令牌/确认那套语义；真正动钱的转账、申购、代扣一律走 planner -> tools。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Final

import app.proactive as proactive
from app import audit, clock
from app.clock import InsufficientFunds, ValidationError
from app.ids import new_id
from app.money import fen_to_yuan_str
from app.use_cases.base import write_case

#: 用户自己补录的渠道值：既方便幂等去重，也一眼看出这行不是银行同步来的。
MANUAL_CHANNEL: Final[str] = "manual"
MANUAL_NOTICE: Final[str] = "这笔是你自己补录的，只进本行流水与演示余额，不对应任何真实支付。"
#: 模拟流水的渠道值；和真实渠道分开，账单里也能一眼看出是演示注入。
SIMULATED_CHANNEL: Final[str] = "simulated"
#: 响应里固定附带的一句人话：这是演示注入，不是真实支付。
SIMULATED_EXPENSE_NOTICE: Final[str] = "这是竞赛演示用的模拟流水，不对应任何真实支付。"
#: 模拟支出的审计类型；旧闭集里没有时退回 tool.executed（payload 仍带 simulated）。
_SIMULATED_EXPENSE_EVENT: Final[str] = "proactive.simulated_expense"
_DAY_MS: Final[int] = 86_400_000


def _simulated_event_type() -> str:
    """审计事件类型：闭集里有专用类型就用专用的，旧闭集退回 tool.executed。"""
    return _SIMULATED_EXPENSE_EVENT if _SIMULATED_EXPENSE_EVENT in audit.EVENT_TYPES else "tool.executed"


def _primary_account(conn: Any) -> tuple[str, int]:
    """本行演示账户：储蓄卡优先，取不到就是数据有问题，直接报错而不是造一个。"""
    account = conn.execute(
        "SELECT id, balance_fen FROM accounts WHERE user_id = ?"
        " ORDER BY CASE type WHEN 'debit' THEN 0 ELSE 1 END, id LIMIT 1",
        (proactive.default_user_id(conn),),
    ).fetchone()
    if account is None:
        raise ValidationError("账户不存在", reason="NO_ACCOUNTS")
    return str(account["id"]), int(account["balance_fen"])


def _today_window(now: int) -> tuple[int, int]:
    """本地自然日的 [起, 止)：幂等只认同一天，跨天再补一笔就是两笔。"""
    day_start = proactive._period_day_start_ms(proactive.local_date(now))
    return day_start, day_start + _DAY_MS


def _row_view(row: Any, *, kind: str) -> dict[str, Any]:
    """把一条本行流水整理成前端好读的形状，kind=manual/simulated 决定那个布尔位。"""
    return {
        "id": str(row["id"]),
        "transaction_id": str(row["id"]),
        "account_id": str(row["account_id"]),
        "direction": str(row["direction"]),
        "amount_fen": int(row["amount_fen"]),
        "amount_yuan": fen_to_yuan_str(int(row["amount_fen"])),
        "counterparty": str(row["counterparty"]),
        "merchant": str(row["counterparty"]),
        "category": str(row["category"]),
        "note": str(row["note"]),
        "ts_ms": int(row["ts_ms"]),
        "balance_after_fen": int(row["balance_after_fen"]),
        "channel": str(row["channel"]),
        kind: True,
    }


def _find_same_day(
    conn: Any,
    *,
    account_id: str,
    direction: str,
    amount_fen: int,
    merchant: str,
    category: str,
    note: str,
    channel: str,
    now: int,
) -> Any:
    """同一天里同参数的那一笔：找到就复用，不重复记账。"""
    day_start, day_end = _today_window(now)
    return conn.execute(
        "SELECT * FROM transactions WHERE account_id = ? AND direction = ?"
        " AND amount_fen = ? AND counterparty = ? AND category = ? AND note = ?"
        " AND channel = ? AND ts_ms >= ? AND ts_ms < ?"
        " ORDER BY ts_ms DESC, id DESC LIMIT 1",
        (account_id, direction, amount_fen, merchant, category, note, channel, day_start, day_end),
    ).fetchone()


def _insert(
    conn: Any,
    *,
    account_id: str,
    balance_fen: int,
    direction: str,
    amount_fen: int,
    merchant: str,
    category: str,
    note: str,
    channel: str,
    now: int,
) -> tuple[str, int]:
    """写流水 + 同步余额，返回 (流水号, 记账后余额)。"""
    balance_after_fen = balance_fen - amount_fen if direction == "out" else balance_fen + amount_fen
    transaction_id = new_id("t")
    conn.execute(
        "INSERT INTO transactions (id, account_id, direction, amount_fen, counterparty,"
        " category, note, ts_ms, balance_after_fen, hold_id, channel)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)",
        (
            transaction_id,
            account_id,
            direction,
            amount_fen,
            merchant,
            category,
            note,
            now,
            balance_after_fen,
            channel,
        ),
    )
    conn.execute("UPDATE accounts SET balance_fen = ? WHERE id = ?", (balance_after_fen, account_id))
    return transaction_id, balance_after_fen


@dataclass(slots=True)
class _Entry:
    """一次记账请求的共用描述：两个入口只是往这个壳里填不同的值与话术。"""

    channel: str
    kind: str
    direction: str
    merchant: str
    amount_fen: int
    category: str
    note: str
    audit_event: str
    tool: str
    notice: str
    insufficient_text: str
    extra: dict[str, Any] = field(default_factory=dict)


def _audit_payload(
    entry: _Entry,
    *,
    account_id: str,
    transaction_id: str,
    idempotent: bool,
    balance_after_fen: int | None = None,
) -> dict[str, Any]:
    """审计里落什么：谁、哪一笔、多少钱、幂等与否，加上两入口各自不同的定位信息。"""
    payload: dict[str, Any] = {
        "tool": entry.tool,
        "ok": True,
        **entry.extra,
        "idempotent": idempotent,
        "account_id": account_id,
        "transaction_id": transaction_id,
        "amount_fen": entry.amount_fen,
        "merchant": entry.merchant,
        "category": entry.category,
    }
    if balance_after_fen is not None:
        payload["balance_after_fen"] = balance_after_fen
        payload["note"] = entry.note
    return payload


def _flow_result(
    entry: _Entry,
    *,
    transaction: dict[str, Any],
    balance_after_fen: int,
    audit_seq: int,
    idempotent: bool,
) -> dict[str, Any]:
    """回给前端的形状：两个入口只差那个布尔位的名字，其余字段一致。"""
    return {
        entry.kind: True,
        "notice": entry.notice,
        "transaction": transaction,
        "balance_after_fen": balance_after_fen,
        "audit_seq": audit_seq,
        "idempotent": idempotent,
    }


def _record_same_day(conn: Any, entry: _Entry, existing: Any, account_id: str) -> dict[str, Any]:
    """同一天同参数已经记过：复用那一笔，审计里标 idempotent=true 后原样返回。"""
    transaction = _row_view(existing, kind=entry.kind)
    audit_seq = audit.append(
        conn,
        entry.audit_event,
        _audit_payload(
            entry,
            account_id=account_id,
            transaction_id=transaction["id"],
            idempotent=True,
        ),
    )
    return _flow_result(
        entry,
        transaction=transaction,
        balance_after_fen=int(existing["balance_after_fen"]),
        audit_seq=audit_seq,
        idempotent=True,
    )


def _record_fresh(
    conn: Any,
    entry: _Entry,
    *,
    account_id: str,
    balance_fen: int,
    now: int,
) -> dict[str, Any]:
    """真正记一笔新的：先查余额够不够，再写流水 + 同步余额 + 落审计。"""
    if entry.direction == "out" and entry.amount_fen > balance_fen:
        raise InsufficientFunds(
            entry.insufficient_text,
            account_id=account_id,
            balance_fen=balance_fen,
            amount_fen=entry.amount_fen,
        )
    transaction_id, balance_after_fen = _insert(
        conn,
        account_id=account_id,
        balance_fen=balance_fen,
        direction=entry.direction,
        amount_fen=entry.amount_fen,
        merchant=entry.merchant,
        category=entry.category,
        note=entry.note,
        channel=entry.channel,
        now=now,
    )
    audit_seq = audit.append(
        conn,
        entry.audit_event,
        _audit_payload(
            entry,
            account_id=account_id,
            transaction_id=transaction_id,
            idempotent=False,
            balance_after_fen=balance_after_fen,
        ),
    )
    return _flow_result(
        entry,
        transaction=_row_view(
            {
                "id": transaction_id,
                "account_id": account_id,
                "direction": entry.direction,
                "amount_fen": entry.amount_fen,
                "counterparty": entry.merchant,
                "category": entry.category,
                "note": entry.note,
                "ts_ms": now,
                "balance_after_fen": balance_after_fen,
                "channel": entry.channel,
            },
            kind=entry.kind,
        ),
        balance_after_fen=balance_after_fen,
        audit_seq=audit_seq,
        idempotent=False,
    )


def _record(conn: Any, entry: _Entry) -> dict[str, Any]:
    """两个入口共用的记账流程：查同日 -> 插入并同步余额 -> 落审计。

    幂等只认「同账户 + 同方向 + 同金额 + 同商户 + 同分类 + 同备注 + 同渠道 + 同一
    本地自然日」，跨天再记一次就是新的一笔（见 tests/test_manual_transaction.py）。
    """
    account_id, balance_fen = _primary_account(conn)
    now = clock.now_ms()
    existing = _find_same_day(
        conn,
        account_id=account_id,
        direction=entry.direction,
        amount_fen=entry.amount_fen,
        merchant=entry.merchant,
        category=entry.category,
        note=entry.note,
        channel=entry.channel,
        now=now,
    )
    if existing is not None:
        return _record_same_day(conn, entry, existing, account_id)
    return _record_fresh(
        conn,
        entry,
        account_id=account_id,
        balance_fen=balance_fen,
        now=now,
    )


@write_case
def manual_record(
    conn: Any,
    *,
    merchant: str,
    amount_fen: int,
    direction: str = "out",
    category: str = "其他",
    note: str = "",
) -> dict[str, Any]:
    """用户自己补一笔流水（接不到银行官方接口时的入口）。"""
    name = str(merchant or "").strip()
    if not name:
        raise ValidationError("商户名不能是空白", field="merchant")
    flow = str(direction or "out")
    return _record(
        conn,
        _Entry(
            channel=MANUAL_CHANNEL,
            kind="manual",
            direction=flow,
            merchant=name,
            amount_fen=int(amount_fen),
            category=str(category or "其他").strip() or "其他",
            note=str(note or "").strip(),
            audit_event="transaction.manual",
            tool="manual_transaction",
            notice=MANUAL_NOTICE,
            insufficient_text="演示账户余额不足，这笔补录没有记上",
            extra={"direction": flow},
        ),
    )


@write_case
def expense_simulate(
    conn: Any,
    *,
    merchant: str,
    amount_fen: int,
    category: str = "餐饮",
    note: str = "",
) -> dict[str, Any]:
    """往演示库注入一笔模拟支出，并同步扣演示账户余额（同一事务）。"""
    name = str(merchant or "").strip()
    if not name:
        raise ValidationError("商户名不能是空白", field="merchant")
    return _record(
        conn,
        _Entry(
            channel=SIMULATED_CHANNEL,
            kind="simulated",
            direction="out",
            merchant=name,
            amount_fen=int(amount_fen),
            category=str(category),
            note=str(note or "").strip(),
            audit_event=_simulated_event_type(),
            tool="simulate_expense",
            notice=SIMULATED_EXPENSE_NOTICE,
            insufficient_text="演示账户余额不足，这笔模拟支出没有注入",
            extra={"simulated": True},
        ),
    )


__all__ = [
    "MANUAL_CHANNEL",
    "MANUAL_NOTICE",
    "SIMULATED_CHANNEL",
    "SIMULATED_EXPENSE_NOTICE",
    "manual_record",
    "expense_simulate",
]
