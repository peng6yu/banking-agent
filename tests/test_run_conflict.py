"""同一会话里只允许一轮没走完的写操作（app/planner/conflict.py）。

为什么单独钉这一层：`RUNS` 是按 session_id 索引的字典，第二轮 `executor.run`
会直接覆盖第一轮。第一轮那张确认卡还留在库里、还能被 `/api/confirm` 回答，
但已经没人驱动它——用户看到的是「点确认没反应」，审计里两轮交错。

这里的红线：
1. 新请求含写操作、上一轮没走完 → 这一轮根本不开始，回 run_id 与「继续/取消」两条出口；
2. 不静默挑一轮继续：多轮未完成要把 run_id 全列出来；
3. 只读请求不拦（查余额不该被一张待确认卡堵住）；
4. 判定以库为准，所以进程重启后依然有效；
5. 已经跑完的轮次不算冲突，恢复也不会重跑已完成的资金节点（否则一笔转账扣两次）。
"""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from app import clock, db
from app.models import Dag
from app.planner import conflict, dag as planner_dag, executor, recover
from scripts.seed import seed

SECRET = "0123456789abcdef" * 4  # 64 位十六进制：能力令牌密钥必须是 hex
AMOUNT_FEN = 50_000  # 500 元 -> R06 黄灯，不需要多因子
START_BALANCE_FEN = 1_286_050
SESSION = "s_conflict"


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


def _read_dag() -> Dag:
    return planner_dag.build({"intent": "query_balance", "slots": {}})


def _conn() -> sqlite3.Connection:
    conn = db.connect()
    db.init_db(conn)
    return conn


