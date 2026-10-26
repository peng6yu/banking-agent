"""人工接管单的仓储与状态机（第 2 层）：只读写 handoffs 表与它引用的执行事实。

**为什么单独一层**：接管单有两条使用线——一条是「操作员领取/拒绝/要求补充信息」
（纯行级状态机 + 审计），另一条是「恢复执行/取消轮次」（要驱动执行器与恢复器，
第 5 层）。planner 在跑偏时要建单（create）、轮次被取代时要关单（close_for_run），
它不能 import 第 5 层，所以行级能力必须落在这一层。

这里只有三件事：读（get / list_handoffs / _public，把 dag_json、decision_json 解成对象）；
写状态（create / claim / reject / need_info / close_for_run，一律经 _transition：条件
UPDATE + 审计同一事务，同一个单不会被两条路径同时改）；快照（_run / _meta_account /
_snapshot / _auth_snapshot 只把库里已有的事实拼成一份可读记录，不重新裁决——裁决与
执行属于 planner 层）。

依赖方向：只认识 audit/clock/db/ids，不认识 planner 与 api；恢复取消在 handoffs.py。
"""

from __future__ import annotations

import json
import logging
import sqlite3
from typing import Any, Final

from app import audit, clock, db
from app.clock import ValidationError
from app.ids import new_id

log = logging.getLogger(__name__)


# 活跃状态：同一轮只允许一张（靠 db.py 的部分唯一索引兜底）。终止态不再接单。
ACTIVE_STATUSES: Final[tuple[str, ...]] = ("pending", "claimed", "need_info")
TERMINAL_STATUSES: Final[frozenset[str]] = frozenset({"rejected", "cancelled", "resumed", "failed"})
# 这些原因缺的是「用户本人的决定」，操作员只能把单退回人，不能替他选收款人。
USER_DECISION_REASONS: Final[frozenset[str]] = frozenset({"PAYEE_AMBIGUOUS", "PAYEE_NOT_FOUND"})
MAX_LIST: Final[int] = 100
NOTE_LIMIT: Final[int] = 500


# ------------------------------------------------------------------ 错误类型
# 每个 code 都是固定中文话术：操作员看的是「下一步该干什么」，不是英文常量。


class HandoffNotFound(ValidationError):
    code = "HANDOFF_NOT_FOUND"
    user_text = "这张接管单不存在，可能已经被处理过，或者编号不对。"


class HandoffAlreadyClaimed(ValidationError):
    code = "HANDOFF_ALREADY_CLAIMED"
    user_text = "这张接管单已经被其他操作员领走了，换一张处理吧。"


class HandoffInvalidState(ValidationError):
    code = "HANDOFF_INVALID_STATE"
    user_text = "这张接管单当前的状态不允许这个操作。"


class HandoffNeedsUserDecision(ValidationError):
    code = "HANDOFF_NEEDS_USER_DECISION"
    user_text = "这单缺的是用户本人的决定，操作员不能替他做。"


class HandoffConfirmExpired(ValidationError):
    code = "HANDOFF_CONFIRM_EXPIRED"
    user_text = "用户那次确认已经过期了，现在恢复会把钱直接执行出去，我不做。"


class HandoffAuthExpired(ValidationError):
    code = "HANDOFF_AUTH_EXPIRED"
    user_text = "这笔操作的授权已经失效，需要用户重新授权后才能恢复。"


class HandoffInsufficientFunds(ValidationError):
    code = "HANDOFF_INSUFFICIENT_FUNDS"
    http_status = 409
    user_text = "账户里可动用的钱不够这一笔了，恢复会失败，先把资金问题告诉用户。"


class HandoffAccountUnavailable(ValidationError):
    code = "HANDOFF_ACCOUNT_UNAVAILABLE"
    http_status = 409
    user_text = "这笔操作对应的账户已经不可用，不能在这个账户上恢复执行。"


class HandoffRunMismatch(ValidationError):
    code = "HANDOFF_RUN_MISMATCH"
    user_text = "这张接管单和当前那轮执行对不上，贸然恢复可能动错账。"


class HandoffResumeFailed(ValidationError):
    code = "HANDOFF_RESUME_FAILED"
    user_text = "恢复没有成功，工单已经记成失败，原因写在备注里。"


class HandoffOperatorRequired(ValidationError):
    code = "HANDOFF_OPERATOR_REQUIRED"
    user_text = "接管单上的每个动作都要写明是谁操作的。"


class HandoffNoteRequired(ValidationError):
    code = "HANDOFF_NOTE_REQUIRED"
    user_text = "要求补充信息时必须写清楚缺什么，否则等于把单原样扔回队列。"


