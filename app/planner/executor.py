from __future__ import annotations

import asyncio
import logging
from typing import Any, Final

from app import audit, clock, db, funds
from app.clock import BankError, ValidationError
from app.models import Action, Dag, Decision
from app.planner import dag as planner_dag
from app.planner.events import emit_event, prompt_event, prompt_payload, trace_event
from app.planner.runner import execute_node, issue_token
from app.planner.state import (
    BUSY_STATES,
    CONTINUE,
    RUNS,
    STOP,
    Confirmation,
    Emit,
    NodeRec,
    RunState,
    balance_of,
    confirm_timeout_ms as default_confirm_timeout_ms,
    factors_ok,
    finish_run,
    mark_confirmation,
    ms_to_seconds,
    new_confirmation,
    open_run,
    persist_decision,
    persist_node,
    release_holds,
    require_confirmation,
    require_run,
    resolve_confirmation,
    resolve_pending,
    set_run_status,
    wait_for_answer,
)
from app.policy.engine import decide
from app.policy.rules import SPEND_TOOLS, PolicyContext
from app.security import breaker, injection
from app.tools import base as tool_base

log = logging.getLogger(__name__)

# 熔断只对「安全边界主动拦下」计数。denials 表的唯一 INSERT 点在
# app/security/breaker.py，这里是生产链路里真正调用它的入口：没有这一行，
# 拒绝计数永远是 0，熔断就只是一段单测里的空转代码。
SECURITY_DENIAL_CODES: Final[frozenset[str]] = frozenset(
    {
        "POLICY_BLOCKED",   # 红色裁决且没给「就这么办」的出口，用户无法在本轮推进
        "POLICY_DENIED",    # 策略层在执行前抛拒
        "TOKEN_MISMATCH",   # app/clock.py 的异常码：能力令牌七步校验没过
        "TOKEN_REJECTED",   # 执行器对外事件码：runner 捕获 TokenMismatch 后转成它
    }
)


def is_security_denial(error_code: str | None) -> bool:
    """这个错误码算不算「一次拒绝」。

    算：安全边界主动拦下、用户在本轮无法靠正常操作翻过去的——见
    SECURITY_DENIAL_CODES。

    不算：用户自己说不要（USER_REJECTED）、没人点头超时（CONFIRM_TIMEOUT）、
    规划期参数写错（VALIDATION_*）、余额不足这类业务失败。
    理由：正常用户输错密码、输错金额后改主意，都不该被当成攻击者；连这种也
    计数的话，熔断第一个被熔掉的是诚实用户，而真正的攻击者照样畅通。
    """
    if not error_code:
        return False
    return error_code in SECURITY_DENIAL_CODES


def record_denial(state: RunState, *, reason: str, action: Action | None = None) -> int:
    """记一次拒绝（生产入口）；返回窗口内累计次数，供日志与测试观察。"""
    count = breaker.record_denial(state.conn, state.session_id, reason=reason, action=action)
    log.info("executor.denial reason=%s session=%s count=%d", reason, state.session_id, count)
    return count

def active_sessions() -> list[str]:
    """当前还在跑（或卡在确认）的会话 ID。"""
    return sorted(RUNS)


def get_run(session_id: str) -> RunState | None:
    """给 API 层查会话用；查不到返回 None 而不是抛错。"""
    return RUNS.get(session_id)


