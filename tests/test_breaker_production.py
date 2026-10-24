"""熔断在生产链路上真的会开闸（t26）。

t26 之前 breaker.record_denial 在 app/ 下零调用点：denials 表只有单测在写，
生产链路 is_open 恒 False，「异常熔断」只是一段空转代码。这份测试全部走
executor.run 真实链路，并且对 record_denial 本身打桩计数——断言的是生产入口
真的被调用，而不是测试自己往表里塞行。

对应文档：docs/05_安全层实现规格.md 5.7。
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

import pytest

from tests import mfa
from app import audit, db
from app.clock import BreakerOpen
from app.ids import new_id
from app.models import Action, Dag, DAGNode
from app.planner import executor
from app.security import breaker, token as token_mod
from scripts.seed import PAYEE_LIJING, seed

SECRET = "b" * 64
SESSION = "s_breaker_prod"
BLOCK_AMOUNT_FEN = -1
YELLOW_AMOUNT_FEN = 50_000


@pytest.fixture()
def bank(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("CAPABILITY_SECRET", SECRET)
    monkeypatch.setattr(db, "DEFAULT_DB_PATH", tmp_path / "bank.db")
    conn = db.connect()
    db.init_db(conn)
    seed(conn)
    conn.close()
    executor.RUNS.clear()
    return db.DEFAULT_DB_PATH


@pytest.fixture()
def denial_calls(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[str]]:
    """给生产入口 breaker.record_denial 打桩：记录调用次数与原因。

    打桩体自己转发到真实现，所以 denials 表里的行数仍然只能由生产代码产生——
    这就是「表里那几行不是我塞的」的证据。
    """
    seen: dict[str, list[str]] = {"reasons": []}
    real = breaker.record_denial

    def spy(conn: Any, session_id: str, **kwargs: Any) -> int:
        seen["reasons"].append(str(kwargs.get("reason", "")))
        return real(conn, session_id, **kwargs)

    monkeypatch.setattr(breaker, "record_denial", spy)
    return seen


def drive(
    session_id: str,
    dag_obj: Dag,
    *,
    answers: list[dict[str, Any]] | None = None,
    confirm_timeout_ms: int = 3_000,
) -> list[tuple[str, dict[str, Any]]]:
    """跑一轮真实执行器并替用户回答确认；只有 future 建好才作答，避免竞态。"""
    events: list[tuple[str, dict[str, Any]]] = []

    async def emit(name: str, data: dict[str, Any]) -> None:
        events.append((name, data))

    async def answerer() -> None:
        handled: set[str] = set()
        used = 0
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            state = executor.get_run(session_id)
            if state is not None:
                for conf in list(state.confirmations.values()):
                    if conf.status != "pending" or conf.future is None or conf.id in handled:
                        continue
                    handled.add(conf.id)
                    if answers is None:
                        continue
                    reply = answers[min(used, len(answers) - 1)]
                    used += 1
                    approve = bool(reply.get("approve", True))
                    card_proofs = (
                        mfa.solve(session_id, conf.id, list(conf.required_factors))
                        if approve and conf.required_factors else []
                    )
                    await executor.resume(
                        session_id, conf.id, approve, factor=reply.get("factor"), proofs=card_proofs
                    )
            await asyncio.sleep(0.005)

    async def main() -> None:
        task = asyncio.create_task(
            executor.run(session_id, dag_obj, emit, confirm_timeout_ms=confirm_timeout_ms)
        )
        helper = asyncio.create_task(answerer())
        try:
            await asyncio.wait_for(task, timeout=20)
        finally:
            helper.cancel()

    asyncio.run(main())
    return events


def transfer_dag(amount_fen: int, note: str = "生日礼物") -> Dag:
    return Dag(
        id=new_id("r"),
        nodes=[
            DAGNode(
                id="n1",
                tool="transfer",
                params={"payee_id": PAYEE_LIJING, "amount_fen": amount_fen, "note": note},
            )
        ],
    )


def read_dag() -> Dag:
    return Dag(id=new_id("r"), nodes=[DAGNode(id="n1", tool="query_balance", params={})])


def denial_rows(session_id: str = SESSION) -> list[tuple[str, str]]:
    conn = db.connect()
    try:
        return [
            (reason, action_json)
            for reason, action_json in conn.execute(
                "SELECT reason, action_json FROM denials WHERE session_id = ? ORDER BY ts_ms, rowid",
                (session_id,),
            )
        ]
    finally:
        conn.close()


def names(events: list[tuple[str, dict[str, Any]]]) -> list[str]:
    return [name for name, _ in events]


def event_codes(events: list[tuple[str, dict[str, Any]]]) -> list[str]:
    return [data.get("code", "") for name, data in events if name == "error"]


def audit_types(session_id: str = SESSION) -> list[str]:
    conn = db.connect()
    try:
        rows = [
            str(event_type)
            for (event_type,) in conn.execute(
                "SELECT event_type FROM audit_log WHERE session_id = ? ORDER BY seq", (session_id,)
            )
        ]
        assert audit.verify_chain(conn) is True, "审计哈希链被破坏"
        return rows
    finally:
        conn.close()


# --- 一、生产入口真的被调用 -----------------------------------------------------


def test_red_block_records_exactly_one_denial(bank: Path, denial_calls: dict[str, list[str]]) -> None:
    """红色且无「就这么办」出口 → 记一次 POLICY_BLOCKED。"""
    events = drive(SESSION, transfer_dag(BLOCK_AMOUNT_FEN))

    assert event_codes(events) == ["POLICY_BLOCKED"]
    assert denial_calls["reasons"] == ["POLICY_BLOCKED"], "生产入口没被调用：熔断永远不会开"
    assert len(denial_rows()) == 1
    assert denial_rows()[0][0] == "POLICY_BLOCKED"
    assert "breaker.open" not in audit_types(), "一次拒绝不该开闸"


def test_three_blocks_open_the_breaker_and_the_next_run_is_refused(
    bank: Path, denial_calls: dict[str, list[str]]
) -> None:
    """5 分钟内第 3 次拒绝开闸 60 秒，之后的请求在 open_run 就被 BreakerOpen 拦下。"""
    for _ in range(3):
        drive(SESSION, transfer_dag(BLOCK_AMOUNT_FEN))

    conn = db.connect()
    try:
        assert breaker.denial_count(conn, SESSION) == 3
        assert breaker.is_open(conn, SESSION) is True
        assert breaker.retry_after_ms(conn, SESSION) > 0
    finally:
        conn.close()
    assert denial_calls["reasons"] == ["POLICY_BLOCKED"] * 3
    assert "breaker.open" in audit_types()

    blocked = drive(SESSION, transfer_dag(BLOCK_AMOUNT_FEN))
    assert event_codes(blocked) == ["BREAKER_OPEN"], "开闸后仍放行，熔断形同虚设"
    # 第 4 次连执行器都没进去，不会写出第 4 行
    assert len(denial_rows()) == 3
    assert denial_calls["reasons"] == ["POLICY_BLOCKED"] * 3


def test_token_mismatch_counts_as_a_denial(
    bank: Path, denial_calls: dict[str, list[str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """令牌七步校验没过（第七步 amount_fen 不符）同样是一次拒绝。"""
    real_issue = executor.issue_token

    def tampered(state: Any, action: Action, decision: Any) -> str | None:
        issued = real_issue(state, action, decision)
        if issued is None:
            return None
        params = dict(action.params)
        params["amount_fen"] = int(params.get("amount_fen", 0)) + 1
        return token_mod.issue(
            Action(tool=action.tool, params=params, account_id=action.account_id),
            level=decision.level,
            secret=state.secret,
        )

    monkeypatch.setattr(executor, "issue_token", tampered)
    events = drive(SESSION, transfer_dag(YELLOW_AMOUNT_FEN), answers=[{"approve": True}])

    # 对外事件码是 TOKEN_REJECTED，落库的 reason 是底层异常码 TOKEN_MISMATCH；
    # 两个码都在 SECURITY_DENIAL_CODES 里，无论走哪个都算一次拒绝。
    assert event_codes(events) == ["TOKEN_REJECTED"]
    assert denial_calls["reasons"] == ["TOKEN_MISMATCH"]
    assert [reason for reason, _ in denial_rows()] == ["TOKEN_MISMATCH"]


# --- 二、对照组：不该记的都不记 ----------------------------------------------


def test_green_read_only_run_records_no_denial(bank: Path, denial_calls: dict[str, list[str]]) -> None:
    """绿灯只读：拒绝计数必须保持 0（对照组，反证「见谁记谁」不是无脑累加）。"""
    events = drive(SESSION, read_dag())

    assert event_codes(events) == []
    assert [data["ok"] for name, data in events if name == "tool"] == [True]
    assert denial_calls["reasons"] == []
    assert denial_rows() == []


def test_user_declining_is_not_a_denial(bank: Path, denial_calls: dict[str, list[str]]) -> None:
    """用户自己说不要：这是正常交互，不该把诚实用户计入熔断。"""
    events = drive(SESSION, transfer_dag(YELLOW_AMOUNT_FEN), answers=[{"approve": False}])

    assert event_codes(events) == [], "用户自己拒绝不该被当成安全拒绝"
    assert [data["ok"] for name, data in events if name == "tool"] == [False]
    assert names(events)[-1] == "done"
    assert denial_calls["reasons"] == []
    assert denial_rows() == []


def test_confirm_timeout_is_not_a_denial(bank: Path, denial_calls: dict[str, list[str]]) -> None:
    """没人点头而超时：同样不是安全拒绝。"""
    events = drive(SESSION, transfer_dag(YELLOW_AMOUNT_FEN), answers=None, confirm_timeout_ms=1)

    assert event_codes(events) == ["CONFIRM_TIMEOUT"]
    assert denial_calls["reasons"] == []
    assert denial_rows() == []


def test_is_security_denial_picks_only_the_security_boundary() -> None:
    """口径钉死：算哪几个、不算哪几个。"""
    for code in executor.SECURITY_DENIAL_CODES:
        assert executor.is_security_denial(code) is True
    for code in (
        "USER_REJECTED",
        "CONFIRM_TIMEOUT",
        "VALIDATION_INVALID_PARAMS",
        "VALIDATION_MISSING_PAYEE",
        "INSUFFICIENT_FUNDS",
        "HOLD_EXPIRED",
        "",
        None,
    ):
        assert executor.is_security_denial(code) is False, code
