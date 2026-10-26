"""手动补录与模拟注入：往本行流水里加一笔，并同步演示余额。

这两条是同一个口径的两个入口，都只写 transactions + accounts：
- 手动补录可以指定账户（储蓄卡/信用卡）与发生时间（最近 90 天，不能是未来）；
- 幂等靠客户端提交编号 client_nonce：同一次提交重试只记一笔，换一笔就换一个编号；
  没带编号就每次都算新的一笔（旧行为是「同日同参数自动去重」，会吞掉一天里
  两杯同价咖啡这种真实的重复消费，已废弃）；
- 信用卡的 balance_fen 是「这张卡还能用多少额度」（种子按额度起算逐笔扣减），
  所以刷卡减、还款加，且不碰储蓄卡余额；储蓄卡才是扣账面余额；
- 补录完立刻按这笔发生的那一个月跑一遍账单三条规则，把与这笔有关的命中带回去；
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
from app.tools import bill
from app.use_cases.base import write_case

#: 用户自己补录的渠道值：既方便幂等去重，也一眼看出这行不是银行同步来的。
MANUAL_CHANNEL: Final[str] = "manual"
MANUAL_NOTICE: Final[str] = "这笔是你自己补录的，只进本行流水与演示余额，不对应任何真实支付。"
#: 信用卡补录的边界说明：只占额度，不动储蓄卡。
CREDIT_NOTICE: Final[str] = "这笔记在信用卡上：只影响这张卡的可用额度，不动储蓄卡的余额。"
#: 模拟流水的渠道值；和真实渠道分开，账单里也能一眼看出是演示注入。
SIMULATED_CHANNEL: Final[str] = "simulated"
#: 响应里固定附带的一句人话：这是演示注入，不是真实支付。
SIMULATED_EXPENSE_NOTICE: Final[str] = "这是竞赛演示用的模拟流水，不对应任何真实支付。"
#: 模拟支出的审计类型；旧闭集里没有时退回 tool.executed（payload 仍带 simulated）。
_SIMULATED_EXPENSE_EVENT: Final[str] = "proactive.simulated_expense"
_DAY_MS: Final[int] = 86_400_000
#: 允许补录的最远过去：再早的数据没有可信的期末余额可以回填。
MAX_BACKDATE_MS: Final[int] = 90 * _DAY_MS


def _simulated_event_type() -> str:
    """审计事件类型：闭集里有专用类型就用专用的，旧闭集退回 tool.executed。"""
    return _SIMULATED_EXPENSE_EVENT if _SIMULATED_EXPENSE_EVENT in audit.EVENT_TYPES else "tool.executed"


def _account(conn: Any, account_id: str | None) -> dict[str, Any]:
    """取要记账的账户：指定了就必须是本人的账户，没指定就用主储蓄卡。

    信用卡的 balance_fen 是可用额度（credit_limit_fen 是总额度），不是欠款；
    这一点由种子数据和 app/views/money.py:_accounts 的注释共同锁定。
    """
    user_id = proactive.default_user_id(conn)
    if account_id:
        row = conn.execute(
            "SELECT * FROM accounts WHERE id = ? AND user_id = ?", (account_id, user_id)
        ).fetchone()
        if row is None:
            raise ValidationError(
                "找不到这个账户，可能已经被销户或者编号不对。",
                reason="ACCOUNT_NOT_FOUND",
                field="account_id",
            )
        return dict(row)
    row = conn.execute(
        "SELECT * FROM accounts WHERE user_id = ?"
        " ORDER BY CASE type WHEN 'debit' THEN 0 ELSE 1 END, id LIMIT 1",
        (user_id,),
    ).fetchone()
    if row is None:
        raise ValidationError("账户不存在", reason="NO_ACCOUNTS")
    return dict(row)


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


def _after_balance(account: dict[str, Any], entry: "_Entry") -> int:
    """按账户类型算记账后余额，并把不够/还不进的挡在这里（一分不动）。"""
    balance_fen = int(account["balance_fen"])
    is_credit = str(account["type"]) == "credit"
    if entry.direction == "out":
        if entry.amount_fen > balance_fen:
            if is_credit:
                raise InsufficientFunds(
                    "这张信用卡的可用额度不够这笔，这笔没有记上。",
                    account_id=str(account["id"]),
                    balance_fen=balance_fen,
                    amount_fen=entry.amount_fen,
                )
            raise InsufficientFunds(
                entry.insufficient_text,
                account_id=str(account["id"]),
                balance_fen=balance_fen,
                amount_fen=entry.amount_fen,
            )
        return balance_fen - entry.amount_fen
    after = balance_fen + entry.amount_fen
    if is_credit:
        limit_fen = int(account["credit_limit_fen"] or 0)
        if after > limit_fen:
            used_fen = max(0, limit_fen - balance_fen)
            raise ValidationError(
                "这张信用卡已经用掉的额度只有 " + fen_to_yuan_str(used_fen)
                + " 元，还不进 " + fen_to_yuan_str(entry.amount_fen) + " 元。",
                field="amount_fen",
                reason="OVER_CREDIT_REPAYMENT",
            )
    return after


def _insert(
    conn: Any,
    *,
    account_id: str,
    balance_after_fen: int,
    entry: "_Entry",
    happened: int,
    client_nonce: str | None,
) -> str:
    """写流水（余额写到账后值），返回流水号。"""
    transaction_id = new_id("t")
    conn.execute(
        "INSERT INTO transactions (id, account_id, direction, amount_fen, counterparty,"
        " category, note, ts_ms, balance_after_fen, hold_id, channel, client_nonce)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)",
        (
            transaction_id,
            account_id,
            entry.direction,
            entry.amount_fen,
            entry.merchant,
            entry.category,
            entry.note,
            happened,
            balance_after_fen,
            entry.channel,
            client_nonce,
        ),
    )
    conn.execute("UPDATE accounts SET balance_fen = ? WHERE id = ?", (balance_after_fen, account_id))
    return transaction_id


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
    account: dict[str, Any],
    transaction_id: str,
    idempotent: bool,
    happened: int,
    hits: list[dict[str, Any]],
    balance_after_fen: int | None = None,
) -> dict[str, Any]:
    """审计里落什么：谁、哪一笔、多少钱、记在哪个账户、命中了哪条规则。"""
    payload: dict[str, Any] = {
        "tool": entry.tool,
        "ok": True,
        **entry.extra,
        "idempotent": idempotent,
        "account_id": str(account["id"]),
        "account_type": str(account["type"]),
        "transaction_id": transaction_id,
        "amount_fen": entry.amount_fen,
        "merchant": entry.merchant,
        "category": entry.category,
        "ts_ms": int(happened),
        "hits": [hit["kind"] for hit in hits],
    }
    if balance_after_fen is not None:
        payload["balance_after_fen"] = balance_after_fen
        payload["note"] = entry.note
    return payload


def _flow_result(
    entry: _Entry,
    *,
    account: dict[str, Any],
    transaction: dict[str, Any],
    balance_after_fen: int,
    audit_seq: int,
    idempotent: bool,
    hits: list[dict[str, Any]],
) -> dict[str, Any]:
    """回给前端的形状：两个入口只差那个布尔位的名字，其余字段一致。"""
    notice = CREDIT_NOTICE if str(account["type"]) == "credit" else entry.notice
    return {
        entry.kind: True,
        "notice": notice,
        "transaction": transaction,
        "balance_after_fen": balance_after_fen,
        "balance_after_yuan": fen_to_yuan_str(balance_after_fen),
        "account": {
            "account_id": str(account["id"]),
            "type": str(account["type"]),
            "name": str(account["name"]),
            "tail": str(account["tail"]),
        },
        "hits": hits,
        "hit_count": len(hits),
        "hits_note": (
            "这笔命中了上面列出的账单规则，可以去账单分析里看依据；"
            "助手提醒也会在下次生成时把它算进去。"
            if hits
            else "这笔没有命中拆单、凌晨境外、订阅涨价三条规则。"
        ),
        "audit_seq": audit_seq,
        "idempotent": idempotent,
    }


def _find_by_nonce(conn: Any, client_nonce: str) -> Any:
    return conn.execute(
        "SELECT * FROM transactions WHERE client_nonce = ?", (client_nonce,)
    ).fetchone()


def _find_same_day(conn: Any, entry: _Entry, account_id: str, happened: int) -> Any:
    """模拟注入专用：同一本地自然日、同参数只注入一次，避免演示脚本重复点。"""
    day_start = proactive._period_day_start_ms(proactive.local_date(happened))
    return conn.execute(
        "SELECT * FROM transactions WHERE account_id = ? AND direction = ?"
        " AND amount_fen = ? AND counterparty = ? AND category = ? AND note = ?"
        " AND channel = ? AND ts_ms >= ? AND ts_ms < ?"
        " ORDER BY ts_ms DESC, id DESC LIMIT 1",
        (
            account_id,
            entry.direction,
            entry.amount_fen,
            entry.merchant,
            entry.category,
            entry.note,
            entry.channel,
            day_start,
            day_start + _DAY_MS,
        ),
    ).fetchone()


def _nonce_conflict(existing: Any, entry: _Entry, account_id: str, happened: int) -> bool:
    """同一个提交编号只许对应同一笔；改了金额/商户/时间却复用编号，是客户端 bug。"""
    return (
        str(existing["account_id"]) != account_id
        or str(existing["direction"]) != entry.direction
        or int(existing["amount_fen"]) != entry.amount_fen
        or str(existing["counterparty"]) != entry.merchant
        or str(existing["category"]) != entry.category
        or str(existing["note"]) != entry.note
        or int(existing["ts_ms"]) != happened
    )


def _record_existing(conn: Any, entry: _Entry, existing: Any, account: dict[str, Any], happened: int) -> dict[str, Any]:
    """同一个提交编号的重试：原样返回那一笔，审计里标 idempotent=true。"""
    transaction_id = str(existing["id"])
    hits = bill.explain_transaction(
        conn,
        account_id=str(account["id"]),
        transaction_id=transaction_id,
        ts_ms=int(existing["ts_ms"]),
    )
    audit_seq = audit.append(
        conn,
        entry.audit_event,
        _audit_payload(
            entry,
            account=account,
            transaction_id=transaction_id,
            idempotent=True,
            happened=happened,
            hits=hits,
        ),
    )
    return _flow_result(
        entry,
        account=account,
        transaction=_row_view(existing, kind=entry.kind),
        balance_after_fen=int(existing["balance_after_fen"]),
        audit_seq=audit_seq,
        idempotent=True,
        hits=hits,
    )


def _record_fresh(
    conn: Any,
    entry: _Entry,
    *,
    account: dict[str, Any],
    happened: int,
    client_nonce: str | None,
) -> dict[str, Any]:
    """真正记一笔新的：先算账后余额（不够就抛），再写流水 + 同步余额 + 落审计。"""
    account_id = str(account["id"])
    balance_after_fen = _after_balance(account, entry)
    transaction_id = _insert(
        conn,
        account_id=account_id,
        balance_after_fen=balance_after_fen,
        entry=entry,
        happened=happened,
        client_nonce=client_nonce,
    )
    hits = bill.explain_transaction(
        conn, account_id=account_id, transaction_id=transaction_id, ts_ms=happened
    )
    audit_seq = audit.append(
        conn,
        entry.audit_event,
        _audit_payload(
            entry,
            account=account,
            transaction_id=transaction_id,
            idempotent=False,
            happened=happened,
            hits=hits,
            balance_after_fen=balance_after_fen,
        ),
    )
    return _flow_result(
        entry,
        account=account,
        transaction=_row_view(
            {
                "id": transaction_id,
                "account_id": account_id,
                "direction": entry.direction,
                "amount_fen": entry.amount_fen,
                "counterparty": entry.merchant,
                "category": entry.category,
                "note": entry.note,
                "ts_ms": happened,
                "balance_after_fen": balance_after_fen,
                "channel": entry.channel,
            },
            kind=entry.kind,
        ),
        balance_after_fen=balance_after_fen,
        audit_seq=audit_seq,
        idempotent=False,
        hits=hits,
    )


def _record(
    conn: Any,
    entry: _Entry,
    *,
    account_id: str | None = None,
    ts_ms: int | None = None,
    client_nonce: str | None = None,
    reuse_same_day: bool = False,
) -> dict[str, Any]:
    """两个入口共用的记账流程：定位账户 -> 校验时间 -> 查重 -> 记账 -> 检测。

    manual 靠 client_nonce 查重（同日同额可以是两笔）；simulate 传 reuse_same_day，
    保留旧的「同一天同参数只注入一次」语义，免得演示脚本连点把余额刷爆。
    """
    account = _account(conn, account_id)
    now = clock.now_ms()
    happened = now if ts_ms is None else int(ts_ms)
    if happened > now:
        raise ValidationError("发生时间不能晚于现在。", field="ts_ms", reason="FUTURE_TS")
    if happened < now - MAX_BACKDATE_MS:
        raise ValidationError(
            "只能补最近 90 天内的账；更早的流水没有可信的期末余额可以回填。",
            field="ts_ms",
            reason="TOO_OLD_TS",
        )
    nonce = str(client_nonce or "").strip() or None
    if nonce:
        existing = _find_by_nonce(conn, nonce)
        if existing is not None:
            # 没显式给时间时，「现在」是服务端每次调用自己取的毫秒，重试一定差几毫秒。
            # 这种差异不是用户改内容，沿用原笔的时间再比对，否则幂等会变成 NONCE_CONFLICT。
            if ts_ms is None:
                happened = int(existing["ts_ms"])
            if _nonce_conflict(existing, entry, str(account["id"]), happened):
                raise ValidationError(
                    "同一个提交编号不能对应两笔不同的账；内容改了就连同编号一起换。",
                    field="client_nonce",
                    reason="NONCE_CONFLICT",
                )
            return _record_existing(conn, entry, existing, account, happened)
    if reuse_same_day:
        existing = _find_same_day(conn, entry, str(account["id"]), happened)
        if existing is not None:
            return _record_existing(conn, entry, existing, account, int(existing["ts_ms"]))
    return _record_fresh(
        conn, entry, account=account, happened=happened, client_nonce=nonce
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
    account_id: str | None = None,
    ts_ms: int | None = None,
    client_nonce: str | None = None,
) -> dict[str, Any]:
    """用户自己补一笔流水（接不到银行官方接口时的入口）。"""
    name = str(merchant or "").strip()
    if not name:
        raise ValidationError("商户名不能是空白", field="merchant")
    return _record(
        conn,
        _Entry(
            channel=MANUAL_CHANNEL,
            kind="manual",
            direction=str(direction or "out"),
            merchant=name,
            amount_fen=int(amount_fen),
            category=str(category or "其他").strip() or "其他",
            note=str(note or "").strip(),
            audit_event="transaction.manual",
            tool="manual_transaction",
            notice=MANUAL_NOTICE,
            insufficient_text="演示账户余额不足，这笔补录没有记上",
            extra={"direction": str(direction or "out")},
        ),
        account_id=account_id,
        ts_ms=ts_ms,
        client_nonce=client_nonce,
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
        reuse_same_day=True,
    )


__all__ = [
    "CREDIT_NOTICE",
    "MANUAL_CHANNEL",
    "MANUAL_NOTICE",
    "MAX_BACKDATE_MS",
    "SIMULATED_CHANNEL",
    "SIMULATED_EXPENSE_NOTICE",
    "manual_record",
    "expense_simulate",
]
