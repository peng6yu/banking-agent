"""断流与重启恢复（app/planner/recover.py）的回归用例。

为什么单独钉这一层：确认卡、节点状态、审计都落了库，但执行状态原先只活在
app/planner/state.py 的 RUNS 字典里。进程一退，用户对着一张还在有效期内的确认卡
怎么点都点不动。这里模拟「内存没了、库还在」的三种真实处境：

1. 卡还挂着没答 → 恢复后应该把卡还给用户，而不是自动执行；
2. 已经跑完 → 恢复绝不能重跑，否则一笔转账扣两次；
3. 点过一次 → 第二次点必须幂等。

红线：恢复不是重新开始，它不重新解析意图、不重新问策略。
"""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from app import audit, clock, db
from app.models import Dag
from app.planner import dag as planner_dag
from app.planner import executor, recover
from app.security import token as token_mod
from scripts.seed import seed

SECRET = "0123456789abcdef" * 4  # 64 位十六进制：能力令牌密钥必须是 hex
AMOUNT_FEN = 50_000  # 500 元 -> R06 黄灯，不需要多因子
START_BALANCE_FEN = 1_286_050


@pytest.fixture()
def bank(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("CAPABILITY_SECRET", SECRET)
    monkeypatch.setattr(db, "DEFAULT_DB_PATH", tmp_path / "bank.db")
    conn = db.connect()
    db.init_db(conn)
    seed(conn)
    conn.close()
    executor.RUNS.clear()
    try:
        yield db.DEFAULT_DB_PATH
    finally:
        clock.unfreeze()
        executor.RUNS.clear()


def _dag(amount_fen: int = AMOUNT_FEN) -> Dag:
    return planner_dag.build(
        {"intent": "transfer", "slots": {"payee_name": "李静", "amount_fen": amount_fen}}
    )


def _conn() -> sqlite3.Connection:
    conn = db.connect()
    db.init_db(conn)
    return conn


def _balance(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT balance_fen FROM accounts WHERE id = 'a_3d7e60c2'"
    ).fetchone()
    return int(row["balance_fen"])


def _audit_kinds(conn: sqlite3.Connection, session_id: str) -> list[str]:
    rows = conn.execute(
        "SELECT event_type FROM audit_log WHERE session_id = ? ORDER BY seq",
        (session_id,),
    ).fetchall()
    return [str(row["event_type"]) for row in rows]


def _run_status(conn: sqlite3.Connection, session_id: str) -> str:
    row = conn.execute(
        "SELECT status FROM dag_runs WHERE session_id = ? ORDER BY created_ms DESC LIMIT 1",
        (session_id,),
    ).fetchone()
    return "" if row is None else str(row["status"])


def _halt_before_confirm(session_id: str, dag: Dag, *, timeout_ms: int = 60_000) -> str:
    """跑到确认卡弹出来就「断流」：取消协程，模拟服务被杀/页面关掉。

    这一步刻意不回答确认——断线时用户根本还没做决定。
    """
    holder: dict[str, Any] = {"cid": None, "ready": None}

    async def emit(name: str, data: dict[str, Any]) -> None:
        if name in ("confirm", "factor") and holder["cid"] is None:
            holder["cid"] = str(data["confirmation_id"])
            if holder["ready"] is not None:
                holder["ready"].set()

    async def drive() -> None:
        holder["ready"] = asyncio.Event()
        task = asyncio.create_task(
            executor.run(session_id, dag, emit, confirm_timeout_ms=timeout_ms)
        )
        try:
            await asyncio.wait_for(holder["ready"].wait(), timeout=5)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(drive())
    assert holder["cid"], "确认卡没弹出来，断流模拟不成立"
    return str(holder["cid"])


def _complete_run(session_id: str, dag: Dag) -> None:
    """完整跑一轮，确认卡由代码自己点掉。"""

    async def emit(name: str, data: dict[str, Any]) -> None:
        if name in ("confirm", "factor"):
            await executor.resume(session_id, str(data["confirmation_id"]), True)

    asyncio.run(executor.run(session_id, dag, emit, confirm_timeout_ms=10_000))


# ------------------------------------------------------------------ 断流后恢复


def test_resume_after_disconnect_hands_the_card_back_instead_of_executing(
    bank: Path,
) -> None:
    """断流只代表用户还没决定：恢复必须停下、把同一张卡还回来。"""
    session = "s_recover_pending"
    cid = _halt_before_confirm(session, _dag())
    executor.RUNS.clear()  # 进程没了

    result = asyncio.run(recover.resume(session))

    assert result["ok"] is True
    assert result["recovered"] is True
    assert result["status"] == "waiting_confirm"
    assert result["confirmation_id"] == cid
    conn = _conn()
    try:
        assert _balance(conn) == START_BALANCE_FEN
        assert _run_status(conn, session) == "waiting"
        assert "transfer.initiated" not in _audit_kinds(conn, session)
    finally:
        conn.close()


def test_confirm_after_restart_really_executes_the_same_plan(bank: Path) -> None:
    """恢复后点确认，钱必须按原来那张卡上的参数真的出账。"""
    session = "s_recover_confirm"
    cid = _halt_before_confirm(session, _dag())
    executor.RUNS.clear()

    result = asyncio.run(recover.confirm(session, cid, True))

    assert result["ok"] is True
    assert result["status"] == "approved"
    assert result["run_status"] == "done"
    conn = _conn()
    try:
        assert _balance(conn) == START_BALANCE_FEN - AMOUNT_FEN
        kinds = _audit_kinds(conn, session)
        assert "confirm.approved" in kinds
        assert "transfer.initiated" in kinds
        hold = conn.execute(
            "SELECT id FROM holds WHERE session_id = ? ORDER BY created_ms DESC LIMIT 1",
            (session,),
        ).fetchone()
        row = conn.execute(
            "SELECT direction, amount_fen, counterparty FROM transactions WHERE hold_id = ?",
            (hold["id"],),
        ).fetchone()
        assert str(row["direction"]) == "out"
        assert int(row["amount_fen"]) == AMOUNT_FEN
        assert "李静" in str(row["counterparty"])
    finally:
        conn.close()


def test_second_click_after_restart_is_idempotent(bank: Path) -> None:
    """同一张卡点两次不能扣两次钱。"""
    session = "s_recover_twice"
    cid = _halt_before_confirm(session, _dag())
    executor.RUNS.clear()

    first = asyncio.run(recover.confirm(session, cid, True))
    second = asyncio.run(recover.confirm(session, cid, True))

    assert first["status"] == "approved"
    assert second["status"] == "approved"
    assert second.get("idempotent") is True
    conn = _conn()
    try:
        assert _balance(conn) == START_BALANCE_FEN - AMOUNT_FEN
        assert _audit_kinds(conn, session).count("confirm.approved") == 1
    finally:
        conn.close()


def test_expired_card_is_refused_after_restart(bank: Path) -> None:
    """重启拖过了有效期：卡作废，钱不动，审计记 confirm.expired（不是 rejected）。"""
    session = "s_recover_expired"
    cid = _halt_before_confirm(session, _dag(), timeout_ms=60_000)
    executor.RUNS.clear()
    # 卡片的有效期跟着这次的等待窗口走（60 秒），所以往后拨 61 秒就该过期。
    clock.freeze(clock.now_ms() + 61_000)

    result = asyncio.run(recover.confirm(session, cid, True))

    assert result["status"] == "expired"
    conn = _conn()
    try:
        assert _balance(conn) == START_BALANCE_FEN
        kinds = _audit_kinds(conn, session)
        assert "confirm.expired" in kinds
        assert "confirm.rejected" not in kinds
    finally:
        conn.close()


def test_rejection_after_restart_is_recorded_as_rejection(bank: Path) -> None:
    session = "s_recover_reject"
    cid = _halt_before_confirm(session, _dag())
    executor.RUNS.clear()

    result = asyncio.run(recover.confirm(session, cid, False))

    assert result["status"] == "rejected"
    conn = _conn()
    try:
        assert _balance(conn) == START_BALANCE_FEN
        assert "confirm.rejected" in _audit_kinds(conn, session)
    finally:
        conn.close()


# ------------------------------------------------------- 绝不重跑已经跑完的一轮


def test_resume_never_reruns_finished_nodes(bank: Path) -> None:
    """完整跑完后清空内存再恢复：余额不许再掉，状态仍是一轮已完成的 run。"""
    session = "s_recover_done"
    _complete_run(session, _dag())
    executor.RUNS.clear()

    result = asyncio.run(recover.resume(session))

    assert result["ok"] is True
    assert result["recovered"] is True
    assert result["run_status"] == "done"
    conn = _conn()
    try:
        assert _balance(conn) == START_BALANCE_FEN - AMOUNT_FEN
        # 只该有一次真实的出账尝试：重复恢复会看到两条 transfer.initiated
        assert _audit_kinds(conn, session).count("transfer.initiated") == 1
    finally:
        conn.close()


def test_rehydrate_rebuilds_the_persisted_graph(bank: Path) -> None:
    """恢复不是重新解析：DAG、节点裁决、确认卡都来自库里。"""
    session = "s_recover_rehydrate"
    cid = _halt_before_confirm(session, _dag())
    executor.RUNS.clear()

    conn = _conn()
    try:
        state = recover.rehydrate(conn, session)
    finally:
        conn.close()
    assert state.session_id == session
    assert cid in state.confirmations
    assert state.confirmations[cid].status == "pending"
    assert [node.tool for node in state.nodes.values()] == ["resolve_payee", "query_balance", "transfer"]
    assert len(state.dag.nodes) == 3  # DAG 从 dag_json 反序列化回来了
    assert state.account_id == "a_3d7e60c2"  # 账户从 meta_json 还原，令牌才能绑定


# ------------------------------------------- 同一会话有多轮时，先恢复没跑完的那轮


def test_recovery_prefers_an_unfinished_run_over_a_newer_finished_one(bank: Path) -> None:
    """会话里先挂一张卡、随后又问了一句已经答完的问题：恢复必须回到那张卡。

    只按 created_ms 取最近一轮会取到后面那轮 done，用户那张卡就再也找不回来
    （前端表现为「刷新后确认卡消失」）。所以选轮次的条件是「未完成优先」。
    """
    session = "s_recover_multi"
    cid = _halt_before_confirm(session, _dag())
    executor.RUNS.clear()

    conn = _conn()
    try:
        row = conn.execute(
            "SELECT * FROM dag_runs WHERE session_id = ? ORDER BY created_ms DESC LIMIT 1",
            (session,),
        ).fetchone()
        assert row is not None
        # 伪造「更晚的一次已完成询问」：单纯按时间排序会挑中它
        with db.tx(conn):
            conn.execute(
                "INSERT INTO dag_runs (id, session_id, dag_json, status, created_ms, updated_ms, meta_json)"
                " VALUES (?, ?, ?, 'done', ?, ?, ?)",
                ("run_newer_done", session, str(row["dag_json"]), int(row["created_ms"]) + 5_000,
                 int(row["updated_ms"]), str(row["meta_json"] or "{}")),
            )
    finally:
        conn.close()

    result = asyncio.run(recover.resume(session))

    assert result["status"] == "waiting_confirm", "未完成的那轮优先，不该被判成已完成"
    assert result["confirmation_id"] == cid



def test_unknown_session_is_a_validation_error(bank: Path) -> None:
    with pytest.raises(Exception) as exc:
        asyncio.run(recover.resume("s_never_existed"))
    assert "会话已结束或不存在" in str(exc.value) or "不存在" in exc.value.user_text
