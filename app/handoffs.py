"""人工接管队列（handoff）：把卡住的一轮 DAG 交接给人，再由人决定是否恢复。

对应文档：docs/benchmark/air_vs_banking_agent.md D15、docs/提交物对照表.md。
赛题条款：3.3「执行过程中支持中断、回退和人工接管」+ 30% 安全合规评分项。

**本模块只负责「要动执行器的那一半」**：恢复（resume）、取消（cancel）以及恢复前的
三种体检（确认卡过期 / 令牌过期 / 余额与失败节点）。工单本身的读写、领取/拒绝/
要求补充信息在 app/handoff_store.py（第 2 层），这样 planner 建单/关单不必反向
import 第 5 层；下面把那些名字原样再导出，调用方仍可用 from app import handoffs
拿到全部能力。

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
import logging
import sqlite3
from typing import Any, Final

from app import audit, clock, db, funds
from app.clock import BankError, ValidationError
from app.handoff_store import (
    ACTIVE_STATUSES,
    MAX_LIST,
    NOTE_LIMIT,
    TERMINAL_STATUSES,
    USER_DECISION_REASONS,
    HandoffAccountUnavailable,
    HandoffAlreadyClaimed,
    HandoffAuthExpired,
    HandoffConfirmExpired,
    HandoffInsufficientFunds,
    HandoffInvalidState,
    HandoffNeedsUserDecision,
    HandoffNotFound,
    HandoffNoteRequired,
    HandoffOperatorRequired,
    HandoffResumeFailed,
    HandoffRunMismatch,
    _auth_snapshot,
    _decode,
    _meta_account,
    _operator,
    _public,
    _run,
    _snapshot,
    _transition,
    claim,
    close_for_run,
    create,
    get,
    list_handoffs,
    need_info,
    reject,
)
from app.planner.runner import MONEY_TOOLS

log = logging.getLogger(__name__)

# 恢复时「进程内只允许一个恢复在跑」：RUNS 本来就是进程内状态，多进程恢复没意义；
# 再加一层 DB 条件 UPDATE（expect=claimed/need_info）防住同进程内的并发点击。
RESUME_LOCK: Final[asyncio.Lock] = asyncio.Lock()


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
