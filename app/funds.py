"""两阶段资金：先 hold（冻结额度），用户确认后再 commit 真正扣款。

对应文档：docs/05_安全层实现规格.md 5.4。

三条路径：held → committed（扣余额 + 写流水 + 更新日累计，五件事一个事务）、
held → released（用户取消 / 工具失败）、到期 → expired（超时自动作废）。
幂等键 = sha256(session_id|node_id|params_hash) 前 32 位：同一次节点重放只会产生一个 hold。
"""

from __future__ import annotations

import datetime as dt
import hashlib
import sqlite3
from dataclasses import dataclass
from typing import Any, Final

from app import audit
from app.clock import HoldExpired, InsufficientFunds, ValidationError, now_ms
from app.db import tx
from app.ids import new_id
from app.log import log
from app.models import Action
from app.security import token as token_mod

HOLD_TTL_MS: Final[int] = 300_000
IDEMPOTENCY_KEY_LEN: Final[int] = 32
SYSTEM_PAYEE_ID: Final[str] = "p_system"
_TERMINAL_STATUSES: Final[frozenset[str]] = frozenset({"released", "expired"})


@dataclass(frozen=True)
class Hold:
    """holds 表的一行。"""

    id: str
    account_id: str
    payee_id: str | None
    amount_fen: int
    note: str
    status: str
    idempotency_key: str
    session_id: str | None
    created_ms: int
    expires_ms: int
    settled_ms: int | None = None


@dataclass(frozen=True)
class HoldOutcome:
    """initiate 的结果；reused=True 表示这次是重放，没有新建 hold。"""

    hold_id: str
    account_id: str
    payee_id: str
    amount_fen: int
    status: str
    idempotency_key: str
    expires_ms: int
    reused: bool = False


@dataclass(frozen=True)
class CommitOutcome:
    """commit 的结果；idempotent=True 表示重复提交，返回同一笔结果。"""

    hold_id: str
    account_id: str
    amount_fen: int
    balance_after_fen: int
    idempotent: bool = False


@dataclass(frozen=True)
class ReleaseOutcome:
    """release 的结果；idempotent=True 表示本来就是终态，没动。"""

    hold_id: str
    status: str
    idempotent: bool = False


def idempotency_key_for(session_id: str, node_id: str, params_hash: str) -> str:
    """幂等键：同一个会话同一个节点同一份参数，永远算出同一个键。"""
    material = f"{session_id}|{node_id}|{params_hash}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:IDEMPOTENCY_KEY_LEN]


def day_of(ts_ms: int) -> str:
    """日累计的日期键，永远由时间戳算，不读环境时区配置。"""
    return dt.datetime.fromtimestamp(ts_ms / 1000).strftime("%Y-%m-%d")


def fetch_hold(conn: sqlite3.Connection, hold_id: str) -> Hold | None:
    row = conn.execute("SELECT * FROM holds WHERE id = ?", (hold_id,)).fetchone()
    if row is None:
        return None
    return Hold(**{key: row[key] for key in row.keys()})


def spent_today_fen(conn: sqlite3.Connection, account_id: str, *, at_ms: int | None = None) -> int:
    """今日已用金额（分），供 PolicyContext 用。"""
    stamp_ms = at_ms if at_ms is not None else now_ms()
    row = conn.execute(
        "SELECT spent_fen FROM daily_limits WHERE account_id = ? AND day = ?",
        (account_id, day_of(stamp_ms)),
    ).fetchone()
    return int(row["spent_fen"]) if row else 0


def initiate(
    conn: sqlite3.Connection,
    action: Action,
    *,
    session_id: str,
    node_id: str,
    token: str | None = None,
    secret: str | None = None,
    payee_id: str | None = None,
) -> HoldOutcome:
    """第 1 阶段：校验令牌 → 幂等去重 → 余额校验 → 建 hold。

    令牌校验放在事务外：它要消耗 nonce 并写 token.rejected 审计，
    放进事务里一旦回滚，审计和 nonce 都会丢，令牌就能被无限重放。
    """
    token_mod.require_token(token, conn, session_id=session_id)
    token_mod.verify(conn, str(token), action, secret=secret)

    amount_fen = _require_amount(action)
    payee = payee_id or str(action.params.get("payee_id") or SYSTEM_PAYEE_ID)
    key = idempotency_key_for(session_id, node_id, token_mod.params_hash(action.params))
    existing = _find_by_key(conn, key)
    if existing is not None:
        audit.append(conn, "transfer.initiated", _initiated_payload(existing, payee, reused=True), session_id=session_id)
        return _to_outcome(existing, reused=True)
    hold_id = _create_hold(conn, action, amount_fen, payee, key, session_id)
    return _to_outcome(fetch_hold(conn, hold_id), reused=False)