# ------------------------------------------------------------------ 小工具


def _decode(raw: object, default: Any) -> Any:
    """把库里的 JSON 文本还原成对象；坏数据返回默认值，不让一张脏单炸掉列表页。"""
    if raw is None or raw == "":
        return default
    if isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(str(raw))
    except (TypeError, ValueError):
        return default


def _public(row: sqlite3.Row) -> dict[str, Any]:
    """对外的工单结构：JSON 列解析成对象，操作员不用自己再反序列化一次。"""
    data = dict(row)
    data["dag"] = _decode(data.pop("dag_json", "{}"), {})
    data["done_nodes"] = _decode(data.pop("done_nodes_json", "[]"), [])
    data["failed_node"] = _decode(data.pop("failed_node_json", "{}"), {})
    data["failure"] = _decode(data.pop("failure_json", "{}"), {})
    data["decision"] = _decode(data.pop("decision_json", "{}"), {})
    data["auth"] = _decode(data.pop("auth_json", "{}"), {})
    return data


def get(conn: sqlite3.Connection, handoff_id: str) -> dict[str, Any]:
    """按 id 读一张单；读不到就抛 HANDOFF_NOT_FOUND（不返回 None）。"""
    row = conn.execute("SELECT * FROM handoffs WHERE id = ?", (handoff_id or "",)).fetchone()
    if row is None:
        raise HandoffNotFound(handoff_id=handoff_id or "")
    return _public(row)


