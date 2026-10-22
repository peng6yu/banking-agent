"""HTTP 端点：10 个接口 + SSE 事件流。

对应文档：docs/07_规划器_API与前端规格.md 7.5 / 7.6。

三条硬规矩：
1. 异常一律走 app/main.py 里注册的处理器，响应体只有 {code, user_text}，
   绝不把堆栈或英文异常名发给浏览器；
2. SSE 帧固定是 "event: {名字}\\ndata: {JSON}\\n\\n"，八个事件名一字不改，
   前端 web/index.html 按名字分发；
3. /api/chat 先 await 完意图解析再返回 StreamingResponse，
   这样模型不可用才能老老实实映射成 503，而不是变成一条流里的错误事件。
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections import deque
from pathlib import Path
from typing import Any, Final

from fastapi import APIRouter
from fastapi.responses import FileResponse, StreamingResponse

from app import audit, clock, db
from app.api.schemas import CancelRequest, ResetRequest
from app.ids import new_id
from app.llm import LLMError, build_adapter
from app.models import ChatRequest, ConfirmRequest
from app.planner import dag as planner_dag
from app.planner import executor, intent
from app.tools.registry import TOOL_NAMES, tool_schemas

log = logging.getLogger(__name__)

router = APIRouter()
SSE_HEADERS: Final[dict[str, str]] = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}
SSE_MEDIA: Final[str] = "text/event-stream"
HISTORY_TURNS: Final[int] = 4
HISTORY_PER_SESSION: Final[int] = 16
INTERNAL_USER_TEXT: Final[str] = "系统开小差了，已经记录。"
WEB_INDEX: Final[Path] = Path(__file__).resolve().parents[2] / "web" / "index.html"

_adapter: Any = None
_history: dict[str, deque[dict[str, str]]] = {}
_SENTINEL: Final[str] = "__end__"


def set_adapter(adapter: Any) -> None:
    """测试与演示注入适配器；传 None 恢复默认（配置里的真实模型或 Mock）。"""
    global _adapter
    _adapter = adapter


def get_adapter() -> Any:
    return _adapter if _adapter is not None else build_adapter()


def frame(name: str, data: dict[str, Any]) -> str:
    """拼一帧 SSE；ensure_ascii=False 让中文在浏览器里可读。"""
    return f"event: {name}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


@router.get("/api/health")
async def health() -> dict[str, Any]:
    """探活：顺带报工具数与时间源，方便评审现场确认跑的是真身。"""
    return {
        "ok": True,
        "status": "ok",
        "model": _model_name(),
        "now_ms": clock.now_ms(),
        "tools": len(TOOL_NAMES),
        "db": _db_ok(),
        "sessions": len(executor.active_sessions()),
    }


@router.get("/api/tools")
async def list_tools() -> dict[str, Any]:
    """在册工具的 JSON Schema，前端用它渲染参数表单。"""
    return {"count": len(TOOL_NAMES), "tools": tool_schemas()}


@router.post("/api/chat")
async def chat(request: ChatRequest) -> StreamingResponse:
    """对话主入口：先解析意图，再把执行过程用 SSE 一条条吐出来。"""
    session_id = request.session_id or new_id("s")
    _remember(session_id, "user", request.text)
    parsed = await _parse(session_id, request.text)
    return StreamingResponse(
        _events(session_id, parsed),
        media_type=SSE_MEDIA,
        headers=SSE_HEADERS,
    )


@router.post("/api/confirm")
async def confirm(request: ConfirmRequest) -> dict[str, Any]:
    """回答一次确认；重复提交幂等，不会产生第二次扣款。"""
    result = await executor.resume(
        request.session_id,
        request.confirmation_id,
        request.approve,
        factor=request.factor,
    )
    _remember(request.session_id, "user", "确认" if request.approve else "取消")
    return result


@router.post("/api/cancel")
async def cancel(request: CancelRequest) -> dict[str, Any]:
    """中途取消：释放全部预授权，整轮 DAG 标 cancelled。"""
    return await executor.cancel(request.session_id, request.reason)


@router.get("/api/session/{session_id}")
async def session(session_id: str) -> dict[str, Any]:
    """查会话：节点状态、待确认卡片、当前余额。"""
    with _conn() as conn:
        head = conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
        if head is None:
            return {"session_id": session_id, "found": False}
        nodes = [
            {
                "node_id": row["node_id"],
                "tool": row["tool"],
                "state": row["state"],
                "decision": json.loads(row["decision_json"] or "null"),
                "result": json.loads(row["result_json"] or "null"),
            }
            for row in conn.execute(
                "SELECT * FROM dag_nodes WHERE run_id = (SELECT id FROM dag_runs WHERE session_id = ?"
                " ORDER BY created_ms DESC LIMIT 1) ORDER BY node_id",
                (session_id,),
            )
        ]
        cards = [
            {"confirmation_id": row["id"], "node_id": row["node_id"], "status": row["status"]}
            for row in conn.execute(
                "SELECT * FROM confirmations WHERE session_id = ? ORDER BY created_ms", (session_id,)
            )
        ]
        balance = conn.execute(
            "SELECT balance_fen FROM accounts ORDER BY CASE type WHEN 'debit' THEN 0 ELSE 1 END, id LIMIT 1"
        ).fetchone()
    return {
        "session_id": session_id,
        "found": True,
        "status": str(head["status"]),
        "created_ms": int(head["created_ms"]),
        "last_ms": int(head["last_ms"]),
        "nodes": nodes,
        "confirmations": cards,
        "balance_fen": int(balance["balance_fen"]) if balance is not None else None,
    }


@router.get("/api/audit")
async def audit_trail(session_id: str, limit: int = 100) -> dict[str, Any]:
    """审计链：既能看链还完不完整，也能看这条会话每一步为什么这么判。"""
    with _conn() as conn:
        rows = conn.execute(
            "SELECT seq, ts_ms, event_type, payload_json FROM audit_log WHERE session_id = ?"
            " ORDER BY seq LIMIT ?",
            (session_id, limit),
        ).fetchall()
        events = [
            {
                "seq": int(row["seq"]),
                "ts_ms": int(row["ts_ms"]),
                "event_type": str(row["event_type"]),
                "payload": json.loads(row["payload_json"]),
            }
            for row in rows
        ]
        return {
            "session_id": session_id,
            "count": len(events),
            "events": events,
            "chain_ok": audit.verify_chain(conn),
            "chain_head": audit.chain_head(conn),
        }


@router.get("/api/audit/verify")
async def audit_verify() -> dict[str, Any]:
    """答辩演示用：一眼看出哈希链还完不完整。"""
    with _conn() as conn:
        return {
            "ok": audit.verify_chain(conn),
            "head": audit.chain_head(conn),
            "count": audit.count_events(conn),
        }


@router.post("/api/demo/reset")
async def reset_demo(request: ResetRequest | None = None) -> dict[str, Any]:
    """演示重置：收回智能体动过的钱、清掉 hold 与限额账，进行中的会话收掉。"""
    from scripts.seed import seed

    for session_id in executor.active_sessions():
        executor.close(session_id)
    _history.clear()
    with _conn() as conn, db.tx(conn):
        seed(conn)
        balance = _rewind_demo(conn)
    return {
        "ok": True,
        "reason": request.reason if request is not None else "演示重置",
        "account_id": balance[0],
        "balance_fen": balance[1],
    }


def _rewind_demo(conn: Any) -> tuple[str | None, int | None]:
    """把 hold_id 非空的流水（只有智能体会产生）反向冲回，并清掉 hold 与限额。"""
    rows = conn.execute(
        "SELECT account_id, direction, amount_fen FROM transactions WHERE hold_id IS NOT NULL"
    ).fetchall()
    for row in rows:
        # 出去的钱是当时扣掉的，冲回就是加回来；进来的反过来减掉。
        delta = int(row["amount_fen"]) if row["direction"] == "out" else -int(row["amount_fen"])
        conn.execute("UPDATE accounts SET balance_fen = balance_fen + ? WHERE id = ?", (delta, row["account_id"]))
    conn.execute("DELETE FROM transactions WHERE hold_id IS NOT NULL")
    conn.execute("DELETE FROM holds")
    conn.execute("DELETE FROM daily_limits")
    conn.execute("DELETE FROM confirmations")
    account = conn.execute(
        "SELECT id, balance_fen FROM accounts ORDER BY CASE type WHEN 'debit' THEN 0 ELSE 1 END, id LIMIT 1"
    ).fetchone()
    if account is None:
        return None, None
    return str(account["id"]), int(account["balance_fen"])


@router.get("/")
async def index() -> Any:
    """根路径直接是前端那一页；文件不在就老实说一声。"""
    if WEB_INDEX.exists():
        return FileResponse(WEB_INDEX)
    return {"service": "银行 AI 智能体", "health": "/api/health"}


async def _parse(session_id: str, text: str) -> intent.IntentResult:
    """解析意图并写审计；模型不可用时把 LLMError 原样抛给处理器。"""
    try:
        parsed = await asyncio.to_thread(_parse_sync, text, _turns(session_id))
    except LLMError:
        _audit_failure(session_id, text, "MODEL_UNAVAILABLE")
        raise
    _audit_intent(session_id, text, parsed)
    return parsed


def _parse_sync(text: str, history: list[dict[str, str]]) -> intent.IntentResult:
    """在线程里跑一次同步阻塞的模型调用，别把事件循环堵住。"""
    return asyncio.run(intent.parse(text, history, adapter=get_adapter()))


async def _events(session_id: str, parsed: intent.IntentResult) -> Any:
    """SSE 生成器：先发 start / intent，反问就到此为止，否则挂上执行器。"""
    queue: asyncio.Queue[str | tuple[str, dict[str, Any]]] = asyncio.Queue()
    await queue.put(("start", {"session_id": session_id, "now_ms": clock.now_ms()}))
    await queue.put(("intent", parsed.to_event() | {"session_id": session_id}))
    if parsed.question:
        _remember(session_id, "assistant", parsed.question)
        await queue.put(("done", {"summary": parsed.question, "question": parsed.question}))
        await queue.put((_SENTINEL, {}))
    else:
        _start_run(session_id, parsed, queue)
    while True:
        item = await queue.get()
        if isinstance(item, tuple) and item[0] == _SENTINEL:
            return
        name, data = item  # type: ignore[misc]
        yield frame(name, data)


def _start_run(session_id: str, parsed: intent.IntentResult, queue: asyncio.Queue[Any]) -> None:
    """建图并在后台跑；解析失败要退化成单步图，不能让整轮会话死掉。"""
    built = planner_dag.build_or_single({"intent": parsed.intent, "slots": parsed.slots})

    async def emit(name: str, data: dict[str, Any]) -> None:
        await queue.put((name, data))

    async def driver() -> None:
        try:
            await executor.run(session_id, built, emit)
        except Exception:
            log.exception("api.chat.driver_failed session=%s", session_id)
            await queue.put(("error", {"code": "INTERNAL", "user_text": INTERNAL_USER_TEXT}))
        finally:
            await queue.put((_SENTINEL, {}))

    asyncio.create_task(driver())


def _audit_intent(session_id: str, text: str, parsed: intent.IntentResult) -> None:
    try:
        with _conn() as conn:
            audit.append(
                conn,
                "intent.parsed",
                {
                    "intent": parsed.intent,
                    "confidence": parsed.confidence,
                    "missing": parsed.missing,
                    "question": parsed.question,
                    "retried": parsed.retried,
                    "source": parsed.source,
                    "text_len": len(text),
                },
                session_id=session_id,
            )
    except Exception:  # noqa: BLE001 - 审计写不进去也不能让对话失败
        log.warning("api.audit.intent_failed session=%s", session_id)


def _audit_failure(session_id: str, text: str, reason: str) -> None:
    _audit_intent(session_id, text, intent.IntentResult(intent="unknown", ok=False, source=reason))


class _conn:
    """临时连接：端点自己开自己关，不给执行器的连接踩坑。"""

    def __enter__(self) -> Any:
        self._conn_obj = db.connect()
        return self._conn_obj

    def __exit__(self, *_exc: object) -> None:
        self._conn_obj.close()


def _remember(session_id: str, role: str, content: str) -> None:
    turns = _history.setdefault(session_id, deque(maxlen=HISTORY_PER_SESSION))
    turns.append({"role": role, "content": content})


def _turns(session_id: str) -> list[dict[str, str]]:
    return list(_history.get(session_id, deque()))[-HISTORY_TURNS:]


def _model_name() -> str:
    """探活顺带报模型名；配置读不到就老实说默认。"""
    try:
        from app.config import Settings

        return str(Settings().model_brain)
    except Exception:  # noqa: BLE001 - 探活不该因为配置问题自己崩掉
        return "unknown"


def _db_ok() -> bool:
    try:
        with _conn() as conn:
            return conn.execute("SELECT 1 FROM accounts LIMIT 1").fetchone() is not None
    except Exception:  # noqa: BLE001 - 探活失败就是 False，不该把探活端点自己搞崩
        return False


__all__ = ["frame", "get_adapter", "router", "set_adapter"]