def _balance(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT balance_fen FROM accounts WHERE id = 'a_3d7e60c2'").fetchone()
    return int(row["balance_fen"])


def _run_ids(conn: sqlite3.Connection, session_id: str) -> list[str]:
    rows = conn.execute(
        "SELECT id FROM dag_runs WHERE session_id = ? ORDER BY created_ms ASC",
        (session_id,),
    ).fetchall()
    return [str(row["id"]) for row in rows]


def _audit_kinds(conn: sqlite3.Connection, session_id: str) -> list[str]:
    rows = conn.execute(
        "SELECT event_type FROM audit_log WHERE session_id = ? ORDER BY seq",
        (session_id,),
    ).fetchall()
    return [str(row["event_type"]) for row in rows]


def _halt(session_id: str, dag: Dag, *, timeout_ms: int = 60_000) -> str:
    """跑到确认卡弹出来就断流，模拟「人还没决定，进程先没了」。"""
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


def _drive(session_id: str, dag: Dag, *, approve: bool = True) -> list[tuple[str, dict[str, Any]]]:
    """跑一轮并收全部事件；approve=True 时替用户点掉确认卡（测试里的「用户」）。"""
    frames: list[tuple[str, dict[str, Any]]] = []

    async def emit(name: str, data: dict[str, Any]) -> None:
        frames.append((name, data))
        if name in ("confirm", "factor") and approve:
            await executor.resume(session_id, str(data["confirmation_id"]), True)

    asyncio.run(executor.run(session_id, dag, emit, confirm_timeout_ms=10_000))
    return frames


def _error(frames: list[tuple[str, dict[str, Any]]]) -> dict[str, Any] | None:
    for name, data in frames:
        if name == "error":
            return data
    return None


# ------------------------------------------------------------- 第二条写操作被挡


def test_second_write_is_refused_and_told_which_run_is_in_the_way(bank: Path) -> None:
    cid = _halt(SESSION, _dag())
    conn = _conn()
    try:
        first_run = _run_ids(conn, SESSION)[0]
        before = _balance(conn)
    finally:
        conn.close()

    frames = _drive(SESSION, _dag())

    err = _error(frames)
    assert err is not None, "第二条写操作必须被明确挡下，而不是悄悄覆盖上一轮"
    assert err["code"] == conflict.CONFLICT_CODE
    assert err["run_id"] == first_run
    assert err["run_ids"] == [first_run]
    actions = {opt["action"]: opt for opt in err["options"]}
    assert actions["continue"]["confirmation_id"] == cid
    assert actions["continue"]["run_id"] == first_run
    assert actions["cancel"]["endpoint"] == "/api/cancel"
    assert "run_id" in err["user_text"] or first_run in err["user_text"]
    assert "不会替你选" in err["user_text"]

    conn = _conn()
    try:
        # 第二轮根本没开：库里还只有一轮，钱没动，第一张卡仍然 pending
        assert _run_ids(conn, SESSION) == [first_run]
        assert _balance(conn) == before
        row = conn.execute("SELECT status FROM confirmations WHERE id = ?", (cid,)).fetchone()
        assert str(row["status"]) == "pending"
        kinds = _audit_kinds(conn, SESSION)
        assert "run.conflict" in kinds
        assert "transfer.initiated" not in kinds
    finally:
        conn.close()


def test_read_only_requests_are_not_blocked(bank: Path) -> None:
    _halt(SESSION, _dag())

    frames = _drive(SESSION, _read_dag())

    assert _error(frames) is None, "查余额不该被一张待确认卡堵住"
    conn = _conn()
    try:
        assert len(_run_ids(conn, SESSION)) == 2
        assert _balance(conn) == START_BALANCE_FEN
    finally:
        conn.close()


def test_conflict_is_still_detected_after_a_restart(bank: Path) -> None:
    _halt(SESSION, _dag())
    executor.RUNS.clear()  # 进程没了，内存里的东西全丢

    frames = _drive(SESSION, _dag())

    err = _error(frames)
    assert err is not None
    assert err["code"] == conflict.CONFLICT_CODE


def test_every_unfinished_run_is_listed_not_just_the_newest(bank: Path) -> None:
    """历史里堆了两轮未完成：必须把 run_id 都列出来，不能只挑最近那轮。"""
    _halt(SESSION, _dag())
    conn = _conn()
    try:
        now = clock.now_ms()
        conn.execute(
            "INSERT INTO dag_runs (id, session_id, dag_json, status, created_ms, updated_ms)"
            " VALUES (?, ?, '{}', 'waiting', ?, ?)",
            ("r_manual_second", SESSION, now + 1, now + 1),
        )
        conn.execute(
            "INSERT INTO dag_nodes (id, run_id, node_id, tool, params_json, state, ts_ms)"
            " VALUES (?, ?, 'n3', 'transfer', '{}', 'waiting_confirm', ?)",
            ("dn_manual_second", "r_manual_second", now + 1),
        )
        first_run = _run_ids(conn, SESSION)[0]
    finally:
        conn.close()

    frames = _drive(SESSION, _dag())

    err = _error(frames)
    assert err is not None
    assert err["run_ids"] == [first_run, "r_manual_second"]
    assert len([opt for opt in err["options"] if opt["action"] == "cancel"]) == 2


def test_cancelling_the_previous_round_unblocks_the_next_write(bank: Path) -> None:
    _halt(SESSION, _dag())
    asyncio.run(executor.cancel(SESSION, "用户先取消上一轮"))

    frames = _drive(SESSION, _dag())

    assert _error(frames) is None
    conn = _conn()
    try:
        assert _balance(conn) == START_BALANCE_FEN - AMOUNT_FEN
        assert "transfer.initiated" in _audit_kinds(conn, SESSION)
    finally:
        conn.close()


def test_expired_card_does_not_silently_unblock_a_new_write(bank: Path) -> None:
    """卡过期了但这一轮还没落定（进程死在超时处理之前）：仍然要如实告知。"""
    _halt(SESSION, _dag(), timeout_ms=60_000)
    clock.freeze(clock.now_ms() + 61_000)

    frames = _drive(SESSION, _dag())

    err = _error(frames)
    assert err is not None
    assert err["conflict"]["runs"][0]["pending_confirmations"][0]["expired"] is True
    assert "没有待答复" in err["user_text"]
    conn = _conn()
    try:
        assert _balance(conn) == START_BALANCE_FEN
    finally:
        conn.close()


# ------------------------------------------------------- 已完成的轮次不算挡路


def test_finished_run_is_not_a_conflict_and_never_double_charges(bank: Path) -> None:
    _drive(SESSION, _dag())  # 第一轮完整跑完
    executor.RUNS.clear()

    frames = _drive(SESSION, _dag())  # 第二轮应该正常开始

    assert _error(frames) is None
    conn = _conn()
    try:
        assert _balance(conn) == START_BALANCE_FEN - AMOUNT_FEN * 2
        assert "run.conflict" not in _audit_kinds(conn, SESSION)
    finally:
        conn.close()


def test_recovering_a_finished_run_does_not_execute_the_money_node_again(bank: Path) -> None:
    _drive(SESSION, _dag())
    executor.RUNS.clear()

    result = asyncio.run(recover.resume(SESSION))

    assert result["run_status"] == "done"
    conn = _conn()
    try:
        assert _balance(conn) == START_BALANCE_FEN - AMOUNT_FEN
        assert _audit_kinds(conn, SESSION).count("transfer.initiated") == 1
    finally:
        conn.close()


# ------------------------------------------- 回答澄清后，提问那一轮要正式结束


def _ambiguous_dag() -> Dag:
    return planner_dag.build(
        {"intent": "transfer", "slots": {"payee_name": "张三", "amount_fen": AMOUNT_FEN}}
    )


def test_answering_a_payee_clarification_supersedes_the_run_that_asked(bank: Path) -> None:
    """点了候选/回了尾号之后，提问那一轮必须结束，而不是继续挡着下一句。

    现场（真浏览器里抓到的）：第一轮停在「有两位都叫张三」，用户点了候选，
    第二句话被冲突闸门拦成 SESSION_RUN_CONFLICT——用户看到的是
    「这个会话还有一轮没走完」，而那一轮其实只是在等一个答案。
    """
    frames: list[tuple[str, dict[str, Any]]] = []

    async def emit(name: str, data: dict[str, Any]) -> None:
        frames.append((name, data))

    asyncio.run(executor.run(SESSION, _ambiguous_dag(), emit, confirm_timeout_ms=5_000))
    done = [data for name, data in frames if name == "done"]
    assert done and (done[-1].get("clarification") or {}).get("kind") == "payee"

    conn = _conn()
    try:
        pending = conflict.unfinished(conn, SESSION)
        assert len(pending) == 1, "提问中的那一轮应该被认成未完成"
        asked_run = pending[0]["run_id"]

        # 用户回答了问题：这一轮按「被回答取代」关闭。
        closed = conflict.supersede_answered_clarifications(SESSION)
        assert closed == [asked_run]
        assert conflict.unfinished(conn, SESSION) == [], "回答之后不能再有未完成轮次"
        row = conn.execute("SELECT status FROM dag_runs WHERE id = ?", (asked_run,)).fetchone()
        assert str(row["status"]) == "cancelled"
        states = {
            str(node["tool"]): str(node["state"])
            for node in conn.execute(
                "SELECT tool, state FROM dag_nodes WHERE run_id = ?", (asked_run,)
            ).fetchall()
        }
        assert states["transfer"] == "skipped"
        assert "run.superseded" in _audit_kinds(conn, SESSION)
        before = _balance(conn)
    finally:
        conn.close()

    # 新的那句话可以正常执行：只扣一次钱，且带 run.superseded 的审计链没断。
    frames2 = _drive(SESSION, _dag())
    assert _error(frames2) is None, "回答澄清之后，新请求不该再被冲突闸门拦住"
    conn = _conn()
    try:
        assert _balance(conn) == before - AMOUNT_FEN
        kinds = _audit_kinds(conn, SESSION)
        assert "run.superseded" in kinds and "confirm.approved" in kinds
    finally:
        conn.close()
