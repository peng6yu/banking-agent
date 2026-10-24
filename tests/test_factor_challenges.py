"""多因子挑战：服务端签发、服务端校验、一次性。

这里钉的是「客户端不能自己宣布验证通过」这条边界：
- 老实现里 POST /api/confirm 传 factor="face,sms" 就算过，等于前端自证；
- 新实现必须拿着一张服务端签发的挑战 + 验证码回来，且只能用一次。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from app import db
from app.planner import dag as planner_dag
from app.planner import executor
from app.security import factors
from scripts.seed import seed
from tests import mfa

SECRET = "b" * 64


@pytest.fixture()
def bank(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("CAPABILITY_SECRET", SECRET)
    monkeypatch.delenv("FACTOR_DEMO_ECHO", raising=False)
    monkeypatch.setattr(db, "DEFAULT_DB_PATH", tmp_path / "bank.db")
    conn = db.connect()
    db.init_db(conn)
    seed(conn)
    conn.close()
    executor.RUNS.clear()
    return db.DEFAULT_DB_PATH


def _challenge(conn: Any, session_id: str = "s_f", confirmation_id: str = "cf_x") -> dict[str, Any]:
    return factors.issue(
        conn, session_id=session_id, confirmation_id=confirmation_id, factor="sms"
    )


# ------------------------------------------------------------------ 单张挑战


def test_code_is_never_stored_in_plain_text(conn: Any) -> None:
    challenge = _challenge(conn)
    assert challenge["code"] and challenge["delivery"] == "demo_echo"
    row = conn.execute(
        "SELECT code_hash FROM factor_challenges WHERE id = ?", (challenge["challenge_id"],)
    ).fetchone()
    assert row["code_hash"] != challenge["code"]
    assert challenge["code"] not in str(row["code_hash"])


def test_a_challenge_can_only_be_used_once(conn: Any) -> None:
    challenge = _challenge(conn)
    proof = {"factor": "sms", "challenge_id": challenge["challenge_id"], "code": challenge["code"]}
    first = factors.verify(conn, session_id="s_f", confirmation_id="cf_x", required=["sms"], proofs=[proof])
    assert first["ok"] is True
    second = factors.verify(conn, session_id="s_f", confirmation_id="cf_x", required=["sms"], proofs=[proof])
    assert second["ok"] is False
    assert second["invalid"][0]["reason"] == "CHALLENGE_USED"


def test_challenge_is_bound_to_session_and_confirmation(conn: Any) -> None:
    challenge = _challenge(conn)
    proof = {"factor": "sms", "challenge_id": challenge["challenge_id"], "code": challenge["code"]}
    other_card = factors.verify(
        conn, session_id="s_f", confirmation_id="cf_other", required=["sms"], proofs=[proof]
    )
    assert other_card["ok"] is False
    assert other_card["invalid"][0]["reason"] == "CHALLENGE_NOT_BOUND"
    other_session = factors.verify(
        conn, session_id="s_other", confirmation_id="cf_x", required=["sms"], proofs=[proof]
    )
    assert other_session["invalid"][0]["reason"] == "CHALLENGE_NOT_BOUND"


def test_wrong_code_burns_attempts_then_locks(conn: Any) -> None:
    challenge = _challenge(conn)
    cid, real = challenge["challenge_id"], challenge["code"]
    wrong = "000000" if real != "000000" else "111111"
    for _ in range(factors.MAX_ATTEMPTS):
        result = factors.verify(
            conn,
            session_id="s_f",
            confirmation_id="cf_x",
            required=["sms"],
            proofs=[{"factor": "sms", "challenge_id": cid, "code": wrong}],
        )
        assert result["ok"] is False
    assert (
        conn.execute("SELECT attempts FROM factor_challenges WHERE id = ?", (cid,)).fetchone()["attempts"]
        == factors.MAX_ATTEMPTS
    )
    locked = factors.verify(
        conn,
        session_id="s_f",
        confirmation_id="cf_x",
        required=["sms"],
        proofs=[{"factor": "sms", "challenge_id": cid, "code": real}],
    )
    assert locked["invalid"][0]["reason"] == "TOO_MANY_ATTEMPTS"


def test_expired_challenge_is_refused(conn: Any) -> None:
    challenge = _challenge(conn)
    later = int(challenge["expires_ms"]) + 1
    result = factors.verify(
        conn,
        session_id="s_f",
        confirmation_id="cf_x",
        required=["sms"],
        proofs=[{"factor": "sms", "challenge_id": challenge["challenge_id"], "code": challenge["code"]}],
        now=later,
    )
    assert result["ok"] is False
    assert result["invalid"][0]["reason"] == "CHALLENGE_EXPIRED"


def test_missing_factor_is_reported_not_silently_passed(conn: Any) -> None:
    challenge = _challenge(conn)
    result = factors.verify(
        conn,
        session_id="s_f",
        confirmation_id="cf_x",
        required=["face", "sms"],
        proofs=[{"factor": "sms", "challenge_id": challenge["challenge_id"], "code": challenge["code"]}],
    )
    assert result["ok"] is False
    assert result["missing"] == ["face"]


def test_echo_can_be_turned_off(conn: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(factors.ECHO_ENV, "0")
    challenge = _challenge(conn)
    assert challenge["delivery"] == "out_of_band"
    assert challenge["code"] is None


# ------------------------------------------------------------ 红灯转账端到端


def _transfer_dag(amount_fen: int) -> Any:
    return planner_dag.build(
        {"intent": "transfer", "slots": {"payee_name": "李静", "amount_fen": amount_fen}}
    )


def _proofs_for(session_id: str, confirmation_id: str, *, code: str | None = None) -> list[dict[str, str]]:
    conn = db.connect()
    try:
        items = mfa.proofs(conn, session_id, confirmation_id)
    finally:
        conn.close()
    if code is not None:
        items = [{**item, "code": code} for item in items]
    return items


def _run_red(session_id: str, *, mode: str) -> tuple[list[tuple[str, dict[str, Any]]], str | None]:
    events: list[tuple[str, dict[str, Any]]] = []
    seen: dict[str, str] = {}

    async def emit(name: str, data: dict[str, Any]) -> None:
        events.append((name, dict(data)))
        if name == "factor":
            cid = str(data["confirmation_id"])
            seen["cid"] = cid
            if mode == "good":
                proofs = _proofs_for(session_id, cid)
            elif mode == "wrong":
                proofs = _proofs_for(session_id, cid, code="000000")
            elif mode == "none":
                proofs = []
            else:  # pragma: no cover - 测试写错 mode 时直接炸
                raise AssertionError(mode)
            await executor.resume(session_id, cid, True, proofs=proofs)

    asyncio.run(
        executor.run(session_id, _transfer_dag(200_000), emit, confirm_timeout_ms=400)
    )
    return events, seen.get("cid")


def _balance() -> int:
    conn = db.connect()
    try:
        return int(conn.execute("SELECT balance_fen FROM accounts WHERE type='debit' LIMIT 1").fetchone()[0])
    finally:
        conn.close()


def _audit_kinds() -> list[str]:
    conn = db.connect()
    try:
        return [row["event_type"] for row in conn.execute("SELECT event_type FROM audit_log ORDER BY seq")]
    finally:
        conn.close()


def test_red_transfer_executes_only_with_server_verified_proofs(bank: Path) -> None:
    before = _balance()
    events, cid = _run_red("s_red_ok", mode="good")
    assert cid
    names = [name for name, _ in events]
    assert "factor" in names and "done" in names
    assert _balance() == before - 200_000
    kinds = _audit_kinds()
    assert "factor.challenge_issued" in kinds
    assert "factor.verified" in kinds
    assert "confirm.approved" in kinds


def test_red_transfer_without_proofs_executes_nothing_and_expires(bank: Path) -> None:
    before = _balance()
    events, cid = _run_red("s_red_none", mode="none")
    assert cid
    names = [name for name, _ in events]
    assert "factor" in names
    assert _balance() == before
    kinds = _audit_kinds()
    assert "confirm.approved" not in kinds
    assert "factor.rejected" in kinds
    # 没人验证 -> 等超时 -> 记 confirm.expired，而不是混成「用户拒绝」
    assert "confirm.expired" in kinds
    assert "confirm.rejected" not in kinds


def test_red_transfer_with_wrong_code_is_refused_and_records_rejection(bank: Path) -> None:
    before = _balance()
    _run_red("s_red_wrong", mode="wrong")
    assert _balance() == before
    conn = db.connect()
    try:
        attempts = conn.execute("SELECT MAX(attempts) AS n FROM factor_challenges").fetchone()["n"]
    finally:
        conn.close()
    assert attempts >= 1
    kinds = _audit_kinds()
    assert "factor.rejected" in kinds
    assert "confirm.approved" not in kinds


# ------------------------------------------------------------------ HTTP 契约


async def _open_pending(session_id: str) -> tuple[str, asyncio.Task[None]]:
    """跑一张红灯图，停在「已弹出确认卡、等人验证」这一刻，返回卡片 ID。

    关键点：卡片必须还是 pending 状态，接口才有资格发挑战；如果等到超时，
    卡片已经 expired，接口应当拒绝（那条路径由下面的 400 用例覆盖）。
    """
    seen: dict[str, str] = {}
    ready = asyncio.Event()

    async def emit(name: str, data: dict[str, Any]) -> None:
        if name == "factor":
            seen["cid"] = str(data["confirmation_id"])
            ready.set()

    task = asyncio.create_task(
        executor.run(session_id, _transfer_dag(200_000), emit, confirm_timeout_ms=15_000)
    )
    await asyncio.wait_for(ready.wait(), timeout=10)
    return seen["cid"], task


async def _post_challenge(session_id: str, citation: str, factor: str | None = None) -> Any:
    import httpx

    from app.main import app

    body: dict[str, Any] = {"session_id": session_id, "confirmation_id": citation}
    if factor is not None:
        body["factor"] = factor
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http:
        return await http.post("/api/factor/challenge", json=body)


async def _with_pending(session_id: str, factor: str | None = None) -> Any:
    cid, task = await _open_pending(session_id)
    try:
        return await _post_challenge(session_id, cid, factor)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def test_challenge_endpoint_issues_one_per_required_factor(bank: Path) -> None:
    """HTTP 层：给一张还在等的红灯卡申请挑战，接口按 required 发两张，且绑定这张卡。"""
    res = asyncio.run(_with_pending("s_red_http"))
    assert res.status_code == 200, res.text
    data = res.json()
    assert sorted(data["required"]) == ["face", "sms"]
    assert len(data["challenges"]) == 2
    assert {item["factor"] for item in data["challenges"]} == {"face", "sms"}
    assert all(item["challenge_id"].startswith("fc_") for item in data["challenges"])


def test_challenge_endpoint_rejects_unknown_factor(bank: Path) -> None:
    res = asyncio.run(_with_pending("s_red_http_bad", factor="passport"))
    assert res.status_code == 400