def list_handoffs(
    conn: sqlite3.Connection,
    *,
    status: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """按状态列出工单（新单在前）。status 为空表示不过滤。"""
    limit = max(1, min(int(limit or 50), MAX_LIST))
    if status is not None and status not in (*ACTIVE_STATUSES, *TERMINAL_STATUSES):
        raise ValidationError("未知的接管单状态", status=status)
    if status is None:
        rows = conn.execute(
            "SELECT * FROM handoffs ORDER BY created_ms DESC, rowid DESC LIMIT ?", (limit,)
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM handoffs WHERE status = ? ORDER BY created_ms DESC, rowid DESC LIMIT ?",
            (status, limit),
        ).fetchall()
    return [_public(row) for row in rows]


def _run(conn: sqlite3.Connection, run_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM dag_runs WHERE id = ?", (run_id,)).fetchone()
    if row is None:
        raise HandoffRunMismatch(run_id=run_id)
    return row


def _meta_account(conn: sqlite3.Connection, run: sqlite3.Row) -> str:
    meta = _decode(run["meta_json"], {})
    return str(meta.get("account_id") or "")


def _snapshot(conn: sqlite3.Connection, run_id: str) -> dict[str, Any]:
    """从库里的节点行拍一张快照：哪些做完了、第一个失败的是谁、各自什么裁决。"""
    rows = conn.execute(
        "SELECT node_id, tool, state, decision_json FROM dag_nodes WHERE run_id = ? ORDER BY rowid",
        (run_id,),
    ).fetchall()
    done = [{"node_id": r["node_id"], "tool": r["tool"]} for r in rows if r["state"] == "done"]
    failed = [
        {"node_id": r["node_id"], "tool": r["tool"]}
        for r in rows
        if r["state"] == "failed"
    ]
    decision: dict[str, Any] = {}
    for r in rows:
        if r["state"] == "failed":
            decision = _decode(r["decision_json"], {})
            break
    return {"done": done, "failed": failed, "decision": decision}


def _auth_snapshot(conn: sqlite3.Connection, run_id: str, extra: dict[str, Any] | None) -> dict[str, Any]:
    """授权快照：只记「当时有哪些确认卡/计划授权」，恢复时一律以库里的实时状态为准。

    reusable_write_token 恒为 False：恢复必然重新裁决并签发新令牌，绝不复用旧写令牌——
    旧令牌要么过期，要么参数哈希对不上，复用它等于绕过令牌的防重放设计。
    """
    cards = conn.execute(
        "SELECT id, node_id, status, expires_ms FROM confirmations"
        " WHERE run_id = ? AND status IN ('pending','approved') ORDER BY created_ms",
        (run_id,),
    ).fetchall()
    snap: dict[str, Any] = {
        "pending_confirmations": [
            {
                "confirmation_id": c["id"],
                "node_id": c["node_id"],
                "status": c["status"],
                "expires_ms": int(c["expires_ms"]),
            }
            for c in cards
        ],
        "plan_authorization_ids": [],
        "reusable_write_token": False,
    }
    if extra:
        snap.update(extra)
    return snap


def _prepare(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    session_id: str,
    reason: str,
    error_code: str,
    note: str,
    account_id: str,
    auth: dict[str, Any] | None,
) -> dict[str, Any]:
    """建单前的全部读入与校验：会话对齐、节点快照、失败事实、授权快照。

    只读，不写库。校验失败就抛（ValidationError / HandoffRunMismatch），
    所以下面的 _insert_row 只管插行 + 审计，不必再重复检查。
    """
    reason = (reason or "").strip()
    if not reason:
        raise ValidationError("接管单必须写明转人工的原因")
    run = _run(conn, run_id)
    run_session = str(run["session_id"] or "")
    if session_id and run_session and session_id != run_session:
        raise HandoffRunMismatch(run_id=run_id, session_id=session_id, run_session=run_session)
    session_id = run_session or session_id
    if not session_id:
        raise ValidationError("接管单缺少会话标识", run_id=run_id)
    snapshot = _snapshot(conn, run_id)
    failure = {
        "reason": reason,
        "error_code": error_code or reason,
        "note": (note or "")[:NOTE_LIMIT],
        "needs_user_decision": reason in USER_DECISION_REASONS,
        "done_nodes": snapshot["done"],
        "failed_nodes": snapshot["failed"],
    }
    return {
        "run": run,
        "session_id": session_id,
        "snapshot": snapshot,
        "failure": failure,
        "account_id": account_id or _meta_account(conn, run),
        "auth_doc": _auth_snapshot(conn, run_id, auth),
        "reason": reason,
        "error_code": error_code or reason,
        "note": (note or "")[:NOTE_LIMIT],
    }


def _insert_row(
    conn: sqlite3.Connection, plan: dict[str, Any], *, user_id: str, request_text: str
) -> tuple[sqlite3.Row, bool]:
    """插一张 pending 单 + 一条审计；撞部分唯一索引就说明并发里有人先建了。

    返回 (行, 是否复用)。幂等不是靠先查后插（那有并发窗口），而是靠 db.py 的
    部分唯一索引 idx_handoffs_active_run：INSERT 撞索引 → 读回原单。
    """
    run = plan["run"]
    snapshot = plan["snapshot"]
    failure = plan["failure"]
    session_id = plan["session_id"]
    reason = plan["reason"]
    error_code = plan["error_code"]
    now = clock.now_ms()
    handoff_id = new_id("ho")
    try:
        with db.tx(conn):
            conn.execute(
                "INSERT INTO handoffs ("
                " id, run_id, session_id, user_id, account_id, status, reason, error_code,"
                " request_text, dag_json, done_nodes_json, failed_node_json, failure_json,"
                " decision_json, auth_json, operator, note, claimed_ms, created_ms, updated_ms"
                ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    handoff_id,
                    str(run["id"]),
                    session_id,
                    user_id or "",
                    plan["account_id"],
                    "pending",
                    reason,
                    error_code,
                    request_text or "",
                    str(run["dag_json"] or "{}"),
                    json.dumps(snapshot["done"], ensure_ascii=False),
                    json.dumps(snapshot["failed"][0] if snapshot["failed"] else {}, ensure_ascii=False),
                    json.dumps(failure, ensure_ascii=False),
                    json.dumps(snapshot["decision"], ensure_ascii=False),
                    json.dumps(plan["auth_doc"], ensure_ascii=False),
                    "",
                    plan["note"],
                    0,
                    now,
                    now,
                ),
            )
            audit.append(
                conn,
                "handoff.created",
                {
                    "handoff_id": handoff_id,
                    "run_id": str(run["id"]),
                    "reason": reason,
                    "error_code": error_code,
                    "done_nodes": len(snapshot["done"]),
                    "failed_nodes": len(snapshot["failed"]),
                    "needs_user_decision": failure["needs_user_decision"],
                    # 审计链不放用户原话：既有注入风险，也没有复核价值；只留长度供排查。
                    "request_chars": len(request_text or ""),
                    "operator": "",
                },
                session_id=session_id,
                task_id=str(run["id"]),
            )
    except sqlite3.IntegrityError:
        existing = conn.execute(
            "SELECT * FROM handoffs WHERE run_id = ? AND status IN ('pending','claimed','need_info')",
            (str(run["id"]),),
        ).fetchone()
        if existing is None:
            raise
        return existing, True
    row = conn.execute("SELECT * FROM handoffs WHERE id = ?", (handoff_id,)).fetchone()
    return row, False


def create(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    session_id: str = "",
    reason: str,
    error_code: str = "",
    request_text: str = "",
    account_id: str = "",
    user_id: str = "",
    note: str = "",
    auth: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """建一张 pending 工单；同一轮已有活跃单时返回原单（幂等）。

    幂等不是靠先查后插（那有并发窗口），而是靠 db.py 的部分唯一索引
    idx_handoffs_active_run：插入撞索引就说明别人先建了，读回原单返回即可。
    所以在 payee 歧义这种「同一轮可能被追问多次」的场景里，重复建单不会灌出一堆单。
    """
    plan = _prepare(
        conn,
        run_id=run_id,
        session_id=session_id,
        reason=reason,
        error_code=error_code,
        note=note,
        account_id=account_id,
        auth=auth,
    )
    row, reused = _insert_row(conn, plan, user_id=user_id, request_text=request_text)
    return {**_public(row), "reused": reused}


# ------------------------------------------------------------------ 操作员动作
# 每个动作 = 一次条件 UPDATE（顺带做并发去重）+ 一条审计。返回更新后的工单。


def _operator(value: str) -> str:
    operator = (value or "").strip()
    if not operator:
        raise HandoffOperatorRequired()
    return operator


def _transition(
    conn: sqlite3.Connection,
    handoff_id: str,
    *,
    status: str,
    operator: str,
    note: str,
    event: str,
    payload: dict[str, Any],
    expect: tuple[str, ...],
    claimed_ms: int | None = None,
) -> dict[str, Any]:
    """条件 UPDATE 到目标状态；目标状态不再成立时抛 INVALID_STATE。

    expect 里的状态集合就是「这一步允许从哪些状态出发」，和 SQL 的 WHERE 一起
    起到乐观锁作用：两个操作员同时点同一张单，只有一个 rowcount=1。
    """
    now = clock.now_ms()
    marks = ",".join("?" for _ in expect)
    sets = "status = ?, operator = ?, note = ?, updated_ms = ?"
    params: list[Any] = [status, operator, note[:NOTE_LIMIT], now]
    if claimed_ms is not None:
        sets += ", claimed_ms = ?"
        params.append(claimed_ms)
    with db.tx(conn):
        cur = conn.execute(
            f"UPDATE handoffs SET {sets} WHERE id = ? AND status IN ({marks})",
            (*params, handoff_id, *expect),
        )
        if cur.rowcount == 0:
            row = conn.execute("SELECT status FROM handoffs WHERE id = ?", (handoff_id,)).fetchone()
            if row is None:
                raise HandoffNotFound(handoff_id=handoff_id)
            raise HandoffInvalidState(handoff_id=handoff_id, status=str(row["status"]), want=status)
        row = conn.execute("SELECT * FROM handoffs WHERE id = ?", (handoff_id,)).fetchone()
        audit.append(conn, event, payload, session_id=str(row["session_id"]), task_id=str(row["run_id"]))
    return _public(row)


def claim(conn: sqlite3.Connection, handoff_id: str, operator: str) -> dict[str, Any]:
    """领取：只有 pending 能被领走；同一张单并发领取只有一个成功。

    UPDATE ... WHERE id=? AND status='pending' 的 rowcount 就是裁决书：
    另一个操作员哪怕在同一个毫秒点下去，也只会拿到 HANDOFF_ALREADY_CLAIMED。
    """
    operator = _operator(operator)
    row = conn.execute("SELECT * FROM handoffs WHERE id = ?", (handoff_id or "",)).fetchone()
    if row is None:
        raise HandoffNotFound(handoff_id=handoff_id or "")
    now = clock.now_ms()
    with db.tx(conn):
        cur = conn.execute(
            "UPDATE handoffs SET status = 'claimed', operator = ?, claimed_ms = ?, updated_ms = ?"
            " WHERE id = ? AND status = 'pending'",
            (operator, now, now, handoff_id),
        )
        if cur.rowcount == 0:
            fresh = conn.execute("SELECT * FROM handoffs WHERE id = ?", (handoff_id,)).fetchone()
            if fresh is None:
                raise HandoffNotFound(handoff_id=handoff_id)
            raise HandoffAlreadyClaimed(
                handoff_id=handoff_id,
                status=str(fresh["status"]),
                operator=str(fresh["operator"] or ""),
            )
        audit.append(
            conn,
            "handoff.claimed",
            {
                "handoff_id": handoff_id,
                "run_id": str(row["run_id"]),
                "operator": operator,
                "claimed_ms": now,
                "previous_status": str(row["status"]),
            },
            session_id=str(row["session_id"]),
            task_id=str(row["run_id"]),
        )
    return get(conn, handoff_id)


def reject(conn: sqlite3.Connection, handoff_id: str, operator: str, note: str = "") -> dict[str, Any]:
    """拒绝：从任何活跃状态都能拒（包括 need_info 的单）。

    拒绝不恢复执行、也不动钱；只是把这张单关掉，让队列不再显示它。
    """
    operator = _operator(operator)
    row = conn.execute("SELECT * FROM handoffs WHERE id = ?", (handoff_id or "",)).fetchone()
    if row is None:
        raise HandoffNotFound(handoff_id=handoff_id or "")
    return _transition(
        conn,
        handoff_id,
        status="rejected",
        operator=operator,
        note=note or "操作员拒绝了这张接管单",
        event="handoff.rejected",
        payload={
            "handoff_id": handoff_id,
            "run_id": str(row["run_id"]),
            "operator": operator,
            "previous_status": str(row["status"]),
            "note": (note or "")[:NOTE_LIMIT],
        },
        expect=ACTIVE_STATUSES,
    )


def need_info(conn: sqlite3.Connection, handoff_id: str, operator: str, note: str) -> dict[str, Any]:
    """要求补充信息：note 必填——说不清缺什么，等于把单原样扔回队列。"""
    operator = _operator(operator)
    note = (note or "").strip()
    if not note:
        raise HandoffNoteRequired(handoff_id=handoff_id or "")
    row = conn.execute("SELECT * FROM handoffs WHERE id = ?", (handoff_id or "",)).fetchone()
    if row is None:
        raise HandoffNotFound(handoff_id=handoff_id or "")
    return _transition(
        conn,
        handoff_id,
        status="need_info",
        operator=operator,
        note=note,
        event="handoff.need_info",
        payload={
            "handoff_id": handoff_id,
            "run_id": str(row["run_id"]),
            "operator": operator,
            "previous_status": str(row["status"]),
            "note": note[:NOTE_LIMIT],
            "reason": "OPERATOR_NEEDS_INFO",
        },
        expect=("pending", "claimed", "need_info"),
    )


def close_for_run(
    conn: sqlite3.Connection,
    run_id: str,
    *,
    reason: str,
    operator: str = "system",
) -> list[str]:
    """把挂在这一轮上的活跃接管单收掉（同步；不代替调用方取消 run）。

    用在「用户已经回答了澄清问题、那一轮按被取代关闭」的路径上：
    问题既然答完了，队列里就不该再留一张等人处理的旧单——否则操作员领取的
    是一轮已经结束、run 都关掉的工单，领取动作本身就没有意义。
    只动 handoff，不动 dag_runs / holds：关轮次与释放资源是调用方的事。
    """
    reason = (reason or "").strip() or "这一轮已经被取代关闭"
    rows = conn.execute(
        "SELECT id, status FROM handoffs WHERE run_id = ? AND status IN (?, ?, ?)",
        (run_id, *ACTIVE_STATUSES),
    ).fetchall()
    closed: list[str] = []
    for row in rows:
        handoff_id = str(row["id"])
        try:
            _transition(
                conn,
                handoff_id,
                status="cancelled",
                operator=operator,
                note=reason,
                event="handoff.cancelled",
                payload={
                    "handoff_id": handoff_id,
                    "run_id": run_id,
                    "operator": operator,
                    "previous_status": str(row["status"]),
                    "reason": reason[:NOTE_LIMIT],
                    "closed_by": "run.superseded",
                },
                expect=ACTIVE_STATUSES,
            )
        except (HandoffInvalidState, HandoffNotFound):
            # 并发里别的操作员刚好把它推走了：状态已经不由我们决定，跳过即可。
            continue
        closed.append(handoff_id)
    return closed


__all__ = [
    "ACTIVE_STATUSES",
    "HandoffAccountUnavailable",
    "HandoffAlreadyClaimed",
    "HandoffAuthExpired",
    "HandoffConfirmExpired",
    "HandoffInsufficientFunds",
    "HandoffInvalidState",
    "HandoffNeedsUserDecision",
    "HandoffNotFound",
    "HandoffNoteRequired",
    "HandoffOperatorRequired",
    "HandoffRunMismatch",
    "MAX_LIST",
    "NOTE_LIMIT",
    "TERMINAL_STATUSES",
    "USER_DECISION_REASONS",
    "claim",
    "close_for_run",
    "create",
    "get",
    "list_handoffs",
    "need_info",
    "reject",
]
