"""人工接管（app/handoffs.py + /api/handoffs）的验收用例。

为什么单独钉这一层：「转人工」原先只是内存里的一个 state.question——进程一退、
或者两个操作员同时点同一张单，队列就说不清谁在处理什么。这里逐条钉住任务二的硬要求：
建单在库里有实体、重开库还在、并发领取只有一个赢家、终态不能复活、过期授权不能
当通行证、恢复不重跑已完成节点、失败绝不粉饰成成功、每个动作都留审计。

红线：恢复是「接着跑已经落库的那张图」，不是重新开始。
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app import clock, db, handoffs
from app.clock import ValidationError
from app.models import Dag
from app.planner import dag as planner_dag
from app.planner import executor
from app.security import api_auth
from app.tools import registry
from scripts.seed import seed

SECRET = "0123456789abcdef" * 4  # 64 位十六进制：能力令牌密钥必须是 hex
AMOUNT_FEN = 50_000  # 500 元 -> 黄灯，只需要一次确认
START_BALANCE_FEN = 1_286_050
ACCOUNT_ID = "a_3d7e60c2"
OP = "op_li"


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


def _dag(payee: str = "李静", amount_fen: int = AMOUNT_FEN) -> Dag:
    return planner_dag.build(
        {"intent": "transfer", "slots": {"payee_name": payee, "amount_fen": amount_fen}}
    )


def _conn() -> sqlite3.Connection:
    conn = db.connect()
    db.init_db(conn)
    return conn


def _balance(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT balance_fen FROM accounts WHERE id = ?", (ACCOUNT_ID,)).fetchone()
    return int(row["balance_fen"])


def _run_id(conn: sqlite3.Connection, session_id: str) -> str:
    row = conn.execute(
        "SELECT id FROM dag_runs WHERE session_id = ? ORDER BY created_ms DESC LIMIT 1",
        (session_id,),
    ).fetchone()
    assert row is not None, "这一轮没落库，接管单无从谈起"
    return str(row["id"])


def _run_status(conn: sqlite3.Connection, session_id: str) -> str:
    row = conn.execute(
        "SELECT status FROM dag_runs WHERE session_id = ? ORDER BY created_ms DESC LIMIT 1",
        (session_id,),
    ).fetchone()
    return "" if row is None else str(row["status"])


def _audit_rows(conn: sqlite3.Connection, session_id: str) -> list[tuple[str, dict[str, Any]]]:
    rows = conn.execute(
        "SELECT event_type, payload_json FROM audit_log WHERE session_id = ? ORDER BY seq",
        (session_id,),
    ).fetchall()
    return [(str(r["event_type"]), json.loads(str(r["payload_json"] or "{}"))) for r in rows]


def _audit_kinds(conn: sqlite3.Connection, session_id: str) -> list[str]:
    return [kind for kind, _ in _audit_rows(conn, session_id)]


def _node_id(dag: Dag, tool: str) -> str:
    for node in planner_dag.topo(dag):
        if node.tool == tool:
            return str(node.id)
    raise AssertionError(f"DAG 里没有 {tool} 节点")


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


def _complete_run(session_id: str, dag: Dag, *, timeout_ms: int = 120_000) -> None:
    """完整跑一轮，确认卡由代码自己点掉。"""

    async def emit(name: str, data: dict[str, Any]) -> None:
        if name in ("confirm", "factor"):
            await executor.resume(session_id, str(data["confirmation_id"]), True)

    asyncio.run(executor.run(session_id, dag, emit, confirm_timeout_ms=timeout_ms))


def _new_handoff(
    conn: sqlite3.Connection,
    session_id: str,
    *,
    reason: str = "CONFIRM_PENDING",
    claim: bool = True,
    **kwargs: Any,
) -> dict[str, Any]:
    """按某个会话的轮次建一张单（默认顺手领取），减少用例里的样板。"""
    run_id = _run_id(conn, session_id)
    row = handoffs.create(conn, run_id=run_id, session_id=session_id, reason=reason, **kwargs)
    if claim:
        row = handoffs.claim(conn, str(row["id"]), OP)
    return row


def _spy_execute(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """记录恢复过程中真正被调用的工具名：用来证明已完成节点没有被重跑。"""
    calls: list[str] = []
    real = registry.execute

    def spy(
        name: str,
        params: dict[str, Any] | None = None,
        token: str | None = None,
        ctx: Any = None,
    ) -> Any:
        calls.append(name)
        return real(name, params, token=token, ctx=ctx)

    monkeypatch.setattr(registry, "execute", spy)
    return calls


# --------------------------------------------------------- 1) 模糊收款人自动建单


def test_payee_ambiguity_creates_pending_handoff(bank: Path) -> None:
    """两位都叫张三：必须停下来问用户，并把这一轮整包挂到接管队列上。"""
    session = "s_handoff_ambiguity"
    dag = _dag("张三")
    events: list[tuple[str, dict[str, Any]]] = []

    async def emit(name: str, data: dict[str, Any]) -> None:
        events.append((name, dict(data)))

    asyncio.run(executor.run(session, dag, emit, confirm_timeout_ms=10_000))

    done = [data for name, data in events if name == "done"]
    assert done and done[-1].get("question"), "歧义必须变成一句追问，而不是猜一个收款人"

    conn = _conn()
    try:
        run_id = _run_id(conn, session)
        pending = handoffs.list_handoffs(conn, status="pending")
        assert len(pending) == 1
        row = pending[0]
        assert row["run_id"] == run_id
        assert row["session_id"] == session
        assert row["reason"] == "PAYEE_AMBIGUOUS"
        assert row["failure"]["needs_user_decision"] is True
        assert row["dag"]["nodes"], "接管单必须带原 DAG，操作员才有得看"
        resolve_node = _node_id(dag, "resolve_payee")
        assert resolve_node in [item["node_id"] for item in row["done_nodes"]]
        assert str(done[-1]["question"]) in str(row["note"])
        kinds = _audit_kinds(conn, session)
        assert "handoff.created" in kinds
        assert "transfer.initiated" not in kinds, "问一句话绝不能顺手把钱转出去"

        # 同一轮被追问多次也只是同一张单：建单是幂等的。
        again = handoffs.create(conn, run_id=run_id, reason="PAYEE_AMBIGUOUS")
        assert again["id"] == row["id"]
        assert again["reused"] is True
    finally:
        conn.close()


# ------------------------------------------------------- 2) 重开库仍然读得到


def test_pending_handoff_survives_reopen(bank: Path) -> None:
    """模拟进程重启：内存全丢，队列必须还能从库里读回来。"""
    session = "s_handoff_restart"
    _halt_before_confirm(session, _dag())
    executor.RUNS.clear()

    conn = _conn()
    try:
        row = _new_handoff(conn, session, claim=False)
        handoff_id = str(row["id"])
    finally:
        conn.close()

    # 全新连接 = 新进程视角：只认库里的数据。
    reopened = db.connect()
    try:
        again = handoffs.get(reopened, handoff_id)
        assert again["status"] == "pending"
        assert [item["id"] for item in handoffs.list_handoffs(reopened, status="pending")] == [
            handoff_id
        ]
    finally:
        reopened.close()


# --------------------------------------------------- 3) 并发领取只有一个赢家


def test_concurrent_claim_has_exactly_one_winner(bank: Path) -> None:
    """两个操作员同一时刻点「领取」：条件 UPDATE 只能让一个 rowcount=1。"""
    session = "s_handoff_claim"
    _halt_before_confirm(session, _dag())
    executor.RUNS.clear()

    conn = _conn()
    try:
        handoff_id = str(_new_handoff(conn, session, claim=False)["id"])
    finally:
        conn.close()

    results: dict[str, str] = {}
    gate = threading.Barrier(2)

    def worker(name: str) -> None:
        worker_conn = db.connect()
        try:
            gate.wait(timeout=5)
            try:
                handoffs.claim(worker_conn, handoff_id, name)
                results[name] = "ok"
            except ValidationError as exc:
                results[name] = exc.code
        finally:
            worker_conn.close()

    threads = [threading.Thread(target=worker, args=(name,)) for name in ("op_a", "op_b")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)

    assert sorted(results.values()) == ["HANDOFF_ALREADY_CLAIMED", "ok"]
    winner = "op_a" if results["op_a"] == "ok" else "op_b"
    conn = _conn()
    try:
        row = handoffs.get(conn, handoff_id)
        assert row["status"] == "claimed"
        assert row["operator"] == winner
        assert row["claimed_ms"] > 0
        assert _audit_kinds(conn, session).count("handoff.claimed") == 1
    finally:
        conn.close()


# ------------------------------------------------ 4) 拒绝之后不能再恢复


def test_rejected_handoff_cannot_be_resumed(bank: Path) -> None:
    session = "s_handoff_reject"
    _halt_before_confirm(session, _dag())
    executor.RUNS.clear()

    conn = _conn()
    try:
        row = _new_handoff(conn, session, claim=False)
        handoff_id = str(row["id"])

        rejected = handoffs.reject(conn, handoff_id, OP, "用户电话里说这笔先不做")
        assert rejected["status"] == "rejected"
        assert "先不做" in rejected["note"]

        # 已经关闭的单既领不动，也恢复不了。
        with pytest.raises(ValidationError) as claimed_exc:
            handoffs.claim(conn, handoff_id, "op_b")
        assert claimed_exc.value.code == "HANDOFF_ALREADY_CLAIMED"

        with pytest.raises(ValidationError) as resume_exc:
            asyncio.run(handoffs.resume(conn, handoff_id, operator=OP))
        assert resume_exc.value.code == "HANDOFF_INVALID_STATE"

        assert _balance(conn) == START_BALANCE_FEN
        kinds = _audit_kinds(conn, session)
        assert "handoff.rejected" in kinds
        assert "transfer.initiated" not in kinds
    finally:
        conn.close()


# --------------------------------------------- 5) 要求补充信息必须写清楚


def test_need_info_requires_note_and_records_it(bank: Path) -> None:
    session = "s_handoff_need_info"
    _halt_before_confirm(session, _dag())
    executor.RUNS.clear()

    conn = _conn()
    try:
        row = _new_handoff(conn, session, claim=False)
        handoff_id = str(row["id"])

        with pytest.raises(ValidationError) as empty_exc:
            handoffs.need_info(conn, handoff_id, OP, "   ")
        assert empty_exc.value.code == "HANDOFF_NOTE_REQUIRED"

        asked = handoffs.need_info(conn, handoff_id, OP, "请用户确认收款人尾号是 4417 还是 6620")
        assert asked["status"] == "need_info"
        assert asked["operator"] == OP
        assert "4417" in asked["note"]

        rows = _audit_rows(conn, session)
        kind, payload = next(item for item in rows if item[0] == "handoff.need_info")
        assert payload["operator"] == OP
        assert "4417" in payload["note"]
    finally:
        conn.close()


# ------------------------------------- 6) 过期确认卡不能拿来恢复


def test_expired_confirmation_cannot_be_resumed(bank: Path) -> None:
    """卡过期 = 用户那一次授权失效：就地作废 + 关单，绝不执行。"""
    session = "s_handoff_expired_card"
    confirmation_id = _halt_before_confirm(session, _dag())
    executor.RUNS.clear()

    # 确认卡的等待窗口是 60 秒（_halt_before_confirm 传的 timeout_ms），往后拨 61 秒即过期。
    clock.freeze(clock.now_ms() + 61_000)

    conn = _conn()
    try:
        row = _new_handoff(conn, session)
        handoff_id = str(row["id"])

        with pytest.raises(ValidationError) as exc:
            asyncio.run(handoffs.resume(conn, handoff_id, operator=OP))
        assert exc.value.code == "HANDOFF_CONFIRM_EXPIRED"

        after = handoffs.get(conn, handoff_id)
        assert after["status"] == "failed"
        assert "过期" in after["note"]

        card = conn.execute(
            "SELECT status FROM confirmations WHERE id = ?", (confirmation_id,)
        ).fetchone()
        assert str(card["status"]) == "expired"

        rows = _audit_rows(conn, session)
        kinds = [kind for kind, _ in rows]
        assert "confirm.expired" in kinds
        assert "handoff.resume_failed" in kinds
        expired_payload = next(payload for kind, payload in rows if kind == "confirm.expired")
        assert expired_payload["reason"] == "HANDOFF_RESUME_EXPIRED"
        failed_payload = next(payload for kind, payload in rows if kind == "handoff.resume_failed")
        assert failed_payload["reason_code"] == "CONFIRM_EXPIRED"

        assert _balance(conn) == START_BALANCE_FEN, "过期的批准一分钱都不能动"

        # failed 是终态：不能再被点一次恢复。
        with pytest.raises(ValidationError) as again:
            asyncio.run(handoffs.resume(conn, handoff_id, operator=OP))
        assert again.value.code == "HANDOFF_INVALID_STATE"
    finally:
        conn.close()


# ------------------------------------- 7) 过期/缺失授权不能拿来恢复


def test_expired_plan_authorization_cannot_be_resumed(bank: Path) -> None:
    """计划授权过期或干脆不存在：恢复前必须重查，不能拿建单时的快照当免死金牌。"""
    session = "s_handoff_expired_auth"
    _halt_before_confirm(session, _dag())
    executor.RUNS.clear()

    conn = _conn()
    try:
        expired_at = clock.now_ms() - 1_000
        row = _new_handoff(
            conn,
            session,
            auth={"plan_authorization": {"id": "pa_old", "expires_ms": expired_at}},
        )
        with pytest.raises(ValidationError) as exc:
            asyncio.run(handoffs.resume(conn, str(row["id"]), operator=OP))
        assert exc.value.code == "HANDOFF_AUTH_EXPIRED"

        after = handoffs.get(conn, str(row["id"]))
        assert after["status"] == "failed"
        rows = _audit_rows(conn, session)
        failed_payload = next(payload for kind, payload in rows if kind == "handoff.resume_failed")
        assert failed_payload["reason_code"] == "PLAN_AUTHORIZATION_EXPIRED"
        assert _balance(conn) == START_BALANCE_FEN
    finally:
        conn.close()

    # 快照里引用了一条库里根本不存在的授权：同样是拒绝，原因码写清楚。
    missing_session = "s_handoff_missing_auth"
    _halt_before_confirm(missing_session, _dag())
    executor.RUNS.clear()
    conn = _conn()
    try:
        row = _new_handoff(
            conn, missing_session, auth={"plan_authorization_ids": ["pa_missing"]}
        )
        with pytest.raises(ValidationError) as exc:
            asyncio.run(handoffs.resume(conn, str(row["id"]), operator=OP))
        assert exc.value.code == "HANDOFF_AUTH_EXPIRED"
        rows = _audit_rows(conn, missing_session)
        failed_payload = next(payload for kind, payload in rows if kind == "handoff.resume_failed")
        assert failed_payload["reason_code"] == "PLAN_AUTHORIZATION_MISSING"
    finally:
        conn.close()


# ---------------------------- 8) 恢复不重跑已完成节点，余额只扣一次


def test_resume_skips_done_nodes_and_charges_once(
    bank: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """断流时前两个节点已经 done：恢复只该补跑 transfer，钱只扣一次。"""
    session = "s_handoff_resume_skip"
    confirmation_id = _halt_before_confirm(session, _dag())

    conn = _conn()
    try:
        # 用户在断流前其实已经点过确认：库里是 approved，恢复要认这张卡、不要再弹一次。
        conn.execute(
            "UPDATE confirmations SET status = 'approved', resolved_ms = ? WHERE id = ?",
            (clock.now_ms(), confirmation_id),
        )
    finally:
        conn.close()
    executor.RUNS.clear()

    conn = _conn()
    try:
        row = _new_handoff(conn, session)
        calls = _spy_execute(monkeypatch)
        result = asyncio.run(handoffs.resume(conn, str(row["id"]), operator=OP))

        assert result["status"] == "resumed"
        assert result["run_status"] == "done"
        assert _balance(conn) == START_BALANCE_FEN - AMOUNT_FEN
        assert calls.count("transfer") == 1
        assert "resolve_payee" not in calls, "done 的节点不许重跑"
        assert "query_balance" not in calls, "done 的节点不许重跑"

        kinds = _audit_kinds(conn, session)
        assert kinds.count("transfer.initiated") == 1
        assert "handoff.resumed" in kinds
        assert handoffs.get(conn, str(row["id"]))["status"] == "resumed"
    finally:
        conn.close()


# ---------------------------- 9) 失败节点绝不被当成成功


def test_failed_node_is_never_reported_as_success(
    bank: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """轮次里已经有 failed 节点：恢复必须拒绝，并把 run 从 done 改回 failed。"""
    session = "s_handoff_failed_node"
    dag = _dag()
    _complete_run(session, dag)  # 钱已经正常出账一次
    executor.RUNS.clear()

    conn = _conn()
    try:
        run_id = _run_id(conn, session)
        assert _run_status(conn, session) == "done"
        transfer_node = _node_id(dag, "transfer")
        # 构造「执行记录里躺着一个失败节点」：哪怕 run 被标成 done，也不能当成功恢复。
        conn.execute(
            "UPDATE dag_nodes SET state = 'failed' WHERE run_id = ? AND node_id = ?",
            (run_id, transfer_node),
        )

        row = _new_handoff(conn, session, reason="INSPECTION")
        calls = _spy_execute(monkeypatch)
        with pytest.raises(ValidationError) as exc:
            asyncio.run(handoffs.resume(conn, str(row["id"]), operator=OP))
        assert exc.value.code == "HANDOFF_RESUME_FAILED"
        assert calls == [], "有失败节点时连一个工具都不该被调用"

        after = handoffs.get(conn, str(row["id"]))
        assert after["status"] == "failed"
        assert _run_status(conn, session) == "failed", "带失败节点的轮次不许留成 done"
        assert _balance(conn) == START_BALANCE_FEN - AMOUNT_FEN, "已经出过账的钱不能再动"
        assert _audit_kinds(conn, session).count("transfer.initiated") == 1

        rows = _audit_rows(conn, session)
        failed_payload = next(payload for kind, payload in rows if kind == "handoff.resume_failed")
        assert failed_payload["reason_code"] == "NODE_FAILED"
    finally:
        conn.close()


# ---------------------------- 10) 缺用户决定的单，操作员不能替用户选


def test_payee_choice_stays_with_the_user(bank: Path) -> None:
    """收款人歧义本质是「用户本人还没选」：操作员恢复只能退回 need_info。"""
    session = "s_handoff_payee_choice"
    dag = _dag("张三")

    async def emit(_name: str, _data: dict[str, Any]) -> None:
        return None

    asyncio.run(executor.run(session, dag, emit, confirm_timeout_ms=10_000))
    executor.RUNS.clear()

    conn = _conn()
    try:
        pending = handoffs.list_handoffs(conn, status="pending")
        assert len(pending) == 1
        handoff_id = str(pending[0]["id"])

        with pytest.raises(ValidationError) as exc:
            asyncio.run(handoffs.resume(conn, handoff_id, operator=OP))
        assert exc.value.code == "HANDOFF_INVALID_STATE", "pending 单要先领取"

        handoffs.claim(conn, handoff_id, OP)
        with pytest.raises(ValidationError) as exc:
            asyncio.run(handoffs.resume(conn, handoff_id, operator=OP))
        assert exc.value.code == "HANDOFF_NEEDS_USER_DECISION"

        after = handoffs.get(conn, handoff_id)
        assert after["status"] == "need_info"
        assert "用户" in after["note"]
        assert _balance(conn) == START_BALANCE_FEN
        assert "transfer.initiated" not in _audit_kinds(conn, session)
    finally:
        conn.close()


# ----------------------------------------- 11) 每个动作都留审计


def test_every_operator_action_is_audited(bank: Path) -> None:
    """领取/拒绝/补充信息/取消/恢复各自一条审计，payload 里带操作员。"""
    # created + claimed：来自一次真实的收款人歧义。
    ambiguity_session = "s_handoff_audit_ambiguity"
    dag = _dag("张三")

    async def emit(_name: str, _data: dict[str, Any]) -> None:
        return None

    asyncio.run(executor.run(ambiguity_session, dag, emit, confirm_timeout_ms=10_000))
    executor.RUNS.clear()

    conn = _conn()
    try:
        pending = handoffs.list_handoffs(conn, status="pending")
        handoffs.claim(conn, str(pending[0]["id"]), OP)
    finally:
        conn.close()

    # rejected
    reject_session = "s_handoff_audit_reject"
    _halt_before_confirm(reject_session, _dag())
    executor.RUNS.clear()
    conn = _conn()
    try:
        handoffs.reject(conn, str(_new_handoff(conn, reject_session, claim=False)["id"]), OP, "不做")
    finally:
        conn.close()

    # need_info
    info_session = "s_handoff_audit_info"
    _halt_before_confirm(info_session, _dag())
    executor.RUNS.clear()
    conn = _conn()
    try:
        handoffs.need_info(conn, str(_new_handoff(conn, info_session, claim=False)["id"]), OP, "缺尾号")
    finally:
        conn.close()

    # cancelled
    cancel_session = "s_handoff_audit_cancel"
    _halt_before_confirm(cancel_session, _dag())
    executor.RUNS.clear()
    conn = _conn()
    try:
        row = _new_handoff(conn, cancel_session, claim=False)
        cancelled = asyncio.run(handoffs.cancel(conn, str(row["id"]), OP, "用户改主意了"))
        assert cancelled["status"] == "cancelled"
    finally:
        conn.close()

    # resumed：这一轮已经跑完，恢复只是确认没有遗留问题（不重跑、不动钱）。
    resumed_session = "s_handoff_audit_resumed"
    _complete_run(resumed_session, _dag())
    executor.RUNS.clear()
    conn = _conn()
    try:
        row = _new_handoff(conn, resumed_session)
        result = asyncio.run(handoffs.resume(conn, str(row["id"]), operator=OP))
        assert result["status"] == "resumed"
    finally:
        conn.close()

    checks = [
        (ambiguity_session, "handoff.created"),
        (ambiguity_session, "handoff.claimed"),
        (reject_session, "handoff.rejected"),
        (info_session, "handoff.need_info"),
        (cancel_session, "handoff.cancelled"),
        (resumed_session, "handoff.resumed"),
    ]
    conn = _conn()
    try:
        for session, kind in checks:
            rows = _audit_rows(conn, session)
            matches = [payload for event, payload in rows if event == kind]
            assert matches, f"{session} 少了 {kind} 审计"
            if kind != "handoff.created":
                assert matches[-1]["operator"] == OP, f"{kind} 没记操作员"
    finally:
        conn.close()


# ----------------------------------------- 12) 接口受保护且能读到队列


def test_handoff_api_is_protected_and_lists_pending(
    bank: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """接管队列能直接看到账户与失败原因，绝不能是匿名可读的运维页面。"""
    session = "s_handoff_api"
    _halt_before_confirm(session, _dag())
    executor.RUNS.clear()

    conn = _conn()
    try:
        handoff_id = str(_new_handoff(conn, session, claim=False)["id"])
    finally:
        conn.close()

    # conftest 默认关掉 HTTP 鉴权（其余用例只关心业务行为）；这条恰恰要真校验。
    monkeypatch.delenv(api_auth.DISABLED_ENV, raising=False)
    monkeypatch.setenv(api_auth.TOKEN_ENV, "handoff-test-token")
    from app.main import app

    client = TestClient(app)

    assert client.get("/api/handoffs").status_code == 401

    headers = {"Authorization": "Bearer handoff-test-token"}
    listed = client.get("/api/handoffs?status=pending", headers=headers)
    assert listed.status_code == 200
    body = listed.json()
    assert body["count"] == 1
    assert body["handoffs"][0]["id"] == handoff_id

    claimed = client.post(
        f"/api/handoffs/{handoff_id}/claim", json={"operator": "api_op"}, headers=headers
    )
    assert claimed.status_code == 200
    assert claimed.json()["handoff"]["status"] == "claimed"
    assert claimed.json()["handoff"]["operator"] == "api_op"

    # 已被领走的单再领一次：明确报「已被别人领走」，而不是静默成功。
    again = client.post(
        f"/api/handoffs/{handoff_id}/claim", json={"operator": "api_op2"}, headers=headers
    )
    assert again.status_code == 400
    assert again.json()["code"] == "HANDOFF_ALREADY_CLAIMED"

    # 手动建单也走同一套结构：同一轮重复建单是幂等的。
    created = client.post(
        "/api/handoffs",
        json={"run_id": _run_id(_conn(), session), "reason": "MANUAL_REVIEW", "operator": "api_op"},
        headers=headers,
    )
    assert created.status_code == 200
    assert created.json()["handoff"]["reused"] is True

# ------------------------------------ 8) 用户回答澄清后，旧接管单一并关闭


def test_answering_the_clarification_closes_the_stale_handoff(bank: Path) -> None:
    """用户回了「尾号4417」= 问题已答完，那一轮按被取代关闭，队列里不许再留旧单。"""
    from app.planner import conflict

    session = "s_handoff_superseded"
    dag = _dag("张三")

    async def emit(name: str, data: dict[str, Any]) -> None:
        return None

    asyncio.run(executor.run(session, dag, emit, confirm_timeout_ms=10_000))
    executor.RUNS.clear()

    conn = _conn()
    try:
        run_id = _run_id(conn, session)
        pending = handoffs.list_handoffs(conn, status="pending")
        assert len(pending) == 1 and pending[0]["run_id"] == run_id
        handoff_id = str(pending[0]["id"])

        closed = conflict.supersede_answered_clarifications(session, conn=conn)
        assert closed == [run_id]

        after = handoffs.get(conn, handoff_id)
        assert after["status"] == "cancelled"
        assert "取代" in after["note"]
        assert handoffs.list_handoffs(conn, status="pending") == [], "答完澄清后队列里不该有旧单"

        payloads = [payload for kind, payload in _audit_rows(conn, session) if kind == "handoff.cancelled"]
        assert payloads and payloads[0]["closed_by"] == "run.superseded"
        assert payloads[0]["run_id"] == run_id
        assert "run.superseded" in _audit_kinds(conn, session)
    finally:
        conn.close()
