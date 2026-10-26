"""聊天链路的进程内状态：会话历史环、模型适配器、SSE 帧格式与常量（第 5 层，不查库）。"""

from __future__ import annotations

from typing import Any, Final
from collections import deque
import json

from app.llm import LLMError, build_adapter



SSE_HEADERS: Final[dict[str, str]] = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}


SSE_MEDIA: Final[str] = "text/event-stream"


HISTORY_TURNS: Final[int] = 4


HISTORY_PER_SESSION: Final[int] = 16


INTERNAL_USER_TEXT: Final[str] = "系统开小差了，已经记录。"


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


CONTEXT_CHARS: Final[int] = 24
