"""赛题「日累计 1000 元」黄红分级的验收矩阵（Task 1）。

赛题原文：小额转账日累计 ≤ 1000 元 = 黄（用户确认）；大额转账日累计 > 1000 元 =
红（人脸/短信/盾多因子）。整改前 R07 把越线写成**硬拒绝且不带因子**，funds 又用
SQL 条件更新把日累计钉死在 1000 以内——两处一起把「红档」做成了「今天到此为止」。
这里按赛题重新定义正确行为，并用真实资金函数（不 mock）逐条钉住：

- 500 + 400 / 500 + 500（正好等于线）仍是黄；
- 500 + 800 越线转红，黄卡提交被回滚、要求重新强验证；
- 今天第一笔就 > 1000 是单笔红（R08）；
- 红档缺验证/错验证/过期验证一律不能落账；验证只绑定本次收款人/金额/账户；
- 两个各自在阈值内的并发操作合计越线时，只能落一笔；
- 预约创建期是黄、到点复判为红 → 挂起待验证，补验证后才执行；
- 余额不足/策略失败/升级回滚的操作不许污染日累计。
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Callable

import pytest

from app import clock, db, funds
from app.clock import BankError, PolicyDenied
from app.models import Action
from app.policy import PolicyContext, decide
from app.policy.rules import DAILY_LIMIT_FEN
from app.security import factors
from app.security import token as token_mod
from app.tools import scheduled
from app.tools.base import build_action
from tests.tool_fixtures import (
    DEBIT_BALANCE,
    DEBIT_ID,
    FROZEN_MS,
    PAYEE_LIJING,
    SECRET,
    USER_ID,
    _balance,
    _token,
    bank,
)

__all__ = ["bank"]

TODAY = funds.day_of(FROZEN_MS)
SMS = "sms"
FACE = "face"


def _action(amount_fen: int, *, account_id: str = DEBIT_ID,
            payee_id: str = PAYEE_LIJING) -> tuple[Action, dict[str, object]]:
    params: dict[str, object] = {"payee_id": payee_id, "amount_fen": amount_fen}
    return build_action("transfer", params, account_id, amount_fen), params


def _decide(conn: sqlite3.Connection, action: Action):
    return decide(
        action,
        PolicyContext(spent_today_fen=funds.spent_today_fen(conn, action.account_id),
                      now_ms=clock.now_ms()),
    )


def _commit(conn: sqlite3.Connection, action: Action, params: dict[str, object],
            *, token: str | None = None, session_id: str = "s_tier", node_id: str = "n1"):
    tok = token or _token(action.tool, params)
    hold = funds.initiate(conn, action, session_id=session_id, node_id=node_id,
                          token=tok, secret=SECRET)
    funds.commit(conn, hold.hold_id, daily_risk_threshold_fen=DAILY_LIMIT_FEN)
    return hold


def _spend(conn: sqlite3.Connection, amount_fen: int, *, session_id: str = "s_seed") -> None:
    action, params = _action(amount_fen)
    _commit(conn, action, params, session_id=session_id)


# ---------------------------------------------------------------- 纯策略矩阵

@pytest.mark.parametrize(
    ("spent_fen", "amount_fen", "level", "rule_id"),
    [
        (50_000, 40_000, "yellow", "R06_transfer_yellow"),   # 500 + 400 = 900 ≤ 1000
        (50_000, 50_000, "yellow", "R06_transfer_yellow"),   # 500 + 500 = 1000，含等于仍是黄
        (50_000, 80_000, "red", "R07_transfer_red_daily"),   # 500 + 800 = 1300 > 1000
        (0, 120_000, "red", "R08_transfer_red_single"),      # 今天没转，单笔就 > 1000
    ],
)
def test_contest_tier_matrix(spent_fen: int, amount_fen: int, level: str, rule_id: str) -> None:
    action, _ = _action(amount_fen)
    decision = decide(action, PolicyContext(spent_today_fen=spent_fen))
    assert (decision.level, decision.rule_id) == (level, rule_id)
    if level == "red":
        assert decision.required_factors == [FACE, SMS], "红档必须给出可完成的强验证因子"
    else:
        assert decision.required_factors == []


# ---------------------------------------------------------------- 真实资金层

def test_500_then_400_stays_yellow_and_both_settle(bank: sqlite3.Connection) -> None:
    """500 + 400 = 900：两笔都是黄，钱都该正常出去。"""
    _spend(bank, 50_000)
    action, params = _action(40_000)
    assert _decide(bank, action).rule_id == "R06_transfer_yellow"
    _commit(bank, action, params, session_id="s_second")
    assert funds.spent_today_fen(bank, DEBIT_ID) == 90_000
    assert _balance(bank) == DEBIT_BALANCE - 90_000


def test_500_then_500_exactly_at_the_line_is_still_yellow(bank: sqlite3.Connection) -> None:
    """500 + 500 = 1000：赛题写的是「≤1000 黄」，等于不是越线。"""
    _spend(bank, 50_000)
    action, params = _action(50_000)
    decision = _decide(bank, action)
    assert (decision.level, decision.rule_id) == ("yellow", "R06_transfer_yellow")
    _commit(bank, action, params, session_id="s_second")
    assert funds.spent_today_fen(bank, DEBIT_ID) == 100_000
    assert _balance(bank) == DEBIT_BALANCE - 100_000


def test_500_then_800_upgrades_to_red_and_a_yellow_card_cannot_settle(
    bank: sqlite3.Connection,
) -> None:
    """500 之后再来 800：判级转红，旧的黄确认/黄令牌不能落账，钱一分不动。"""
    _spend(bank, 50_000)
    action, params = _action(80_000)
    decision = _decide(bank, action)
    assert (decision.level, decision.rule_id) == ("red", "R07_transfer_red_daily")
    assert decision.required_factors == [FACE, SMS]

    hold = funds.initiate(bank, action, session_id="s_third", node_id="n1",
                          token=_token("transfer", params), secret=SECRET)
    with pytest.raises(PolicyDenied) as excinfo:
        funds.commit(bank, hold.hold_id, daily_risk_threshold_fen=DAILY_LIMIT_FEN)
    assert excinfo.value.code == "DAILY_RISK_UPGRADE_REQUIRED"

    # 整笔回滚：余额、日累计都没有被这笔污染；hold 还锁着等重新验证或释放
    assert _balance(bank) == DEBIT_BALANCE - 50_000
    assert funds.spent_today_fen(bank, DEBIT_ID) == 50_000
    assert funds.fetch_hold(bank, hold.hold_id).status == "held"


def test_red_after_rewriting_the_card_may_settle(bank: sqlite3.Connection) -> None:
    """同一笔 800 在用户真的验过人脸+短信之后（红牌 hold）可以落账：这是红档的定义。"""
    _spend(bank, 50_000)
    action, params = _action(80_000)
    red = token_mod.issue(action, level="red", secret=SECRET)
    _commit(bank, action, params, token=red, session_id="s_verified")
    assert funds.spent_today_fen(bank, DEBIT_ID) == 130_000
    assert _balance(bank) == DEBIT_BALANCE - 130_000


def test_single_transfer_over_1000_is_red_even_with_nothing_spent_today(
    bank: sqlite3.Connection,
) -> None:
    """今天还没转，单笔 1200：R08 红，不是「今天额度还够」的黄。"""
    action, params = _action(120_000)
    decision = _decide(bank, action)
    assert (decision.level, decision.rule_id) == ("red", "R08_transfer_red_single")
    assert decision.required_factors == [FACE, SMS]

    hold = funds.initiate(bank, action, session_id="s_big", node_id="n1",
                          token=_token("transfer", params), secret=SECRET)
    with pytest.raises(PolicyDenied) as excinfo:
        funds.commit(bank, hold.hold_id, daily_risk_threshold_fen=DAILY_LIMIT_FEN)
    assert excinfo.value.code == "DAILY_RISK_UPGRADE_REQUIRED"
    assert _balance(bank) == DEBIT_BALANCE


# ---------------------------------------------------------------- 强验证本身

def test_red_without_verification_cannot_produce_a_usable_proof(bank: sqlite3.Connection) -> None:
    """缺验证 / 错验证 / 过期验证：三种都过不了服务端校验，因此签不出红牌。"""
    missing = factors.verify(bank, session_id="s_f", confirmation_id="cf_1",
                             required=[FACE, SMS], proofs=[])
    assert missing["ok"] is False and missing["missing"] == [FACE, SMS]

    challenge = factors.issue(bank, session_id="s_f", confirmation_id="cf_2", factor=SMS)
    wrong_code = "000000" if challenge["code"] != "000000" else "111111"
    wrong = factors.verify(bank, session_id="s_f", confirmation_id="cf_2", required=[SMS],
                           proofs=[{"factor": SMS, "challenge_id": challenge["challenge_id"],
                                    "code": wrong_code}])
    assert wrong["ok"] is False and wrong["invalid"][0]["reason"] == "CODE_MISMATCH"

    expired_challenge = factors.issue(bank, session_id="s_f", confirmation_id="cf_3", factor=SMS)
    expired = factors.verify(
        bank, session_id="s_f", confirmation_id="cf_3", required=[SMS],
        proofs=[{"factor": SMS, "challenge_id": expired_challenge["challenge_id"],
                 "code": expired_challenge["code"]}],
        now=int(expired_challenge["expires_ms"]) + 1,
    )
    assert expired["ok"] is False and expired["invalid"][0]["reason"] == "CHALLENGE_EXPIRED"


def test_red_token_binds_the_exact_payee_amount_and_account(bank: sqlite3.Connection) -> None:
    """强验证只绑定本次收款人/金额/账户：换人、改金额、换账户都不能复用。"""
    action, params = _action(80_000)
    red = token_mod.issue(action, level="red", secret=SECRET)

    other_payee = build_action("transfer", {"payee_id": "p_zhang_a", "amount_fen": 80_000},
                               DEBIT_ID, 80_000)
    with pytest.raises(BankError) as payee_err:
        funds.initiate(bank, other_payee, session_id="s_bind", node_id="n1",
                       token=red, secret=SECRET)
    assert payee_err.value.code in {"TOKEN_MISMATCH", "TOKEN_REJECTED"}

    bigger = build_action("transfer", {"payee_id": PAYEE_LIJING, "amount_fen": 90_000},
                          DEBIT_ID, 90_000)
    with pytest.raises(BankError) as amount_err:
        funds.initiate(bank, bigger, session_id="s_bind2", node_id="n1",
                       token=red, secret=SECRET)
    assert amount_err.value.code in {"TOKEN_MISMATCH", "TOKEN_REJECTED"}


# ---------------------------------------------------------------- 并发合计越线

def test_two_yellow_transfers_concurrently_cross_the_line(bank: sqlite3.Connection) -> None:
    """两笔 600 各自都在阈值内，合计 1200 越线：只能落一笔，另一笔升级强验证。

    这是「创建确认卡时的判级不能当作提交时的通行证」的并发证据：策略层先读后判
    天生有竞态，真正的裁决必须在扣款同一个事务里（funds._assert_daily_tier）。
    """
    path = str(bank.execute("PRAGMA database_list").fetchone()[2])
    barrier = threading.Barrier(2)
    results: list[object] = [None, None]
    params = {"payee_id": PAYEE_LIJING, "amount_fen": 60_000}

    def worker(index: int, session_id: str) -> None:
        conn = db.connect(path)
        try:
            funds.spent_today_fen(conn, DEBIT_ID)  # 两个线程都读到「今天还没花」
            barrier.wait(timeout=10)
            action, _ = _action(60_000)
            hold = funds.initiate(conn, action, session_id=session_id, node_id="n1",
                                  token=_token("transfer", params), secret=SECRET)
            try:
                funds.commit(conn, hold.hold_id, daily_risk_threshold_fen=DAILY_LIMIT_FEN)
                results[index] = "committed"
            except PolicyDenied as exc:
                results[index] = exc.code
        finally:
            conn.close()

    threads = [threading.Thread(target=worker, args=(0, "s_c1")),
               threading.Thread(target=worker, args=(1, "s_c2"))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert not any(thread.is_alive() for thread in threads)

    assert sorted(str(item) for item in results) == ["DAILY_RISK_UPGRADE_REQUIRED", "committed"]
    assert _balance(bank) == DEBIT_BALANCE - 60_000, "只能扣一笔"
    assert funds.spent_today_fen(bank, DEBIT_ID) == 60_000


# ---------------------------------------------------------------- 回滚不污染累计

def test_rolled_back_operations_do_not_bump_the_daily_total(bank: sqlite3.Connection) -> None:
    """余额不足 / 触发绝对上限 / 升级强验证：失败方向都不能增加当日累计。"""
    _spend(bank, 50_000)
    assert funds.spent_today_fen(bank, DEBIT_ID) == 50_000

    # 绝对上限（显式配置）：红牌也拒，累计不动
    action, params = _action(80_000)
    red = token_mod.issue(action, level="red", secret=SECRET)
    hold = funds.initiate(bank, action, session_id="s_cap", node_id="n1", token=red, secret=SECRET)
    with pytest.raises(PolicyDenied) as cap_err:
        funds.commit(bank, hold.hold_id, absolute_daily_cap_fen=60_000)
    assert cap_err.value.code == "DAILY_ABSOLUTE_CAP_EXCEEDED"
    assert funds.spent_today_fen(bank, DEBIT_ID) == 50_000

    # 余额不足：先把账户余额压低，initiate 阶段就该失败，累计不动
    bank.execute("UPDATE accounts SET balance_fen = ? WHERE id = ?", (10_000, DEBIT_ID))
    small, small_params = _action(30_000)
    with pytest.raises(BankError) as poor_err:
        funds.initiate(bank, small, session_id="s_poor", node_id="n1",
                       token=_token("transfer", small_params), secret=SECRET)
    assert poor_err.value.code == "INSUFFICIENT_FUNDS"
    assert funds.spent_today_fen(bank, DEBIT_ID) == 50_000


# ---------------------------------------------------------------- 预约跨级

def _insert_scheduled(conn: sqlite3.Connection, sched_id: str, amount_fen: int,
                      due_ms: int) -> None:
    conn.execute(
        "INSERT INTO scheduled_transfers (id, user_id, account_id, payee_id, amount_fen, note,"
        " due_ms, recurrence, status, created_ms, executed_ms, factor_verified_ms, defer_count,"
        " idempotency_key, claim_token, claimed_ms)"
        " VALUES (?,?,?,?,?,'',?,'once','scheduled',?,0,0,0,?,'',0)",
        (sched_id, USER_ID, DEBIT_ID, PAYEE_LIJING, amount_fen, due_ms,
         FROZEN_MS, f"idem-{sched_id}"),
    )


def test_scheduled_created_yellow_but_executes_red(bank: sqlite3.Connection) -> None:
    """创建时今天还没花钱（黄），到点那天累计已 600 → 复判为红，挂起等强验证。"""
    # 创建期：500 元的预约在当时确实是黄档（只判单笔，日线留到出账日）
    creation = decide(build_action("scheduled_transfer",
                                   {"payee_id": PAYEE_LIJING, "amount_fen": 50_000},
                                   DEBIT_ID, 50_000),
                      PolicyContext(spent_today_fen=0, now_ms=FROZEN_MS))
    assert creation.level == "yellow"

    _insert_scheduled(bank, "sch_tier", 50_000, FROZEN_MS - 1_000)
    with db.tx(bank):
        bank.execute(
            "INSERT INTO daily_user_limits (user_id, day, spent_fen, tx_count) VALUES (?,?,?,1)",
            (USER_ID, TODAY, 60_000),
        )
    results = scheduled.execute_due(bank, now=FROZEN_MS, secret=SECRET)
    assert results[0]["status"] == "needs_verification", "到点复判为红必须挂起，不能绕过日累计"
    row = bank.execute("SELECT * FROM scheduled_transfers WHERE id='sch_tier'").fetchone()
    assert int(row["needs_verification"]) == 1
    assert _balance(bank) == DEBIT_BALANCE, "挂起期间钱不能动"
    events = [r["event_type"] for r in bank.execute(
        "SELECT event_type FROM audit_log WHERE session_id='sched:sch_tier' ORDER BY seq")]
    assert "scheduled.needs_verification" in events

    # 补一次强验证：两个因子都过之后才真正出账
    proofs = []
    for factor in (FACE, SMS):
        challenge = scheduled.challenge_scheduled(bank, "sch_tier", factor, secret=SECRET,
                                                  now=FROZEN_MS)
        proofs.append({"factor": factor, "challenge_id": challenge["challenge_id"],
                       "code": challenge["code"]})
    verified = scheduled.verify_and_execute(bank, "sch_tier", proofs=proofs,
                                            secret=SECRET, now=FROZEN_MS)
    assert verified["status"] == "executed"
    assert _balance(bank) == DEBIT_BALANCE - 50_000
    assert funds.spent_today_fen(bank, DEBIT_ID) == 110_000
    after = bank.execute("SELECT * FROM scheduled_transfers WHERE id='sch_tier'").fetchone()
    assert int(after["factor_verified_ms"]) == 0, "一次性授权用完即焚"
    assert int(after["needs_verification"]) == 0
