"""两阶段资金：先 hold（冻结额度），用户确认后由 commit 真正扣款；redeem 是它的入账版。

对应文档：docs/05_安全层实现规格.md 5.4。幂等键 = sha256(session_id|node_id|params_hash)
前 32 位：同一次节点重放只会产生一个 hold。
"""

from __future__ import annotations

import datetime as dt
import hashlib
import sqlite3
from dataclasses import dataclass
from typing import Final

from app import audit
from app.clock import (
    HoldExpired,
    InsufficientFunds,
    PolicyDenied,
    ValidationError,
    now_ms,
)
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
    return Hold(**dict(row))


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
    check_balance: bool = True,
) -> HoldOutcome:
    """第 1 阶段：校验令牌 → 幂等去重 → 余额校验 → 建 hold。

    令牌校验放在事务外：它要消耗 nonce 并写 token.rejected 审计，
    放进事务里一旦回滚，审计和 nonce 都会丢，令牌就能被无限重放。
    check_balance=False 只给「钱往卡里进」的场景用（理财赎回）：这种 hold
    拦的是回款额度，出账方向才需要先看卡里有没有钱。
    """
    token_mod.require_token(token, conn, session_id=session_id)
    token_mod.verify(conn, str(token), action, secret=secret)

    amount_fen = _require_amount(action)
    payee = payee_id or str(action.params.get("payee_id") or SYSTEM_PAYEE_ID)
    key = idempotency_key_for(session_id, node_id, token_mod.params_hash(action.params))
    row = conn.execute("SELECT * FROM holds WHERE idempotency_key = ?", (key,)).fetchone()
    if row is not None:
        replayed = Hold(**dict(row))
        payload = {"hold_id": replayed.id, "amount_fen": replayed.amount_fen, "payee_id": payee}
        audit.append(conn, "transfer.initiated", {**payload, "reused": True}, session_id=session_id)
        return _to_outcome(replayed, reused=True)
    hold_id = _create_hold(conn, action, amount_fen, payee, key, session_id, check_balance)
    return _to_outcome(fetch_hold(conn, hold_id), reused=False)


def initiate_split(
    conn: sqlite3.Connection,
    action: Action,
    *,
    session_id: str,
    node_id: str,
    shares: list[tuple[str, int]],
    token: str | None = None,
    secret: str | None = None,
) -> list[HoldOutcome]:
    """AA 拆分：一次授权、N 条台账。

    令牌绑的是**总额**（策略层批的就是这个总额），但每一份钱单独建 hold、单独 commit，
    这样账上能看到「张三 250、李静 250」而不是一条 500 的糊涂账。
    三条硬校验：份额之和必须等于令牌授权的总额、每份金额为正、每份的收款人不能重复。
    """
    token_mod.require_token(token, conn, session_id=session_id)
    token_mod.verify(conn, str(token), action, secret=secret)
    total = _require_amount(action)
    if not shares:
        raise ValidationError("没有指定分摊对象", reason="SPLIT_EMPTY")
    if len({pid for pid, _ in shares}) != len(shares):
        raise ValidationError("分摊对象有重复", reason="SPLIT_DUPLICATE_PAYEE")
    if any(amount <= 0 for _, amount in shares):
        raise ValidationError("每份金额必须为正", reason="SPLIT_BAD_SHARE")
    if sum(amount for _, amount in shares) != total:
        raise ValidationError(
            "分摊金额之和必须等于授权总额", reason="SPLIT_SUM_MISMATCH", total_fen=total
        )
    outcomes: list[HoldOutcome] = []
    for index, (payee_id, amount) in enumerate(shares):
        sub = Action(
            tool=action.tool,
            params={"payee_id": payee_id, "amount_fen": amount, "share_index": index},
            account_id=action.account_id,
            amount_fen=amount,
        )
        key = idempotency_key_for(
            session_id, f"{node_id}#{index}", token_mod.params_hash(sub.params)
        )
        hold_id = _create_hold(conn, sub, amount, payee_id, key, session_id, True)
        outcomes.append(_to_outcome(fetch_hold(conn, hold_id), reused=False))
    return outcomes


