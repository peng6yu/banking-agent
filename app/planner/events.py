"""SSE 事件与文案：把内部状态翻译成前端能直接渲染的字段。

事件名与字段名和 docs/07 7.6 一一对应，和已实现的前端 web/index.html 对齐：
start / intent / trace / confirm / factor / tool / done / error，一个不多一个不少。
"""

from __future__ import annotations

import inspect
from typing import Any

from app.models import Action, Decision
from app.money import fen_to_yuan_str
from app.planner.state import (
    CONFIRM_TIMEOUT_MS,
    Confirmation,
    Emit,
    action_text,
    tool_name,
)
from app.policy.engine import explain


def summarize(tool: str, data: dict[str, Any]) -> str:
    """一句话摘要：给前端 tool 事件和最终 done 都用。"""
    amount = fen_to_yuan_str(int(data.get("amount_fen") or 0))
    if tool == "transfer":
        return f"已转出 {amount} 元"
    if tool == "reserve_funds":
        return f"已预留 {amount} 元"
    if tool == "pay_merchant":
        return f"已在 {data.get('merchant', '商户')} 支付 {amount} 元"
    if tool == "cancel_subscription":
        return f"已取消订阅「{data.get('name') or data.get('sub_id')}」"
    if tool == "query_balance":
        return f"余额 {_first_account(data).get('balance_yuan', '0.00')} 元"
    if tool == "query_transactions":
        return f"共查到 {data.get('count', 0)} 笔交易"
    if tool == "analyze_bill":
        return f"账单支出合计 {data.get('total_out_yuan', '0.00')} 元"
    if tool == "list_subs":
        return f"在订订阅 {data.get('count', 0)} 项，每月 {data.get('monthly_total_yuan', '0.00')} 元"
    if tool == "list_cards":
        return f"名下卡片 {data.get('count', 0)} 张"
    if tool == "resolve_payee":
        return f"找到收款人 {data.get('query', '')}"
    if tool in CARD_WRITE_TOOLS:
        # 卡片五件套各自带一句人话（含幂等与挂失终态的差别），直接用工具的话
        return str(data.get("summary") or f"{tool} 执行完成")
    return f"{tool} 执行完成"


CARD_WRITE_TOOLS = frozenset({
    "apply_virtual_card",
    "set_card_limit",
    "freeze_card",
    "unfreeze_card",
    "report_card_loss",
})


def _first_account(data: dict[str, Any]) -> dict[str, Any]:
    accounts = [item for item in (data.get("accounts") or []) if isinstance(item, dict)]
    return accounts[0] if accounts else {}



def prompt_event(decision: Decision) -> str:
    """红绿黄三色对应三种事件：factor 要双因子，confirm 只要点头。"""
    return "factor" if decision.level == "red" else "confirm"


def prompt_payload(node: Any, conf: Confirmation, decision: Decision) -> dict[str, Any]:
    amount = conf.action.amount_fen
    return {
        "confirmation_id": conf.id,
        "node_id": node.id,
        "tool": node.tool,
        "level": conf.level,
        "rule_id": decision.rule_id,
        "action_text": conf.action_text,
        "amount_fen": amount,
        "amount": fen_to_yuan_str(amount) if amount else None,
        "amount_yuan": fen_to_yuan_str(amount) if amount else None,
        "payee_name": conf.payee_name,
        "slots": {"payee_name": conf.payee_name} if conf.payee_name else {},
        "required": list(conf.required_factors),
        "expires_in_s": CONFIRM_TIMEOUT_MS // 1000,
        "reason_text": decision.reason_text,
    }


def trace_event(node: Any, action: Action, decision: Decision) -> dict[str, Any]:
    """trace 事件：前端策略卡片直接吃这个。"""
    return {
        "node_id": node.id,
        "tool": node.tool,
        "tool_name": tool_name(node.tool),
        "level": decision.level,
        "rule_id": decision.rule_id,
        "reason_text": decision.reason_text,
        "explain": explain(decision, action),
        "amount_fen": action.amount_fen,
    }


async def emit_event(emit: Emit, name: str, data: dict[str, Any]) -> None:
    """发一个 SSE 事件；emit 可以是同步函数也可以是协程。"""
    result = emit(name, data)
    if inspect.isawaitable(result):
        await result


__all__ = ["action_text", "emit_event", "prompt_event", "prompt_payload", "summarize", "tool_name", "trace_event"]
