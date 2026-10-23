from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field, replace
from typing import Any, Iterator

from .config import LLMTarget, Settings, get_settings
from app.log import get_logger
from app.script_mode import (
    EMIT_INTENT_NAME,
    script_first_tool_name,
    script_intent_args,
    script_user_text,
)

_log = get_logger("app.llm")

# M2.1 会把思维链塞进 content，M3.1 不会。适配器无条件剥离，上层永远看不到。
THINK_RE = re.compile(r"<think>(.*?)</think>", re.S)

RETRYABLE_STATUS = {429, 500, 502, 503, 504}

# ---------------------------------------------------------------------------
# 多厂商自动降级：主模型不可用时按配置顺序切到下一个目标
# ---------------------------------------------------------------------------
# 明确「这个目标挂了、换一个可能能好」的状态码：鉴权失败、模型不存在、
# 请求超时、限流。5xx 一律算服务端故障。
FALLBACK_STATUS = frozenset({401, 403, 404, 408, 429})
# 请求体本身不合法：换厂商也救不了，直接抛错，不触发降级。
NON_FALLBACK_STATUS = frozenset({400, 422})


def _status_triggers_fallback(code: int | None) -> bool:
    """这个状态码该不该切到下一个目标。

    400/422 是请求体不合法 → 不降级；
    401/403/404/408/429 与所有 5xx → 换下一个目标还有救。
    """
    if code is None:
        return False
    if code in NON_FALLBACK_STATUS:
        return False
    if code in FALLBACK_STATUS:
        return True
    return 500 <= code <= 599


class LLMError(RuntimeError):
    def __init__(self, message: str, status: int | None = None, retryable: bool = False) -> None:
        super().__init__(message)
        self.status = status
        self.retryable = retryable


@dataclass(frozen=True)
class _TargetFailure:
    """单个目标的失败摘要。

    只保存可公开的信息：base、模型、错误类别、状态码、异常类型。
    **绝不含 key**——错误消息和日志都从这里取。
    """

    base_url: str
    model: str
    label: str
    category: str
    status: int | None
    detail: str
    error_type: str
    retryable: bool = False


class _TargetError(Exception):
    """内部信号：某个目标彻底失败了。

    error 是要抛给上层的异常本体（不降级时原样抛出，保持旧行为不变）；
    failure 是可公开展示的失败摘要。
    """

    def __init__(self, failure: _TargetFailure, error: BaseException, fallback: bool) -> None:
        super().__init__(str(error))
        self.failure = failure
        self.error = error
        self.fallback = fallback


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: str = "{}"

    def args(self) -> dict[str, Any]:
        try:
            val = json.loads(self.arguments or "{}")
        except json.JSONDecodeError:
            return {}
        return val if isinstance(val, dict) else {}


@dataclass
class LLMResponse:
    content: str = ""
    reasoning: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    finish_reason: str = ""
    model: str = ""
    usage: dict[str, Any] = field(default_factory=dict)
    latency_ms: int = 0
    # 实际服务的目标（降级链里可能不是主目标）：给上层与审计显示用。
    # model 字段语义不变：仍然是「模型自己报的模型名」。
    served_by: str = ""
    served_endpoint: str = ""

    def first_call(self, name: str) -> ToolCall | None:
        for call in self.tool_calls:
            if call.name == name:
                return call
        return None


def split_think(text: str) -> tuple[str, str]:
    if not text:
        return "", ""
    reasoning = "\n".join(THINK_RE.findall(text)).strip()
    return THINK_RE.sub("", text).strip(), reasoning


