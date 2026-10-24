from __future__ import annotations

import logging
from typing import Any, Final

from app import audit, clock, funds
from app.clock import BankError, TokenMismatch
from app.models import Action, Decision, ToolResult
from app.planner import dag as planner_dag
from app.planner.clarify import remember as remember_clarify
from app.planner.events import emit_event, summarize, trace_event
from app.planner.state import (
    CONTINUE,
    STOP,
    TOKEN_TTL_MS,
    Emit,
    NodeRec,
    RunState,
    balance_of,
    persist_decision,
    persist_node,
)
from app.policy.engine import decide
from app.policy.rules import PolicyContext
from app.security import token as token_mod
from app.tools import base as tool_base
from app.tools import registry as tools

log = logging.getLogger(__name__)

# 工具层已经把两阶段资金走完（transfer 内部 commit），planner 不碰 app/funds
DISAMBIGUATE_LIMIT: Final[int] = 1
# 会动钱、且参数里有明码金额的工具：金额红线要在这些节点上先裁决。
# 判的是节点声明的金额，不读任何余额工具的结果。
MONEY_TOOLS: Final[frozenset[str]] = frozenset({"transfer", "reserve_funds", "pay_merchant"})

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
    """执行后处理：收款人歧义闸门 → 记账 → 发 tool 事件。"""
    if not result.ok:
        rec.error_code = result.error_code
        persist_node(state, rec, "failed")
        summary = result.error_text or "这一步没成功。"
        await emit_event(emit, "tool", {"node_id": node.id, "ok": False, "summary": summary, "error_code": result.error_code})
        await emit_event(emit, "error", {"code": result.error_code or "TOOL_FAILED", "user_text": summary})
        return CONTINUE
    data = dict(result.data)
    question = _payee_gate(node, data)
    if question is not None:
        # 记下澄清上下文：下一轮用户只回「尾号4417」也能接回这句原话（app/planner/clarify.py）。
        remember_clarify(
            state.session_id,
            text=state.user_text,
            query=str(data.get("query") or ""),
            candidates=[dict(item) for item in (data.get("candidates") or []) if isinstance(item, dict)],
            question=question,
        )
        blocked = await block_on_amount(state, node, emit)
        if blocked is not None:
            return blocked
        state.question = question
        rec.summary = question
        persist_node(state, rec, "done")
        await emit_event(emit, "tool", {"node_id": node.id, "ok": True, "summary": question})
        await emit_event(
            emit,
            "done",
            {
                "summary": question,
                "question": question,
                "balance_after_fen": balance_of(state),
                # 候选收款人必须以结构化形式下发：前端要渲染可点的列表，
                # 而不是让用户照着一段中文自己敲尾号。
                "clarification": _payee_clarification(node, data, question),
            },
        )
        return STOP
    state.results[node.id] = data
    rec.summary = summarize(node.tool, data)
    rec.result = data
    state.notes.append(rec.summary)
    state.balance_after_fen = balance_of(state)
    persist_node(state, rec, "done")
    await emit_event(emit, "tool", {"node_id": node.id, "ok": True, "summary": rec.summary})
    return CONTINUE


def _payee_clarification(node: Any, data: dict[str, Any], question: str) -> dict[str, Any]:
    """把"等用户选收款人"这件事表达成结构化数据。

    候选里的手机号只给掩码：确认卡需要区分同名的人，不需要把完整号码摊开。
    """
    candidates = []
    for item in data.get("candidates") or []:
        if not isinstance(item, dict):
            continue
        candidates.append(
            {
                "payee_id": str(item.get("payee_id") or ""),
                "name": str(item.get("name") or ""),
                "bank": item.get("bank"),
                "card_tail": item.get("card_tail"),
                "alias": item.get("alias"),
                "phone_masked": _mask_phone(item.get("phone")),
            }
        )
    return {
        "kind": "payee",
        "node_id": node.id,
        "query": str(data.get("query") or ""),
        "count": len(candidates),
        "candidates": candidates,
        "question": question,
        # 用户既可以点列表，也可以直接回「尾号4417」或「第二个」。
        "accepts": ["select_candidate", "reply_text"],
        "note": "选好之后我会重新生成确认卡片；金额、收款人任何一项变了，旧的授权都会作废。",
    }


def _mask_phone(phone: Any) -> str | None:
    text = str(phone or "").strip()
    if len(text) < 7:
        return None
    return f"{text[:3]}****{text[-4:]}"


