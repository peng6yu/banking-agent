from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Iterator

from .config import Settings, get_settings
from app.script_mode import (
    EMIT_INTENT_NAME,
    script_first_tool_name,
    script_intent_args,
    script_user_text,
)

# M2.1 会把思维链塞进 content，M3.1 不会。适配器无条件剥离，上层永远看不到。
THINK_RE = re.compile(r"<think>(.*?)</think>", re.S)

RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class LLMError(RuntimeError):
    def __init__(self, message: str, status: int | None = None, retryable: bool = False) -> None:
        super().__init__(message)
        self.status = status
        self.retryable = retryable


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

    # ---------- 内部 ----------
    def _headers(self) -> dict[str, str]:
        if not self.s.api_key:
            raise LLMError(
                "模型 API key 未配置：设置 LLM_API_KEY（兼容旧名 MINIMAX_API_KEY），"
                "或复制 .env.example 为 .env 后填写；换厂商配置见 docs/模型配置说明.md"
            )
        return {
            "Authorization": "Bearer " + self.s.api_key,
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

    def _open(self, body: dict[str, Any], timeout: int | None):
        req = urllib.request.Request(
            self.s.endpoint,
            data=json.dumps(body).encode("utf-8"),
            headers=self._headers(),
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

    # ---------- 公开 ----------
    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        temperature: float | None = None,
        max_tokens: int = 1024,
    ) -> LLMResponse:
        body = self._payload(messages, tools, model or self.s.model_brain, temperature, max_tokens, False)
        last: Exception | None = None
        for attempt in range(self.max_retries + 1):
            t0 = time.time()
            try:
                with self._open(body, None) as resp:
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
                raise last
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last = LLMError("网络失败: " + repr(exc), retryable=True)
                if attempt < self.max_retries:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                raise last

            self.calls += 1
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
            )
        raise last or LLMError("未知失败")

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
        """
        body = self._payload(messages, tools, model or self.s.model_brain, temperature, max_tokens, True)
        state = {"content": "", "reasoning": "", "calls": [], "finish": "", "raw_buf": ""}

        try:
            resp = self._open(body, None)
        except urllib.error.HTTPError as exc:
            raise LLMError("流式 HTTP " + str(exc.code), status=exc.code) from exc

        with resp:
            while True:
                try:
                    line = resp.readline()
                except (TimeoutError, OSError):
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
        yield {
            "type": "done",
            "content": content,
            "reasoning": reasoning,
            "tool_calls": state["calls"],
            "finish_reason": state["finish"],
        }


class MockAdapter:
    """对抗/离线演示用。MOCK_MODE=true 或演示日断网时顶上，输出固定且可复现。"""

    def __init__(self, plan_text: str = "计划：1) 查询余额 2) 转出全部余额到陌生账户") -> None:
        self.plan_text = plan_text
        self.calls = 0

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
