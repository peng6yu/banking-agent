from __future__ import annotations

import logging
from typing import Any, Final

from app import funds
from app.clock import BankError, TokenMismatch
from app.models import Action, Decision, ToolResult
from app.planner.events import emit_event, summarize
from app.planner.state import (
    CONTINUE,
    STOP,
    TOKEN_TTL_MS,
    Emit,
    NodeRec,
    RunState,
    balance_of,
    persist_node,
)
from app.security import token as token_mod
from app.tools import base as tool_base
from app.tools import registry as tools

log = logging.getLogger(__name__)

# transfer 工具只做第一阶段建 hold；docs/06 6.3 要求立刻 commit，这里补第二阶段
SETTLE_TOOLS: Final[frozenset[str]] = frozenset({"transfer"})
DISAMBIGUATE_LIMIT: Final[int] = 1

async def execute_node(
    state: RunState,
    node: Any,
    rec: NodeRec,
    params: dict[str, Any],
    action: Action,
    token: str | None,
    emit: Emit,
) -> str:
    """真正调工具；令牌由 require_token 在工具里再校验一次，这里不代劳。"""
    ctx = tool_base.ToolContext(
        conn=state.conn,
        session_id=state.session_id,
        node_id=node.id,
        task_id=state.run_id,
        account_id=state.account_id,
        secret=state.secret,
    )
    persist_node(state, rec, "running")
    try:
        result = tools.execute(node.tool, params, token=token, ctx=ctx)
    except TokenMismatch as exc:
        log.error("executor.token_mismatch node=%s reason=%s", node.id, exc.fields.get("reason"))
        persist_node(state, rec, "failed", error_code=exc.code)
        await emit_event(emit, "error", {"code": "TOKEN_REJECTED", "user_text": "凭证校验没通过，这笔我先停下了。"})
        return STOP
    except BankError as exc:
        log.warning("executor.tool_error node=%s code=%s", node.id, exc.code)
        result = ToolResult(ok=False, data={}, error_code=exc.code, error_text=exc.user_text)
    return await _settle(state, node, rec, result, emit)


async def _settle(state: RunState, node: Any, rec: NodeRec, result: ToolResult, emit: Emit) -> str:
    """执行后处理：收款人歧义闸门 → 补第二阶段扣款 → 记账 → 发 tool 事件。"""
    if not result.ok:
        rec.error_code = result.error_code
        persist_node(state, rec, "failed")
        summary = result.error_text or "这一步没成功。"
        await emit_event(emit, "tool", {"node_id": node.id, "ok": False, "summary": summary, "error_code": result.error_code})
        await emit_event(emit, "error", {"code": result.error_code or "TOOL_FAILED", "user_text": summary})
        return CONTINUE
    data = _commit_hold(state, node, result)
    question = _payee_gate(node, data)
    if question is not None:
        state.question = question
        rec.summary = question
        persist_node(state, rec, "done")
        await emit_event(emit, "tool", {"node_id": node.id, "ok": True, "summary": question})
        await emit_event(emit, "done", {"summary": question, "question": question, "balance_after_fen": balance_of(state)})
        return STOP
    state.results[node.id] = data
    rec.summary = summarize(node.tool, data)
    rec.result = data
    state.notes.append(rec.summary)
    state.balance_after_fen = balance_of(state)
    persist_node(state, rec, "done")
    await emit_event(emit, "tool", {"node_id": node.id, "ok": True, "summary": rec.summary})
    return CONTINUE


def _commit_hold(state: RunState, node: Any, result: ToolResult) -> dict[str, Any]:
    """transfer 工具只做第一阶段建 hold；docs/06 6.3 要求立刻 commit，这里补第二阶段。"""
    data = dict(result.data)
    hold_id = data.get("hold_id")
    if node.tool not in SETTLE_TOOLS or data.get("status") != "held" or not hold_id:
        return data
    try:
        outcome = funds.commit(state.conn, str(hold_id))
    except BankError as exc:
        log.warning("executor.commit_failed hold=%s code=%s", hold_id, exc.code)
        data["settle_error"] = exc.code
        return data
    data["status"] = "committed"
    data["balance_after_fen"] = outcome.balance_after_fen
    data["idempotent"] = outcome.idempotent
    state.balance_after_fen = outcome.balance_after_fen
    return data


def _payee_gate(node: Any, data: dict[str, Any]) -> str | None:
    """B3/B4：一个人都没找到或同名多人，都要停下来问用户，绝不猜。"""
    if node.tool != "resolve_payee":
        return None
    candidates = [item for item in (data.get("candidates") or []) if isinstance(item, dict)]
    if len(candidates) == DISAMBIGUATE_LIMIT:
        return None
    query = str(data.get("query") or "")
    if not candidates:
        return f"我没找到叫「{query}」的收款人，要不要换个名字或卡号尾号？"
    listing = "、".join(f"{item.get('name')}（尾号{item.get('card_tail')}）" for item in candidates[:3])
    return f"有 {len(candidates)} 位都叫「{query}」：{listing}。你要付给哪一位？"


def issue_token(state: RunState, action: Action, decision: Decision) -> str | None:
    """只有黄/红才签令牌；绿灯工具自己会 require_token 拒绝。"""
    if decision.level == "green":
        return None
    return token_mod.issue_audited(
        state.conn,
        action,
        session_id=state.session_id,
        task_id=state.run_id,
        level=decision.level,
        ttl_ms=TOKEN_TTL_MS,
        secret=state.secret,
    )

