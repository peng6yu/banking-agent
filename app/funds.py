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

from app import audit, channels
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
    # 建 hold 那一刻令牌裁决出来的等级。commit 只认这一列——
    # 它是「用户当时到底验过什么」的服务端记录，不是调用方现在怎么说。
    risk_level: str = "yellow"


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


def idempotency_key_for(
    session_id: str, node_id: str, params_hash: str, *, run_id: str = ""
) -> str:
    """幂等键：同一轮（run）同一节点同一份参数，永远算出同一个键。

    为什么要带上 run_id：节点号是模板固定的（转账永远是 n3）。只按
    「会话+节点+参数」算键，用户在同一个会话里第二次说「再给李静转 500」时
    会命中上一轮那条 hold，工具回报「已转出 500」但钱根本没动——这是**假成功**，
    比重复扣款更难发现。带上本轮 run_id 后：
    - 同一轮里点两次确认 / 刷新后重放（run 不变）→ 仍然幂等；
    - 用户明确再说一次（新的一轮）→ 当成新的一笔，如实扣款。
    跨会话/跨标签页的重复提交仍未覆盖，那是客户端 request_id 的契约，写在风险清单里。
    """
    material = f"{session_id}|{node_id}|{params_hash}"
    if run_id:
        material += f"|{run_id}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:IDEMPOTENCY_KEY_LEN]


def day_of(ts_ms: int) -> str:
    """日累计的日期键，永远由时间戳算，不读环境时区配置。"""
    return dt.datetime.fromtimestamp(ts_ms / 1000).strftime("%Y-%m-%d")


def fetch_hold(conn: sqlite3.Connection, hold_id: str) -> Hold | None:
    row = conn.execute("SELECT * FROM holds WHERE id = ?", (hold_id,)).fetchone()
    if row is None:
        return None
    return Hold(**dict(row))


def _hold_by_key(conn: sqlite3.Connection, key: str) -> Hold | None:
    row = conn.execute("SELECT * FROM holds WHERE idempotency_key = ?", (key,)).fetchone()
    return Hold(**dict(row)) if row is not None else None


def account_user_id(conn: sqlite3.Connection, account_id: str) -> str | None:
    """账户属于谁。日累计按这个人的**所有账户**合起来算（赛题口径：用户维度）。"""
    row = conn.execute("SELECT user_id FROM accounts WHERE id = ?", (account_id,)).fetchone()
    return str(row["user_id"]) if row else None


def spent_today_fen(conn: sqlite3.Connection, account_id: str, *, at_ms: int | None = None) -> int:
    """今日已用金额（分），供 PolicyContext 用。

    口径是**用户维度**：同一自然人名下所有账户当日出账合计。
    赛题原文把「日累计 ≤1000 元 / >1000 元」定义成同一个人当天的大额分级，
    按账户各算各的会给出「换张卡就绕过红档」的漏洞。
    账户维度的那本账仍在 daily_limits 里（账单口径），由 account_spent_today_fen 读。
    """
    stamp_ms = at_ms if at_ms is not None else now_ms()
    user_id = account_user_id(conn, account_id)
    if user_id is None:
        # 账户不存在时按账户口径兜底，保证纯函数式查询不会抛异常
        return account_spent_today_fen(conn, account_id, at_ms=stamp_ms)
    row = conn.execute(
        "SELECT spent_fen FROM daily_user_limits WHERE user_id = ? AND day = ?",
        (user_id, day_of(stamp_ms)),
    ).fetchone()
    return int(row["spent_fen"]) if row else 0


def account_spent_today_fen(
    conn: sqlite3.Connection, account_id: str, *, at_ms: int | None = None
) -> int:
    """账户维度的今日出账（账务/账单口径，不参与策略分级）。"""
    stamp_ms = at_ms if at_ms is not None else now_ms()
    row = conn.execute(
        "SELECT spent_fen FROM daily_limits WHERE account_id = ? AND day = ?",
        (account_id, day_of(stamp_ms)),
    ).fetchone()
    return int(row["spent_fen"]) if row else 0


def reserved_budget_fen(conn: sqlite3.Connection, account_id: str) -> int:
    """生日/计划预算里仍然有效（status='reserved'）的预留金额。

    status='paying' 不计：那一刻已经建了资金 hold，钱由 hold 占着，
    再算一遍预算就是同一笔钱占两次，会把用户的可支配金额凭空砍半。
    兼容老库：plan_budgets 还没有 account_id 列时视为没有预留。
    """
    cols = {str(r["name"]) for r in conn.execute("PRAGMA table_info(plan_budgets)").fetchall()}
    if "account_id" not in cols:
        return 0
    row = conn.execute(
        "SELECT COALESCE(SUM(amount_fen), 0) FROM plan_budgets"
        " WHERE account_id = ? AND status = 'reserved'",
        (account_id,),
    ).fetchone()
    return int(row[0]) if row else 0