async def block_on_amount(state: RunState, node: Any, emit: Emit) -> str | None:
    """收款人问不出来的时候，先按金额判一次：红灯先落裁决，再决定是拒还是等因子。

    为什么这个顺序：R02/R07/R08 只看 action 里的金额，跟收款人是谁没关系。
    一笔 5 万的转账无论转给谁都过不了单笔限额——先反问「换个收款人名字试试」，
    等于用一句提示把用户往一条永远走不通的路上引，而且整条 policy.decided 审计永远不会落。
    只拦红灯：金额在黄区间的正常转账仍然先澄清收款人再弹确认，交互顺序不动。

    红灯有两种，不能混为一谈（t39，赛题口径是「强验证」不是「禁止」）：
    - 可验证的红（R07/R08/R09/R10，required_factors 非空）：策略层已经写明
      「face+sms 验完就能过」。这里只负责把裁决落下来（policy.decided + trace），
      然后放行收款人闸门；等 DAG 走到真正的花钱节点，由执行层弹 factor 事件、
      走双因子，验过才放款。没有因子就硬拒，等于把「等因子」说成「不可能」。
    - 不可执行的红（required_factors 为空，如 R03/R11/R12）：没有因子可验，
      才是终局拒绝（POLICY_BLOCKED + policy.blocked + 节点 failed）。

    返回 STOP 表示已经终局拒绝，返回 None 表示照旧走收款人歧义闸门。
    """
    target = _money_node_with_amount(state, node)
    if target is None:
        return None
    amount = _literal_amount(target.params)
    action = Action(tool=target.tool, params={"amount_fen": amount}, account_id=state.account_id, amount_fen=amount)
    now_ms = clock.now_ms()
    decision = decide(
        action,
        PolicyContext(
            spent_today_fen=funds.spent_today_fen(state.conn, state.account_id, at_ms=now_ms),
            now_ms=now_ms,
        ),
    )
    if decision.level != "red":
        return None
    rec = state.nodes[target.id]
    persist_decision(state, rec, dict(target.params), decision)
    audit.append(
        state.conn,
        "policy.decided",
        {
            "node_id": target.id,
            "tool": target.tool,
            "level": decision.level,
            "rule_id": decision.rule_id,
            "reason_code": decision.reason_code,
            "amount_fen": amount,
        },
        session_id=state.session_id,
    )
    await emit_event(emit, "trace", trace_event(target, action, decision))
    if decision.required_factors:
        # 可验证的红：裁决照落（policy.decided + trace 都在），但不终局拒绝。
        # 因子由执行层在真正的花钱节点弹（factor 事件 + 双因子），验过才放款。
        log.info(
            "runner.amount_red_waiting_factor session=%s node=%s rule=%s amount=%s factors=%s",
            state.session_id,
            target.id,
            decision.rule_id,
            amount,
            ",".join(decision.required_factors),
        )
        return None
    await emit_event(
        emit,
        "error",
        {"code": "POLICY_BLOCKED", "user_text": decision.reason_text or "这笔金额超过我这边能办的单子上限，我先不做了。"},
    )
    # 同上：策略拦截记 policy.blocked，别占用户「拒绝」的位置。
    audit.append(
        state.conn,
        "policy.blocked",
        {
            "node_id": target.id,
            "reason": "POLICY_BLOCKED",
            "stage": "PRE_PAYEE_GATE",
            "rule_id": decision.rule_id,
            "level": decision.level,
            "amount_fen": amount,
        },
        session_id=state.session_id,
    )
    persist_node(state, rec, "failed", error_code="POLICY_BLOCKED")
    log.warning("runner.amount_blocked session=%s node=%s rule=%s amount=%s", state.session_id, target.id, decision.rule_id, amount)
    return STOP


def _money_node_with_amount(state: RunState, node: Any) -> Any | None:
    """本图里第一个金额写死在参数上的花钱节点；引用取值的（说「全部」）等余额出来再说。"""
    if node.tool != "resolve_payee":
        return None
    for candidate in planner_dag.topo(state.dag):
        if candidate.tool in MONEY_TOOLS and _literal_amount(candidate.params) is not None:
            return candidate
    return None


def _literal_amount(params: dict[str, Any]) -> int | None:
    value = params.get("amount_fen")
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


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

