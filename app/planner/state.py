"""执行器的状态与落库：一轮 DAG 跑动的全部活状态都住在这儿。

拆文件的理由：executor.py 要控制函数长度与可读性，而状态机本身是独立的一层，
执行流程、事件拼装、状态落库三者分开看各自都短，加起来也不会超过 docs/08 的 400 行上限。

三层依赖方向（无环）：
    executor → runner → state / events
    events   → state
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Final

from app import audit, clock, db, funds
from app.clock import BankError, ValidationError
from app.ids import new_id
from app.models import Action, Dag, Decision
from app.money import fen_to_yuan_str
from app.security import breaker
from app.tools import base as tool_base

log = logging.getLogger(__name__)

Emit = Callable[[str, dict[str, Any]], Any]
#: 确认卡有效期：**毫秒**。对外承诺的「120 秒」就是这个常量（换算用 ms_to_seconds）。
CONFIRM_TIMEOUT_MS: Final[int] = 120_000
#: 能力令牌有效期：也是毫秒（app/security/token.py 里 exp_ms 与 now_ms() 同单位）。
TOKEN_TTL_MS: Final[int] = 120_000


def ms_to_seconds(ms: int) -> float:
    """毫秒 -> 秒。asyncio.wait_for / asyncio.sleep 收的是秒，别把它们当毫秒用。"""
    return ms / 1000
BUSY_STATES: Final[tuple[str, ...]] = ("pending", "running", "waiting_confirm", "waiting_factor")
CONTINUE: Final[str] = "continue"
STOP: Final[str] = "stop"
RUNS: dict[str, RunState] = {}

@dataclass
class Confirmation:
    """一次待用户点头的确认；future 由 /api/confirm 或 /api/cancel 唤醒。"""

    id: str
    node_id: str
    action: Action
    level: str
    required_factors: list[str]
    action_text: str
    expires_ms: int
    created_ms: int
    payee_name: str | None = None
    status: str = "pending"
    future: asyncio.Future[dict[str, Any]] | None = None


@dataclass
class NodeRec:
    """节点运行态；状态机 pending → running → done/failed/skipped/…"""

    node_id: str
    tool: str
    state: str = "pending"
    params: dict[str, Any] = field(default_factory=dict)
    decision: dict[str, Any] | None = None
    result: dict[str, Any] | None = None
    summary: str = ""
    error_code: str | None = None


@dataclass
class RunState:
    """一轮 DAG 的全部活状态。"""

    session_id: str
    run_id: str
    dag: Dag
    conn: sqlite3.Connection
    account_id: str = ""
    secret: str | None = None
    nodes: dict[str, NodeRec] = field(default_factory=dict)
    confirmations: dict[str, Confirmation] = field(default_factory=dict)
    results: dict[str, dict[str, Any]] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    cancelled: bool = False
    cancel_reason: str = ""
    question: str | None = None
    balance_after_fen: int | None = None


def action_text(tool: str, action: Action, *, payee: str | None = None) -> str:
    """给用户看的一句话：工具 + 金额 + 收款人，不含任何英文码。

    收款人优先用查库拿到的姓名：params 里只有 payee_id，用户看不懂 id。
    """
    amount = action.amount_fen
    money_text = fen_to_yuan_str(amount) + " 元" if amount else "这笔"
    if tool in ("transfer", "reserve_funds", "pay_merchant"):
        who = payee or str(action.params.get("payee_name") or action.params.get("note") or "")
        return f"{money_text}（{tool_name(tool)}{('，' + who) if who else ''}）"
    return f"{tool_name(tool)}"


def tool_name(tool: str) -> str:
    """工具的中文名，前端与审计里都不出现英文函数名。"""
    return {
        "transfer": "转账",
        "reserve_funds": "预留资金",
        "pay_merchant": "商户支付",
        "cancel_subscription": "取消订阅",
    }.get(tool, tool)


def open_run(session_id: str, dag: Dag, account_id: str | None, secret: str | None) -> RunState:
    """开库、清过期预授权、建会话与 run 行、写 dag.built 审计。"""
    conn = db.connect()
    try:
        db.sweep_expired_holds(conn)
        breaker.ensure_available(conn, session_id)
        if ensure_session(conn, session_id):
            audit.append(conn, "session.start", {"session_id": session_id}, session_id=session_id)
        account = tool_base.primary_account(conn, account_id)
        state = RunState(
            session_id=session_id,
            run_id=dag.id,
            dag=dag,
            conn=conn,
            account_id=str(account["id"]),
            secret=secret,
            nodes={node.id: NodeRec(node_id=node.id, tool=node.tool) for node in dag.nodes},
        )
        insert_run(state)
        audit.append(
            conn,
            "dag.built",
            {"run_id": state.run_id, "nodes": [{"node_id": n.id, "tool": n.tool} for n in dag.nodes]},
            session_id=session_id,
        )
        return state
    except BaseException:
        conn.close()
        raise


def finish_run(state: RunState) -> None:
    """收尾：补一条 session.end，关连接，从活跃表里摘掉。"""
    try:
        audit.append(
            state.conn,
            "session.end",
            {
                "run_id": state.run_id,
                "cancelled": state.cancelled,
                "notes": list(state.notes),
                "balance_after_fen": state.balance_after_fen,
            },
            session_id=state.session_id,
        )
        state.conn.execute(
            "UPDATE sessions SET status = ?, last_ms = ? WHERE id = ?",
            ("cancelled" if state.cancelled else "done", clock.now_ms(), state.session_id),
        )
    except Exception:  # noqa: BLE001 - 收尾失败不该把已发出去的结果推翻
        log.warning("executor.finish.failed session=%s", state.session_id)
    finally:
        state.conn.close()
        RUNS.pop(state.session_id, None)


def new_confirmation(state: RunState, node: Any, action: Action, decision: Decision) -> Confirmation:
    now_ms = clock.now_ms()
    who = _display_payee(state.conn, action)
    conf = Confirmation(
        id=new_id("cf"),
        node_id=node.id,
        action=action,
        level=decision.level,
        required_factors=list(decision.required_factors),
        action_text=action_text(node.tool, action, payee=who),
        created_ms=now_ms,
        expires_ms=now_ms + CONFIRM_TIMEOUT_MS,
        payee_name=who,
    )
    state.confirmations[conf.id] = conf
    return conf


def _display_payee(conn: sqlite3.Connection, action: Action) -> str | None:
    """确认卡片要显示收款人姓名；参数里只有 payee_id 时去 payees 表换名字。"""
    payee_id = action.params.get("payee_id")
    if not payee_id:
        return None
    return tool_base.payee_name(conn, str(payee_id))


def wait_for_answer(conf: Confirmation) -> asyncio.Future[dict[str, Any]]:
    if conf.future is None:
        conf.future = asyncio.get_running_loop().create_future()
    return conf.future


def resolve_confirmation(state: RunState, conf: Confirmation, status: str, *, audit_reason: str) -> None:
    """落定一次确认：改状态、写审计、唤醒等待中的执行器。"""
    conf.status = status
    state.conn.execute(
        "UPDATE confirmations SET status = ?, resolved_ms = ? WHERE id = ?",
        (status, clock.now_ms(), conf.id),
    )
    audit.append(
        state.conn,
        "confirm.approved" if status == "approved" else "confirm.rejected",
        {"confirmation_id": conf.id, "node_id": conf.node_id, "reason": audit_reason},
        session_id=state.session_id,
    )
    resolve_pending(state, {"approve": status == "approved", "confirmation_id": conf.id})


def resolve_pending(state: RunState, answer: dict[str, Any]) -> None:
    """把答案递给所有在等的确认；已解决的不动。"""
    for conf in state.confirmations.values():
        if conf.future is not None and not conf.future.done():
            conf.future.set_result(dict(answer))


def require_run(session_id: str) -> RunState:
    state = RUNS.get(session_id)
    if state is None:
        raise ValidationError("会话已结束或不存在", session_id=session_id)
    return state


def require_confirmation(state: RunState, confirmation_id: str) -> Confirmation:
    conf = state.confirmations.get(confirmation_id)
    if conf is None:
        raise ValidationError("确认不存在或已过期", confirmation_id=confirmation_id)
    return conf


def factors_ok(conf: Confirmation, factor: str | None) -> bool:
    """红色动作要双因子；face+sms / sms,face 都认，缺一个都不放行。"""
    if not conf.required_factors:
        return True
    given = {part.strip() for part in str(factor or "").replace("+", ",").replace(",", " ").split()}
    return set(conf.required_factors).issubset(given)


def release_holds(state: RunState, reason: str) -> int:
    """E3/E4：把本会话还锁着的钱全部放回去，返回释放条数。"""
    rows = state.conn.execute(
        "SELECT id FROM holds WHERE session_id = ? AND status = 'held' ORDER BY id",
        (state.session_id,),
    ).fetchall()
    released = 0
    for row in rows:
        try:
            funds.release(state.conn, str(row["id"]), reason=reason, session_id=state.session_id)
            released += 1
        except BankError as exc:
            log.warning("executor.release_failed hold=%s code=%s", row["id"], exc.code)
    return released


def persist_node(state: RunState, rec: NodeRec, node_state: str, *, error_code: str | None = None) -> None:
    """节点状态落库；DAG 面板和中断续跑都靠这张表。"""
    rec.state = node_state
    if error_code is not None:
        rec.error_code = error_code
    state.conn.execute(
        "UPDATE dag_nodes SET state = ?, result_json = ?, ts_ms = ? WHERE run_id = ? AND node_id = ?",
        (
            node_state,
            json.dumps({"summary": rec.summary, "error_code": rec.error_code}, ensure_ascii=False),
            clock.now_ms(),
            state.run_id,
            rec.node_id,
        ),
    )


def persist_decision(state: RunState, rec: NodeRec, params: dict[str, Any], decision: Decision) -> None:
    rec.params = dict(params)
    rec.decision = {
        "level": decision.level,
        "rule_id": decision.rule_id,
        "required_factors": list(decision.required_factors),
    }
    state.conn.execute(
        "UPDATE dag_nodes SET params_json = ?, decision_json = ? WHERE run_id = ? AND node_id = ?",
        (
            json.dumps(params, ensure_ascii=False),
            json.dumps(rec.decision, ensure_ascii=False),
            state.run_id,
            rec.node_id,
        ),
    )


def insert_run(state: RunState) -> None:
    now_ms = clock.now_ms()
    with db.tx(state.conn):
        state.conn.execute(
            "INSERT INTO dag_runs (id, session_id, dag_json, status, created_ms, updated_ms)"
            " VALUES (?, ?, ?, 'running', ?, ?)",
            (state.run_id, state.session_id, state.dag.model_dump_json(), now_ms, now_ms),
        )
        for node in state.dag.nodes:
            state.conn.execute(
                "INSERT INTO dag_nodes (id, run_id, node_id, tool, params_json, state, ts_ms)"
                " VALUES (?, ?, ?, ?, ?, 'pending', ?)",
                (new_id("n"), state.run_id, node.id, node.tool, json.dumps(node.params, ensure_ascii=False), now_ms),
            )


def set_run_status(state: RunState, status: str) -> None:
    state.conn.execute(
        "UPDATE dag_runs SET status = ?, updated_ms = ? WHERE id = ?",
        (status, clock.now_ms(), state.run_id),
    )


def ensure_session(conn: sqlite3.Connection, session_id: str) -> bool:
    """建会话行；返回 True 表示这是新会话。"""
    cursor = conn.execute(
        "INSERT OR IGNORE INTO sessions (id, created_ms, last_ms, title, status) VALUES (?, ?, ?, '', 'active')",
        (session_id, clock.now_ms(), clock.now_ms()),
    )
    conn.execute("UPDATE sessions SET last_ms = ? WHERE id = ?", (clock.now_ms(), session_id))
    return bool(cursor.rowcount)


def balance_of(state: RunState) -> int | None:
    row = state.conn.execute("SELECT balance_fen FROM accounts WHERE id = ?", (state.account_id,)).fetchone()
    return int(row["balance_fen"]) if row is not None else state.balance_after_fen

