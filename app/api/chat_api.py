"""聊天接口：/api/chat 的 SSE、确认、因子挑战、会话恢复与回看（第 5 层）。"""

from __future__ import annotations

from fastapi import APIRouter

from typing import Any, Final
from fastapi.responses import FileResponse, StreamingResponse
import json

from app.models import Action, ChatRequest, ConfirmRequest, Decision, FactorChallengeRequest
from app.api.schemas import (
    AlertResolveRequest,
    BirthdayPayRequest,
    BirthdayPlanCreate,
    CancelRequest,
    ManualTransactionRequest,
    MonitorRunRequest,
    PlanCreate,
    PlanRunRequest,
    PlanUpdate,
    ReminderCreate,
    ResetRequest,
    SavingsGoalCreate,
    ScheduledChallengeRequest,
    ScheduledVerifyRequest,
    SavingsGoalUpdate,
    SimulatedExpenseRequest,
    SubscriptionVerdictClearRequest,
    SubscriptionVerdictRequest,
    SuitabilityAssessRequest,
)
from app.planner.clarify import augment as augment_clarification
from app.planner.clarify import forget as forget_clarification
import app.chat_history as chat_history
from app import audit, clock, db
from app.planner import assist, executor, intent, payment_guard, recover, ticker
from app.planner import events as planner_events
from app.api.http import payload
import app.use_cases as use_cases

from app.api.chat_flow import (
    _assist_parse,
    _events,
    _parse,
    _resume_session_id,
)

from app.api.chat_state import (
    SSE_HEADERS,
    SSE_MEDIA,
)

from app.api.chat_trace import (
    _remember,
)

#: 每个端点文件自带一个空 router，由 app/api/routes.py 统一挂到 /api 下。
router = APIRouter()




@router.post("/api/chat")
async def chat(request: ChatRequest) -> StreamingResponse:
    """对话主入口：先解析意图，再把执行过程用 SSE 一条条吐出来。

    没带 session_id 时接上最近一条会话（本机单用户演示），而不是每句话新开一条：
    前端丢一次 sessionStorage、换一个标签页、用 curl 试一句，都会落在同一条线上，
    助手才看得见前几轮说过什么。真要多开一条，客户端自己带一个新的 session_id 就行。
    """
    session_id = request.session_id or _resume_session_id()
    _remember(session_id, "user", request.text)
    # 上一轮如果停在收款人歧义上，这一轮的「尾号4417 / 第一个」要先补全再解析，
    # 否则解析器只看到一句没有金额的短回答，只能把问题再问一遍（已知限制 3）。
    resumed_text = augment_clarification(session_id, request.text)
    parse_text = resumed_text or request.text
    if resumed_text is None:
        forget_clarification(session_id)
    # 储蓄目标和提醒是确定性操作，先走快路：模型不用猜槽位，用户也不用一轮轮回答。
    parsed = _assist_parse(session_id, parse_text) or await _parse(session_id, parse_text)
    return StreamingResponse(
        # supersede：这句话是在回答上一轮的反问。答完之后那一轮就该结束，
        # 不能继续挂成「没走完的写操作」把这一轮也挡掉。
        _events(session_id, parsed, user_text=parse_text, supersede=resumed_text is not None),
        media_type=SSE_MEDIA,
        headers=SSE_HEADERS,
    )


@router.post("/api/confirm")
async def confirm(request: ConfirmRequest) -> dict[str, Any]:
    """回答一次确认；重复提交幂等，不会产生第二次扣款。"""
    result = await executor.resume(
        request.session_id,
        request.confirmation_id,
        request.approve,
        factor=request.factor,
        proofs=[item.model_dump() for item in (request.proofs or [])],
    )
    _remember(request.session_id, "user", "确认" if request.approve else "取消")
    return result


@router.post("/api/factor/challenge")
async def factor_challenge(request: FactorChallengeRequest) -> dict[str, Any]:
    """签发多因子挑战：真正的放行依据在服务端，前端只拿得到挑战号和一次验证码。"""
    return payload(use_cases.confirmations.challenge_factor(**request.model_dump()))


@router.post("/api/session/{session_id}/recover")
async def recover_session(session_id: str) -> dict[str, Any]:
    """服务重启后接着跑：库里有记录就能恢复，不用用户重说一遍。"""
    return await recover.resume(session_id)


@router.post("/api/cancel")
async def cancel(request: CancelRequest) -> dict[str, Any]:
    """中途取消：释放全部预授权，整轮 DAG 标 cancelled。"""
    return await executor.cancel(request.session_id, request.reason)


