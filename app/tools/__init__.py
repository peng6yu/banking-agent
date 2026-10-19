"""工具层：10 个银行工具与工具注册表。

对应文档：docs/06_工具层与场景实现规格.md、docs/02 目录结构与文件职责.md 2.2。

只读工具可以直接调；写工具第一句就是 require_token，没有令牌一律 MissingCapability。
工具层不做任何「该不该做」的判断，那是 app/policy 的活（docs/06 6.7）。
"""

from __future__ import annotations

from app.tools.base import (
    ToolContext,
    fail_result,
    ok_result,
    require_token,
    tool_result,
)
from app.tools.registry import (
    READONLY_TOOL_NAMES,
    TOOL_NAMES,
    TOOLS,
    WRITE_TOOL_NAMES,
    ToolSpec,
    execute,
    get_tool,
    tool_schemas,
    validate_params,
)

__all__ = [
    "READONLY_TOOL_NAMES",
    "TOOLS",
    "TOOL_NAMES",
    "WRITE_TOOL_NAMES",
    "ToolContext",
    "ToolSpec",
    "execute",
    "fail_result",
    "get_tool",
    "ok_result",
    "require_token",
    "tool_result",
    "tool_schemas",
    "validate_params",
]
