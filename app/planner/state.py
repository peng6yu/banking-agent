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
import os
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
#: 确认卡有效期的**默认值**（毫秒）。实际取值走 confirm_timeout_ms()：
#: 环境变量 CONFIRM_TIMEOUT_S（秒，10—3600）可覆盖，便于命令行/IM 客户端按自己的节奏调。
CONFIRM_TIMEOUT_MS: Final[int] = 120_000
#: 确认卡有效期的覆盖项（单位秒）；不设或非法时回落到 CONFIRM_TIMEOUT_MS。
CONFIRM_TIMEOUT_ENV: Final[str] = "CONFIRM_TIMEOUT_S"
MIN_CONFIRM_TIMEOUT_S: Final[int] = 10
MAX_CONFIRM_TIMEOUT_S: Final[int] = 3600
#: 能力令牌有效期：也是毫秒（app/security/token.py 里 exp_ms 与 now_ms() 同单位）。
TOKEN_TTL_MS: Final[int] = 120_000


def ms_to_seconds(ms: int) -> float:
    """毫秒 -> 秒。asyncio.wait_for / asyncio.sleep 收的是秒，别把它们当毫秒用。"""
    return ms / 1000


def confirm_timeout_ms() -> int:
    """确认卡有效期（毫秒）：默认 120 秒，可用 CONFIRM_TIMEOUT_S 覆盖。

    非法值不 raise 而是回落到默认值再打一条警告——这是个运行期调参项，
    为了一个拼错的变量名把整条确认链路变成 500，比沿用默认值更糟。
    """
    raw = os.environ.get(CONFIRM_TIMEOUT_ENV, "").strip()
    if not raw:
        return CONFIRM_TIMEOUT_MS
    try:
        seconds = int(raw)
    except ValueError:
        log.warning("planner.confirm_timeout.invalid value=%r fallback_ms=%s", raw, CONFIRM_TIMEOUT_MS)
        return CONFIRM_TIMEOUT_MS
    if not MIN_CONFIRM_TIMEOUT_S <= seconds <= MAX_CONFIRM_TIMEOUT_S:
        log.warning(
            "planner.confirm_timeout.out_of_range value=%s min=%s max=%s fallback_ms=%s",
            seconds,
            MIN_CONFIRM_TIMEOUT_S,
            MAX_CONFIRM_TIMEOUT_S,
            CONFIRM_TIMEOUT_MS,
        )
        return CONFIRM_TIMEOUT_MS
    return seconds * 1000
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
    #: 这一轮的用户原话：澄清闸门要以它为准补全（「尾号4417」→ 原话+尾号）。
    user_text: str = ""
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


def open_run(
    session_id: str,
    dag: Dag,
    account_id: str | None,
    secret: str | None,
    user_text: str = "",
) -> RunState:
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
            user_text=user_text,
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
        expires_ms=now_ms + confirm_timeout_ms(),
        payee_name=who,
        # future 在这里就建好，不推迟到 wait_for_answer 那一刻：
        # 答案可能早到——程序化客户端（IM Bot、自动化脚本）收到 confirm 帧就立刻
        # POST /api/confirm。future 建晚了，resolve_pending 会把早到的答案静默丢掉，
        # 执行器只能干等到 120 秒 CONFIRM_TIMEOUT。早建 future，早到的答案照样接得住。
        future=_new_answer_future(),
    )
    # 落库：这张表原先只有 UPDATE 没有 INSERT，于是它恒空——`/api/session` 永远列不出
    # 卡片，resolve_confirmation 的 UPDATE 也就成了空操作。建卡是唯一入口，插入放这里。
    state.conn.execute(
        "INSERT INTO confirmations"
        " (id, session_id, run_id, node_id, action_json, status, created_ms, expires_ms)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            conf.id,
            state.session_id,
            state.run_id,
            conf.node_id,
            action.model_dump_json(),
            conf.status,
            conf.created_ms,
            conf.expires_ms,
        ),
    )
    state.confirmations[conf.id] = conf
    return conf


def _new_answer_future() -> asyncio.Future[dict[str, Any]]:
    """建一个「等答案」的 future；必须在事件循环里调用。"""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError as exc:
        raise RuntimeError(
            "new_confirmation 必须在事件循环里调用：确认卡要能接住早到的答案"
        ) from exc
    return loop.create_future()


def _display_payee(conn: sqlite3.Connection, action: Action) -> str | None:
    """确认卡片要显示收款人姓名；参数里只有 payee_id 时去 payees 表换名字。"""
    payee_id = action.params.get("payee_id")
    if not payee_id:
        return None
    return tool_base.payee_name(conn, str(payee_id))


def wait_for_answer(conf: Confirmation) -> asyncio.Future[dict[str, Any]]:
    """取确认卡的「等答案」future。

    正常路径上 future 在 new_confirmation 里就建好了，这里只是把它取出来；
    留着兜底是为了将来若有别的建卡路径忘了建 future，也不至于把答案丢掉。
    """
    if conf.future is None:
        conf.future = _new_answer_future()
    return conf.future


def mark_confirmation(state: RunState, conf: Confirmation, status: str) -> None:
    """改确认卡状态并落库。

    超时（CONFIRM_TIMEOUT）与策略拦停（POLICY_BLOCKED）这两条路径不经过
    resolve_confirmation——它们不唤醒执行器、写的也不是同一句审计文案——但它们同样
    必须落库：只在内存里改 conf.status，表里那张卡就会永远停在 pending，
    `/api/session` 会一直把它当作「还在等你点」。
    """
    conf.status = status
    state.conn.execute(
        "UPDATE confirmations SET status = ?, resolved_ms = ? WHERE id = ?",
        (status, clock.now_ms(), conf.id),
    )


def resolve_confirmation(state: RunState, conf: Confirmation, status: str, *, audit_reason: str) -> None:
    """落定一次确认：改状态、写审计、唤醒等待中的执行器。"""
    mark_confirmation(state, conf, status)
    audit.append(
        state.conn,
        "confirm.approved" if status == "approved" else "confirm.rejected",
        {"confirmation_id": conf.id, "node_id": conf.node_id, "reason": audit_reason},
        session_id=state.session_id,
    )
    resolve_pending(state, {"approve": status == "approved", "confirmation_id": conf.id})


def resolve_pending(state: RunState, answer: dict[str, Any]) -> None:
    """把答案递给所有在等的确认；已解决的不动。

    广播仍然安全，靠两条：
    1) 执行器是串行的，同一时刻只有一张卡在等（黄牌/红牌都是等完才走下一步）；
    2) resume() 对已落定的确认幂等返回，不会拿一张旧卡的答案来广播。
    """
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

