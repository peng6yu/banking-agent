"""HTTP 装配层：把各端点模块挂成一个 router，并保留聊天链路的对外名字（第 5 层）。

W4 为什么拆：这个文件曾是 1320 行，聊天主流程、计划、钱相关端点全挤在一起，
改一处要滚很久才能确认没碰到别的。现在按「一次请求关心什么」拆成 9 个模块，
本文件只做两件事——把 router 拼起来，把外面（app/main.py、脚本、测试）用过的
名字重新导出。**装配是唯一职责，这里不写业务逻辑。**

    app/api/auth.py        口令校验
    app/api/chat_state.py  进程内状态（历史环、适配器、SSE 帧）
    app/api/chat_hints.py  缺槽位时的可点候选
    app/api/chat_trace.py  取证与记忆
    app/api/chat_flow.py   解析→澄清→建图→执行
    app/api/chat_api.py    /api/chat 及其同伴
    app/api/core_api.py    健康、工具清单、审计链、根路径
    app/api/plans_api.py   计划/生日/预约/提醒
    app/api/money_api.py   储蓄/订阅/理财/预警/记账/重置

依赖方向不变：api → planner → policy → tools → db，只允许向下。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from app.api import chat_api, core_api, money_api, plans_api
from app.api.auth import OPEN_PATHS, Unauthorized, _presented_token, require_api_token
from app.api.chat_api import (
    cancel,
    chat,
    confirm,
    factor_challenge,
    recover_session,
    session,
)
from app.api.chat_flow import (
    _assist_parse,
    _contextualize,
    _emit_assist,
    _events,
    _last_user_text,
    _learn,
    _memory_block,
    _parse,
    _parse_sync,
    _resume_session_id,
    _start_run,
)
from app.api.chat_hints import (
    _INTENT_EXAMPLES,
    _QUICK_CHOICES,
    _SLOT_QUESTION_HINTS,
    _broad_choices,
    _choices_for,
    _focused_slots,
)
from app.api.chat_state import (
    CONTEXT_CHARS,
    HISTORY_PER_SESSION,
    HISTORY_TURNS,
    INTERNAL_USER_TEXT,
    SSE_HEADERS,
    SSE_MEDIA,
    _adapter,
    _history,
    _SENTINEL,
    frame,
    get_adapter,
    set_adapter,
)
from app.api.core_api import audit_trail, audit_verify, health, index, list_tools
from app.api.chat_trace import (
    FALLBACK_AUDIT_NAME,
    _audit_failure,
    _audit_injection_reflection,
    _audit_intent,
    _audit_knowledge,
    _db_ok,
    _fallback_audit_path,
    _model_name,
    _remember,
    _turns,
    _write_fallback_audit,
)
#: 所有端点共用的父 router：口令校验挂在这一层，子 router 只声明路径。
router = APIRouter(dependencies=[Depends(require_api_token)])
router.include_router(core_api.router)
router.include_router(chat_api.router)
router.include_router(plans_api.router)
router.include_router(money_api.router)

__all__ = [
    "frame",
    "get_adapter",
    "router",
    "set_adapter",
    # 端点函数与聊天链路的内部名字：脚本与测试一直用 `routes.chat(...)` 这种写法，
    # 拆分不动调用方，这里原样再导出（不是新入口，改动仍应落在 app/api/*_api.py）。
    "audit_trail",
    "audit_verify",
    "cancel",
    "chat",
    "confirm",
    "factor_challenge",
    "health",
    "index",
    "list_tools",
    "recover_session",
    "session",
]