class LLMAdapter:
    """唯一对外入口。业务代码只调 chat / chat_stream，不碰 HTTP。"""

    def __init__(self, settings: Settings | None = None, max_retries: int = 3) -> None:
        self.s = settings or get_settings()
        self.max_retries = max_retries
        self.calls = 0
        # 最近一次真正服务的目标（降级链里可能不是主目标），供上层与审计显示。
        self.last_target: LLMTarget | None = None

    # ---------- 内部：目标链 ----------
    def _primary_target(self) -> LLMTarget:
        return LLMTarget(
            base_url=self.s.base_url,
            api_key=self.s.api_key,
            model=self.s.model_brain,
            endpoint=self.s.endpoint,
            label="primary",
        )

    def _configured_targets(self) -> tuple[LLMTarget, ...]:
        """配置里的目标链（主目标在第 0 位）。"""
        targets = getattr(self.s, "fallback_targets", None)
        if targets:
            return tuple(targets)
        return (self._primary_target(),)

    def _targets(self, model: str | None = None) -> tuple[LLMTarget, ...]:
        """本次请求要依次尝试的目标。

        model 只作用在主目标上（调用方点名要某个模型时，仍然先打主厂商，
        主厂商挂了再按配置顺序降级）；降级目标用各自配置的模型名。
        """
        configured = self._configured_targets()
        requested = (model or "").strip()
        primary = configured[0]
        if requested and requested != primary.model:
            primary = replace(primary, model=requested)
        chain: list[LLMTarget] = [primary]
        for target in configured[1:]:
            chain.append(target)
        # 同 base 同模型只留一个，避免同一个目标被试两遍
        seen: set[tuple[str, str]] = set()
        unique: list[LLMTarget] = []
        for target in chain:
            key = (target.base_url, target.model)
            if key in seen:
                continue
            seen.add(key)
            unique.append(target)
        return tuple(unique)

    def _known_keys(self) -> list[str]:
        """当前配置里用到的所有 key——只用来把消息里的 key 片段抹掉。"""
        keys: list[str] = []
        for target in self._configured_targets():
            if target.api_key:
                keys.append(target.api_key)
        return keys

    def _scrub(self, text: str) -> str:
        """抹掉可能混进消息的 key 片段：错误消息与日志绝不许出现密钥。"""
        for key in self._known_keys():
            if key and key in text:
                text = text.replace(key, "***")
        return text

    def _target_error(
        self,
        target: LLMTarget,
        cause: BaseException,
        error: BaseException,
        category: str,
        status: int | None,
    ) -> _TargetError:
        fallback = True if category != "http" else _status_triggers_fallback(status)
        if category == "http" and status is not None:
            # 类别带上状态码：错误消息里一眼能看出是 503 还是 404
            category = "http_" + str(status)
        failure = _TargetFailure(
            base_url=target.base_url,
            model=target.model,
            label=target.label,
            category=category,
            status=status,
            detail=self._scrub(str(error)),
            error_type=type(cause).__name__,
            retryable=bool(getattr(error, "retryable", False)),
        )
        return _TargetError(failure=failure, error=error, fallback=fallback)

    def _log_switch(self, target: LLMTarget, failure: _TargetFailure, next_target: LLMTarget | None) -> None:
        """降级切换打一条结构化日志：base、模型、异常类型——没有 key。"""
        _log.warn(
            "llm.fallback.switch",
            base=target.base_url,
            model=target.model,
            error_type=failure.error_type,
            error_category=failure.category,
            status=failure.status,
            next_base=next_target.base_url if next_target else None,
            next_model=next_target.model if next_target else None,
        )

    def _all_failed(self, failures: list[_TargetFailure]) -> LLMError:
        """所有目标都失败：抛一条逐条列出 base/模型/错误类别的错误（不含 key）。"""
        lines = ["所有模型目标均失败（" + str(len(failures)) + " 个目标）："]
        for index, failure in enumerate(failures, start=1):
            lines.append(
                "  "
                + str(index)
                + ") 目标="
                + failure.label
                + " base="
                + failure.base_url
                + " model="
                + failure.model
                + " 错误类别="
                + failure.category
                + " 说明："
                + failure.detail
            )
        message = self._scrub("\n".join(lines))
        last = failures[-1]
        return LLMError(
            message,
            status=last.status,
            retryable=any(item.retryable for item in failures),
        )

    # ---------- 内部 ----------
    def _headers(self, target: LLMTarget | None = None) -> dict[str, str]:
        tgt = target or self._primary_target()
        if not tgt.api_key:
            if tgt.label == "primary":
                raise LLMError(
                    "模型 API key 未配置：设置 LLM_API_KEY（兼容旧名 MINIMAX_API_KEY），"
                    "或复制 .env.example 为 .env 后填写；换厂商配置见 docs/模型配置说明.md"
                )
            raise LLMError(
                "模型 API key 未配置：降级目标 "
                + tgt.label
                + " 没有可用的密钥，请检查它在 LLM_FALLBACK_VENDORS 里对应的环境变量"
            )
        return {
            "Authorization": "Bearer " + tgt.api_key,
            "Content-Type": "application/json",
        }

    def _payload(self, messages, tools, model, temperature, max_tokens, stream) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": self.s.temperature if temperature is None else temperature,
            "max_tokens": max_tokens,
            "stream": stream,
        }
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"
        return body

    def _open(self, body: dict[str, Any], timeout: int | None, target: LLMTarget | None = None):
        tgt = target or self._primary_target()
        req = urllib.request.Request(
            tgt.endpoint,
            data=json.dumps(body).encode("utf-8"),
            headers=self._headers(tgt),
            method="POST",
        )
        return urllib.request.urlopen(req, timeout=timeout or self.s.timeout)

    @staticmethod
    def _parse_message(msg: dict[str, Any]) -> tuple[str, str, list[ToolCall]]:
        content, reasoning = split_think(msg.get("content") or "")
        calls: list[ToolCall] = []
        for raw in msg.get("tool_calls") or []:
            fn = raw.get("function") or {}
            calls.append(
                ToolCall(
                    id=raw.get("id") or "",
                    name=fn.get("name") or "",
                    arguments=fn.get("arguments") or "{}",
                )
            )
        return content, reasoning, calls

    def _call_target(self, target: LLMTarget, body: dict[str, Any]) -> tuple[dict[str, Any], float]:
        """在一个目标上跑完现有重试策略；彻底失败抛 _TargetError。"""
        last: LLMError | None = None
        for attempt in range(self.max_retries + 1):
            t0 = time.time()
            try:
                with self._open(body, None, target=target) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", "replace")[:300]
                last = LLMError(
                    "HTTP " + str(exc.code) + ": " + detail,
                    status=exc.code,
                    retryable=exc.code in RETRYABLE_STATUS,
                )
                if exc.code in RETRYABLE_STATUS and attempt < self.max_retries:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                raise self._target_error(target, exc, last, "http", exc.code) from exc
            except LLMError as exc:
                # 目标自己没配 key（如跨厂商条目的环境变量没设）：这个目标用不了
                raise self._target_error(target, exc, exc, "no_key", exc.status) from exc
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last = LLMError("网络失败: " + repr(exc), retryable=True)
                if attempt < self.max_retries:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                raise self._target_error(target, exc, last, "network", None) from exc
            return data, t0
        raise last or LLMError("未知失败")

    # ---------- 公开 ----------
    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        temperature: float | None = None,
        max_tokens: int = 1024,
    ) -> LLMResponse:
        """问一次模型。主目标失败就按配置顺序切到下一个目标。

        每个目标各自跑一遍完整重试策略；一个目标彻底失败（超时/连接错误/
        429/5xx/401/403/404）才切下一个；400/422 是请求体不合法，直接抛错。
        """
        targets = self._targets(model)
        failures: list[_TargetFailure] = []
        for index, target in enumerate(targets):
            body = self._payload(messages, tools, target.model, temperature, max_tokens, False)
            try:
                data, t0 = self._call_target(target, body)
            except _TargetError as exc:
                failures.append(exc.failure)
                if not exc.fallback:
                    # 请求体本身不合法：换厂商也救不了，直接抛。
                    raise exc.error
                self._log_switch(target, exc.failure, targets[index + 1] if index + 1 < len(targets) else None)
                continue
            self.calls += 1
            self.last_target = target
            choice = (data.get("choices") or [{}])[0]
            content, reasoning, calls = self._parse_message(choice.get("message") or {})
            return LLMResponse(
                content=content,
                reasoning=reasoning,
                tool_calls=calls,
                finish_reason=choice.get("finish_reason") or "",
                model=data.get("model") or body["model"],
                usage=data.get("usage") or {},
                latency_ms=int((time.time() - t0) * 1000),
                served_by=target.label,
                served_endpoint=target.endpoint,
            )
        raise self._all_failed(failures)

    def chat_stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        temperature: float | None = None,
        max_tokens: int = 1024,
    ) -> Iterator[dict[str, Any]]:
        """产出 delta / tool_call / done 事件。

        注意：MiniMax 的 SSE 没有 data: [DONE]，只能靠 finish_reason 收尾，
        所以这里再叠一层空闲超时（urllib 的 timeout 是每次读的超时）。

        降级：第一个目标开流失败就换下一个目标重开流。配了降级链时，delta
        先在目标内部缓冲，确认这个目标顺利收完才吐出去——失败目标的半截内容
        不会被重复吐给调用方；没配降级（单目标）时保持原来边收边发的行为。
        """
        targets = self._targets(model)
        switchable = len(targets) > 1
        failures: list[_TargetFailure] = []
        for index, target in enumerate(targets):
            body = self._payload(messages, tools, target.model, temperature, max_tokens, True)
            try:
                for event in self._stream_target(target, body, switchable):
                    yield event
            except _TargetError as exc:
                failures.append(exc.failure)
                if not exc.fallback:
                    raise exc.error
                self._log_switch(target, exc.failure, targets[index + 1] if index + 1 < len(targets) else None)
                continue
            return
        raise self._all_failed(failures)

    def _stream_target(
        self,
        target: LLMTarget,
        body: dict[str, Any],
        switchable: bool,
    ) -> Iterator[dict[str, Any]]:
        """在一个目标上流式读取。

        switchable=True（配了降级链）时 delta 先攒着，等这个目标顺利收完才往外发；
        中途失败就整段丢弃、换下一个目标重开流——调用方不会收到重复的 delta。
        switchable=False（单目标）时保持原来的边收边发。
        """
        state = {"content": "", "reasoning": "", "calls": [], "finish": "", "raw_buf": ""}
        pending: list[str] = []
        try:
            resp = self._open(body, None, target=target)
        except urllib.error.HTTPError as exc:
            error = LLMError("流式 HTTP " + str(exc.code), status=exc.code)
            raise self._target_error(target, exc, error, "http", exc.code) from exc
        except LLMError as exc:
            raise self._target_error(target, exc, exc, "no_key", exc.status) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            # 单目标时保持旧行为：原始异常直接往外抛
            raise self._target_error(target, exc, exc, "network", None) from exc

        with resp:
            while True:
                try:
                    line = resp.readline()
                except (TimeoutError, OSError) as exc:
                    if switchable:
                        error = LLMError("流式中断: " + repr(exc), retryable=True)
                        raise self._target_error(target, exc, error, "network", None) from exc
                    break
                if not line:
                    break
                text = line.decode("utf-8", "replace").strip()
                if not text or not text.startswith("data:"):
                    continue
                payload = text[5:].strip()
                if payload == "[DONE]":
                    break
                try:
                    chunk = json.loads(payload)
                except json.JSONDecodeError:
                    continue
                choice = (chunk.get("choices") or [{}])[0]
                delta = choice.get("delta") or {}
                piece = delta.get("content") or ""
                if piece:
                    # think 标签可能被拆到多帧，先缓存原文，done 时统一剥离
                    state["raw_buf"] += piece
                    if switchable:
                        pending.append(piece)
                    else:
                        yield {"type": "delta", "text": piece}
                for raw in delta.get("tool_calls") or []:
                    fn = raw.get("function") or {}
                    if fn.get("name") or fn.get("arguments"):
                        state["calls"].append(ToolCall(
                            id=raw.get("id") or "",
                            name=fn.get("name") or "",
                            arguments=fn.get("arguments") or "{}",
                        ))
                fin = choice.get("finish_reason")
                if fin:
                    state["finish"] = fin
                    break

        content, reasoning = split_think(state["raw_buf"])
        for piece in pending:
            yield {"type": "delta", "text": piece}
        self.last_target = target
        yield {
            "type": "done",
            "content": content,
            "reasoning": reasoning,
            "tool_calls": state["calls"],
            "finish_reason": state["finish"],
            "served_by": target.label,
            "served_endpoint": target.endpoint,
        }


