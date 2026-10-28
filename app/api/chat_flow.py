"""聊天主流程：解析→澄清→建图→执行→推事件（第 5 层，写库只经用例）。"""

from __future__ import annotations

import logging

from typing import Any, Final
import asyncio

from app.clock import BankError, NotFound, ValidationError
from app.llm import LLMError, build_adapter
from app.planner import assist, executor, intent, payment_guard, recover, ticker
from app.planner import dag as planner_dag
import app.chat_history as chat_history
from app import audit, clock, db
from app.planner.clarify import forget as forget_clarification
from app.planner.clarify import remember_slots
import app.memory as memory
import app.memory_extract as memory_extract
from app.ids import new_id
import app.proactive as proactive
import app.scope as scope

from app.api.chat_hints import (
    _choices_for,
)

from app.api.chat_state import (
    CONTEXT_CHARS,
    INTERNAL_USER_TEXT,
    _SENTINEL,
    frame,
    get_adapter,
)

from app.api.chat_trace import (
    _audit_failure,
    _audit_injection_reflection,
    _audit_intent,
    _audit_knowledge,
    _remember,
    _turns,
    _write_fallback_audit,
)

log = logging.getLogger(__name__)


def _resume_session_id() -> str:
    """没带会话号时接上最近一条有内容的会话；一条都没有才新建。

    读失败（库不可用）时退回新会话：接不上历史最多是「这轮没上下文」，
    但不能因为查一次历史把整轮对话挡在 500 上。
    """
    try:
        with db.open_conn() as conn:
            latest = chat_history.latest(conn, limit=1)
        found = str(latest.get("session_id") or "")
        if found:
            return found
    except Exception:  # noqa: BLE001 - 历史查不到就当没有历史
        log.warning("api.chat.resume_failed", exc_info=True)
    return new_id("s")


def _assist_parse(session_id: str, text: str) -> intent.IntentResult | None:
    """命中储蓄/提醒关键词就构造一个 assist 结果；没命中返回 None 走模型。"""
    if assist.detect(text) is None:
        return None
    parsed = intent.IntentResult(
        intent="assist",
        slots={"assist_text": text},
        confidence=1.0,
        source="assist",
    )
    _audit_intent(session_id, text, parsed)
    return parsed


async def _emit_assist(
    session_id: str,
    parsed: intent.IntentResult,
    queue: asyncio.Queue[Any],
    *,
    user_text: str,
) -> None:
    """assist 快路：同步算完再一次性发 done，不建图、不动钱。"""
    text = user_text or str(parsed.slots.get("assist_text") or "")
    try:
        with db.open_conn() as conn:
            result = await asyncio.to_thread(assist.handle, conn, text)
    except BankError as exc:
        result = {"summary": exc.user_text, "choices": [], "action": "assist_error", "data": {}}
    except Exception:  # noqa: BLE001 - 快路失败退回一句人话，别让整轮会话断掉
        log.exception("api.chat.assist_failed session=%s", session_id)
        result = {
            "summary": "这个操作没做成，换个说法再试一次？",
            "choices": [],
            "action": "assist_error",
            "data": {},
        }
    if not result:
        result = {"summary": "没太理解这句，可以换个说法。", "choices": [], "action": "assist_none", "data": {}}
    summary = str(result.get("summary") or "")
    _remember(session_id, "assistant", summary)
    _learn(session_id, text, summary)
    await queue.put(
        (
            "done",
            {
                "summary": summary,
                "intent": "assist",
                "action": result.get("action"),
                "choices": result.get("choices") or [],
                "data": result.get("data") or {},
            },
        )
    )
    await queue.put((_SENTINEL, {}))


async def _parse(session_id: str, text: str) -> intent.IntentResult:
    """解析意图并写审计；模型不可用时把 LLMError 原样抛给处理器。"""
    try:
        parsed = await asyncio.to_thread(
            _parse_sync,
            text,
            _turns(session_id),
            session_id=session_id,
            memory_block=await asyncio.to_thread(_memory_block),
        )
    except LLMError:
        _audit_failure(session_id, text, "MODEL_UNAVAILABLE")
        raise
    _contextualize(session_id, parsed)
    _audit_intent(session_id, text, parsed)
    _audit_knowledge(session_id, parsed)
    _audit_injection_reflection(session_id, parsed)
    return parsed


