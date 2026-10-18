"""工具注册表：10 个工具的名字、参数模式、处理器与写标记。

对应文档：docs/06_工具层与场景实现规格.md 6.1/6.2、docs/02 目录结构与文件职责.md 2.2。

名字是权威表，顺序与 docs/06 6.1 一致，TOOL_NAMES 会拿去做回归测试；
执行器只认这张表，模型给出的工具名不在表里就是 ToolNotFound。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Final

from app import audit, db
from app.clock import ToolNotFound, ValidationError, now_ms
from app.log import log
from app.models import ToolResult
from app.money import MAX_SINGLE_FEN
from app.tools import bill, cards, queries, subs, transfers
from app.tools.base import ToolContext

_AMOUNT_FEN: Final[dict[str, Any]] = {
    "type": "integer",
    "minimum": 1,
    "maximum": MAX_SINGLE_FEN,
    "description": "金额，单位分（1 元 = 100 分），必须是最小的整数分",
}
_TAIL: Final[dict[str, Any]] = {
    "type": "string",
    "minLength": 4,
    "maxLength": 4,
    "description": "卡号后四位；不填就是默认账户",
}


@dataclass(frozen=True)
class ToolSpec:
    """一个工具的注册项：模型看得到的部分 + 内部处理器。"""

    name: str
    description: str
    params: dict[str, Any]
    handler: Callable[[ToolContext, dict[str, Any], str | None], ToolResult]
    writes: bool

    def schema(self) -> dict[str, Any]:
        """喂给模型的 JSON Schema，不带 handler，避免把内部实现暴露出去。"""
        return {"name": self.name, "description": self.description, "params": self.params}


TOOL_SPECS: Final[tuple[ToolSpec, ...]] = (
    ToolSpec(
        name="query_balance",
        description="查账户余额，金额单位是分。不传尾号就返回名下全部账户，主账户排第一个。",
        params={
            "type": "object",
            "properties": {"account_tail": _TAIL},
            "required": [],
            "additionalProperties": False,
        },
        handler=queries.query_balance,
        writes=False,
    ),
    ToolSpec(
        name="query_transactions",
        description="查最近的收支流水，支持时间天数、分类、金额区间和条数。查不到就返回空集合。",
        params={
            "type": "object",
            "properties": {
                "days": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 400,
                    "description": "回看天数，默认 30；超过 365 按 365 截断",
                },
                "category": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 12,
                    # 不写 enum：分类非法时工具要回 BAD_CATEGORY 并列出合法分类，
                    # 这是 docs/06 6.3 的分支，不该在校验层就变成 ValidationError
                    "description": "只看某一个分类，合法值：" + "、".join(queries.CATEGORIES),
                },
                "min_fen": {"type": "integer", "minimum": 0, "description": "金额下限（分）"},
                "max_fen": {"type": "integer", "minimum": 0, "description": "金额上限（分）"},
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": queries.MAX_LIMIT,
                    "description": "最多回几条，默认 20，最多 100",
                },
            },
            "required": [],
            "additionalProperties": False,
        },
        handler=queries.query_transactions,
        writes=False,
    ),
    ToolSpec(
        name="analyze_bill",
        description=(
            "按月或按年分析账单，汇总分类、商户 Top，"
            "并检出拆单、凌晨境外、订阅涨价三类异常。"
        ),
        params={
            "type": "object",
            "properties": {
                "period": {"type": "string", "enum": ["month", "year"], "description": "分析粒度"},
                "year": {"type": "integer", "minimum": 1970, "maximum": 2999},
                "month": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 12,
                    "description": "period=month 时必填",
                },
            },
            "required": ["period", "year"],
            "additionalProperties": False,
        },
        handler=bill.analyze_bill,
        writes=False,
    ),
    ToolSpec(
        name="list_subs",
        description="列出生效中的订阅扣款，含金额、周期和下次扣费日期。",
        params={
            "type": "object",
            "properties": {
                "include_cancelled": {
                    "type": "boolean",
                    "description": "是否把已取消的也列出来，默认只列生效中的",
                }
            },
            "required": [],
            "additionalProperties": False,
        },
        handler=subs.list_subs,
        writes=False,
    ),
    ToolSpec(
        name="list_cards",
        description="列出名下银行卡，含实体卡/虚拟卡、状态与单笔限额。",
        params={
            "type": "object",
            "properties": {"account_tail": _TAIL},
            "required": [],
            "additionalProperties": False,
        },
        handler=cards.list_cards,
        writes=False,
    ),
    ToolSpec(
        name="resolve_payee",
        description="按姓名或备注找收款人；同名多个时必须带卡尾号区分。",
        params={
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 50,
                    "description": "姓名或备注名，例如 李静 / 老妈",
                }
            },
            "required": ["name"],
            "additionalProperties": False,
        },
        handler=queries.resolve_payee,
        writes=False,
    ),
    ToolSpec(
        name="transfer",
        description="向通讯录收款人转账。第一阶段只建预授权，钱未扣款，等确认后才真正转出。",
        params={
            "type": "object",
            "properties": {
                "payee_id": {"type": "string", "minLength": 1, "maxLength": 64},
                "amount_fen": _AMOUNT_FEN,
                "note": {"type": "string", "maxLength": 100, "description": "转账备注"},
            },
            "required": ["payee_id", "amount_fen"],
            "additionalProperties": False,
        },
        handler=transfers.transfer,
        writes=True,
    ),
    ToolSpec(
        name="cancel_subscription",
        description="取消一条订阅扣款。已经取消过的再取消一次算幂等成功。",
        params={
            "type": "object",
            "properties": {"sub_id": {"type": "string", "minLength": 1, "maxLength": 64}},
            "required": ["sub_id"],
            "additionalProperties": False,
        },
        handler=subs.cancel_subscription,
        writes=True,
    ),
    ToolSpec(
        name="reserve_funds",
        description="预授权一笔钱：只占住额度，不产生转账流水，不扣款。",
        params={
            "type": "object",
            "properties": {
                "amount_fen": _AMOUNT_FEN,
                "purpose": {
                    "type": "string",
                    "maxLength": 50,
                    "description": "预授权用途，例如 酒店押金",
                },
            },
            "required": ["amount_fen"],
            "additionalProperties": False,
        },
        handler=transfers.reserve_funds,
        writes=True,
    ),
    ToolSpec(
        name="pay_merchant",
        description="在支持的商户付款：没有预授权就现建一笔再扣，有有效预授权就直接扣。",
        params={
            "type": "object",
            "properties": {
                "merchant": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 32,
                    "description": "商户代码，目前只有 flower_shop 与 cake_shop",
                },
                "item": {
                    "type": "string", "minLength": 1, "maxLength": 50, "description": "买了什么",
                },
                "amount_fen": _AMOUNT_FEN,
                "hold_id": {
                    "type": "string",
                    "maxLength": 64,
                    "description": "已有的预授权 ID；有就复用这笔额度付款",
                },
            },
            "required": ["merchant", "item", "amount_fen"],
            "additionalProperties": False,
        },
        handler=transfers.pay_merchant,
        writes=True,
    ),
)

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
