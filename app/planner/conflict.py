"""同一会话里只允许一轮「没走完的写操作」。

为什么需要（这是任务书第四项要求）：
一个会话同时挂两轮时，后一轮的 `executor.run` 会把内存里的 `RUNS[session_id]`
覆盖掉，而前一轮那张确认卡还留在库里、还能被点、还能被 `/api/confirm` 回答。
结果是：用户点确认「没反应」，审计里两轮交错，事后说不清钱是哪一轮出去的。

这里的职责只有一条：**发现并如实告知，不替用户做选择**。
- 新请求含写操作、且该会话还有没走完的写轮次 → 这一轮直接不开始，回 run_id 与两条出口；
- 只读请求不拦（查余额不该被一张待确认卡堵住）；
- 判定以库为准（dag_runs / dag_nodes / confirmations），所以进程重启后依然有效；
- 有多个未完成轮次时把 run_id 全列出来，不静默挑其中一轮继续。
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Any, Final

from app import audit, clock, db
from app.models import Dag
from app.tools.registry import WRITE_TOOL_NAMES

# 只有这两种状态的 run 才可能「还没走完」：running 是正在跑，waiting 是在等人回答。
UNFINISHED_RUN_STATES: Final[tuple[str, ...]] = ("running", "waiting")
# 节点停在四个状态之一，说明这一轮的写操作还没有落定。
BUSY_NODE_STATES: Final[tuple[str, ...]] = ("pending", "running", "waiting_confirm", "waiting_factor")
CONFLICT_CODE: Final[str] = "SESSION_RUN_CONFLICT"
MAX_REPORTED_RUNS: Final[int] = 5


def _time_text(ms: int) -> str:
    return datetime.fromtimestamp(int(ms) / 1000).strftime("%Y-%m-%d %H:%M")


def new_write_tools(dag: Dag) -> list[str]:
    """这一轮请求里真正会动钱/动状态的工具。全是查询就返回空列表。"""
    return [node.tool for node in dag.nodes if node.tool in WRITE_TOOL_NAMES]


def _run_detail(conn: sqlite3.Connection, row: Any, now_ms: int) -> dict[str, Any] | None:
    """一轮 run 的未完成细节；写节点都已经落定就返回 None（不算挡路）。"""
    nodes = conn.execute(
        "SELECT node_id, tool, state FROM dag_nodes WHERE run_id = ? ORDER BY node_id",
        (row["id"],),
    ).fetchall()
    busy = [
        {"node_id": str(node["node_id"]), "tool": str(node["tool"]), "state": str(node["state"])}
        for node in nodes
        if node["tool"] in WRITE_TOOL_NAMES and node["state"] in BUSY_NODE_STATES
    ]
    if not busy:
        return None
    cards = conn.execute(
        "SELECT id, node_id, status, expires_ms FROM confirmations"
        " WHERE run_id = ? AND status = 'pending' ORDER BY created_ms",
        (row["id"],),
    ).fetchall()
    pending = [
        {
            "confirmation_id": str(card["id"]),
            "node_id": str(card["node_id"]),
            "expires_ms": int(card["expires_ms"]),
            "expired": int(card["expires_ms"]) <= now_ms,
            "expires_in_s": max(0, int(card["expires_ms"]) - now_ms) // 1000,
        }
        for card in cards
    ]
    return {
        "run_id": str(row["id"]),
        "status": str(row["status"]),
        "created_ms": int(row["created_ms"]),
        "created_text": _time_text(int(row["created_ms"])),
        "pending_confirmations": pending,
        "write_nodes": busy,
    }


def unfinished(conn: sqlite3.Connection, session_id: str) -> list[dict[str, Any]]:
    """这个会话里所有「还有写节点没落定」的轮次，按时间正序。"""
    rows = conn.execute(
        "SELECT id, status, created_ms FROM dag_runs"
        " WHERE session_id = ? AND status IN (?, ?) ORDER BY created_ms ASC",
        (session_id, *UNFINISHED_RUN_STATES),
    ).fetchall()
    now_ms = clock.now_ms()
    found: list[dict[str, Any]] = []
    for row in rows:
        detail = _run_detail(conn, row, now_ms)
        if detail is not None:
            found.append(detail)
    return found


def _message(runs: list[dict[str, Any]]) -> str:
    """给用户看的一句话：几轮没走完、卡在哪、下一步能做什么。"""
    cards = [card for run in runs for card in run["pending_confirmations"] if not card["expired"]]
    if len(runs) == 1:
        head = f"这个会话里还有一轮没走完（run_id {runs[0]['run_id']}，{runs[0]['created_text']} 发起）"
    else:
        ids = "、".join(run["run_id"] for run in runs[:MAX_REPORTED_RUNS])
        more = "" if len(runs) <= MAX_REPORTED_RUNS else f" 等 {len(runs)} 轮"
        head = f"这个会话里有 {len(runs)} 轮没走完（{ids}{more}）"
    if cards:
        tail = f"其中 {len(cards)} 张确认卡还在等你的答复"
    else:
        tail = "现在没有待答复的卡片，但写节点还没有落定"
    return (
        f"{head}，{tail}。这一轮我不开始，也不会替你选其中一轮继续："
        "请先把那张卡确认或拒绝，或者明确取消上一轮，再发新的写操作。"
    )


def _options(session_id: str, runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    options: list[dict[str, Any]] = []
    for run in runs[:MAX_REPORTED_RUNS]:
        alive = [card for card in run["pending_confirmations"] if not card["expired"]]
        options.append(
            {
                "action": "continue",
                "run_id": run["run_id"],
                "confirmation_id": alive[0]["confirmation_id"] if alive else None,
                "how": "点那张确认卡；进程重启过的话用 POST /api/session/{sid}/recover 把这一轮接回来",
                "endpoint": f"/api/session/{session_id}/recover",
            }
        )
        options.append(
            {
                "action": "cancel",
                "run_id": run["run_id"],
                "how": "POST /api/cancel（会释放这一轮的全部预授权，并把这轮标成 cancelled）",
                "endpoint": "/api/cancel",
            }
        )
    return options


SUPERSEDE_REASON: Final[str] = "CLARIFICATION_ANSWERED"


def supersede_answered_clarifications(
    session_id: str, *, conn: sqlite3.Connection | None = None
) -> list[str]:
    """用户刚刚回答了上一轮的澄清问题：把那一轮正式关掉，别让它继续挡路。

    为什么必须显式关：停在收款人歧义上的那一轮，run 还是 waiting、转账节点还是 pending，
    冲突闸门于是把「用户用来回答的那句话」也当成新的写请求挡掉——表现就是
    「点了候选，页面却说这个会话还有一轮没走完」。而那一轮本来没有授权、没有动钱，
    它唯一的作用是提问；问题答完了，它的生命周期就该结束。

    边界：只关「没有待确认卡片」的轮次。有卡在等签字的是「等确认」，不是「等澄清」，
    替用户取消掉就等于替他拒绝了一笔他自己发起的交易。
    """
    own = conn is None
    target = db.connect() if own else conn
    closed: list[str] = []
    try:
        for run in unfinished(target, session_id):
            if run["pending_confirmations"]:
                continue
            now = clock.now_ms()
            with db.tx(target):
                target.execute(
                    "UPDATE dag_nodes SET state = 'skipped', ts_ms = ?"
                    " WHERE run_id = ? AND state IN (?, ?, ?, ?)",
                    (now, run["run_id"], *BUSY_NODE_STATES),
                )
                target.execute(
                    "UPDATE dag_runs SET status = 'cancelled', updated_ms = ?"
                    " WHERE id = ? AND status IN (?, ?)",
                    (now, run["run_id"], *UNFINISHED_RUN_STATES),
                )
                audit.append(
                    target,
                    "run.superseded",
                    {
                        "run_id": run["run_id"],
                        "reason": SUPERSEDE_REASON,
                        "skipped_nodes": [node["node_id"] for node in run["write_nodes"]],
                        "note": "这一轮停在澄清问题上，用户已经回答；它没有授权、没有动钱，按被取代关闭。",
                    },
                    session_id=session_id,
                )
                # 问题答完了，这一轮就不再需要人接手：把挂在它上面的活跃接管单一起收掉，
                # 免得队列里留一张 run 已经关闭、操作员领了也没法恢复的旧单。
                # 延迟导入：handoffs → runner，放在模块顶部会绕进 executor 的导入环。
                from app import handoffs as handoffs_mod

                handoffs_mod.close_for_run(
                    target,
                    run["run_id"],
                    reason=f"用户已回答澄清（{SUPERSEDE_REASON}），这一轮按被取代关闭",
                )
            closed.append(run["run_id"])
    finally:
        if own:
            target.close()
    return closed


def detect(session_id: str, dag: Dag, *, conn: sqlite3.Connection | None = None) -> dict[str, Any] | None:
    """新请求是否要先被上一轮挡住；挡住时返回结构化冲突，方便接口层与测试直接断言。"""
    tools = new_write_tools(dag)
    if not tools:
        return None
    own = conn is None
    target = db.connect() if own else conn
    try:
        runs = unfinished(target, session_id)
        if not runs:
            return None
        payload: dict[str, Any] = {
            "conflict": True,
            "code": CONFLICT_CODE,
            "session_id": session_id,
            "run_id": runs[0]["run_id"],
            "run_ids": [run["run_id"] for run in runs],
            "runs": runs,
            "new_tools": tools,
            "message": _message(runs),
            "options": _options(session_id, runs),
            "note": "这是有意为之：并发两轮写操作会让确认卡与执行结果对不上。",
        }
        # 冲突也要留痕：事后查「为什么这句话没被执行」时必须能对上。
        audit.append(
            target,
            "run.conflict",
            {
                "run_ids": payload["run_ids"],
                "new_tools": tools,
                "pending_cards": sum(len(run["pending_confirmations"]) for run in runs),
            },
            session_id=session_id,
        )
        return payload
    finally:
        if own:
            target.close()


__all__ = [
    "BUSY_NODE_STATES",
    "CONFLICT_CODE",
    "SUPERSEDE_REASON",
    "UNFINISHED_RUN_STATES",
    "detect",
    "new_write_tools",
    "supersede_answered_clarifications",
    "unfinished",
]