def _contextualize(session_id: str, parsed: intent.IntentResult) -> None:
    """没听懂时把上一句的话题带进反问里——「你到底想办什么」这种空话等于没有记忆。

    只改「没槽位可问」的两类：intent=unknown 与置信度不够。缺槽位的反问（给谁转、
    多少钱）已经有确定的问题，再叠一句上下文只会把问句变长。
    有上一句就念出它（截断到 CONTEXT_CHARS），让用户看着自己刚说的话改口；
    没有上一句（今天第一句）就保持原来的问句，不硬编上下文。
    """
    if parsed.missing:
        return
    if parsed.intent != "unknown" and not (0 < parsed.confidence < intent.CONFIDENCE_FLOOR):
        return
    topic = _last_user_text(session_id)
    if not topic:
        return
    head = f"上一句你问的是「{topic}」，这一句我没太跟上。"
    if parsed.intent == "unknown":
        parsed.question = f"{head}是想接着这件事往下办，还是换一件事？说得再具体一点我就动手。"
    else:
        parsed.question = f"{head}{parsed.question}"


def _last_user_text(session_id: str) -> str:
    """这条会话里「上一句」用户说的话——截断到 CONTEXT_CHARS。

    当前这句话在进解析之前就已经 _remember 进历史了，所以最近那条 user 轮要跳过，
    否则反问里会念用户刚说过的同一句，看起来像在复读。
    """
    skipped_current = False
    for item in reversed(_turns(session_id)):
        if str(item.get("role")) != "user":
            continue
        if not skipped_current:
            skipped_current = True
            continue
        text = str(item.get("content") or "").strip()
        if text:
            return text[:CONTEXT_CHARS] + ("…" if len(text) > CONTEXT_CHARS else "")
    return ""


def _memory_block() -> str:
    """这位用户的长期记忆（模型上下文里的「他是谁」）；没有记忆或读不出来就返回空串。

    记忆是增强项：库读不出来只记一条 warning，绝不让一轮对话因为「想不起前人是谁」而失败。
    """
    try:
        with db.open_conn() as conn:
            return memory.prompt_block(conn, user_id=proactive.default_user_id(conn))
    except Exception:  # noqa: BLE001 - 记忆读失败只当没有记忆
        log.warning("api.chat.memory_block_failed", exc_info=True)
        return ""


def _learn(session_id: str, user_text: str, assistant_text: str) -> None:
    """一轮说完之后在后台抽长期记忆：不阻塞回包、失败只记日志。

    这是「越用越聪明」的那一半：用户不用专门说「请记住」，助手从对话里自己捡事实。
    显式记忆（/api/memory、快路的「记住…」）走同步路径，用户点完立刻能看到。
    """
    if not user_text.strip():
        return

    async def worker() -> None:
        try:
            with db.open_conn() as conn:
                await asyncio.to_thread(
                    memory_extract.run,
                    conn,
                    user_id=proactive.default_user_id(conn),
                    user_text=user_text,
                    assistant_text=assistant_text,
                    adapter=get_adapter(),
                )
        except Exception:  # noqa: BLE001 - 学不会也不能影响对话
            log.warning("api.chat.learn_failed session=%s", session_id, exc_info=True)

    try:
        asyncio.get_running_loop().create_task(worker())
    except RuntimeError:  # 没有事件循环（同步测试）就干脆不学
        _ = session_id


def _parse_sync(
    text: str,
    history: list[dict[str, str]],
    *,
    session_id: str | None = None,
    memory_block: str = "",
) -> intent.IntentResult:
    """在线程里跑一次同步阻塞的模型调用，别把事件循环堵住。

    session_id 一路带到底：原话命中注入时要压一根「属于这个会话」的棒，
    执行层取棒时按会话认领——接力棒不许跨会话串味（app/security/injection.py）。
    """
    return asyncio.run(
        intent.parse(
            text,
            history,
            adapter=get_adapter(),
            session_id=session_id,
            memory_block=memory_block,
        )
    )


