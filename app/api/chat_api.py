"""聊天接口：/api/chat 的 SSE、确认、因子挑战、会话恢复与回看（第 5 层）。"""

from __future__ import annotations

from fastapi import APIRouter

from typing import Any, Final
from fastapi.responses import FileResponse, StreamingResponse

from app.models import ChatRequest, ConfirmRequest, FactorChallengeRequest
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
from app import db
from app.planner import assist, executor, intent, payment_guard, recover, ticker
from app.views.executions import session_detail_view
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


@router.get("/api/session/{session_id}")
async def session(session_id: str) -> dict[str, Any]:
    """查会话：节点状态、待确认卡片、当前余额。"""
    with db.open_conn() as conn:
        return session_detail_view(conn, session_id)


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
