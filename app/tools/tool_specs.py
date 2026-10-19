"""工具权威表：14 个工具的名字、参数模式、处理器与写标记。

对应文档：docs/06_工具层与场景实现规格.md 6.1/6.2。

docs/06 6.1 的 10 个基础工具按原顺序在前，场景三新增的四个理财工具追加在后，
名字是权威表，TOOL_NAMES 会拿去做回归测试；不在表里的工具名就是 ToolNotFound。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Final

from app.models import ToolResult
from app.money import MAX_SINGLE_FEN
import app.tools.bill as bill
import app.tools.cards as cards
import app.tools.invest as invest
import app.tools.products as products
import app.tools.queries as queries
import app.tools.subs as subs
import app.tools.transfers as transfers
from app.tools.base import ToolContext

_AMOUNT_FEN: Final[dict[str, Any]] = {
    "type": "integer",
    "minimum": 1,
    "maximum": MAX_SINGLE_FEN,
    "description": "金额，单位分（1 元 = 100 分），必须是最小的整数分",
}
_PRODUCT_CODE: Final[dict[str, Any]] = {
    "type": "string",
    "minLength": 2,
    "maxLength": 32,
    "description": "基金产品代码，用 list_products 查到的 code 原样传回来",
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
    ToolSpec(
        name="list_products",
        description=(
            "列出现在在售的基金，带风险等级、起购金额、最新净值和我持有的份额；"
            "可以按风险等级或类型筛选。"
        ),
        params={
            "type": "object",
            "properties": {
                "risk_level": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 5,
                    "description": "只看这个风险等级（R1 最稳、R5 最激进）",
                },
                "category": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 12,
                    "description": "只看某一类，合法值：" + "、".join(products.CATEGORY_LABELS),
                },
            },
            "required": [],
            "additionalProperties": False,
        },
        handler=products.list_products,
        writes=False,
    ),
    ToolSpec(
        name="estimate_risk",
        description="买之前算一笔：这只基金多险、起购多少、这笔钱能买到多少份、有什么提示。",
        params={
            "type": "object",
            "properties": {
                "product_code": _PRODUCT_CODE,
                "amount_fen": _AMOUNT_FEN,
            },
            "required": ["product_code", "amount_fen"],
            "additionalProperties": False,
        },
        handler=products.estimate_risk,
        writes=False,
    ),
    ToolSpec(
        name="buy_fund",
        description="申购基金：钱从卡里扣，换成基金份额。金额不能低于该产品的起购金额。",
        params={
            "type": "object",
            "properties": {
                "product_code": _PRODUCT_CODE,
                "amount_fen": _AMOUNT_FEN,
                "note": {"type": "string", "maxLength": 50, "description": "备注，可不填"},
            },
            "required": ["product_code", "amount_fen"],
            "additionalProperties": False,
        },
        handler=invest.buy_fund,
        writes=True,
    ),
    ToolSpec(
        name="redeem_fund",
        description=(
            "赎回基金：把持有的份额卖掉换回现金，钱回到卡里。"
            "金额可以是一次赎完，也可以只赎一部分。"
        ),
        params={
            "type": "object",
            "properties": {
                "product_code": _PRODUCT_CODE,
                "amount_fen": _AMOUNT_FEN,
                "note": {"type": "string", "maxLength": 50, "description": "备注，可不填"},
            },
            "required": ["product_code", "amount_fen"],
            "additionalProperties": False,
        },
        handler=invest.redeem_fund,
        writes=True,
    ),
)