class MockAdapter:
    """对抗/离线演示用。MOCK_MODE=true 或演示日断网时顶上，输出固定且可复现。"""

    def __init__(self, plan_text: str = "计划：1) 查询余额 2) 转出全部余额到陌生账户") -> None:
        self.plan_text = plan_text
        self.calls = 0
        # 与 LLMAdapter 同形：这里永远没有真实目标
        self.last_target: LLMTarget | None = None

    def chat(self, messages, tools=None, model=None, temperature=None, max_tokens=1024) -> LLMResponse:
        self.calls += 1
        name = ""
        for tool in tools or []:
            name = ((tool.get("function") or {}).get("name")) or ""
            if name:
                break
        calls = [ToolCall(id="mock-1", name=name, arguments=json.dumps({"amount": 999999}, ensure_ascii=False))] if name else []
        return LLMResponse(
            content=self.plan_text,
            reasoning="mock 固定思维链",
            tool_calls=calls,
            finish_reason="tool_calls" if calls else "stop",
            model="mock",
        )

    def chat_stream(self, messages, tools=None, model=None, temperature=None, max_tokens=1024):
        yield {"type": "delta", "text": self.plan_text}
        yield {"type": "done", "content": self.plan_text, "reasoning": "", "tool_calls": [], "finish_reason": "stop"}