async def _events(
    session_id: str,
    parsed: intent.IntentResult,
    *,
    user_text: str = "",
    supersede: bool = False,
) -> Any:
    """SSE 生成器：先发 start / intent，反问就到此为止，否则挂上执行器。"""
    queue: asyncio.Queue[str | tuple[str, dict[str, Any]]] = asyncio.Queue()
    # 纵深防御：即便将来有人重构掉意图层的 guard，接口层也拿原话再挡一次。
    # 命中时改的是同一个 IntentResult，SSE 的 intent 帧与审计都看得到原因。
    out_of_scope = False
    if user_text:
        risk = payment_guard.inspect(user_text)
        if risk.ambiguous and not (
            parsed.intent == "split_bill" and payment_guard.has_split_marker(user_text)
        ):
            parsed.question = parsed.question or risk.question
            parsed.guard_reason = parsed.guard_reason or risk.reason
            parsed.ok = False
        # 业务边界：这个助手只处理钱的事。与钱无关的请求既不建对象也不走 DAG，
        # 只说明边界并给出最接近的资金动作（app/scope.py 是唯一出处）。
        if parsed.intent == "unknown":
            boundary = scope.out_of_scope_reply(user_text)
            if boundary:
                parsed.question = boundary
                parsed.ok = False
                out_of_scope = True
    await queue.put(("start", {"session_id": session_id, "now_ms": clock.now_ms()}))
    await queue.put(("intent", parsed.to_event() | {"session_id": session_id}))
    if parsed.intent == "assist":
        await _emit_assist(session_id, parsed, queue, user_text=user_text)
    elif parsed.question:
        _remember(session_id, "assistant", parsed.question)
        _learn(session_id, user_text, parsed.question)
        # 反问轮记一笔上下文：下一句「100 元」「李静」要能拼回这句问话，
        # 否则照着选项点下去只会被当成新消息重新解析（点了等于白点）。
        remember_slots(session_id, text=user_text, question=parsed.question, slots=parsed.missing)
        await queue.put(
            (
                "done",
                {
                    "summary": parsed.question,
                    "question": parsed.question,
                    "out_of_scope": out_of_scope,
                    "choices": await asyncio.to_thread(_choices_for, parsed),
                },
            )
        )
        await queue.put((_SENTINEL, {}))
    else:
        # 真的要执行了：上一轮的「待补槽位」作废，别让下一句又把旧话拼回来。
        forget_clarification(session_id)
        _start_run(session_id, parsed, queue, user_text=user_text, supersede=supersede)
    while True:
        item = await queue.get()
        if isinstance(item, tuple) and item[0] == _SENTINEL:
            return
        name, data = item  # type: ignore[misc]
        yield frame(name, data)


def _start_run(
    session_id: str,
    parsed: intent.IntentResult,
    queue: asyncio.Queue[Any],
    *,
    user_text: str = "",
    supersede: bool = False,
) -> None:
    """建图并在后台跑；解析失败要退化成单步图，不能让整轮会话死掉。"""
    built = planner_dag.build_or_single(
        {"intent": parsed.intent, "slots": parsed.slots},
        cross=parsed.knowledge_cross or None,
    )

    async def emit(name: str, data: dict[str, Any]) -> None:
        if name == "done":
            summary = str(data.get("summary") or "")
            # 执行轮也要把助手的话记进对话记忆：只记用户那一句的话，下一轮模型看到的
            # 就是一串连续的用户消息，会把新的一句当成上一句的补充（实测「查一下我这个
            # 月花了多少」之后接「帮我给张三转 800 元」会被解析成账单分析）。
            if summary:
                _remember(session_id, "assistant", summary)
            # 执行完这一轮再学：用户说了什么、助手最后怎么回的，一起交给记忆抽取。
            _learn(session_id, user_text, summary)
        await queue.put((name, data))

    async def driver() -> None:
        try:
            # user_text 交给执行层：澄清闸门要用原话把短回答补全。
            await executor.run(session_id, built, emit, user_text=user_text, supersede=supersede)
        except Exception as exc:  # noqa: BLE001 - 执行期任何异常都只能降级成 INTERNAL
            trace_id = new_id("tr")
            log.exception("api.chat.driver_failed session=%s trace=%s", session_id, trace_id)
            _write_fallback_audit(
                "run.failed",
                {"error_type": type(exc).__name__, "node_tool": None},
                session_id=session_id,
                trace_id=trace_id,
                reason="run_failed",
            )
            # 回包里带上 trace_id：用户报「系统开小差」时能对上这条降级记录
            await queue.put(
                ("error", {"code": "INTERNAL", "user_text": INTERNAL_USER_TEXT, "trace_id": trace_id})
            )
        finally:
            await queue.put((_SENTINEL, {}))

    asyncio.create_task(driver())