def commit(
    conn: sqlite3.Connection,
    hold_id: str,
    *,
    counterparty: str | None = None,
    daily_limit_fen: int | None = None,
) -> CommitOutcome:
    """第 2 阶段：真正扣钱。重复 commit 幂等；已释放或已过期一律 HoldExpired。

    counterparty 只给「钱付给谁不是收款人」的场景用（理财申购付给基金）；
    不给就按 hold 的收款人取名字，和转账表现一致。

    daily_limit_fen 是**扣钱这一刻**要守的日累计硬线（分）。给了就在同一个事务里
    条件 upsert：超线则整笔回滚——余额不动、hold 仍留在 held 等过期释放，
    钱既不会凭空少也不会凭空多。给 None 表示这笔出账不占用日消费额度
    （入账、冲正、理财申购这类）。
    """
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
    try:
        return _settle(conn, hold, counterparty, daily_limit_fen=daily_limit_fen)
    except PolicyDenied as exc:
        # 拒绝也要留痕：事务已回滚（钱没扣、hold 还在），这条补在事务外。
        audit.append(
            conn,
            "transfer.initiated",
            {
                "hold_id": hold.id,
                "amount_fen": hold.amount_fen,
                "rejected": True,
                "reason": exc.code,
            },
            session_id=hold.session_id,
        )
        raise


def refund(
    conn: sqlite3.Connection,
    action: Action,
    *,
    original_txn_id: str,
    reason: str,
    session_id: str,
    token: str | None = None,
    secret: str | None = None,
) -> dict[str, object]:
    '''冲正一笔已出账的交易：把钱加回去，并留下双向关联。

    为什么必须放在 funds 层：项目的不变量是「余额只经 funds 变动」，工具层直接 UPDATE accounts
    就绕过了这条线。冲正是**入账**，但也得走同一个 _post_leg，否则流水和余额会各说各话。

    四道拒绝：原交易不存在/不属于本账户、原交易不是出账、已经是冲正交易本身、已经冲正过。
    已冲正过按幂等成功返回，不重复入账——重复点击「撤销」不该多出一笔钱。
    '''
    token_mod.require_token(token, conn, session_id=session_id)
    token_mod.verify(conn, str(token), action, secret=secret)
    original = _fetch_txn(conn, original_txn_id)
    if original is None or str(original['account_id']) != str(action.account_id):
        raise ValidationError('找不到这笔交易', reason='TXN_NOT_FOUND', txn_id=original_txn_id)
    if original['reversal_of']:
        raise ValidationError('这笔本身就是冲正交易，不能再冲正', reason='TXN_REVERSAL_OF_REVERSAL')
    if str(original['direction']) != 'out':
        raise ValidationError('只有出账才能冲正', reason='TXN_NOT_OUT')
    amount_fen = int(original['amount_fen'])
    if amount_fen != int(action.amount_fen):
        raise ValidationError('冲正金额必须与原交易一致', reason='TXN_AMOUNT_MISMATCH')
    if original['reversed_by']:
        return {'original_txn_id': original_txn_id, 'reversal_txn_id': str(original['reversed_by']),
                'amount_fen': amount_fen, 'idempotent': True}
    stamp_ms = now_ms()
    with tx(conn):
        balance_after_fen = _post_leg(
            conn, account_id=str(action.account_id), direction='in', amount_fen=amount_fen,
            counterparty=str(original['counterparty']), category='转账',
            note='冲正：' + reason, hold_id=None, stamp_ms=stamp_ms,
        )
        reversal_id = conn.execute(
            'SELECT id FROM transactions WHERE account_id = ? ORDER BY rowid DESC LIMIT 1',
            (str(action.account_id),),
        ).fetchone()['id']
        conn.execute('UPDATE transactions SET reversal_of = ? WHERE id = ?',
                     (original_txn_id, str(reversal_id)))
        conn.execute('UPDATE transactions SET reversed_by = ? WHERE id = ?',
                     (str(reversal_id), original_txn_id))
        audit.append(
            conn,
            'transfer.reversed',
            {
                'original_txn_id': original_txn_id,
                'reversal_txn_id': str(reversal_id),
                'amount_fen': amount_fen,
                'reason': reason,
                'balance_after_fen': balance_after_fen,
            },
            session_id=session_id,
        )
    return {'original_txn_id': original_txn_id, 'reversal_txn_id': str(reversal_id),
            'amount_fen': amount_fen, 'balance_after_fen': balance_after_fen, 'idempotent': False}


def _fetch_txn(conn: sqlite3.Connection, txn_id: str) -> sqlite3.Row | None:
    return conn.execute('SELECT * FROM transactions WHERE id = ?', (txn_id,)).fetchone()


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


