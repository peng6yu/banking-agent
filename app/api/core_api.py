"""基础接口：健康、工具清单、审计链、根路径静态页（第 5 层）。"""

from __future__ import annotations

from fastapi import APIRouter

from typing import Any, Final
from fastapi.responses import FileResponse, StreamingResponse
from pathlib import Path
import json

from app.tools.registry import TOOL_NAMES, tool_schemas
from app import audit, clock, db, timefmt
from app.planner import assist, executor, intent, payment_guard, recover, ticker

from app.api.chat_trace import (
    _db_ok,
    _model_name,
)

#: 每个端点文件自带一个空 router，由 app/api/routes.py 统一挂到 /api 下。
router = APIRouter()




WEB_INDEX: Final[Path] = Path(__file__).resolve().parents[2] / "web" / "index.html"


@router.get("/api/health")
async def health() -> dict[str, Any]:
    """探活：顺带报工具数与时间源，方便评审现场确认跑的是真身。"""
    return {
        "ok": True,
        "status": "ok",
        "model": _model_name(),
        "now_ms": clock.now_ms(),
        "now_text": timefmt.maybe_minute_text(clock.now_ms()),
        "tools": len(TOOL_NAMES),
        "db": _db_ok(),
        "sessions": len(executor.active_sessions()),
    }


@router.get("/api/tools")
async def list_tools() -> dict[str, Any]:
    """在册工具的 JSON Schema，前端用它渲染参数表单。"""
    return {"ok": True, "count": len(TOOL_NAMES), "tools": tool_schemas()}


@router.get("/api/audit")
async def audit_trail(session_id: str, limit: int = 100) -> dict[str, Any]:
    """审计链：既能看链还完不完整，也能看这条会话每一步为什么这么判。"""
    with db.open_conn() as conn:
        rows = conn.execute(
            "SELECT seq, ts_ms, event_type, payload_json FROM audit_log WHERE session_id = ?"
            " ORDER BY seq LIMIT ?",
            (session_id, limit),
        ).fetchall()
        events = [
            {
                "seq": int(row["seq"]),
                "ts_ms": int(row["ts_ms"]),
                "ts_text": timefmt.maybe_minute_text(int(row["ts_ms"])),
                "event_type": str(row["event_type"]),
                "payload": json.loads(row["payload_json"]),
            }
            for row in rows
        ]
        return {
            "ok": True,
            "session_id": session_id,
            "count": len(events),
            "events": events,
            "chain_ok": audit.verify_chain(conn),
            "chain_head": audit.chain_head(conn),
        }


@router.get("/api/audit/verify")
async def audit_verify() -> dict[str, Any]:
    """答辩演示用：一眼看出哈希链还完不完整。"""
    with db.open_conn() as conn:
        return {
            "ok": audit.verify_chain(conn),
            "head": audit.chain_head(conn),
            "count": audit.count_events(conn),
        }


@router.get("/")
async def index() -> Any:
    """根路径直接是前端那一页；文件不在就老实说一声。"""
    if WEB_INDEX.exists():
        return FileResponse(WEB_INDEX)
    return {"service": "银行 AI 智能体", "health": "/api/health"}
