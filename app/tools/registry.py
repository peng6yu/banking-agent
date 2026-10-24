"""工具注册表：35 个工具的解析、校验与执行。

对应文档：docs/06_工具层与场景实现规格.md 6.1/6.2、docs/02 目录结构与文件职责.md 2.2。

权威表本身放在 app/tools/tool_specs.py，这里只管三件事：
把名字变成可执行的工具、按 JSON Schema 校验参数、执行时统一记一条 tool.executed 审计。
执行器只认这张表，模型给出的工具名不在表里就是 ToolNotFound。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, Final

# 模块级导入（不用 from 包 import 模块）：tests/test_imports.py 用 ast 按模块名登记依赖，
# 包级导入会被它漏掉，允许依赖表就会与代码现状对不上。
import app.audit as audit  # noqa: PLR0402
import app.db as db  # noqa: PLR0402
from app.clock import ToolNotFound, ValidationError, now_ms
from app.log import log
from app.models import ToolResult
from app.tools.base import ToolContext
from app.tools.tool_specs import TOOL_SPECS, ToolSpec

TOOLS: Final[dict[str, ToolSpec]] = {spec.name: spec for spec in TOOL_SPECS}
TOOL_NAMES: Final[tuple[str, ...]] = tuple(spec.name for spec in TOOL_SPECS)
READONLY_TOOL_NAMES: Final[tuple[str, ...]] = tuple(
    spec.name for spec in TOOL_SPECS if not spec.writes
)
WRITE_TOOL_NAMES: Final[tuple[str, ...]] = tuple(spec.name for spec in TOOL_SPECS if spec.writes)


def get_tool(name: str) -> ToolSpec:
    """按名字取工具；不在权威表里就是 ToolNotFound，不做模糊匹配。"""
    spec = TOOLS.get(str(name))
    if spec is None:
        raise ToolNotFound(reason="UNKNOWN_TOOL", tool=str(name), known=list(TOOL_NAMES))
    return spec


def tool_schemas() -> list[dict[str, Any]]:
    """给模型看的工具清单，顺序即 docs/06 6.1 的顺序。"""
    return [spec.schema() for spec in TOOL_SPECS]


def validate_params(spec: ToolSpec, params: dict[str, Any] | None) -> dict[str, Any]:
    """按 JSON Schema 校验参数：多字段、缺字段、类型、枚举、区间都要拦。"""
    checked: dict[str, Any] = {} if params is None else params
    if not isinstance(checked, dict):
        raise ValidationError("工具参数必须是对象", tool=spec.name)
    properties = spec.params.get("properties", {})
    unexpected = sorted(set(checked) - set(properties))
    if unexpected:
        raise ValidationError(
            "工具参数里有不认识的字段", tool=spec.name, unexpected=unexpected
        )
    missing = [key for key in spec.params.get("required", []) if key not in checked]
    if missing:
        raise ValidationError("工具参数缺字段", tool=spec.name, missing=missing)
    for key, value in checked.items():
        _check_value(spec.name, key, value, properties.get(key, {}))
    return checked


def execute(
    name: str,
    params: dict[str, Any] | None = None,
    token: str | None = None,
    ctx: ToolContext | None = None,
) -> ToolResult:
    """执行一个工具：校验参数 → 调 handler → 记一条 tool.executed 审计。

    只读工具不要求令牌（docs/07 7.4），token 传 None 也没关系；写工具没有令牌
    会在 handler 第一句抛 MissingCapability，这里不做任何兜底。
    """
    spec = get_tool(name)
    checked = validate_params(spec, params)
    started_ms = now_ms()
    with _session(ctx) as context:
        result = spec.handler(context, checked, token)
        audit.append(
            context.conn,
            "tool.executed",
            {
                "tool": spec.name,
                "ok": result.ok,
                "error_code": result.error_code,
                "writes": spec.writes,
                "node_id": context.node_id,
            },
            session_id=context.session_id,
            task_id=context.task_id,
        )
    log.info("tool.executed", tool=spec.name, ok=result.ok, ms=now_ms() - started_ms)
    return result


@contextmanager
def _session(ctx: ToolContext | None) -> Iterator[ToolContext]:
    """连接归调用方所有；没给 ctx 才临时开一条，用完立刻关。"""
    if ctx is not None:
        yield ctx
        return
    conn: sqlite3.Connection = db.connect()
    try:
        yield ToolContext(conn=conn)
    finally:
        conn.close()


def _check_value(tool: str, key: str, value: Any, spec: dict[str, Any]) -> None:
    kind = spec.get("type")
    if kind == "string":
        _check_string(tool, key, value, spec)
    elif kind == "integer":
        _check_integer(tool, key, value, spec)
    elif kind == "boolean" and not isinstance(value, bool):
        raise ValidationError("参数类型不对", tool=tool, field=key, expected="boolean")


def _check_string(tool: str, key: str, value: Any, spec: dict[str, Any]) -> None:
    if not isinstance(value, str):
        raise ValidationError("参数类型不对", tool=tool, field=key, expected="string")
    if "enum" in spec and value not in spec["enum"]:
        raise ValidationError(
            "参数取值不在允许范围", tool=tool, field=key, allowed=list(spec["enum"])
        )
    if "minLength" in spec and len(value) < int(spec["minLength"]):
        raise ValidationError("参数太短", tool=tool, field=key, min_length=spec["minLength"])
    if "maxLength" in spec and len(value) > int(spec["maxLength"]):
        raise ValidationError("参数太长", tool=tool, field=key, max_length=spec["maxLength"])


def _check_integer(tool: str, key: str, value: Any, spec: dict[str, Any]) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValidationError("参数类型不对", tool=tool, field=key, expected="integer")
    if "minimum" in spec and value < int(spec["minimum"]):
        raise ValidationError("参数小于下限", tool=tool, field=key, minimum=spec["minimum"])
    if "maximum" in spec and value > int(spec["maximum"]):
        raise ValidationError("参数大于上限", tool=tool, field=key, maximum=spec["maximum"])


__all__ = [
    "READONLY_TOOL_NAMES",
    "TOOLS",
    "TOOL_NAMES",
    "TOOL_SPECS",
    "WRITE_TOOL_NAMES",
    "ToolSpec",
    "execute",
    "get_tool",
    "tool_schemas",
    "validate_params",
]