def commit(conn: sqlite3.Connection, hold_id: str) -> CommitOutcome:
    """第 2 阶段：真正扣钱。重复 commit 幂等；已释放或已过期一律 HoldExpired。"""
    hold = fetch_hold(conn, hold_id)
    if hold is None:
        raise ValidationError("预授权不存在", hold_id=hold_id)
    if hold.status == "committed":
        return _committed_outcome(conn, hold, idempotent=True)
    if hold.status in _TERMINAL_STATUSES:
        raise HoldExpired(reason=f"HOLD_{hold.status.upper()}", hold_id=hold_id)
    if hold.expires_ms < now_ms():
        _expire(conn, hold)
        raise HoldExpired(reason="HOLD_EXPIRED", hold_id=hold_id)
    return _settle(conn, hold)


def release(
    conn: sqlite3.Connection,
    hold_id: str,
    *,
    reason: str = "USER_CANCELLED",
    session_id: str | None = None,
) -> ReleaseOutcome:
    """释放 hold；已经是终态就原样返回，重复释放不会报错也不会改账。"""
    hold = fetch_hold(conn, hold_id)
    if hold is None:
        raise ValidationError("预授权不存在", hold_id=hold_id)
    if hold.status != "held":
        return ReleaseOutcome(hold_id=hold.id, status=hold.status, idempotent=True)
    with tx(conn):
        conn.execute(
            "UPDATE holds SET status = 'released', settled_ms = ? WHERE id = ? AND status = 'held'",
            (now_ms(), hold.id),
        )
        audit.append(conn, "transfer.released", {"hold_id": hold.id, "reason": reason}, session_id=session_id)
    log.info("funds.released", hold_id=hold.id, reason=reason)
    return ReleaseOutcome(hold_id=hold.id, status="released", idempotent=False)


def _require_amount(action: Action) -> int:
    amount_fen = action.amount_fen
    if amount_fen is None or isinstance(amount_fen, bool) or amount_fen <= 0:
        raise ValidationError("金额不正确，请重新输入", amount_fen=amount_fen)
    return amount_fen


def _find_by_key(conn: sqlite3.Connection, key: str) -> Hold | None:
    row = conn.execute("SELECT * FROM holds WHERE idempotency_key = ?", (key,)).fetchone()
    return Hold(**{name: row[name] for name in row.keys()}) if row else None


def _create_hold(
    conn: sqlite3.Connection,
    action: Action,
    amount_fen: int,
    payee_id: str,
    key: str,
    session_id: str,
) -> str:
    """余额不足时抛 InsufficientFunds，并在事务外补一条 transfer.initiated 审计。"""
    stamp_ms = now_ms()
    hold_id = new_id("h")
    try:
        with tx(conn):
            if conn.execute("SELECT 1 FROM payees WHERE id = ?", (payee_id,)).fetchone() is None:
                raise ValidationError("收款人不存在", payee_id=payee_id)
            row = conn.execute(
                "SELECT balance_fen FROM accounts WHERE id = ?", (action.account_id,)
            ).fetchone()
            if row is None:
                raise ValidationError("账户不存在", account_id=action.account_id)
            balance_fen = int(row["balance_fen"])
            if balance_fen < amount_fen:
                raise InsufficientFunds(
                    balance_fen=balance_fen, amount_fen=amount_fen, account_id=action.account_id
                )
            conn.execute(
                "INSERT INTO holds (id, account_id, payee_id, amount_fen, note, status,"
                " idempotency_key, session_id, created_ms, expires_ms)"
                " VALUES (?, ?, ?, ?, ?, 'held', ?, ?, ?, ?)",
                (
                    hold_id,
                    action.account_id,
                    payee_id,
                    amount_fen,
                    _note(action),
                    key,
                    session_id,
                    stamp_ms,
                    stamp_ms + HOLD_TTL_MS,
                ),
            )
            audit.append(
                conn,
                "transfer.initiated",
                {"hold_id": hold_id, "amount_fen": amount_fen, "payee_id": payee_id},
                session_id=session_id,
            )
    except InsufficientFunds:
        audit.append(
            conn,
            "transfer.initiated",
            {"amount_fen": amount_fen, "payee_id": payee_id, "rejected": True, "reason": "INSUFFICIENT_FUNDS"},
            session_id=session_id,
        )
        raise
    return hold_id


def _note(action: Action) -> str:
    raw = action.params.get("note")
    return raw if isinstance(raw, str) else ""


def _initiated_payload(hold: Hold, payee_id: str, *, reused: bool) -> dict[str, Any]:
    return {"hold_id": hold.id, "amount_fen": hold.amount_fen, "payee_id": payee_id, "reused": reused}


def _to_outcome(hold: Hold | None, *, reused: bool) -> HoldOutcome:
    if hold is None:  # pragma: no cover - 插入后立刻读不到属于数据库损坏，宁可炸出来
        raise ValidationError("预授权写入失败")
    return HoldOutcome(
        hold_id=hold.id,
        account_id=hold.account_id,
        payee_id=hold.payee_id or SYSTEM_PAYEE_ID,
        amount_fen=hold.amount_fen,
        status=hold.status,
        idempotency_key=hold.idempotency_key,
        expires_ms=hold.expires_ms,
        reused=reused,
    )