async def run(
    session_id: str,
    dag: Dag,
    emit: Emit,
    *,
    account_id: str | None = None,
    secret: str | None = None,
    confirm_timeout_ms: int | None = None,
    user_text: str = "",
) -> None:
    """跑一张执行图；只发事件，不返回业务数据。

    confirm_timeout_ms 的单位是**毫秒**；传 None 时取 state.confirm_timeout_ms()——
    默认 120_000（120 秒），可用环境变量 CONFIRM_TIMEOUT_S 覆盖；内部换算成秒再交给 asyncio。
    """
    if confirm_timeout_ms is None:
        confirm_timeout_ms = default_confirm_timeout_ms()
    state: RunState | None = None
    # 用户原话里的注入（IM 转发、夹带指令）不在工具参数里，只在这一轮认领：
    # 认领后并进每个 Action 的 untrusted_fields，R04 再把它升成双因子。
    taint = injection.claim_turn(session_id)
    try:
        state = open_run(session_id, dag, account_id, secret, user_text)
        RUNS[session_id] = state
        if taint is not None:
            audit_injection(
                state,
                source="user_text",
                fields=list(taint.fields),
                categories=taint.patterns,
                escalated=taint.level_floor != "green",
                level_floor=taint.level_floor,
            )
        await _drive(state, emit, confirm_timeout_ms, taint)
    except BankError as exc:
        log.warning("executor.run.failed code=%s session=%s", exc.code, session_id)
        if state is not None:
            _mark_failed(state, exc)
        await emit_event(emit, "error", exc.to_dict())
    except Exception as exc:  # noqa: BLE001 - 只把「数据库忙」翻译成人话，其余照抛
        # 「写锁没抢到」是可分类的临时故障，不该被当成未预期异常抛给最外层兜底：
        # 翻译成 SystemBusy 后用户看到的是「系统正忙」，节点也照常标 failed。
        busy = db.as_busy_error(exc)
        if busy is None:
            raise
        log.warning("executor.run.busy code=%s session=%s", busy.code, session_id)
        if state is not None:
            _mark_failed(state, busy)
        await emit_event(emit, "error", busy.to_dict())
    finally:
        injection.release_turn(taint)
        if state is not None:
            finish_run(state)


async def resume(session_id: str, confirmation_id: str, approve: bool, *, factor: str | None = None) -> dict[str, Any]:
    """回答一次确认；重复提交返回第一次的结果，不产生第二次副作用。"""
    state = require_run(session_id)
    conf = require_confirmation(state, confirmation_id)
    if conf.status != "pending":
        return {"ok": True, "status": conf.status, "idempotent": True}
    if not approve:
        resolve_confirmation(state, conf, "rejected", audit_reason="USER_REJECTED")
        return {"ok": True, "status": "rejected"}
    if not factors_ok(conf, factor):
        return {"ok": True, "status": "waiting_factor", "required": list(conf.required_factors)}
    resolve_confirmation(state, conf, "approved", audit_reason="USER_APPROVED")
    return {"ok": True, "status": "approved"}


async def cancel(session_id: str, reason: str = "用户中途取消") -> dict[str, Any]:
    """中途取消：释放本会话全部 held 预授权，节点标 skipped，整轮标 cancelled。"""
    state = require_run(session_id)
    state.cancelled = True
    state.cancel_reason = reason
    released = release_holds(state, reason)
    for rec in state.nodes.values():
        if rec.state in BUSY_STATES:
            persist_node(state, rec, "skipped")
    resolve_pending(state, {"cancelled": True})
    set_run_status(state, "cancelled")
    audit.append(state.conn, "session.end", {"reason": reason, "released_holds": released}, session_id=state.session_id)
    log.info("executor.cancel session=%s released=%d", session_id, released)
    return {"ok": True, "cancelled": True, "released_holds": released}


def close(session_id: str) -> None:
    """立刻收掉一轮（演示重置用）。"""
    state = RUNS.get(session_id)
    if state is None:
        return
    resolve_pending(state, {"cancelled": True})
    finish_run(state)


async def _drive(
    state: RunState,
    emit: Emit,
    confirm_timeout_ms: int,
    taint: injection.TurnTaint | None = None,
) -> None:
    """主循环：拓扑序逐个节点跑，跑不动的下游标 skipped。"""
    for node in planner_dag.topo(state.dag):
        if state.cancelled:
            return
        rec = state.nodes[node.id]
        if not _ready(state, node):
            persist_node(state, rec, "skipped", error_code="UPSTREAM_NOT_DONE")
            continue
        if await _step(state, node, rec, emit, confirm_timeout_ms, taint) == STOP:
            return
    await _done(state, emit)