def _create_hold(
    conn: sqlite3.Connection,
    action: Action,
    amount_fen: int,
    payee_id: str,
    key: str,
    session_id: str,
    check_balance: bool,
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
            if check_balance and balance_fen < amount_fen:
                raise InsufficientFunds(
                    balance_fen=balance_fen, amount_fen=amount_fen, account_id=action.account_id
                )
            note = str(action.params.get("note") or "")
            conn.execute(
                "INSERT INTO holds (id, account_id, payee_id, amount_fen, note, status,"
                " idempotency_key, session_id, created_ms, expires_ms)"
                " VALUES (?, ?, ?, ?, ?, 'held', ?, ?, ?, ?)",
                (hold_id, action.account_id, payee_id, amount_fen, note, key,
                 session_id, stamp_ms, stamp_ms + HOLD_TTL_MS),
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


def _to_outcome(hold: Hold | None, *, reused: bool) -> HoldOutcome:
    if hold is None:  # pragma: no cover - 插入后立刻读不到属于数据库损坏，宁可炸出来
        raise ValidationError("预授权写入失败")
    return HoldOutcome(hold_id=hold.id, account_id=hold.account_id, amount_fen=hold.amount_fen,
                       status=hold.status, idempotency_key=hold.idempotency_key,
                       expires_ms=hold.expires_ms, payee_id=hold.payee_id or SYSTEM_PAYEE_ID,
                       reused=reused)


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


def redeem(conn: sqlite3.Connection, hold_id: str, *, counterparty: str) -> int:
    """第 2 阶段的入账版：hold 还在，把这笔钱打进卡里（理财赎回回款）。

    与 commit 互补的一条腿：commit 只出不进，赎回的钱要回到储蓄卡。
    幂等、超时、终态的判定与 commit 完全一致；余额仍然只经本模块改，
    工具层绝不允许自己 UPDATE accounts。返回入账后的余额（分）。
    """
    hold = fetch_hold(conn, hold_id)
    if hold is None:
        raise ValidationError("预授权不存在", hold_id=hold_id)
    if hold.status == "committed":
        return _committed_outcome(conn, hold, idempotent=True).balance_after_fen
    if hold.status in _TERMINAL_STATUSES:
        raise HoldExpired(reason=f"HOLD_{hold.status.upper()}", hold_id=hold_id)
    if hold.expires_ms < now_ms():
        _expire(conn, hold)
        raise HoldExpired(reason="HOLD_EXPIRED", hold_id=hold_id)
    stamp_ms = now_ms()
    with tx(conn):
        balance_after_fen = _post_leg(
            conn, account_id=hold.account_id, direction="in", amount_fen=hold.amount_fen,
            counterparty=counterparty, category="invest", note=hold.note or "赎回回款",
            hold_id=hold.id, stamp_ms=stamp_ms,
        )
        conn.execute(
            "UPDATE holds SET status = 'committed', settled_ms = ? WHERE id = ? AND status = 'held'",
            (stamp_ms, hold.id),
        )
        audit.append(
            conn,
            "transfer.committed",
            {"hold_id": hold.id, "direction": "in", "amount_fen": hold.amount_fen,
             "balance_after_fen": balance_after_fen},
            session_id=hold.session_id,
        )
    log.info("funds.redeemed", hold_id=hold.id, balance_after_fen=balance_after_fen)
    return balance_after_fen


def _settle(
    conn: sqlite3.Connection,
    hold: Hold,
    counterparty: str | None = None,
    *,
    daily_limit_fen: int | None = None,
) -> CommitOutcome:
    """扣余额、写流水、改 hold、更新日累计、写审计：五件事一个事务，任一步失败整体回滚。"""
    stamp_ms = now_ms()
    with tx(conn):
        balance_after_fen = _post_leg(
            conn, account_id=hold.account_id, direction="out", amount_fen=hold.amount_fen,
            counterparty=counterparty or _counterparty(conn, hold.payee_id), category="转账",
            note=hold.note or "", hold_id=hold.id, stamp_ms=stamp_ms,
        )
        conn.execute(
            "UPDATE holds SET status = 'committed', settled_ms = ? WHERE id = ? AND status = 'held'",
            (stamp_ms, hold.id),
        )
        _bump_daily_limit(conn, hold.account_id, hold.amount_fen, stamp_ms, limit_fen=daily_limit_fen)
        audit.append(
            conn,
            "transfer.committed",
            {"hold_id": hold.id, "balance_after_fen": balance_after_fen},
            session_id=hold.session_id,
        )
    log.info("funds.committed", hold_id=hold.id, balance_after_fen=balance_after_fen)
    return CommitOutcome(hold_id=hold.id, account_id=hold.account_id, amount_fen=hold.amount_fen,
                         balance_after_fen=balance_after_fen)


def _post_leg(
    conn: sqlite3.Connection,
    *,
    account_id: str,
    direction: str,
    amount_fen: int,
    counterparty: str,
    category: str,
    note: str,
    hold_id: str | None,
    stamp_ms: int,
) -> int:
    """记一笔资金变动：改余额 + 写一条流水，返回变动后的余额。出账要校验余额够不够。"""
    if direction == "out":
        cursor = conn.execute(
            "UPDATE accounts SET balance_fen = balance_fen - ? WHERE id = ? AND balance_fen >= ?",
            (amount_fen, account_id, amount_fen),
        )
        if cursor.rowcount != 1:
            raise InsufficientFunds(hold_id=hold_id, amount_fen=amount_fen)
    else:
        conn.execute("UPDATE accounts SET balance_fen = balance_fen + ? WHERE id = ?",
                     (amount_fen, account_id))
    balance_after_fen = _balance_after(conn, account_id)
    conn.execute(
        "INSERT INTO transactions (id, account_id, direction, amount_fen, counterparty,"
        " category, note, ts_ms, balance_after_fen, hold_id, channel)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'app')",
        (new_id("t"), account_id, direction, amount_fen, counterparty, category,
         note, stamp_ms, balance_after_fen, hold_id),
    )
    return balance_after_fen


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


def _bump_daily_limit(
    conn: sqlite3.Connection,
    account_id: str,
    amount_fen: int,
    stamp_ms: int,
    *,
    limit_fen: int | None = None,
) -> None:
    """记今天花了多少；给了 limit_fen 就顺手守住这条线。

    为什么守卫必须长在这里：策略层判「今天已花多少」是**先读后判**，
    两个会话并发时会同时读到同一个旧值，于是两笔都放行——日线形同虚设
    （t4 的 test_concurrent_daily_limit_blocks_the_second_transfer 复现的就是这个洞）。
    钱真正动的这一层才有与扣款同一个事务的原子视图：条件 upsert 的
    WHERE spent_fen + excluded.spent_fen <= ? 让超线的那笔 rowcount 为 0（没写进去），
    调用方在同一事务里回滚——余额不动、hold 仍留在 held 等过期释放。
    """
    if limit_fen is None:
        conn.execute(
            "INSERT INTO daily_limits (account_id, day, spent_fen, tx_count) VALUES (?, ?, ?, 1)"
            " ON CONFLICT(account_id, day) DO UPDATE SET"
            " spent_fen = spent_fen + excluded.spent_fen, tx_count = tx_count + 1",
            (account_id, day_of(stamp_ms), amount_fen),
        )
        return
    if amount_fen > limit_fen:
        raise PolicyDenied(
            "DAILY_LIMIT_EXCEEDED",
            account_id=account_id,
            amount_fen=amount_fen,
            limit_fen=limit_fen,
            spent_fen=spent_today_fen(conn, account_id, at_ms=stamp_ms),
        )
    cursor = conn.execute(
        "INSERT INTO daily_limits (account_id, day, spent_fen, tx_count) VALUES (?, ?, ?, 1)"
        " ON CONFLICT(account_id, day) DO UPDATE SET"
        " spent_fen = spent_fen + excluded.spent_fen, tx_count = tx_count + 1"
        " WHERE spent_fen + excluded.spent_fen <= ?",
        (account_id, day_of(stamp_ms), amount_fen, limit_fen),
    )
    if cursor.rowcount != 1:
        raise PolicyDenied(
            "DAILY_LIMIT_EXCEEDED",
            account_id=account_id,
            amount_fen=amount_fen,
            limit_fen=limit_fen,
            spent_fen=spent_today_fen(conn, account_id, at_ms=stamp_ms),
        )


def _committed_outcome(conn: sqlite3.Connection, hold: Hold, *, idempotent: bool) -> CommitOutcome:
    """重复 commit：从流水里取当初扣完的余额，保证两次调用返回同一个结果。"""
    row = conn.execute("SELECT balance_after_fen FROM transactions"
                       " WHERE hold_id = ? ORDER BY ts_ms LIMIT 1", (hold.id,)).fetchone()
    balance_after_fen = int(row["balance_after_fen"]) if row else _balance_after(conn, hold.account_id)
    return CommitOutcome(hold_id=hold.id, account_id=hold.account_id, amount_fen=hold.amount_fen,
                         balance_after_fen=balance_after_fen, idempotent=idempotent)


__all__ = [
    "HOLD_TTL_MS", "IDEMPOTENCY_KEY_LEN", "SYSTEM_PAYEE_ID",
    "CommitOutcome", "Hold", "HoldOutcome", "ReleaseOutcome",
    "commit", "day_of", "fetch_hold", "idempotency_key_for",
    "initiate", "redeem", "release", "spent_today_fen",
]