def held_fen(conn: sqlite3.Connection, account_id: str) -> int:
    """仍在途的资金 hold 金额（状态 held 且没过期）。"""
    row = conn.execute(
        "SELECT COALESCE(SUM(amount_fen), 0) FROM holds"
        " WHERE account_id = ? AND status = 'held' AND expires_ms > ?",
        (account_id, now_ms()),
    ).fetchone()
    return int(row[0]) if row else 0


def available_fen(conn: sqlite3.Connection, account_id: str) -> int:
    """可支配金额 = 账面余额 - 在途资金 hold - 有效预算预留。

    三层的区别必须体现在数字上，否则「可支配」就是一句空话：
    - 账面余额是银行侧的事实，不因为 hold 或预算而变；
    - hold 是马上要落账的临时冻结（5 分钟），再拿去花会在 commit 时失败；
    - 预算预留是用户自己安排给未来项目的钱，动它之前得先改安排。
    所有资金写操作（即时转账、预约出账、AA、商户付款、理财申购）校验余额时都过
    _create_hold 这一层；hold 在 commit 里还有并发 UPDATE … >= amount 的原子性兜底。
    """
    row = conn.execute("SELECT balance_fen FROM accounts WHERE id = ?", (account_id,)).fetchone()
    if row is None:
        raise ValidationError("账户不存在", account_id=account_id)
    return int(row["balance_fen"]) - held_fen(conn, account_id) - reserved_budget_fen(
        conn, account_id
    )


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
    run_id: str = "",
) -> HoldOutcome:
    """第 1 阶段：校验令牌 → 幂等去重 → 余额校验 → 建 hold。

    令牌校验放在事务外：它要消耗 nonce 并写 token.rejected 审计，
    放进事务里一旦回滚，审计和 nonce 都会丢，令牌就能被无限重放。
    check_balance=False 只给「钱往卡里进」的场景用（理财赎回）：这种 hold
    拦的是回款额度，出账方向才需要先看卡里有没有钱。
    """
    token_mod.require_token(token, conn, session_id=session_id)
    tok_payload = token_mod.verify(conn, str(token), action, secret=secret)

    amount_fen = _require_amount(action)
    payee = payee_id or str(action.params.get("payee_id") or SYSTEM_PAYEE_ID)
    params_hash = token_mod.params_hash(action.params)
    key = idempotency_key_for(session_id, node_id, params_hash, run_id=run_id)
    row = _hold_by_key(conn, key)
    if row is not None and row.status not in _TERMINAL_STATUSES:
        audit.append(
            conn,
            "transfer.initiated",
            {"hold_id": row.id, "amount_fen": row.amount_fen, "payee_id": payee, "reused": True},
            session_id=session_id,
        )
        return _to_outcome(row, reused=True)
    if row is not None:
        # 旧幂等键指着一份已经释放/过期的 hold（典型：撞日累计线被资金层拒掉，
        # 用户补完强验证再来一次）。继续用它只会拿到 HoldExpired，
        # 所以用令牌 nonce 派生一把新键：同一张新授权仍然幂等，旧尸体不再挡住重试。
        key = idempotency_key_for(
            session_id, f"{node_id}#{tok_payload.nonce}", params_hash, run_id=run_id
        )
        row = _hold_by_key(conn, key)
        if row is not None:
            audit.append(
                conn,
                "transfer.initiated",
                {"hold_id": row.id, "amount_fen": row.amount_fen, "payee_id": payee, "reused": True},
                session_id=session_id,
            )
            return _to_outcome(row, reused=True)
    hold_id = _create_hold(
        conn, action, amount_fen, payee, key, session_id, check_balance,
        risk_level=tok_payload.level,
    )
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
    run_id: str = "",
) -> list[HoldOutcome]:
    """AA 拆分：一次授权、N 条台账。

    令牌绑的是**总额**（策略层批的就是这个总额），但每一份钱单独建 hold、单独 commit，
    这样账上能看到「张三 250、李静 250」而不是一条 500 的糊涂账。
    三条硬校验：份额之和必须等于令牌授权的总额、每份金额为正、每份的收款人不能重复。
    """
    token_mod.require_token(token, conn, session_id=session_id)
    tok_payload = token_mod.verify(conn, str(token), action, secret=secret)
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
        params_hash = token_mod.params_hash(sub.params)
        key = idempotency_key_for(session_id, f"{node_id}#{index}", params_hash, run_id=run_id)
        existing = _hold_by_key(conn, key)
        if existing is not None:
            if existing.status not in _TERMINAL_STATUSES:
                outcomes.append(_to_outcome(existing, reused=True))
                continue
            # 同 initiate：旧 hold 已作废，用 nonce 派生新键再建一份。
            key = idempotency_key_for(
                session_id, f"{node_id}#{index}#{tok_payload.nonce}", params_hash, run_id=run_id
            )
            existing = _hold_by_key(conn, key)
            if existing is not None:
                outcomes.append(_to_outcome(existing, reused=True))
                continue
        hold_id = _create_hold(
            conn, sub, amount, payee_id, key, session_id, True, risk_level=tok_payload.level
        )
        outcomes.append(_to_outcome(fetch_hold(conn, hold_id), reused=False))
    return outcomes