def _card_view(conn: Any, row: Any) -> dict[str, Any]:
    """一条确认记录 → 前端能直接渲染的卡片。

    刷新页面时服务端如果只回确认号，用户就得在看不见金额和收款人的情况下再签一次字。
    所以这里把 action_json 与 dag_nodes.decision_json 重新拼回一张完整的卡：
    字段和第一次推送时是同一套（planner.events.confirmation_frame），不是另写一份文案。
    历史数据解析不出来时 card 为 None，前端退回「只有确认号」的提示，不假装能签字。
    """
    item: dict[str, Any] = {
        "confirmation_id": str(row["id"]),
        "node_id": str(row["node_id"]),
        "status": str(row["status"]),
        "expires_ms": int(row["expires_ms"]),
        "expired": int(row["expires_ms"]) <= clock.now_ms(),
        "card": None,
    }
    try:
        action = Action.model_validate_json(row["action_json"])
        node = conn.execute(
            "SELECT tool, decision_json FROM dag_nodes WHERE run_id = ? AND node_id = ?",
            (row["run_id"], row["node_id"]),
        ).fetchone()
        raw = (node["decision_json"] if node else None) or ""
        if not raw:
            return item
        saved = json.loads(raw)
        # 老数据里只有 level/rule_id/required_factors（当时没存理由），缺的键补空串，
        # 不要用严格模型校验把整张卡判死；理由缺了就退回「看执行记录」的指引。
        decision = Decision(
            level=str(saved.get("level") or "yellow"),
            rule_id=str(saved.get("rule_id") or ""),
            reason_code=str(saved.get("reason_code") or ""),
            reason_text=str(saved.get("reason_text") or ""),
            required_factors=[str(factor) for factor in (saved.get("required_factors") or [])],
            limit_fen=saved.get("limit_fen"),
            spent_today_fen=saved.get("spent_today_fen"),
        )
        remaining = int(row["expires_ms"]) - clock.now_ms()
        item["card"] = planner_events.confirmation_frame(
            conn,
            confirmation_id=str(row["id"]),
            node_id=str(row["node_id"]),
            tool=str(node["tool"]),
            action=action,
            decision=decision,
            expires_in_s=max(0, remaining // 1000),
            status=str(row["status"]),
        )
    except Exception:  # noqa: BLE001 - 历史数据缺字段：宁可只回确认号，也不编卡片
        return item
    return item


@router.get("/api/session/{session_id}")
async def session(session_id: str) -> dict[str, Any]:
    """查会话：节点状态、待确认卡片、当前余额。"""
    with db.open_conn() as conn:
        head = conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
        if head is None:
            return {"session_id": session_id, "found": False}
        nodes = [
            {
                "node_id": row["node_id"],
                "tool": row["tool"],
                "state": row["state"],
                "decision": json.loads(row["decision_json"] or "null"),
                "result": json.loads(row["result_json"] or "null"),
            }
            for row in conn.execute(
                "SELECT * FROM dag_nodes WHERE run_id = (SELECT id FROM dag_runs WHERE session_id = ?"
                " ORDER BY created_ms DESC LIMIT 1) ORDER BY node_id",
                (session_id,),
            )
        ]
        cards = [_card_view(conn, row) for row in conn.execute(
            "SELECT * FROM confirmations WHERE session_id = ? ORDER BY created_ms", (session_id,)
        )]
        balance = conn.execute(
            "SELECT balance_fen FROM accounts ORDER BY CASE type WHEN 'debit' THEN 0 ELSE 1 END, id LIMIT 1"
        ).fetchone()
    return {
        "session_id": session_id,
        "found": True,
        "status": str(head["status"]),
        "created_ms": int(head["created_ms"]),
        "last_ms": int(head["last_ms"]),
        "nodes": nodes,
        "confirmations": cards,
        "balance_fen": int(balance["balance_fen"]) if balance is not None else None,
    }


@router.get("/api/chat/history")
async def chat_history_turns(session_id: str = "", limit: int = 20) -> dict[str, Any]:
    """某个会话最近几轮对话：刷新页面/换设备时把气泡画回来。"""
    sid = str(session_id or "").strip()
    if not sid:
        return {"ok": True, "session_id": "", "found": False, "turns": [], "title": ""}
    with db.open_conn() as conn:
        turns = chat_history.recent(conn, sid, limit=max(1, min(50, int(limit))))
        head = chat_history.title(conn, sid)
    return {
        "ok": True,
        "session_id": sid,
        "found": bool(turns),
        "title": head,
        "turns": turns,
    }


@router.get("/api/chat/latest")
async def chat_history_latest(limit: int = 20) -> dict[str, Any]:
    """最近一次真的聊过的会话：首屏没存 session_id 时接着它，而不是新开一个。"""
    with db.open_conn() as conn:
        data = chat_history.latest(conn, limit=max(1, min(50, int(limit))))
    return {"ok": True, **data}