def _expire(conn: sqlite3.Connection, hold: Hold) -> None:
    """超时作废；在事务外做，这样抛 HoldExpired 时状态与审计都留得住。"""
    conn.execute(
        "UPDATE holds SET status = 'expired', settled_ms = ? WHERE id = ? AND status = 'held'",
        (now_ms(), hold.id),
    )
    audit.append(
        conn,
        "transfer.released",
        {"hold_id": hold.id, "reason": "HOLD_TIMEOUT", "amount_fen": hold.amount_fen},
    )
    log.info("funds.expired", hold_id=hold.id)


def _settle(conn: sqlite3.Connection, hold: Hold) -> CommitOutcome:
    """扣余额、写流水、改 hold、更新日累计、写审计：五件事一个事务，任一步失败整体回滚。"""
    stamp_ms = now_ms()
    with tx(conn):
        cursor = conn.execute(
            "UPDATE accounts SET balance_fen = balance_fen - ? WHERE id = ? AND balance_fen >= ?",
            (hold.amount_fen, hold.account_id, hold.amount_fen),
        )
        if cursor.rowcount != 1:
            raise InsufficientFunds(hold_id=hold.id, amount_fen=hold.amount_fen)
        balance_after_fen = _balance_after(conn, hold.account_id)
        conn.execute(
            "INSERT INTO transactions (id, account_id, direction, amount_fen, counterparty,"
            " category, note, ts_ms, balance_after_fen, hold_id, channel)"
            " VALUES (?, ?, 'out', ?, ?, 'transfer', ?, ?, ?, ?, 'app')",
            (
                new_id("t"),
                hold.account_id,
                hold.amount_fen,
                _counterparty(conn, hold.payee_id),
                hold.note or "",
                stamp_ms,
                balance_after_fen,
                hold.id,
            ),
        )
        conn.execute(
            "UPDATE holds SET status = 'committed', settled_ms = ? WHERE id = ? AND status = 'held'",
            (stamp_ms, hold.id),
        )
        _bump_daily_limit(conn, hold.account_id, hold.amount_fen, stamp_ms)
        audit.append(
            conn,
            "transfer.committed",
            {"hold_id": hold.id, "balance_after_fen": balance_after_fen},
            session_id=hold.session_id,
        )
    log.info("funds.committed", hold_id=hold.id, balance_after_fen=balance_after_fen)
    return CommitOutcome(
        hold_id=hold.id,
        account_id=hold.account_id,
        amount_fen=hold.amount_fen,
        balance_after_fen=balance_after_fen,
    )


def _balance_after(conn: sqlite3.Connection, account_id: str) -> int:
    row = conn.execute("SELECT balance_fen FROM accounts WHERE id = ?", (account_id,)).fetchone()
    if row is None:
        raise ValidationError("账户不存在", account_id=account_id)
    return int(row["balance_fen"])


def _counterparty(conn: sqlite3.Connection, payee_id: str | None) -> str:
    if not payee_id or payee_id == SYSTEM_PAYEE_ID:
        return "系统预授权"
    row = conn.execute("SELECT name FROM payees WHERE id = ?", (payee_id,)).fetchone()
    return str(row["name"]) if row else payee_id


def _bump_daily_limit(conn: sqlite3.Connection, account_id: str, amount_fen: int, stamp_ms: int) -> None:
    conn.execute(
        "INSERT INTO daily_limits (account_id, day, spent_fen, tx_count) VALUES (?, ?, ?, 1)"
        " ON CONFLICT(account_id, day) DO UPDATE SET"
        " spent_fen = spent_fen + excluded.spent_fen, tx_count = tx_count + 1",
        (account_id, day_of(stamp_ms), amount_fen),
    )


def _committed_outcome(conn: sqlite3.Connection, hold: Hold, *, idempotent: bool) -> CommitOutcome:
    """重复 commit：从流水里取当初扣完的余额，保证两次调用返回同一个结果。"""
    row = conn.execute(
        "SELECT balance_after_fen FROM transactions WHERE hold_id = ? ORDER BY ts_ms LIMIT 1", (hold.id,)
    ).fetchone()
    balance_after_fen = int(row["balance_after_fen"]) if row else _balance_after(conn, hold.account_id)
    return CommitOutcome(
        hold_id=hold.id,
        account_id=hold.account_id,
        amount_fen=hold.amount_fen,
        balance_after_fen=balance_after_fen,
        idempotent=idempotent,
    )


__all__ = [
    "HOLD_TTL_MS",
    "IDEMPOTENCY_KEY_LEN",
    "SYSTEM_PAYEE_ID",
    "CommitOutcome",
    "Hold",
    "HoldOutcome",
    "ReleaseOutcome",
    "commit",
    "day_of",
    "fetch_hold",
    "idempotency_key_for",
    "initiate",
    "release",
    "spent_today_fen",
]
