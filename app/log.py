"""结构化单行 JSON 日志。

对应文档：docs/08_编码规范.md 8.6。

给开发者看，可变，写 stderr；与审计（app/audit.py，给评审看、只增不改）是两套东西。
禁止手工拼 JSON：字段一律走 kwargs，由本模块统一序列化，保证一行一条、字段有序。
"""

from __future__ import annotations

import json
import sys
import traceback
from typing import Any, Final, TextIO

from app.clock import now_ms

_LEVELS: Final[dict[str, int]] = {"debug": 10, "info": 20, "warn": 30, "error": 40}
_DEFAULT_LEVEL: Final[str] = "info"
_SENSITIVE_KEYS: Final[frozenset[str]] = frozenset(
    {"api_key", "token", "authorization", "password", "secret"}
)


class JsonLogger:
    """把一条条结构化事件打成单行 JSON 写到 stderr。"""

    def __init__(self, name: str, level: str = _DEFAULT_LEVEL, stream: TextIO | None = None) -> None:
        self.name = name
        self.level = level if level in _LEVELS else _DEFAULT_LEVEL
        self._stream: TextIO | None = stream

    def bind(self, **fields: Any) -> "BoundLogger":
        """派生一个带固定字段的子 logger（跨层传 session_id 时省事）。"""
        return BoundLogger(self, fields)

    def log(self, level: str, event: str, **fields: Any) -> None:
        if _LEVELS.get(level, 20) < _LEVELS[self.level]:
            return
        record = {
            "ts_ms": now_ms(),
            "level": level,
            "logger": self.name,
            "event": event,
            **_safe_fields(fields),
        }
        self._write(json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")))

    def debug(self, event: str, **fields: Any) -> None:
        self.log("debug", event, **fields)

    def info(self, event: str, **fields: Any) -> None:
        self.log("info", event, **fields)

    def warn(self, event: str, **fields: Any) -> None:
        self.log("warn", event, **fields)

    def error(self, event: str, exc: BaseException | None = None, **fields: Any) -> None:
        if exc is not None:
            fields = {**fields, "exc_type": type(exc).__name__, "exc_text": str(exc)}
            fields.setdefault("stack", "".join(traceback.format_exception(exc))[-2000:])
        self.log("error", event, **fields)

    def _write(self, line: str) -> None:
        stream = self._stream if self._stream is not None else sys.stderr
        stream.write(line + "\n")
        stream.flush()


class BoundLogger:
    """绑定了固定字段的 logger，转发给底层 JsonLogger。"""

    def __init__(self, logger: JsonLogger, fields: dict[str, Any]) -> None:
        self._logger = logger
        self._fields = fields

    def bind(self, **fields: Any) -> "BoundLogger":
        return BoundLogger(self._logger, {**self._fields, **fields})

    def debug(self, event: str, **fields: Any) -> None:
        self._logger.debug(event, **{**self._fields, **fields})

    def info(self, event: str, **fields: Any) -> None:
        self._logger.info(event, **{**self._fields, **fields})

    def warn(self, event: str, **fields: Any) -> None:
        self._logger.warn(event, **{**self._fields, **fields})

    def error(self, event: str, exc: BaseException | None = None, **fields: Any) -> None:
        self._logger.error(event, exc, **{**self._fields, **fields})


def get_logger(name: str, level: str = _DEFAULT_LEVEL) -> JsonLogger:
    """按模块名取一个 logger。"""
    return JsonLogger(name, level)


def log_event(logger: JsonLogger, event: str, **fields: Any) -> None:
    """docs/02 2.x 要求的函数式入口，等价于 logger.info(event, **fields)。"""
    logger.info(event, **fields)


def _safe_fields(fields: dict[str, Any]) -> dict[str, Any]:
    """日志字段兜底：不可序列化的对象转字符串，敏感字段直接抹掉。"""
    safe: dict[str, Any] = {}
    for key, value in fields.items():
        if key in _SENSITIVE_KEYS:
            safe[key] = "***"
        elif isinstance(value, (str, int, float, bool)) or value is None:
            safe[key] = value
        else:
            safe[key] = str(value)
    return safe


# 默认 logger，模块直接 log.info('xxx', ...) 即可
log: Final[JsonLogger] = get_logger("app")

__all__ = ["JsonLogger", "BoundLogger", "get_logger", "log_event", "log"]