def commit(
    conn: sqlite3.Connection,
    hold_id: str,
    *,
    counterparty: str | None = None,
    daily_risk_threshold_fen: int | None = None,
    absolute_daily_cap_fen: int | None = None,
) -> CommitOutcome:
    """第 2 阶段：真正扣钱。重复 commit 幂等；已释放或已过期一律 HoldExpired。

    counterparty 只给「钱付给谁不是收款人」的场景用（理财申购付给基金）；
    不给就按 hold 的收款人取名字，和转账表现一致。

    两个日累计参数都在**扣钱这一刻**、与扣款同一个事务里核对（先读后判的并发洞
    只有这一层能补）：

    - daily_risk_threshold_fen：黄/红分级线。本笔会让当日累计越线时，
      只有 hold.risk_level == 'red'（用户真的验过人脸+短信）才放行；
      黄卡一律整笔回滚并抛 DAILY_RISK_UPGRADE_REQUIRED——旧黄卡不能替红操作签字。
    - absolute_daily_cap_fen：可选绝对上限，与验证等级无关，越线就
      DAILY_ABSOLUTE_CAP_EXCEEDED（因子不能给监管额度加码）。
    - 两个都传 None 表示这笔出账不占用日消费额度（入账、冲正、理财申购这类）。
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
        return _settle(
            conn, hold, counterparty,
            daily_risk_threshold_fen=daily_risk_threshold_fen,
            absolute_daily_cap_fen=absolute_daily_cap_fen,
        )
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
    *,
    risk_level: str = "yellow",
) -> str:
    """余额不足时抛 InsufficientFunds，并在事务外补一条 transfer.initiated 审计。"""
    stamp_ms = now_ms()
    hold_id = new_id("h")
    try:
        with tx(conn):
            if conn.execute("SELECT 1 FROM payees WHERE id = ?", (payee_id,)).fetchone() is None:
                raise ValidationError("收款人不存在", payee_id=payee_id)
            # 通道闸门分两步问：先问「这条通道通不通」（账户/卡状态，不带金额），
            # 再问「余额够不够」，最后才问「单笔限额」。顺序是有意的：
            # 卡被冻结比余额不足更根本；而余额不足比限额超了更该先说——
            # 用户听到「你只有 100 元」才知道问题出在哪，「超限额」会误导他去调额度。
            channels.assert_spendable(conn, action.account_id, None)
            row = conn.execute(
                "SELECT balance_fen FROM accounts WHERE id = ?", (action.account_id,)
            ).fetchone()
            if row is None:
                raise ValidationError("账户不存在", account_id=action.account_id)
            # 余额校验必须用**可支配金额**：生日预算预留过的钱不算可支配，
            # 否则「额度查询说已预留、转账照样花掉」就是假预留。
            balance_fen = int(row["balance_fen"]) - reserved_budget_fen(conn, action.account_id)
            if check_balance and balance_fen < amount_fen:
                raise InsufficientFunds(
                    balance_fen=balance_fen, amount_fen=amount_fen, account_id=action.account_id
                )
            channels.assert_spendable(conn, action.account_id, amount_fen)
            note = str(action.params.get("note") or "")
            conn.execute(
                "INSERT INTO holds (id, account_id, payee_id, amount_fen, note, status,"
                " risk_level, idempotency_key, session_id, created_ms, expires_ms)"
                " VALUES (?, ?, ?, ?, ?, 'held', ?, ?, ?, ?, ?)",
                (hold_id, action.account_id, payee_id, amount_fen, note, risk_level, key,
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
    daily_risk_threshold_fen: int | None = None,
    absolute_daily_cap_fen: int | None = None,
) -> CommitOutcome:
    """扣余额、写流水、改 hold、更新日累计、写审计：五件事一个事务，任一步失败整体回滚。

    分级判定放在事务里、扣款之前：事务是 BEGIN IMMEDIATE，同一时刻只有一个写者，
    所以这里读到的「今日已用」就是本笔 commit 时点的真实值——两个会话并发提交时，
    第二笔必然看见第一笔已经记进去的累计，不会双双拿旧的黄灯裁决。
    """
    stamp_ms = now_ms()
    with tx(conn):
        if daily_risk_threshold_fen is not None or absolute_daily_cap_fen is not None:
            _assert_daily_tier(conn, hold, stamp_ms,
                               daily_risk_threshold_fen=daily_risk_threshold_fen,
                               absolute_daily_cap_fen=absolute_daily_cap_fen)
        # 建 hold 到扣款之间用户可能把卡冻了：扣钱这一刻再核一次通道，
        # 与「余额在扣款那一刻再核一次」是同一个道理。
        channels.assert_spendable(conn, hold.account_id, hold.amount_fen)
        balance_after_fen = _post_leg(
            conn, account_id=hold.account_id, direction="out", amount_fen=hold.amount_fen,
            counterparty=counterparty or _counterparty(conn, hold.payee_id), category="转账",
            note=hold.note or "", hold_id=hold.id, stamp_ms=stamp_ms,
        )
        conn.execute(
            "UPDATE holds SET status = 'committed', settled_ms = ? WHERE id = ? AND status = 'held'",
            (stamp_ms, hold.id),
        )
        _bump_daily_counters(conn, hold.account_id, hold.amount_fen, stamp_ms)
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


def _assert_daily_tier(
    conn: sqlite3.Connection,
    hold: Hold,
    stamp_ms: int,
    *,
    daily_risk_threshold_fen: int | None,
    absolute_daily_cap_fen: int | None,
) -> None:
    """扣款前在这个事务里核对日累计分级（调用方已在 BEGIN IMMEDIATE 里）。

    为什么守卫必须长在这里：策略层判「今天已花多少」是**先读后判**，
    两个会话并发时会同时读到同一个旧值，于是两笔都放行——分级形同虚设。
    钱真正动的这一层才有与扣款同一个事务的原子视图。

    两条线语义不同，不能合并：
    - 风险阈值：越线 = 大额 = 红。红灯的 hold（risk_level='red'，即用户
      在这次支付上真的验过人脸+短信）可以过；黄卡一律拦，抛
      DAILY_RISK_UPGRADE_REQUIRED，让上层重新走强验证流程。
      「验过就能继续」正是赛题对红档的定义，而「不许继续」是没有依据的硬拒绝。
    - 绝对上限：只在部署显式配置了绝对限额时存在，与验证等级无关。
    """
    amount_fen = hold.amount_fen
    user_id = account_user_id(conn, hold.account_id)
    day = day_of(stamp_ms)
    spent_before = spent_today_fen(conn, hold.account_id, at_ms=stamp_ms)
    after = spent_before + amount_fen
    if absolute_daily_cap_fen is not None and after > absolute_daily_cap_fen:
        raise PolicyDenied(
            "DAILY_ABSOLUTE_CAP_EXCEEDED",
            account_id=hold.account_id,
            hold_id=hold.id,
            amount_fen=amount_fen,
            absolute_cap_fen=absolute_daily_cap_fen,
            spent_fen=spent_before,
        )
    if (
        daily_risk_threshold_fen is not None
        and after > daily_risk_threshold_fen
        and hold.risk_level != "red"
    ):
        raise PolicyDenied(
            "DAILY_RISK_UPGRADE_REQUIRED",
            account_id=hold.account_id,
            hold_id=hold.id,
            amount_fen=amount_fen,
            threshold_fen=daily_risk_threshold_fen,
            spent_fen=spent_before,
            hold_risk_level=hold.risk_level,
            user_id=user_id,
            day=day,
        )


def _bump_daily_counters(
    conn: sqlite3.Connection,
    account_id: str,
    amount_fen: int,
    stamp_ms: int,
) -> None:
    """把本笔记进两本账：账户维度（账单口径）与用户维度（策略口径），同一事务。

    两本账必须同源更新，否则「按账户看的账单」和「判级用的累计」会长期打架，
    这正是本轮要修的口径不一致。
    """
    day = day_of(stamp_ms)
    conn.execute(
        "INSERT INTO daily_limits (account_id, day, spent_fen, tx_count) VALUES (?, ?, ?, 1)"
        " ON CONFLICT(account_id, day) DO UPDATE SET"
        " spent_fen = spent_fen + excluded.spent_fen, tx_count = tx_count + 1",
        (account_id, day, amount_fen),
    )
    user_id = account_user_id(conn, account_id)
    if user_id is None:  # 账户不存在：上面的 UPDATE 已经会失败，这里只是防御
        return
    conn.execute(
        "INSERT INTO daily_user_limits (user_id, day, spent_fen, tx_count) VALUES (?, ?, ?, 1)"
        " ON CONFLICT(user_id, day) DO UPDATE SET"
        " spent_fen = spent_fen + excluded.spent_fen, tx_count = tx_count + 1",
        (user_id, day, amount_fen),
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