async def _step(
    state: RunState,
    node: Any,
    rec: NodeRec,
    emit: Emit,
    confirm_timeout_ms: int,
    taint: injection.TurnTaint | None = None,
) -> str:
    """一个节点：解析参数 → 问策略 → 该确认就确认 → 执行。"""
    try:
        params = planner_dag.resolve_refs(node.params, state.results)
    except ValidationError as exc:
        # _fail_node 是协程：漏掉 await 时它根本不会执行（节点不会标 failed、也不发事件），
        # 这条分支等于没写。订阅名匹配不上要走的就是这里。
        return await _fail_node(state, rec, emit, exc)
    audit_param_hits(state, node, params, taint)
    action = build_node_action(state, node, params, taint)
    decision = ask_policy(state, action, node)
    persist_decision(state, rec, params, decision)
    await emit_event(emit, "trace", trace_event(node, action, decision))
    if decision.level != "green":
        approved, stop = await _guard(state, node, rec, action, decision, emit, confirm_timeout_ms)
        if stop:
            return STOP
        if not approved:
            return CONTINUE
    token = issue_token(state, action, decision)
    status = await execute_node(state, node, rec, params, action, token, emit)
    if is_security_denial(rec.error_code):
        # 执行阶段被安全边界拒掉（令牌七步没过 / 策略层抛拒）同样计一次；
        # 余额不足、参数写错、用户改主意都不计。
        record_denial(state, reason=str(rec.error_code), action=action)
    return status


def log_injection_hit(field: str, result: Any) -> None:
    """注入命中的日志回调：签名是 (字段名, 扫描结果) 两个位置参数。

    它是回调，不是 Logger。把 logging.Logger 实例直接传进 mark_from_params，
    用户备注里一出现注入词就会抛 TypeError: 'Logger' object is not callable，
    整轮被兜底成 INTERNAL 返回 500（t13 blocker）。
    """
    patterns = getattr(result, "patterns", ())
    log.info("injection.detected field=%s patterns=%s", field, ",".join(patterns))


def build_node_action(
    state: RunState,
    node: Any,
    params: dict[str, Any],
    taint: injection.TurnTaint | None = None,
) -> Action:
    """构造 Action；命中的注入字段交给 R04 自己升级验证等级。"""
    action = tool_base.build_action(node.tool, params, state.account_id, params.get("amount_fen"))
    hits = injection.scan_params(params)
    if hits:
        action = injection.mark_from_params(action, hits, log_hits=log_injection_hit)
    if taint is not None:
        # 原话里的注入照样登记成不可信字段：参数干净的那一步也得升红，
        # 否则「备注留白、注入写在用户消息里」就能绕过 R04
        for name in taint.fields:
            action = injection.mark_untrusted(action, name)
            log_injection_hit(
                name,
                injection.ScanResult(hit=True, patterns=taint.patterns, score=taint.score),
            )
    return action


def _escalates(tool: str, level_floor: str) -> bool:
    """这次注入命中会不会抬高验证等级：花费类动作走 R04，或本轮原话的注入下限不是绿。"""
    return tool in SPEND_TOOLS or level_floor != "green"


def audit_injection(
    state: RunState,
    *,
    source: str,
    fields: list[str],
    categories: tuple[str, ...],
    node: Any | None = None,
    escalated: bool = False,
    level_floor: str = "green",
) -> None:
    """把一次注入命中写进审计链。

    载荷里只有字段名、类别、来源和是否抬高了验证等级，绝不写命中的原文：
    审计链是只追加的，原文里可能有卡号、密码、别人的聊天内容，写进去就再也删不掉了。
    """
    payload: dict[str, Any] = {
        "source": source,
        "fields": list(fields),
        "categories": list(categories),
        "escalated": escalated,
        "level_floor": level_floor,
    }
    if node is not None:
        payload["node_id"] = node.id
        payload["tool"] = node.tool
    audit.append(state.conn, "injection.detected", payload, session_id=state.session_id)
    log.info(
        "injection.audited source=%s fields=%s categories=%s",
        source,
        ",".join(fields),
        ",".join(categories),
    )


