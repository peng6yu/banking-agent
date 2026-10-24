"""资金不变量回归测试：幂等、并发、hold 过期、部分失败、日累计限额。

钱是最不能错的东西。正路径（转了多少钱、余额变没变）别的文件已经覆盖，
这里只钉「异常路径会不会多扣、少扣、卡住」：

1. 同一幂等键重复提交 → 只扣一次；
2. 两个会话并发转同一账户 → 总扣款不超过余额，不出现负余额；
3. hold 超时（300 秒）→ 自动释放，钱回到可用余额；超时后再 commit 必须失败；
4. commit 阶段炸了 → hold 不释放也不误扣（失败方向安全：钱宁可留 hold 等释放，
   也不能凭空扣掉）；
5. 日累计分级 → 两笔各 900 元同时提交，第二笔必须升级强验证（不能两笔都放行）；
   红牌（真验过）可以越线，绝对上限（显式配置）才硬拒。

测的是真实资金函数（app/funds.py、app/tools/transfers.py），不 mock 钱的路径；
并发用例用 threading + 真实 sqlite 文件库（不是 :memory:），让锁竞争真实发生。
每条不变量都写成精确数字断言：不变量一旦被破坏，用例必须红。
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Callable

import pytest

from app import clock, db, funds
from app.clock import BankError, HoldExpired, PolicyDenied
from app.models import Action
from app.policy.rules import DAILY_LIMIT_FEN
from app.tools import transfers
from app.tools.base import ToolContext
from tests.tool_fixtures import (
    CREDIT_ID,
    DEBIT_BALANCE,
    DEBIT_ID,
    FROZEN_MS,
    PAYEE_LIJING,
    SECRET,
    _balance,
    _token,
    bank,
    ctx,
)

__all__ = ["bank", "ctx"]


# ---------- 辅助 ----------

def _db_path(conn: sqlite3.Connection) -> str:
    """从连接里取真实文件库路径：并发用例每个线程自己开一条连接。"""
    return str(conn.execute("PRAGMA database_list").fetchone()[2])


def _set_balance(conn: sqlite3.Connection, balance_fen: int) -> None:
    conn.execute("UPDATE accounts SET balance_fen = ? WHERE id = ?", (balance_fen, DEBIT_ID))


def _txn_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """只取本用例产生的流水：种子流水的 hold_id 是空的，带 hold_id 的才是资金两阶段的账。"""
    return list(
        conn.execute(
            "SELECT id, direction, amount_fen, hold_id, balance_after_fen FROM transactions"
            " WHERE account_id = ? AND hold_id IS NOT NULL ORDER BY ts_ms, id",
            (DEBIT_ID,),
        ).fetchall()
    )


def _hold_statuses(conn: sqlite3.Connection) -> list[str]:
    return [str(row["status"]) for row in conn.execute("SELECT status FROM holds").fetchall()]


def _rejected_audits(conn: sqlite3.Connection) -> list[dict[str, object]]:
    """审计里所有「被拒绝」的记录：拒绝也要留痕，这是操作审计的要求。"""
    rejected: list[dict[str, object]] = []
    for row in conn.execute(
        "SELECT seq, event_type, payload_json FROM audit_log ORDER BY seq"
    ).fetchall():
        payload = json.loads(row["payload_json"])
        if payload.get("rejected"):
            rejected.append({"seq": row["seq"], "event_type": row["event_type"], **payload})
    return rejected


def _run_concurrently(workers: list[Callable[[threading.Barrier], object]]) -> list[object]:
    """并发跑几个真实线程，返回各线程的结果；线程里的异常原样带回主线程。

    worker 拿到的是同一道闸门（Barrier）：各线程自己决定在什么位置对齐，
    这样「两个会话同时读到同一份旧数据」这种竞态才是真的，不是排好队的假并发。
    """
    barrier = threading.Barrier(len(workers))
    results: list[object] = [None] * len(workers)
    errors: list[BaseException] = []

    def runner(index: int, worker: Callable[[threading.Barrier], object]) -> None:
        try:
            results[index] = worker(barrier)
        except BaseException as exc:  # noqa: BLE001 - 线程里的异常必须带回主线程
            errors.append(exc)

    threads = [
        threading.Thread(target=runner, args=(index, worker)) for index, worker in enumerate(workers)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert not any(thread.is_alive() for thread in threads), "并发用例超时了"
    if errors:
        raise errors[0]
    return results


class _FlakyAudit:
    """把审计写账这一步变成会炸的：模拟 commit 最后一步（记账后写审计）失败。

    故障注入点在审计层而不是资金函数本身：钱的那套逻辑（扣余额、写流水、
    改 hold、日累计）照常跑一遍，只是最后一步炸了——这正是「部分失败」的现实形状。
    """

    def __init__(self, error: BaseException) -> None:
        self.error = error

    def append(
        self,
        conn: sqlite3.Connection,
        event_type: str,
        payload: dict[str, object],
        *,
        session_id: str | None = None,
        task_id: str | None = None,
    ) -> int:
        if event_type == "transfer.committed":
            raise self.error
        return 0


def _transfer_action(amount_fen: int) -> tuple[Action, dict[str, object]]:
    params: dict[str, object] = {"payee_id": PAYEE_LIJING, "amount_fen": amount_fen}
    action = Action(
        tool="transfer", params=params, account_id=DEBIT_ID, amount_fen=amount_fen
    )
    return action, params


# ---------- 1. 幂等：同一个幂等键只扣一次 ----------

def test_same_idempotency_key_reuses_one_hold(bank) -> None:
    """同一个幂等键只会有一笔 hold：重放不二次占额度，重复 commit 也只扣一次。"""
    action, params = _transfer_action(30_000)
    first = funds.initiate(
        bank, action, session_id="s_idem", node_id="n1",
        token=_token("transfer", params), secret=SECRET,
    )
    second = funds.initiate(
        bank, action, session_id="s_idem", node_id="n1",
        token=_token("transfer", params), secret=SECRET,
    )
    assert first.hold_id == second.hold_id
    assert (first.reused, second.reused) == (False, True)
    assert len(bank.execute("SELECT id FROM holds").fetchall()) == 1

    outcome = funds.commit(bank, first.hold_id)
    again = funds.commit(bank, first.hold_id)
    assert outcome.idempotent is False
    assert again.idempotent is True
    assert _balance(bank) == DEBIT_BALANCE - 30_000
    assert len(_txn_rows(bank)) == 1


def test_same_transfer_submitted_twice_deducts_only_once(bank, ctx) -> None:
    """同一笔转账提交两次（同一幂等键）→ 只扣一次，第二次是幂等命中。"""
    params = {"payee_id": PAYEE_LIJING, "amount_fen": 30_000}
    first = transfers.transfer(ctx, params, _token("transfer", params))
    second = transfers.transfer(ctx, params, _token("transfer", params))

    assert first.ok is True and second.ok is True
    assert first.data["status"] == "committed"
    assert second.data["status"] == "committed"
    assert second.data["idempotent"] is True
    assert _balance(bank) == DEBIT_BALANCE - 30_000
    assert len(_txn_rows(bank)) == 1


# ---------- 2. 并发：同一账户不超支 ----------

def test_concurrent_transfers_from_two_sessions_never_overdraw(bank) -> None:
    """两个会话并发对同一账户转账 → 总扣款不超过余额，不出现负余额。

    金额刻意选成「两笔加起来超过余额、但不超过日累计硬线」，
    这样拦住第二笔的必须是余额校验本身，不是别的规则。
    """
    start = 50_000
    amount = 30_000
    _set_balance(bank, start)
    db_path = _db_path(bank)
    params = {"payee_id": PAYEE_LIJING, "amount_fen": amount}

    def make_worker(session_id: str) -> Callable[[threading.Barrier], object]:
        def worker(barrier: threading.Barrier) -> object:
            conn = db.connect(db_path)
            try:
                barrier.wait(timeout=10)
                context = ToolContext(
                    conn=conn, session_id=session_id, node_id="n1",
                    account_id=DEBIT_ID, secret=SECRET,
                )
                return transfers.transfer(context, params, _token("transfer", params))
            finally:
                conn.close()

        return worker

    results = _run_concurrently([make_worker("s_a"), make_worker("s_b")])

    committed = [r for r in results if r.ok and r.data.get("status") == "committed"]
    assert len(committed) == 1, "两笔都扣成功了，总扣款超过余额"
    assert _balance(bank) == start - amount
    assert _balance(bank) >= 0
    outs = [row for row in _txn_rows(bank) if row["direction"] == "out"]
    assert len(outs) == 1
    assert int(outs[0]["amount_fen"]) == amount
    assert _hold_statuses(bank).count("committed") == 1


# ---------- 3. hold 超时：自动释放，超时后不能再 commit ----------

def test_hold_expires_after_300_seconds_and_money_stays_available(bank, ctx) -> None:
    """hold 超时（300 秒）后自动释放，钱回到可用余额；超时后再 commit 必须失败。"""
    assert funds.HOLD_TTL_MS == 300_000
    action, params = _transfer_action(30_000)
    hold = funds.initiate(
        bank, action, session_id="s_ttl", node_id="n1",
        token=_token("transfer", params), secret=SECRET,
    )
    assert hold.expires_ms == FROZEN_MS + 300_000
    assert _balance(bank) == DEBIT_BALANCE, "hold 只是锁额度，不动钱"

    other = funds.initiate(
        bank, action, session_id="s_ttl2", node_id="n1",
        token=_token("transfer", params), secret=SECRET,
    )
    clock.freeze(FROZEN_MS + funds.HOLD_TTL_MS + 1)

    # 超时后再 commit 必须失败，钱一分不能扣
    with pytest.raises(HoldExpired):
        funds.commit(bank, hold.hold_id)
    assert funds.fetch_hold(bank, hold.hold_id).status == "expired"
    assert _balance(bank) == DEBIT_BALANCE
    assert _txn_rows(bank) == []

    # 自动释放：清扫把还在等的 hold 也作废掉
    assert db.sweep_expired_holds(bank) == 1
    assert funds.fetch_hold(bank, other.hold_id).status == "expired"

    # 钱回到可用余额：同一笔金额照样能转出去
    result = transfers.transfer(ctx, params, _token("transfer", params))
    assert result.ok is True
    assert result.data["status"] == "committed"
    assert _balance(bank) == DEBIT_BALANCE - 30_000


# ---------- 4. 部分失败：hold 不释放也不误扣 ----------

def test_commit_stage_failure_keeps_hold_and_deducts_nothing(bank, monkeypatch) -> None:
    """commit 阶段抛错时，hold 不释放也不误扣（钱宁可留 hold 等释放）。"""
    action, params = _transfer_action(30_000)
    hold = funds.initiate(
        bank, action, session_id="s_partial", node_id="n1",
        token=_token("transfer", params), secret=SECRET,
    )
    monkeypatch.setattr(funds, "audit", _FlakyAudit(RuntimeError("注入的账本写失败")))

    with pytest.raises(RuntimeError):
        funds.commit(bank, hold.hold_id)

    # 一分没扣，账本没多一笔，今天的额度也没动
    assert _balance(bank) == DEBIT_BALANCE
    assert _txn_rows(bank) == []
    assert funds.spent_today_fen(bank, DEBIT_ID) == 0
    # hold 没有被释放，仍在等超时或重试
    assert funds.fetch_hold(bank, hold.hold_id).status == "held"


def test_tool_keeps_money_in_hold_when_commit_fails(bank, ctx, monkeypatch) -> None:
    """工具层同样不误扣：commit 阶段炸了，钱还锁在 hold 里，结果带 settle_error。"""
    params = {"payee_id": PAYEE_LIJING, "amount_fen": 30_000}
    monkeypatch.setattr(funds, "audit", _FlakyAudit(BankError("COMMIT_LEDGER_WRITE_FAILED")))

    result = transfers.transfer(ctx, params, _token("transfer", params))

    assert result.ok is True
    assert result.data["settle_error"] == "COMMIT_LEDGER_WRITE_FAILED"
    assert _balance(bank) == DEBIT_BALANCE
    assert _txn_rows(bank) == []
    hold = funds.fetch_hold(bank, str(result.data["hold_id"]))
    assert hold is not None
    assert hold.status == "held"


# ---------- 5. 日累计分级：越线重新强验证，不是无依据硬拒 ----------
# 2026-10 整改：赛题把日累计 1000 元定义成**黄红分级线**（≤1000 黄、>1000 红），
# 不是「今天到此为止」的硬限额。资金层因此分成两道语义不同的闸：
#   · 风险阈值：越线的黄卡一律回滚并抛 DAILY_RISK_UPGRADE_REQUIRED，让上层重新走强验证；
#     用户真的验过人脸+短信（hold.risk_level='red'）才放行——「验过就能继续」。
#   · 绝对上限：只有部署显式配置才存在（默认 None），越线硬拒，因子不能加码。

def _red_token(tool: str, params: dict[str, object]) -> str:
    """签一张**已经强验证过**的红牌令牌：hold 落库时 risk_level='red'。"""
    from app.security import token as token_mod

    action = Action(tool=tool, params=params, account_id=DEBIT_ID,
                    amount_fen=int(params["amount_fen"]))
    return token_mod.issue(action, level="red", secret=SECRET)


def test_daily_risk_threshold_upgrades_a_yellow_hold(bank) -> None:
    """今日已出账 900 元，第二笔 900 元的黄卡不能直接过：整笔回滚、要求重新强验证。"""
    action, params = _transfer_action(90_000)
    first = funds.initiate(
        bank, action, session_id="s_daily1", node_id="n1",
        token=_token("transfer", params), secret=SECRET,
    )
    funds.commit(bank, first.hold_id, daily_risk_threshold_fen=DAILY_LIMIT_FEN)
    assert funds.spent_today_fen(bank, DEBIT_ID) == 90_000

    second = funds.initiate(
        bank, action, session_id="s_daily2", node_id="n1",
        token=_token("transfer", params), secret=SECRET,
    )
    with pytest.raises(PolicyDenied) as excinfo:
        funds.commit(bank, second.hold_id, daily_risk_threshold_fen=DAILY_LIMIT_FEN)
    assert excinfo.value.code == "DAILY_RISK_UPGRADE_REQUIRED"

    # 整笔回滚：一分没扣，hold 还锁着等上层重新验证或释放，日累计没被这笔污染
    assert _balance(bank) == DEBIT_BALANCE - 90_000
    assert funds.fetch_hold(bank, second.hold_id).status == "held"
    assert funds.spent_today_fen(bank, DEBIT_ID) == 90_000

    rejected = _rejected_audits(bank)
    assert len(rejected) == 1
    assert rejected[0]["reason"] == "DAILY_RISK_UPGRADE_REQUIRED"
    assert rejected[0]["amount_fen"] == 90_000


def test_red_hold_after_verification_may_cross_the_threshold(bank) -> None:
    """用户真验过人脸+短信（红牌 hold），累计 1800 元也应当放行——这是赛题的红档定义。"""
    action, params = _transfer_action(90_000)
    first = funds.initiate(
        bank, action, session_id="s_red1", node_id="n1",
        token=_token("transfer", params), secret=SECRET,
    )
    funds.commit(bank, first.hold_id, daily_risk_threshold_fen=DAILY_LIMIT_FEN)

    second = funds.initiate(
        bank, action, session_id="s_red2", node_id="n1",
        token=_red_token("transfer", params), secret=SECRET,
    )
    assert funds.fetch_hold(bank, second.hold_id).risk_level == "red"
    funds.commit(bank, second.hold_id, daily_risk_threshold_fen=DAILY_LIMIT_FEN)

    assert _balance(bank) == DEBIT_BALANCE - 180_000
    assert funds.spent_today_fen(bank, DEBIT_ID) == 180_000
    assert _rejected_audits(bank) == []


def test_absolute_daily_cap_rejects_even_a_red_hold(bank) -> None:
    """显式配置的绝对上限与验证等级无关：红牌也不能越线，因子不能给监管额度加码。"""
    action, params = _transfer_action(90_000)
    hold = funds.initiate(
        bank, action, session_id="s_cap", node_id="n1",
        token=_red_token("transfer", params), secret=SECRET,
    )
    with pytest.raises(PolicyDenied) as excinfo:
        funds.commit(bank, hold.hold_id, daily_risk_threshold_fen=None,
                     absolute_daily_cap_fen=50_000)
    assert excinfo.value.code == "DAILY_ABSOLUTE_CAP_EXCEEDED"
    assert _balance(bank) == DEBIT_BALANCE
    assert funds.spent_today_fen(bank, DEBIT_ID) == 0


def test_daily_spend_is_user_scoped_across_accounts(bank) -> None:
    """日累计是**用户维度**：同一人名下换一张卡花，也要算进同一个当日累计。

    赛题原文的「日累计」按人算；若按账户各算各的，用户换卡就能绕过红档，
    这正是本轮要修的口径不一致（daily_user_limits 与 daily_limits 两本账）。
    """
    action, params = _transfer_action(90_000)
    first = funds.initiate(
        bank, action, session_id="s_u1", node_id="n1",
        token=_token("transfer", params), secret=SECRET,
    )
    funds.commit(bank, first.hold_id, daily_risk_threshold_fen=DAILY_LIMIT_FEN)

    # 同一个人的另一个账户 a_credit 再发起 200 元：用户维度已 900，合计 1100 越线
    bank.execute("UPDATE accounts SET balance_fen = 200_000 WHERE id = ?", (CREDIT_ID,))
    params2 = {"payee_id": PAYEE_LIJING, "amount_fen": 20_000}
    action2 = Action(tool="transfer", params=params2, account_id=CREDIT_ID, amount_fen=20_000)
    from app.security import token as token_mod

    tok = token_mod.issue(action2, secret=SECRET)
    hold = funds.initiate(bank, action2, session_id="s_u2", node_id="n1", token=tok, secret=SECRET)
    with pytest.raises(PolicyDenied) as excinfo:
        funds.commit(bank, hold.hold_id, daily_risk_threshold_fen=DAILY_LIMIT_FEN)
    assert excinfo.value.code == "DAILY_RISK_UPGRADE_REQUIRED"
    # 账本口径仍是各账户一本，但策略口径已经是同一个人的合计
    assert funds.spent_today_fen(bank, CREDIT_ID) == 90_000


def test_concurrent_daily_risk_upgrade_blocks_the_second_transfer(bank) -> None:
    """两笔各 900 元并发提交：只有一笔能过，另一笔必须升级强验证（不能两笔都放行）。"""
    db_path = _db_path(bank)
    params = {"payee_id": PAYEE_LIJING, "amount_fen": 90_000}

    def make_worker(session_id: str) -> Callable[[threading.Barrier], object]:
        def worker(barrier: threading.Barrier) -> object:
            conn = db.connect(db_path)
            try:
                # 策略层先读一次今日已用：这一步天生有竞态，两个会话可能都读到 0
                spent_before = funds.spent_today_fen(conn, DEBIT_ID)
                barrier.wait(timeout=10)
                context = ToolContext(
                    conn=conn, session_id=session_id, node_id="n1",
                    account_id=DEBIT_ID, secret=SECRET,
                )
                result = transfers.transfer(context, params, _token("transfer", params))
                return spent_before, result
            finally:
                conn.close()

        return worker

    outcomes = _run_concurrently([make_worker("s_a"), make_worker("s_b")])
    spent_before = [int(pair[0]) for pair in outcomes]
    results = [pair[1] for pair in outcomes]

    # 竞态前提成立：两个会话都以为今天还没花
    assert spent_before == [0, 0]
    committed = [r for r in results if r.ok and r.data.get("status") == "committed"]
    assert len(committed) == 1, "两笔 900 元都放行了，日累计分级形同虚设"

    blocked = [r for r in results if r is not committed[0]]
    assert len(blocked) == 1
    assert blocked[0].ok is False
    assert blocked[0].error_code == "DAILY_RISK_UPGRADE_REQUIRED"
    assert blocked[0].data.get("settle_error") == "DAILY_RISK_UPGRADE_REQUIRED"

    assert len([row for row in _txn_rows(bank) if row["direction"] == "out"]) == 1
    assert funds.spent_today_fen(bank, DEBIT_ID) == 90_000
    assert _balance(bank) == DEBIT_BALANCE - 90_000
    assert _balance(bank) >= 0

    rejected = _rejected_audits(bank)
    assert len(rejected) == 1
    assert rejected[0]["reason"] == "DAILY_RISK_UPGRADE_REQUIRED"
