"""重启恢复：内存里的 RUNS 丢了之后，从 SQLite 重建一轮并接着跑。

为什么需要它：确认卡、节点状态、审计都落了库，但执行状态原先只活在
app/planner/state.py 的 RUNS 字典里。服务重启（或换个进程）之后，
POST /api/confirm 只会得到「会话已结束或不存在」——用户看着一张还在有效期内的
确认卡，却怎么也点不动。这一层把「库里有记录」变成「还能接着执行」。

三条边界，写在代码里也写在文档里：
1. 不重新解析意图、不重新问策略：DAG 与 policy.decided 都在库里，恢复只按原图继续；
2. 绝不重跑终态节点：done/failed/skipped 一律跳过，否则一笔转账会被扣两次；
3. 遇到「还没人确认过的新卡」就停下（halt_on_new_confirm），把决定权交回用户，
   不在一个 HTTP 请求里干等 120 秒。
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Final

from app import audit, clock, db
from app.clock import ValidationError
from app.models import Action, Dag
from app.planner.executor import _drive
from app.planner.state import (
    BUSY_STATES,
    Confirmation,
    NodeRec,
    RunState,
    action_text,
    confirm_timeout_ms,
    persist_node,
    release_holds,
    resolve_pending,
    set_run_status,
)
from app.security import factors, token as token_mod
from app.tools.base import primary_account


UNFINISHED_RUN_STATES: Final[tuple[str, ...]] = ("running", "waiting")


def _run_row(conn: sqlite3.Connection, session_id: str) -> sqlite3.Row | None:
    """这个会话里最该被恢复的那一轮 DAG；没有就返回 None。

    先挑**未完成**（running / waiting）的轮次，再按时间倒序。
    只按时间取最近一轮是不够的：用户在同一会话里又问了一句已经答完的问题，
    新一轮 done 会盖住上一轮还挂着的确认卡，恢复就永远找不到它了。
    """
    unfinished = ",".join("?" for _ in UNFINISHED_RUN_STATES)
    row = conn.execute(
        f"SELECT * FROM dag_runs WHERE session_id = ? AND status IN ({unfinished})"
        " ORDER BY created_ms DESC LIMIT 1",
        (session_id, *UNFINISHED_RUN_STATES),
    ).fetchone()
    if row is not None:
        return row
    return conn.execute(
        "SELECT * FROM dag_runs WHERE session_id = ? ORDER BY created_ms DESC LIMIT 1",
        (session_id,),
    ).fetchone()


def _decode(raw: Any, default: Any) -> Any:
    """宽松解 JSON：老库里的字段可能是空串或旧格式，读不出来就当默认值。"""
    if not raw:
        return default
    try:
        value = json.loads(str(raw))
    except (TypeError, ValueError):
        return default
    return value if isinstance(value, type(default)) else default


def rehydrate(conn: sqlite3.Connection, session_id: str) -> RunState:
    """从库里重建 RunState；节点、结果、确认卡都按最后一次落库的状态还原。"""
    run = _run_row(conn, session_id)
    if run is None:
        raise ValidationError("会话已结束或不存在", session_id=session_id)
    try:
        dag = Dag.model_validate_json(str(run["dag_json"]))
    except ValueError as exc:  # pragma: no cover - DAG 落库时已经校验过
        raise ValidationError("这一轮的 DAG 读不出来，无法恢复", session_id=session_id) from exc
    meta = _decode(run["meta_json"], {})
    nodes: dict[str, NodeRec] = {}
    results: dict[str, dict[str, Any]] = {}
    for row in conn.execute(
        "SELECT * FROM dag_nodes WHERE run_id = ? ORDER BY node_id", (run["id"],)
    ):
        payload = _decode(row["result_json"], {})
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        rec = NodeRec(
            node_id=str(row["node_id"]),
            tool=str(row["tool"]),
            state=str(row["state"]),
            params=_decode(row["params_json"], {}),
            decision=_decode(row["decision_json"], None),
            result=data,
            summary=str(payload.get("summary") or ""),
            error_code=payload.get("error_code"),
        )
        nodes[rec.node_id] = rec
        if rec.state == "done" and data:
            results[rec.node_id] = data
    confirmations: dict[str, Confirmation] = {}
    for row in conn.execute(
        "SELECT * FROM confirmations WHERE session_id = ? AND run_id = ? ORDER BY created_ms",
        (session_id, run["id"]),
    ):
        try:
            action = Action.model_validate_json(str(row["action_json"]))
        except ValueError:  # pragma: no cover - 建卡时已校验
            continue
        rec = nodes.get(str(row["node_id"]))
        decision = rec.decision if rec and rec.decision else {}
        required = [str(item) for item in (decision.get("required_factors") or [])]
        level = str(decision.get("level") or "yellow")
        confirmations[str(row["id"])] = Confirmation(
            id=str(row["id"]),
            node_id=str(row["node_id"]),
            action=action,
            level=level,
            required_factors=required,
            action_text=action_text(rec.tool if rec else action.tool, action),
            expires_ms=int(row["expires_ms"]),
            created_ms=int(row["created_ms"]),
            status=str(row["status"]),
            future=None,
        )
    # 老库（本轮之前的 dag_runs）没有 meta_json，账户会读成空串；
    # 那样签发出来的能力令牌绑的是 ""，执行时必然 ACCOUNT_MISMATCH。
    # 读不到就按运行时的同一条规则（primary_account）补回来，而不是硬用空串。
    account_id = str(meta.get("account_id") or "")
    if not account_id:
        account_id = str(primary_account(conn, None)["id"])
    return RunState(
        session_id=session_id,
        run_id=str(run["id"]),
        dag=dag,
        conn=conn,
        account_id=account_id,
        secret=token_mod.load_secret(None),
        nodes=nodes,
        confirmations=confirmations,
        results=results,
        user_text=str(meta.get("user_text") or ""),
    )


def _noop_emit() -> Any:
    """恢复时的 emit：事件只进内存列表，不假装还有一条 SSE 连着。"""
    events: list[dict[str, Any]] = []

    async def emit(name: str, data: dict[str, Any]) -> None:
        events.append({"event": name, "data": data})

    emit.events = events  # type: ignore[attr-defined]
    return emit


def _run_status(conn: sqlite3.Connection, run_id: str) -> str:
    row = conn.execute("SELECT status FROM dag_runs WHERE id = ?", (run_id,)).fetchone()
    return str(row["status"]) if row is not None else "unknown"


def _finalize(state: RunState, run_status: str) -> None:
    """恢复通道跑完一轮后的收尾：补 session.end、把会话状态改掉。

    正常通道里这些由 executor.run 的 finally 做；恢复通道不经过它，
    少了这一步「执行记录」页会一直把已经跑完的会话显示成进行中。
    只在 run 进入终态时补，且按 run_id 去重——恢复可能被点很多次。
    """
    if run_status not in ("done", "failed"):
        return
    now_ms = clock.now_ms()
    status = "cancelled" if state.cancelled else ("done" if run_status == "done" else "failed")
    with db.tx(state.conn):
        state.conn.execute(
            "UPDATE sessions SET status = ?, last_ms = ? WHERE id = ?",
            ("cancelled" if state.cancelled else "done", now_ms, state.session_id),
        )
        seen = state.conn.execute(
            "SELECT COUNT(*) AS n FROM audit_log WHERE session_id = ? AND event_type = 'session.end'"
            " AND json_extract(payload_json, '$.run_id') = ?",
            (state.session_id, state.run_id),
        ).fetchone()
        if int(seen["n"] or 0) == 0:
            audit.append(
                state.conn,
                "session.end",
                {
                    "run_id": state.run_id,
                    "cancelled": state.cancelled,
                    "notes": list(state.notes),
                    "balance_after_fen": state.balance_after_fen,
                    "recovered": True,
                    "session_status": status,
                },
                session_id=state.session_id,
            )


async def _drive_recovered(state: RunState) -> dict[str, Any]:
    """按原图接着跑；遇到新确认卡就停下，把决定权交回用户。"""
    emit = _noop_emit()
    await _drive(state, emit, confirm_timeout_ms(), None, halt_on_new_confirm=True)
    _finalize(state, _run_status(state.conn, state.run_id))
    return {
        "ok": True,
        "recovered": True,
        "run_status": _run_status(state.conn, state.run_id),
        "events": emit.events[-12:],  # type: ignore[attr-defined]
    }


async def confirm(
    session_id: str,
    confirmation_id: str,
    approve: bool,
    *,
    proofs: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """重启后点确认：库里那张卡 + 服务端校验 + 接着执行。"""
    conn = db.connect()
    try:
        row = conn.execute(
            "SELECT * FROM confirmations WHERE id = ? AND session_id = ?",
            (confirmation_id, session_id),
        ).fetchone()
        if row is None:
            raise ValidationError("确认不存在或已过期", confirmation_id=confirmation_id)
        status = str(row["status"])
        if status != "pending":
            return {"ok": True, "status": status, "idempotent": True, "recovered": True}
        if int(row["expires_ms"]) <= clock.now_ms():
            with db.tx(conn):
                conn.execute(
                    "UPDATE confirmations SET status = 'expired', resolved_ms = ? WHERE id = ?",
                    (clock.now_ms(), confirmation_id),
                )
                audit.append(
                    conn,
                    "confirm.expired",
                    {"confirmation_id": confirmation_id, "reason": "CONFIRM_TIMEOUT"},
                    session_id=session_id,
                )
            return {"ok": True, "status": "expired", "recovered": True}
        state = rehydrate(conn, session_id)
        conf = state.confirmations.get(confirmation_id)
        if conf is None:
            raise ValidationError("确认不存在或已过期", confirmation_id=confirmation_id)
        if not approve:
            with db.tx(conn):
                conn.execute(
                    "UPDATE confirmations SET status = 'rejected', resolved_ms = ? WHERE id = ?",
                    (clock.now_ms(), confirmation_id),
                )
                audit.append(
                    conn,
                    "confirm.rejected",
                    {"confirmation_id": confirmation_id, "reason": "USER_REJECTED"},
                    session_id=session_id,
                )
            conf.status = "rejected"
            return {"ok": True, "status": "rejected", "recovered": True}
        if conf.required_factors:
            check = factors.verify(
                conn,
                session_id=session_id,
                confirmation_id=confirmation_id,
                required=conf.required_factors,
                proofs=proofs or [],
            )
            if not check["ok"]:
                return {
                    "ok": True,
                    "status": "waiting_factor",
                    "required": list(conf.required_factors),
                    "proof_state": check["reason"],
                    "missing": check["missing"],
                    "invalid": check["invalid"],
                    "recovered": True,
                }
        with db.tx(conn):
            conn.execute(
                "UPDATE confirmations SET status = 'approved', resolved_ms = ? WHERE id = ?",
                (clock.now_ms(), confirmation_id),
            )
            audit.append(
                conn,
                "confirm.approved",
                {"confirmation_id": confirmation_id},
                session_id=session_id,
            )
        conf.status = "approved"
        result = await _drive_recovered(state)
        result["status"] = "approved" if result["run_status"] in ("running", "waiting", "done") else result["run_status"]
        return result
    finally:
        conn.close()


async def resume(session_id: str) -> dict[str, Any]:
    """不带新决定地接着跑：已经批准过的确认卡直接执行，遇到新卡就停。"""
    conn = db.connect()
    try:
        state = rehydrate(conn, session_id)
        pending = [
            conf
            for conf in state.confirmations.values()
            if conf.status == "pending" and conf.expires_ms > clock.now_ms()
        ]
        if pending:
            return {
                "ok": True,
                "recovered": True,
                "status": "waiting_confirm",
                "confirmation_id": pending[0].id,
                "run_status": _run_status(conn, state.run_id),
            }
        result = await _drive_recovered(state)
        result["status"] = result["run_status"]
        return result
    finally:
        conn.close()


async def cancel(session_id: str, reason: str = "用户取消（重启后）") -> dict[str, Any]:
    """重启后再点「中断」：内存里没有这一轮，但库里还有，必须照样能取消。

    不这样做的话，页面上那张卡只能等它自己过期，用户想换个说法重来会一直被
    conflict 挡住——「取消」按钮在刷新之后失灵，是比冲突本身更糟的体验。
    """
    conn = db.connect()
    try:
        try:
            state = rehydrate(conn, session_id)
        except ValidationError:
            return {"ok": True, "cancelled": False, "reason": "没有可取消的轮次", "released_holds": 0}
        status = _run_status(conn, state.run_id)
        if status in ("done", "cancelled", "failed"):
            return {
                "ok": True,
                "cancelled": False,
                "idempotent": True,
                "run_status": status,
                "released_holds": 0,
            }
        state.cancelled = True
        state.cancel_reason = reason
        released = release_holds(state, reason)
        for rec in state.nodes.values():
            if rec.state in BUSY_STATES:
                persist_node(state, rec, "skipped")
        resolve_pending(state, {"cancelled": True})
        set_run_status(state, "cancelled")
        audit.append(
            conn,
            "session.end",
            {"reason": reason, "released_holds": released, "recovered": True},
            session_id=session_id,
        )
        return {
            "ok": True,
            "cancelled": True,
            "released_holds": released,
            "run_status": "cancelled",
            "run_id": state.run_id,
        }
    finally:
        conn.close()


__all__ = ["cancel", "confirm", "rehydrate", "resume"]