def audit_param_hits(
    state: RunState,
    node: Any,
    params: dict[str, Any],
    taint: injection.TurnTaint | None = None,
) -> None:
    """工具参数里的命中（note / name / merchant / alias…）逐字段登记一条审计。

    只登记不拒绝：拦不拦、升不升级由 policy.decide 判，这里只负责留证据。
    """
    hits = injection.scan_params(params)
    if not hits:
        return
    categories = tuple(sorted({name for result in hits.values() for name in result.patterns}))
    audit_injection(
        state,
        source="tool_params",
        fields=sorted(hits),
        categories=categories,
        node=node,
        escalated=_escalates(node.tool, "red" if taint is not None else "green"),
        level_floor=taint.level_floor if taint is not None else "green",
    )


def ask_policy(state: RunState, action: Action, node: Any) -> Decision:
    """颜色只由 policy.decide() 给；执行器一行自判都没有。"""
    now_ms = clock.now_ms()
    ctx = PolicyContext(
        spent_today_fen=funds.spent_today_fen(state.conn, state.account_id, at_ms=now_ms),
        now_ms=now_ms,
    )
    decision = decide(action, ctx)
    audit.append(
        state.conn,
        "policy.decided",
        {
            "node_id": node.id,
            "tool": node.tool,
            "level": decision.level,
            "rule_id": decision.rule_id,
            "reason_code": decision.reason_code,
            "amount_fen": action.amount_fen,
        },
        session_id=state.session_id,
    )
    return decision


async def _guard(
    state: RunState,
    node: Any,
    rec: NodeRec,
    action: Action,
    decision: Decision,
    emit: Emit,
    confirm_timeout_ms: int,
) -> tuple[bool, bool]:
    """黄/红都要用户点头；返回 (是否放行, 是否整轮到此为止)。

    confirm_timeout_ms 的单位是**毫秒**。
    """
    conf = new_confirmation(state, node, action, decision)
    if decision.level == "red" and not decision.required_factors:
        await _blocked(state, node, conf, decision, emit, action)
        return False, True
    # 确认卡的 future 在 new_confirmation 里就建好了：答案早到（程序化客户端收到帧就回）
    # 也能被接住，不会被 resolve_pending 丢掉后干等 120 秒。
    await emit_event(emit, prompt_event(decision), prompt_payload(node, conf, decision))
    audit.append(
        state.conn,
        "confirm.requested",
        {"confirmation_id": conf.id, "node_id": node.id, "level": conf.level, "required": conf.required_factors},
        session_id=state.session_id,
    )
    persist_node(state, rec, "waiting_factor" if decision.level == "red" else "waiting_confirm")
    try:
        # wait_for 收的是**秒**，这里存的是**毫秒**（confirm_timeout_ms），换算别省。
        answer = await asyncio.wait_for(wait_for_answer(conf), ms_to_seconds(confirm_timeout_ms))
    except TimeoutError:
        await _timeout(state, rec, conf, emit)
        return False, True
    if answer.get("cancelled") or state.cancelled:
        return False, True
    if not answer.get("approve"):
        await _rejected(state, rec, conf, emit)
        return False, False
    return True, False


