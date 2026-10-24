"""人工接管队列（handoff）：把卡住的一轮 DAG 交接给人，再由人决定是否恢复。

对应文档：docs/benchmark/air_vs_banking_agent.md D15、docs/提交物对照表.md。
赛题条款：3.3「执行过程中支持中断、回退和人工接管」+ 30% 安全合规评分项。

设计边界（为什么要这么写）：

1. 工单只搬运「已经落库的事实」：run_id / DAG / 已完成与失败节点 / 裁决 / 授权
   全部来自 dag_runs、dag_nodes、confirmations，不凭据内存里的 RUNS 重新编一个故事。
2. 恢复不重新裁决、不重放写工具：一律交给 app.planner.recover.resume 按原图继续，
   执行器会跳过 done/failed/skipped 节点，所以已经扣过的钱不会扣第二次。
3. 操作员不能代替用户点确认卡：恢复只驱动「用户已经批准」的节点，遇到还没人确认过的
   新卡就回到 need_info，等用户本人来点（这也是 resume 不接受 approve 参数的原因）。
4. 恢复失败必须留下 failed + note + handoff.resume_failed 审计，绝不把 run 标成功：
   executor._done 会在「有节点失败但后面还有节点跑完」时把 run 写成 done，
   这里用一次事后复核把这个谎纠回来。
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from typing import Any, Final

from app import audit, clock, db, funds
from app.clock import BankError, ValidationError
from app.ids import new_id
from app.planner.runner import MONEY_TOOLS

log = logging.getLogger(__name__)

# 活跃状态：同一轮只允许一张（靠 db.py 的部分唯一索引兜底）。终止态不再接单。
ACTIVE_STATUSES: Final[tuple[str, ...]] = ("pending", "claimed", "need_info")
TERMINAL_STATUSES: Final[frozenset[str]] = frozenset({"rejected", "cancelled", "resumed", "failed"})
# 这些原因缺的是「用户本人的决定」，操作员只能把单退回人，不能替他选收款人。
USER_DECISION_REASONS: Final[frozenset[str]] = frozenset({"PAYEE_AMBIGUOUS", "PAYEE_NOT_FOUND"})
# 恢复时「进程内只允许一个恢复在跑」：RUNS 本来就是进程内状态，多进程恢复没意义；
# 再加一层 DB 条件 UPDATE（expect=claimed/need_info）防住同进程内的并发点击。
RESUME_LOCK: Final[asyncio.Lock] = asyncio.Lock()
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
    account_id = account_id or _meta_account(conn, run)
    failure = {
        "reason": reason,
        "error_code": error_code or reason,
        "note": (note or "")[:NOTE_LIMIT],
        "needs_user_decision": reason in USER_DECISION_REASONS,
        "done_nodes": snapshot["done"],
        "failed_nodes": snapshot["failed"],
    }
    auth_doc = _auth_snapshot(conn, run_id, auth)
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
                    run_id,
                    session_id,
                    user_id or "",
                    account_id,
                    "pending",
                    reason,
                    error_code or reason,
                    request_text or "",
                    str(run["dag_json"] or "{}"),
                    json.dumps(snapshot["done"], ensure_ascii=False),
                    json.dumps(snapshot["failed"][0] if snapshot["failed"] else {}, ensure_ascii=False),
                    json.dumps(failure, ensure_ascii=False),
                    json.dumps(snapshot["decision"], ensure_ascii=False),
                    json.dumps(auth_doc, ensure_ascii=False),
                    "",
                    (note or "")[:NOTE_LIMIT],
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
                    "run_id": run_id,
                    "reason": reason,
                    "error_code": error_code or reason,
                    "done_nodes": len(snapshot["done"]),
                    "failed_nodes": len(snapshot["failed"]),
                    "needs_user_decision": failure["needs_user_decision"],
                    # 审计链不放用户原话：既有注入风险，也没有复核价值；只留长度供排查。
                    "request_chars": len(request_text or ""),
                    "operator": "",
                },
                session_id=session_id,
                task_id=run_id,
            )
    except sqlite3.IntegrityError:
        existing = conn.execute(
            "SELECT * FROM handoffs WHERE run_id = ? AND status IN ('pending','claimed','need_info')",
            (run_id,),
        ).fetchone()
        if existing is None:
            raise
        return {**_public(existing), "reused": True}
    row = conn.execute("SELECT * FROM handoffs WHERE id = ?", (handoff_id,)).fetchone()
    return {**_public(row), "reused": False}


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


async def cancel(
    conn: sqlite3.Connection,
    handoff_id: str,
    operator: str,
    reason: str = "",
) -> dict[str, Any]:
    """取消这张单，并把还卡着的那轮执行一起取消（释放 hold，别让余额悬着）。

    先关单再取消 run：即使取消 run 失败（会话早没了），工单状态也已经是对的，
    不会出现「队列里还挂着一张永远处理不完的单」。
    """
    operator = _operator(operator)
    row = conn.execute("SELECT * FROM handoffs WHERE id = ?", (handoff_id or "",)).fetchone()
    if row is None:
        raise HandoffNotFound(handoff_id=handoff_id or "")
    reason = (reason or "").strip() or "操作员取消了这张接管单"
    updated = _transition(
        conn,
        handoff_id,
        status="cancelled",
        operator=operator,
        note=reason,
        event="handoff.cancelled",
        payload={
            "handoff_id": handoff_id,
            "run_id": str(row["run_id"]),
            "operator": operator,
            "previous_status": str(row["status"]),
            "reason": reason[:NOTE_LIMIT],
        },
        expect=ACTIVE_STATUSES,
    )
    from app.planner import recover  # 延迟导入：recover → executor → runner，避免模块级环

    try:
        updated["run_cancel"] = await recover.cancel(str(row["session_id"]), reason=f"人工接管单取消：{reason}")
    except BankError as exc:  # 会话已经结束/轮次已终态：单已关掉就够了
        log.info("取消接管单时未能取消执行轮次 handoff=%s: %s", handoff_id, exc.code)
        updated["run_cancel"] = {"ok": False, "code": exc.code}
    return updated


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


# ------------------------------------------------------------------ 恢复


def _refuse(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    error: BankError,
    *,
    status: str,
    note: str,
    payload: dict[str, Any],
) -> BankError:
    """把单落成 failed / need_info 并留审计，然后把异常返回给调用方 raise。

    恢复失败一定要有明确状态：谁点过、因为什么没成、当时的 run 状态，
    都要能从 handoffs.note 和审计里查出来，而不是只在 HTTP 响应里飘一下。
    """
    event = "handoff.resume_failed" if status == "failed" else "handoff.need_info"
    _transition(
        conn,
        str(row["id"]),
        status=status,
        operator=str(row["operator"] or ""),
        note=note,
        event=event,
        payload={
            "handoff_id": str(row["id"]),
            "run_id": str(row["run_id"]),
            "session_id": str(row["session_id"]),
            "operator": str(row["operator"] or ""),
            "resume_status": status,
            **payload,
        },
        expect=("claimed", "need_info"),
    )
    return error


def _expire_stale_cards(conn: sqlite3.Connection, row: sqlite3.Row, now: int) -> int:
    """把这张单对应轮次里已经过期的确认卡就地作废并审计，返回作废张数。

    pending 卡直接置 expired；approved 卡保留「用户确实批准过」的历史，
    但审计里记一条 confirm.expired（status_before=approved）——
    恢复不承认过期批准，否则等于把一次几分钟有效的授权无限延长。
    """
    cards = conn.execute(
        "SELECT id, node_id, status, expires_ms FROM confirmations"
        " WHERE run_id = ? AND status IN ('pending','approved')",
        (str(row["run_id"]),),
    ).fetchall()
    stale = [c for c in cards if int(c["expires_ms"]) <= now]
    if not stale:
        return 0
    with db.tx(conn):
        for card in stale:
            if card["status"] == "pending":
                conn.execute(
                    "UPDATE confirmations SET status = 'expired', resolved_ms = ?"
                    " WHERE id = ? AND status = 'pending'",
                    (now, card["id"]),
                )
            audit.append(
                conn,
                "confirm.expired",
                {
                    "confirmation_id": card["id"],
                    "node_id": card["node_id"],
                    "status_before": card["status"],
                    "expires_ms": int(card["expires_ms"]),
                    "reason": "HANDOFF_RESUME_EXPIRED",
                },
                session_id=str(row["session_id"]),
                task_id=str(row["run_id"]),
            )
    return len(stale)


def _auth_stale(conn: sqlite3.Connection, auth: dict[str, Any], now: int) -> str | None:
    """检查授权快照还有效吗；无效返回原因码，有效返回 None。

    目前接的是计划授权（plan_authorizations）：恢复前必须重查库里的实时状态，
    不能拿建单时的快照当免死金牌。快照里的 inline expires_ms 也要过一遍，
    因为建单时可能引用了一条后来被撤销的授权。
    """
    inline = auth.get("plan_authorization")
    if isinstance(inline, dict) and inline.get("expires_ms") is not None:
        if int(inline["expires_ms"]) <= now:
            return "PLAN_AUTHORIZATION_EXPIRED"
    ids = auth.get("plan_authorization_ids") or []
    if isinstance(ids, str):
        ids = [ids]
    for pa_id in ids:
        pa = conn.execute(
            "SELECT status, expires_ms FROM plan_authorizations WHERE id = ?", (str(pa_id),)
        ).fetchone()
        if pa is None:
            return "PLAN_AUTHORIZATION_MISSING"
        if str(pa["status"]) != "active":
            return f"PLAN_AUTHORIZATION_{str(pa['status']).upper()}"
        if int(pa["expires_ms"]) <= now:
            return "PLAN_AUTHORIZATION_EXPIRED"
    return None


def _pending_money_fen(conn: sqlite3.Connection, run_id: str) -> int:
    """还没执行的动钱节点一共要花多少（只认字面量金额）。

    参数里是 @n1.xxx 这种引用时算不出来，跳过：那种节点要么上游没做完、要么
    真跑起来由工具层自己校验余额，这里只做「明显不够就别恢复」的粗筛。
    """
    rows = conn.execute(
        "SELECT tool, params_json FROM dag_nodes"
        " WHERE run_id = ? AND state IN ('pending','running','waiting_confirm','waiting_factor')",
        (run_id,),
    ).fetchall()
    total = 0
    for row in rows:
        if row["tool"] not in MONEY_TOOLS:
            continue
        params = _decode(row["params_json"], {})
        amount = params.get("amount_fen") if isinstance(params, dict) else None
        if isinstance(amount, bool) or not isinstance(amount, int) or amount <= 0:
            continue
        total += amount
    return total


def _failed_nodes(conn: sqlite3.Connection, run_id: str) -> list[str]:
    rows = conn.execute(
        "SELECT node_id FROM dag_nodes WHERE run_id = ? AND state = 'failed' ORDER BY rowid",
        (run_id,),
    ).fetchall()
    return [str(r["node_id"]) for r in rows]


async def resume(
    conn: sqlite3.Connection,
    handoff_id: str,
    *,
    operator: str,
    note: str = "",
) -> dict[str, Any]:
    """操作员触发恢复：先按实时业务状态复核，再走原 DAG 续跑。

    复核顺序是有讲究的（任何一条不过就地关单，不 drive）：
    1. 轮次还在、且就是这单对应的那一轮（不能拿旧单去驱动新轮次）；
    2. 过期确认卡就地作废（钱不能凭过期批准动）；
    3. 授权（计划授权等）仍在有效期；
    4. 账户仍存在且可用；
    5. 没执行完的动钱节点金额 <= 可动用余额；
    6. 已经 failed 的节点不重跑，也不许把这一轮标成成功；
    7. 缺用户本人决定（收款人歧义）的单退回 need_info，操作员不能替用户选。

    全部通过后只用 recover.resume 驱动——执行器自己跳过 done/failed/skipped，
    所以「已完成节点不重复执行」不是靠本函数的小心，而是靠执行器的终态跳过。
    """
    operator = _operator(operator)
    async with RESUME_LOCK:
        row = conn.execute("SELECT * FROM handoffs WHERE id = ?", (handoff_id or "",)).fetchone()
        if row is None:
            raise HandoffNotFound(handoff_id=handoff_id or "")
        status = str(row["status"])
        if status == "pending":
            raise HandoffInvalidState(handoff_id=handoff_id, status=status, hint="先领取再恢复")
        if status in TERMINAL_STATUSES or status not in ("claimed", "need_info"):
            raise HandoffInvalidState(handoff_id=handoff_id, status=status)
        if str(row["operator"] or "") and str(row["operator"]) != operator:
            # 换人处理要重新走一次领取语义：不允许两个操作员对同一单各推一把。
            raise HandoffAlreadyClaimed(handoff_id=handoff_id, operator=str(row["operator"]))

        now = clock.now_ms()
        run = conn.execute("SELECT * FROM dag_runs WHERE id = ?", (str(row["run_id"]),)).fetchone()
        if run is None or str(run["session_id"]) != str(row["session_id"]):
            raise _refuse(
                conn,
                row,
                HandoffRunMismatch(handoff_id=handoff_id, run_id=str(row["run_id"])),
                status="failed",
                note="找不到这张单对应的执行轮次，无法恢复。",
                payload={"reason_code": "RUN_MISSING"},
            )
        newer = conn.execute(
            "SELECT id FROM dag_runs WHERE session_id = ? AND status IN ('running','waiting')"
            " ORDER BY created_ms DESC",
            (str(row["session_id"]),),
        ).fetchall()
        if newer and str(newer[0]["id"]) != str(row["run_id"]):
            raise _refuse(
                conn,
                row,
                HandoffRunMismatch(
                    handoff_id=handoff_id, run_id=str(row["run_id"]), active_run=str(newer[0]["id"])
                ),
                status="failed",
                note="这个会话已经有更新的一轮在执行，恢复旧轮会动错账。",
                payload={"reason_code": "NEWER_RUN_ACTIVE", "active_run": str(newer[0]["id"])},
            )

        # 过期确认卡：就地作废 + 审计，然后拒绝恢复（钱不能凭过期批准动）
        expired = _expire_stale_cards(conn, row, now)
        if expired:
            raise _refuse(
                conn,
                row,
                HandoffConfirmExpired(handoff_id=handoff_id, expired=expired),
                status="failed",
                note=f"有 {expired} 张确认卡已经过期，已就地作废；需要用户重新发起并确认。",
                payload={"reason_code": "CONFIRM_EXPIRED", "expired_cards": expired},
            )

        auth = _decode(row["auth_json"], {})
        auth_reason = _auth_stale(conn, auth, now)
        if auth_reason:
            raise _refuse(
                conn,
                row,
                HandoffAuthExpired(handoff_id=handoff_id, reason_code=auth_reason),
                status="failed",
                note="这笔操作的授权已经失效，恢复前必须让用户重新授权。",
                payload={"reason_code": auth_reason},
            )

        account_id = str(row["account_id"] or _meta_account(conn, run))
        account = (
            conn.execute(
                "SELECT id, status, balance_fen FROM accounts WHERE id = ?", (account_id,)
            ).fetchone()
            if account_id
            else None
        )
        if account is None or str(account["status"]) != "active":
            raise _refuse(
                conn,
                row,
                HandoffAccountUnavailable(handoff_id=handoff_id, account_id=account_id),
                status="failed",
                note="这张单对应的账户已经不存在或不可用，不能在这个账户上恢复执行。",
                payload={"reason_code": "ACCOUNT_UNAVAILABLE", "account_id": account_id},
            )

        need_fen = _pending_money_fen(conn, str(row["run_id"]))
        if need_fen:
            available = funds.available_fen(conn, account_id)
            if available < need_fen:
                raise _refuse(
                    conn,
                    row,
                    HandoffInsufficientFunds(
                        handoff_id=handoff_id,
                        need_fen=need_fen,
                        available_fen=available,
                    ),
                    status="failed",
                    note=f"可动用余额不够：还需要 {need_fen} 分，只有 {available} 分。",
                    payload={
                        "reason_code": "INSUFFICIENT_FUNDS",
                        "need_fen": need_fen,
                        "available_fen": available,
                    },
                )

        failed_before = _failed_nodes(conn, str(row["run_id"]))
        if failed_before:
            conn.execute(
                "UPDATE dag_runs SET status = 'failed', updated_ms = ? WHERE id = ?",
                (clock.now_ms(), str(row["run_id"])),
            )
            raise _refuse(
                conn,
                row,
                HandoffResumeFailed(handoff_id=handoff_id, failed_nodes=failed_before),
                status="failed",
                note="这一轮已经有节点失败（" + "、".join(failed_before) + "），失败节点不会重跑，不能当成能恢复。",
                payload={"reason_code": "NODE_FAILED", "failed_nodes": failed_before},
            )

        failure = _decode(row["failure_json"], {})
        if str(row["reason"]) in USER_DECISION_REASONS or failure.get("needs_user_decision"):
            raise _refuse(
                conn,
                row,
                HandoffNeedsUserDecision(handoff_id=handoff_id, reason=str(row["reason"])),
                status="need_info",
                note="收款人这一项缺的是用户本人的选择，请让用户明确付给哪一位后再恢复。",
                payload={"reason_code": "USER_DECISION_REQUIRED", "reason": str(row["reason"])},
            )

        run_status = str(run["status"])
        if run_status == "cancelled":
            raise _refuse(
                conn,
                row,
                HandoffInvalidState(handoff_id=handoff_id, status=status, run_status=run_status),
                status="cancelled",
                note="这一轮执行已经被取消，工单随之关闭。",
                payload={"reason_code": "RUN_CANCELLED", "run_status": run_status},
            )
        if run_status == "failed":
            raise _refuse(
                conn,
                row,
                HandoffResumeFailed(handoff_id=handoff_id, run_status=run_status),
                status="failed",
                note="这一轮执行已经失败，没有可继续的节点。",
                payload={"reason_code": "RUN_FAILED", "run_status": run_status},
            )
        if run_status == "done":
            updated = _transition(
                conn,
                handoff_id,
                status="resumed",
                operator=operator,
                note=(note or "这一轮已经执行完毕，恢复只需确认没有遗留问题。"),
                event="handoff.resumed",
                payload={
                    "handoff_id": handoff_id,
                    "run_id": str(row["run_id"]),
                    "session_id": str(row["session_id"]),
                    "operator": operator,
                    "run_status": run_status,
                    "replayed": False,
                },
                expect=("claimed", "need_info"),
            )
            return {"ok": True, "status": "resumed", "handoff": updated, "run_status": run_status, "replayed": False}

        from app.planner import recover  # 延迟导入：recover → executor → runner，避免模块级环

        try:
            outcome = await recover.resume(str(row["session_id"]))
        except ValidationError as exc:
            raise _refuse(
                conn,
                row,
                HandoffResumeFailed(handoff_id=handoff_id, detail=str(exc.detail)),
                status="failed",
                note="恢复时重新装配执行状态失败：" + (str(exc.detail) or "会话已结束或不存在"),
                payload={"reason_code": "REHYDRATE_FAILED", "detail": str(exc.detail)},
            )

        run_after = conn.execute(
            "SELECT status FROM dag_runs WHERE id = ?", (str(row["run_id"]),)
        ).fetchone()
        run_status_after = str(run_after["status"]) if run_after is not None else "missing"
        failed_after = _failed_nodes(conn, str(row["run_id"]))
        if failed_after or run_status_after == "failed":
            conn.execute(
                "UPDATE dag_runs SET status = 'failed', updated_ms = ? WHERE id = ?",
                (clock.now_ms(), str(row["run_id"])),
            )
            raise _refuse(
                conn,
                row,
                HandoffResumeFailed(
                    handoff_id=handoff_id, failed_nodes=failed_after, run_status=run_status_after
                ),
                status="failed",
                note="恢复后仍有节点处于失败状态，已把这一轮改回 failed，绝不当作成功。",
                payload={
                    "reason_code": "RESUME_LEFT_FAILED_NODES",
                    "failed_nodes": failed_after,
                    "run_status": run_status_after,
                },
            )

        pending_cards = conn.execute(
            "SELECT COUNT(*) FROM confirmations WHERE run_id = ? AND status = 'pending' AND expires_ms > ?",
            (str(row["run_id"]), clock.now_ms()),
        ).fetchone()[0]
        if run_status_after != "done" or pending_cards:
            updated = _transition(
                conn,
                handoff_id,
                status="need_info",
                operator=operator,
                note=(note + "；" if note else "")
                + "恢复时还有待用户本人决定的确认或追问，操作员不能代点，处理完再恢复。",
                event="handoff.need_info",
                payload={
                    "handoff_id": handoff_id,
                    "run_id": str(row["run_id"]),
                    "session_id": str(row["session_id"]),
                    "operator": operator,
                    "run_status": run_status_after,
                    "pending_confirmations": int(pending_cards),
                    "reason": "WAITING_USER",
                },
                expect=("claimed", "need_info"),
            )
            return {
                "ok": True,
                "status": "need_info",
                "handoff": updated,
                "run_status": run_status_after,
                "events": list(outcome.get("events") or []),
            }

        updated = _transition(
            conn,
            handoff_id,
            status="resumed",
            operator=operator,
            note=note or "恢复完成，剩余节点已按原 DAG 跑完。",
            event="handoff.resumed",
            payload={
                "handoff_id": handoff_id,
                "run_id": str(row["run_id"]),
                "session_id": str(row["session_id"]),
                "operator": operator,
                "run_status": run_status_after,
                "replayed": bool(outcome.get("recovered")),
            },
            expect=("claimed", "need_info"),
        )
        return {
            "ok": True,
            "status": "resumed",
            "handoff": updated,
            "run_status": run_status_after,
            "events": list(outcome.get("events") or []),
        }


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
    "HandoffResumeFailed",
    "HandoffRunMismatch",
    "TERMINAL_STATUSES",
    "cancel",
    "claim",
    "close_for_run",
    "create",
    "get",
    "list_handoffs",
    "need_info",
    "reject",
    "resume",
]