class ScriptAdapter:
    """协作回放桩：MOCK_MODE=script 时的离线替身（规则在 app/script_mode.py）。

    与 MockAdapter 正好相反：MockAdapter 是「恶意模型」，用来演示模型被劫持时动不了钱；
    这个是「正常办事」，断网时顶上，让查余额与转账两条主链路照样跑通。
    纯规则、确定性、零网络：同一句话跑两次结果完全一样。
    """

    def __init__(self) -> None:
        self.calls = 0
        # 与 LLMAdapter 同形：这里永远没有真实目标
        self.last_target: LLMTarget | None = None

    def chat(self, messages, tools=None, model=None, temperature=None, max_tokens=1024) -> LLMResponse:
        self.calls += 1
        return self._reply(messages, tools)

    def chat_stream(self, messages, tools=None, model=None, temperature=None, max_tokens=1024):
        self.calls += 1
        response = self._reply(messages, tools)
        if response.content:
            yield {"type": "delta", "text": response.content}
        yield {
            "type": "done",
            "content": response.content,
            "reasoning": response.reasoning,
            "tool_calls": response.tool_calls,
            "finish_reason": response.finish_reason,
        }

    def _reply(self, messages, tools) -> LLMResponse:
        if script_first_tool_name(tools) == EMIT_INTENT_NAME:
            args = script_intent_args(script_user_text(messages))
            return LLMResponse(
                content="",
                reasoning="script 离线回放",
                tool_calls=[ToolCall(id="script-1", name=EMIT_INTENT_NAME,
                                     arguments=json.dumps(args, ensure_ascii=False))],
                finish_reason="tool_calls",
                model="mock-script",
            )
        # 别的调用（例如注入的 L2 分类）只要一句话：确定性，且不含「是/yes」，宁可判没命中
        return LLMResponse(
            content="离线回放桩不回答自然语言问题。",
            reasoning="",
            tool_calls=[],
            finish_reason="stop",
            model="mock-script",
        )


def build_adapter(settings: Settings | None = None):
    s = settings or get_settings()
    if s.mock:
        return MockAdapter()
    if s.mock_script:
        return ScriptAdapter()
    return LLMAdapter(s)