async def _blocked(
    state: RunState,
    node: Any,
    conf: Confirmation,
    decision: Decision,
    emit: Emit,
    action: Action | None = None,
) -> None:
    """E5：红色但没要因子——先告知被拦，再让用户改主意，不给「就这么办」的出口。"""
    mark_confirmation(state, conf, "expired")
    await emit_event(
        emit,
        "factor",
        {
            "confirmation_id": conf.id,
            "node_id": node.id,
            "level": decision.level,
            "required": [],
            "can_cancel": True,
            "message": decision.reason_text,
        },
    )
    audit.append(
        state.conn,
        "confirm.rejected",
        {"confirmation_id": conf.id, "node_id": node.id, "reason": "POLICY_BLOCKED"},
        session_id=state.session_id,
    )
    record_denial(state, reason="POLICY_BLOCKED", action=action)
    persist_node(state, state.nodes[node.id], "failed", error_code="POLICY_BLOCKED")
    await emit_event(emit, "error", {"code": "POLICY_BLOCKED", "user_text": decision.reason_text or "这笔操作被安全策略拦下了。"})


async def _timeout(state: RunState, rec: NodeRec, conf: Confirmation, emit: Emit) -> None:
    """E3：120 秒没人点头，这笔作废，预授权全部释放。"""
    mark_confirmation(state, conf, "expired")
    released = release_holds(state, "CONFIRM_TIMEOUT")
    audit.append(
        state.conn,
        "confirm.rejected",
        {"confirmation_id": conf.id, "node_id": rec.node_id, "reason": "TIMEOUT", "released_holds": released},
        session_id=state.session_id,
    )
    persist_node(state, rec, "failed", error_code="CONFIRM_TIMEOUT")
    state.notes.append(f"{rec.tool}：等确认超时，已作废。")
    await emit_event(
        emit,
        "error",
        {"code": "CONFIRM_TIMEOUT", "user_text": "等太久没等到你的确认，这笔我已经作废了，钱也退回账户了。"},
    )


async def _rejected(state: RunState, rec: NodeRec, conf: Confirmation, emit: Emit) -> None:
    """E2：用户说不——只标这一笔失败，下游自然 skipped。"""
    conf.status = "rejected"
    released = release_holds(state, "USER_REJECTED")
    audit.append(
        state.conn,
        "confirm.rejected",
        {"confirmation_id": conf.id, "node_id": rec.node_id, "reason": "USER_REJECTED", "released_holds": released},
        session_id=state.session_id,
    )
    persist_node(state, rec, "failed", error_code="USER_REJECTED")
    state.notes.append(f"{rec.tool}：你说不要，已经取消。")
    await emit_event(emit, "tool", {"node_id": rec.node_id, "ok": False, "summary": state.notes[-1], "error_code": "USER_REJECTED"})


async def _done(state: RunState, emit: Emit) -> None:
    """正常收尾：发 done，标 run/session 完成。"""
    summary = "；".join(state.notes) if state.notes else "已经处理完了。"
    set_run_status(state, "done")
    await emit_event(emit, "done", {"summary": summary, "balance_after_fen": balance_of(state), "question": state.question})


def _ready(state: RunState, node: Any) -> bool:
    """上游全部 done 才跑这一步（E1）。"""
    return all(state.nodes[parent].state == "done" for parent in node.depends_on if parent in state.nodes)


async def _fail_node(state: RunState, rec: NodeRec, emit: Emit, exc: BankError) -> str:
    """参数引用取不到值等规划期错误：这步失败，下游自然 skipped。"""
    persist_node(state, rec, "failed", error_code=exc.code)
    await emit_event(emit, "tool", {"node_id": rec.node_id, "ok": False, "summary": exc.user_text, "error_code": exc.code})
    await emit_event(emit, "error", exc.to_dict())
    return CONTINUE


def _mark_failed(state: RunState, exc: BankError) -> None:
    """整轮异常：把还在 busy 的节点一次性标 failed。"""
    set_run_status(state, "failed")
    for rec in state.nodes.values():
        if rec.state in BUSY_STATES:
            persist_node(state, rec, "failed", error_code=exc.code)

