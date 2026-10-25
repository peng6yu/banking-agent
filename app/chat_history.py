"""会话记忆：把每一轮对话按会话存进库，重启之后还接得上。

为什么需要这个模块：以前对话历史只放在一个进程内存的 dict 里
（app/api/routes.py 的 _history），服务一重启就没了，前端也从不回显——
用户打开页面看到一片空白，自然觉得「每问一句都是一个新会话」。
会话本身其实一直没变（前端把 session_id 存进了 sessionStorage），
缺的是「记得住、看得见」。

三条边界：
1. 这里只存对话原文（用户说的、助手回的），不存工具结果、不存凭证；
2. 记忆不参与任何权限判断：确认、因子、限额永远在服务端重新裁决，
   不能因为「上一轮聊过」就放行；
3. 只保留最近 KEEP_TURNS 轮，超出的删掉——库是演示用的，不该无限长。

注意：本模块**不写 sessions 表**。sessions 行由执行器（planner.state.open_run）
负责建，它靠「新插入」判断要不要写 session.start 审计；如果这里为了存标题提前
INSERT 一行，那次执行就会被当成老会话，session.start 审计直接消失。所以标题
从 chat_turns 的第一句用户消息里现算，最近会话也从 chat_turns 的 MAX(ts_ms) 找。
"""

from __future__ import annotations

from typing import Any, Final

import app.db as db
from app.clock import now_ms

#: 允许落库的角色：只有用户与助手，工具/系统消息不进对话记忆。
ROLES: Final[tuple[str, ...]] = ("user", "assistant")
#: 单条消息最长保留多少字：超长原文截断，避免一条流水日志撑爆历史。
MAX_CONTENT: Final[int] = 4000
#: 一个会话最多留多少轮（一问一答算两轮）。
KEEP_TURNS: Final[int] = 200
#: 默认一次取多少轮给前端/模型。
DEFAULT_LIMIT: Final[int] = 20
#: 会话标题取第一条用户消息的前多少个字。
TITLE_CHARS: Final[int] = 20


def append(
    conn,
    session_id: str,
    role: str,
    content: str,
    *,
    ts_ms: int | None = None,
) -> dict[str, Any]:
    """追加一轮对话（只写 chat_turns，不动 sessions）。"""
    sid = str(session_id or "").strip()
    if not sid:
        raise ValueError("session_id 不能为空")
    who = str(role or "").strip()
    if who not in ROLES:
        raise ValueError(f"不认识的角色：{who}")
    text = str(content or "")[:MAX_CONTENT]
    at = int(ts_ms) if ts_ms is not None else now_ms()
    with db.tx(conn):
        row = conn.execute(
            "SELECT COALESCE(MAX(seq), 0) AS last FROM chat_turns WHERE session_id = ?", (sid,)
        ).fetchone()
        seq = int(row["last"]) + 1
        conn.execute(
            "INSERT INTO chat_turns (session_id, seq, role, content, ts_ms) VALUES (?, ?, ?, ?, ?)",
            (sid, seq, who, text, at),
        )
        conn.execute(
            "DELETE FROM chat_turns WHERE session_id = ? AND seq <= ?", (sid, seq - KEEP_TURNS)
        )
    return {"session_id": sid, "seq": seq, "role": who, "content": text, "ts_ms": at}


def recent(conn, session_id: str, *, limit: int = DEFAULT_LIMIT) -> list[dict[str, Any]]:
    """某会话最近 limit 轮，按时间正序返回（前端从上往下画气泡）。"""
    sid = str(session_id or "").strip()
    if not sid:
        return []
    rows = conn.execute(
        "SELECT * FROM chat_turns WHERE session_id = ? ORDER BY seq DESC LIMIT ?",
        (sid, max(1, int(limit))),
    ).fetchall()
    return [
        {"role": str(row["role"]), "content": str(row["content"]), "ts_ms": int(row["ts_ms"]), "seq": int(row["seq"])}
        for row in reversed(rows)
    ]


def title(conn, session_id: str) -> str:
    """会话标题：第一条用户消息的前 TITLE_CHARS 个字；没有就返回空串。"""
    sid = str(session_id or "").strip()
    if not sid:
        return ""
    row = conn.execute(
        "SELECT content FROM chat_turns WHERE session_id = ? AND role = 'user'"
        " ORDER BY seq ASC LIMIT 1",
        (sid,),
    ).fetchone()
    return str(row["content"]).strip()[:TITLE_CHARS] if row is not None else ""


def latest(conn, *, limit: int = DEFAULT_LIMIT) -> dict[str, Any]:
    """最近一个真的聊过的会话（及它的最近 limit 轮）。

    只看 chat_turns，不看 dag_runs：多数会话一句话就答完了，不会建 DAG，
    用 DAG 找「最近会话」会漏掉一半。
    """
    head = conn.execute(
        "SELECT session_id, MAX(ts_ms) AS last_ms FROM chat_turns"
        " GROUP BY session_id ORDER BY last_ms DESC LIMIT 1"
    ).fetchone()
    if head is None:
        return {"session_id": "", "turns": [], "last_ms": 0, "title": ""}
    sid = str(head["session_id"])
    return {
        "session_id": sid,
        "turns": recent(conn, sid, limit=limit),
        "last_ms": int(head["last_ms"]),
        "title": title(conn, sid),
    }


def clear(conn, session_id: str) -> dict[str, Any]:
    """忘掉一个会话的对话记忆（用户主动「重新开始」时用）；不动审计、不动计划。"""
    sid = str(session_id or "").strip()
    if not sid:
        raise ValueError("session_id 不能为空")
    with db.tx(conn):
        removed = conn.execute("DELETE FROM chat_turns WHERE session_id = ?", (sid,)).rowcount
    return {"session_id": sid, "removed": int(removed or 0)}


__all__ = ["DEFAULT_LIMIT", "KEEP_TURNS", "ROLES", "append", "clear", "latest", "recent", "title"]
