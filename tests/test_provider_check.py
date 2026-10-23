"""scripts/provider_check 的单测：monkeypatch 打桩 urllib.request.urlopen。

覆盖：四项检查各自的通过与失败分支、退出码、输出不含 key、--json 结构、
以及命令行只允许覆盖 base/model（不接受 key 参数）。
"""

from __future__ import annotations

import io
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Self

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import provider_check  # noqa: E402

FAKE_KEY = "sk-fake-0123456789abcdef"
VENDOR_BASE = "https://vendor.test"
VENDOR_MODEL = "vendor-model"

TEXT_BODY: dict[str, Any] = {
    "id": "chatcmpl-text",
    "object": "chat.completion",
    "model": VENDOR_MODEL,
    "choices": [
        {
            "index": 0,
            "message": {"role": "assistant", "content": "好"},
            "finish_reason": "stop",
        }
    ],
}

TOOL_BODY: dict[str, Any] = {
    "id": "chatcmpl-tool",
    "object": "chat.completion",
    "model": VENDOR_MODEL,
    "choices": [
        {
            "index": 0,
            "message": {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {
                            "name": "query_balance",
                            "arguments": '{"card_tail": "1234"}',
                        },
                    }
                ],
            },
            "finish_reason": "tool_calls",
        }
    ],
}

# MiniMax 风格：没有 data: [DONE]，靠 finish_reason 收尾
SSE_LINES_NO_DONE = [
    b'data: {"choices":[{"delta":{"content":"\xe4\xbd\xa0\xe5\xa5\xbd"}}]}\n',
    b'data: {"choices":[{"delta":{"content":"\xef\xbc\x8c\xe4\xb8\x96\xe7\x95\x8c"}}]}\n',
    b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n',
]

# OpenAI 风格：先给 finish_reason，再给 data: [DONE]
SSE_LINES_WITH_DONE = [
    b'data: {"choices":[{"delta":{"content":"\xe4\xbd\xa0\xe5\xa5\xbd"}}]}\n',
    b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n',
    b"data: [DONE]\n",
]

# 只有 [DONE] 没有 finish_reason：不符合本项目的收尾要求
SSE_LINES_DONE_ONLY = [
    b'data: {"choices":[{"delta":{"content":"\xe4\xbd\xa0\xe5\xa5\xbd"}}]}\n',
    b"data: [DONE]\n",
]


class FakeResponse:
    """假响应：既能一次性 read()，也能逐行 readline() 模拟 SSE。"""

    def __init__(
        self,
        body: bytes = b"",
        lines: list[bytes] | None = None,
        status: int = 200,
        content_type: str = "application/json",
    ) -> None:
        self._body = io.BytesIO(body)
        self._lines = list(lines) if lines is not None else None
        self.status = status
        self.headers = {"Content-Type": content_type}

    def read(self, *args: Any) -> bytes:
        return self._body.read(*args)

    def readline(self) -> bytes:
        if self._lines is None:
            return self._body.readline()
        if self._lines:
            return self._lines.pop(0)
        return b""

    def getcode(self) -> int:
        return self.status

    def close(self) -> None:
        return None

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


def json_response(payload: dict[str, Any], status: int = 200) -> FakeResponse:
    return FakeResponse(json.dumps(payload).encode("utf-8"), status=status)


def http_error(code: int, body: str = "") -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        "https://vendor.test/v1/chat/completions",
        code,
        "boom",
        {},
        io.BytesIO(body.encode("utf-8")),
    )


def install_fake(
    monkeypatch: pytest.MonkeyPatch, responses: list[Any]
) -> list[urllib.request.Request]:
    """按调用顺序返回假响应；用完最后一个就复用最后一个。记下所有请求。"""
    calls: list[urllib.request.Request] = []

    def fake_urlopen(req: urllib.request.Request, timeout: Any = None) -> FakeResponse:
        calls.append(req)
        item = responses[min(len(calls) - 1, len(responses) - 1)]
        if isinstance(item, BaseException):
            raise item
        return item

    monkeypatch.setattr(provider_check.urllib.request, "urlopen", fake_urlopen)
    return calls


def all_pass_responses() -> list[Any]:
    return [
        json_response(TEXT_BODY),
        json_response(TOOL_BODY),
        json_response(TOOL_BODY),
        FakeResponse(lines=SSE_LINES_NO_DONE, content_type="text/event-stream"),
    ]


def request_body(req: urllib.request.Request) -> dict[str, Any]:
    return json.loads((req.data or b"{}").decode("utf-8"))


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """把仓库 .env 里可能残留的厂商变量清干净，保证用例只认自己设的值。"""
    for name in (
        "LLM_API_KEY",
        "LLM_BASE_URL",
        "LLM_MODEL",
        "LLM_ENDPOINT",
        "LLM_MODEL_FAST",
        "LLM_MODEL_BACKUP",
        "MINIMAX_API_KEY",
        "MINIMAX_API_HOST",
        "MODEL_BRAIN",
        "MODEL_FAST",
        "MODEL_BACKUP",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("LLM_API_KEY", FAKE_KEY)
    monkeypatch.setenv("LLM_BASE_URL", VENDOR_BASE)
    monkeypatch.setenv("LLM_MODEL", VENDOR_MODEL)


# --------------------------------------------------------------------------
# 全通过
# --------------------------------------------------------------------------
def test_all_pass_exit_code_zero(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    install_fake(monkeypatch, all_pass_responses())
    rc = provider_check.main([])
    out = capsys.readouterr().out
    assert rc == 0
    assert "结论：可用（4/4 项通过）" in out
    assert out.count("PASS") == 4
    assert "FAIL" not in out


def test_text_output_has_table_and_evidence(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    install_fake(monkeypatch, all_pass_responses())
    provider_check.main([])
    out = capsys.readouterr().out
    assert "检查项" in out and "结果" in out and "证据" in out
    for name in ("连通性", "工具调用", "tool_choice:auto", "SSE 流式"):
        assert name in out
    assert "finish_reason=stop" in out


def test_env_suggestion_gives_base_and_model(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    install_fake(monkeypatch, all_pass_responses())
    provider_check.main([])
    out = capsys.readouterr().out
    assert "LLM_BASE_URL=" + VENDOR_BASE in out
    assert "LLM_MODEL=" + VENDOR_MODEL in out


# --------------------------------------------------------------------------
# a) 连通性
# --------------------------------------------------------------------------
def test_connectivity_fail_on_http_500(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    install_fake(monkeypatch, [http_error(500, '{"error":"boom"}')])
    rc = provider_check.main([])
    out = capsys.readouterr().out
    assert rc == 1
    assert "FAIL" in out
    assert "HTTP 500" in out
    assert "结论：不可用" in out


def test_connectivity_fail_on_network_error(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    install_fake(monkeypatch, [urllib.error.URLError("connection refused")])
    rc = provider_check.main([])
    out = capsys.readouterr().out
    assert rc == 1
    assert "网络错误" in out


def test_connectivity_fail_on_non_openai_shape(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    install_fake(monkeypatch, [json_response({"ok": True, "result": "not openai"})])
    rc = provider_check.main([])
    out = capsys.readouterr().out
    assert rc == 1
    assert "不是 OpenAI 兼容结构" in out


# --------------------------------------------------------------------------
# b) 工具调用
# --------------------------------------------------------------------------
def test_tool_calls_fail_when_response_has_no_tool_calls(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    install_fake(monkeypatch, [json_response(TEXT_BODY), json_response(TEXT_BODY)])
    rc = provider_check.main([])
    out = capsys.readouterr().out
    assert rc == 1
    assert "没有 tool_calls" in out


def test_tool_calls_pass_evidence_lists_function(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    install_fake(monkeypatch, all_pass_responses())
    provider_check.main([])
    out = capsys.readouterr().out
    assert "tool_calls" in out and "query_balance" in out


# --------------------------------------------------------------------------
# c) tool_choice:auto
# --------------------------------------------------------------------------
def test_tool_choice_fail_on_400(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    responses = all_pass_responses()
    responses[2] = http_error(400, '{"error":"tool_choice is not supported"}')
    install_fake(monkeypatch, responses)
    rc = provider_check.main([])
    out = capsys.readouterr().out
    assert rc == 1
    assert "HTTP 400" in out
    assert "tool_choice" in out


def test_tool_choice_fail_on_500(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    responses = all_pass_responses()
    responses[2] = http_error(503, "upstream busy")
    install_fake(monkeypatch, responses)
    rc = provider_check.main([])
    out = capsys.readouterr().out
    assert rc == 1
    assert "HTTP 503" in out


# --------------------------------------------------------------------------
# d) SSE 流式
# --------------------------------------------------------------------------
def test_sse_pass_without_done_marker(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    """MiniMax 风格：没有 data: [DONE]，靠 finish_reason 收尾也算通过。"""
    install_fake(monkeypatch, all_pass_responses())
    rc = provider_check.main([])
    out = capsys.readouterr().out
    assert rc == 0
    assert "无 [DONE]" in out
    assert "finish_reason=stop" in out


def test_sse_pass_with_done_marker(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    """OpenAI 风格：finish_reason 与 data: [DONE] 都给，照样通过。"""
    responses = all_pass_responses()
    responses[3] = FakeResponse(lines=SSE_LINES_WITH_DONE, content_type="text/event-stream")
    install_fake(monkeypatch, responses)
    rc = provider_check.main([])
    out = capsys.readouterr().out
    assert rc == 0
    assert "finish_reason=stop" in out


def test_sse_fail_when_only_done_marker(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    """只有 [DONE] 没有 finish_reason：本项目靠 finish_reason 收尾，判 FAIL。"""
    responses = all_pass_responses()
    responses[3] = FakeResponse(lines=SSE_LINES_DONE_ONLY, content_type="text/event-stream")
    install_fake(monkeypatch, responses)
    rc = provider_check.main([])
    out = capsys.readouterr().out
    assert rc == 1
    assert "没有 finish_reason 收尾" in out


def test_sse_fail_when_no_data_lines(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    responses = all_pass_responses()
    responses[3] = FakeResponse(b'{"choices":[]}', content_type="application/json")
    install_fake(monkeypatch, responses)
    rc = provider_check.main([])
    out = capsys.readouterr().out
    assert rc == 1
    assert "没有读到 data: 行" in out


def test_sse_fail_when_delta_content_empty(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    responses = all_pass_responses()
    responses[3] = FakeResponse(
        lines=[
            b'data: {"choices":[{"delta":{}}]}\n',
            b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n',
        ],
        content_type="text/event-stream",
    )
    install_fake(monkeypatch, responses)
    rc = provider_check.main([])
    out = capsys.readouterr().out
    assert rc == 1
    assert "delta.content 为空" in out


def test_sse_fail_when_no_finish_reason(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    responses = all_pass_responses()
    responses[3] = FakeResponse(
        lines=[
            b'data: {"choices":[{"delta":{"content":"\xe4\xbd\xa0\xe5\xa5\xbd"}}]}\n',
            b'data: {"choices":[{"delta":{"content":"!"}}]}\n',
        ],
        content_type="text/event-stream",
    )
    install_fake(monkeypatch, responses)
    rc = provider_check.main([])
    out = capsys.readouterr().out
    assert rc == 1
    assert "没有 finish_reason 收尾" in out


def test_sse_fail_on_http_error(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    responses = all_pass_responses()
    responses[3] = http_error(404, '{"error":"no such endpoint"}')
    install_fake(monkeypatch, responses)
    rc = provider_check.main([])
    out = capsys.readouterr().out
    assert rc == 1
    assert "HTTP 404" in out


# --------------------------------------------------------------------------
# 退出码 / 缺 key
# --------------------------------------------------------------------------
def test_exit_code_is_one_when_any_check_fails(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    responses = all_pass_responses()
    responses[1] = json_response(TEXT_BODY)
    install_fake(monkeypatch, responses)
    rc = provider_check.main([])
    out = capsys.readouterr().out
    assert rc == 1
    assert "结论：不可用（3/4 项通过）" in out


def test_missing_key_makes_all_checks_fail_without_network(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    calls = install_fake(monkeypatch, all_pass_responses())
    rc = provider_check.main([])
    out = capsys.readouterr().out
    assert rc == 1
    assert calls == []
    assert out.count("FAIL") == 4
    assert "LLM_API_KEY" in out


# --------------------------------------------------------------------------
# 输出里不能有 key
# --------------------------------------------------------------------------
def test_output_never_contains_key(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    install_fake(monkeypatch, all_pass_responses())
    provider_check.main([])
    text_out = capsys.readouterr().out
    assert FAKE_KEY not in text_out

    install_fake(monkeypatch, all_pass_responses())
    provider_check.main(["--json"])
    json_out = capsys.readouterr().out
    assert FAKE_KEY not in json_out
    payload = json.loads(json_out)
    # 报告里只有 api_key_configured 这个布尔量，没有任何 key 字段
    assert payload["api_key_configured"] is True
    assert FAKE_KEY not in json.dumps(payload, ensure_ascii=False)


def test_key_echoed_by_vendor_is_scrubbed(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    """厂商报错里回显了 key 也不能漏出去。"""
    responses = all_pass_responses()
    responses[0] = http_error(401, '{"error":"invalid api key: ' + FAKE_KEY + '"}')
    install_fake(monkeypatch, responses)
    rc = provider_check.main([])
    out = capsys.readouterr().out
    assert rc == 1
    assert FAKE_KEY not in out
    assert "***" in out


# --------------------------------------------------------------------------
# --json
# --------------------------------------------------------------------------
def test_json_structure(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    install_fake(monkeypatch, all_pass_responses())
    rc = provider_check.main(["--json"])
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert rc == 0
    assert payload["ok"] is True
    assert payload["conclusion"] == "可用"
    assert payload["passed"] == 4 and payload["total"] == 4
    assert payload["exit_code"] == 0
    assert payload["api_key_configured"] is True
    assert payload["base_url"] == VENDOR_BASE
    assert payload["model"] == VENDOR_MODEL
    assert len(payload["checks"]) == 4
    ids = [c["id"] for c in payload["checks"]]
    assert ids == ["connectivity", "tool_calls", "tool_choice_auto", "sse_stream"]
    for item in payload["checks"]:
        assert item["status"] in ("PASS", "FAIL")
        assert isinstance(item["ok"], bool)
        assert item["evidence"]
    assert payload["env_suggestion"]["LLM_BASE_URL"] == VENDOR_BASE
    assert payload["env_suggestion"]["LLM_MODEL"] == VENDOR_MODEL
    assert FAKE_KEY not in out


def test_json_reports_failure_and_exit_code(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    install_fake(monkeypatch, [http_error(500, "boom")])
    rc = provider_check.main(["--json"])
    payload = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert payload["ok"] is False
    assert payload["exit_code"] == 1
    assert payload["conclusion"] == "不可用"
    assert payload["checks"][0]["status"] == "FAIL"


# --------------------------------------------------------------------------
# 命令行参数
# --------------------------------------------------------------------------
def test_base_url_and_model_override(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    calls = install_fake(monkeypatch, all_pass_responses())
    rc = provider_check.main(["--base-url", "https://other.test/v1", "--model", "other-model"])
    out = capsys.readouterr().out
    assert rc == 0
    assert calls[0].full_url == "https://other.test/v1/chat/completions"
    assert request_body(calls[0])["model"] == "other-model"
    assert "LLM_BASE_URL=https://other.test/v1" in out
    assert "LLM_MODEL=other-model" in out


def test_base_url_override_ignores_env_endpoint_override(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    monkeypatch.setenv("LLM_ENDPOINT", "https://old.test/v1/chat/completions")
    calls = install_fake(monkeypatch, all_pass_responses())
    provider_check.main(["--base-url", "https://new.test"])
    assert calls[0].full_url == "https://new.test/v1/chat/completions"


def test_env_endpoint_override_is_used_without_cli_flags(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    monkeypatch.setenv("LLM_ENDPOINT", "https://api.special.test/v4/chat/completions")
    calls = install_fake(monkeypatch, all_pass_responses())
    provider_check.main([])
    assert calls[0].full_url == "https://api.special.test/v4/chat/completions"


def test_no_key_argument_accepted(capsys: pytest.CaptureFixture) -> None:
    """key 不许从命令行传：--api-key 必须被 argparse 直接拒掉。"""
    with pytest.raises(SystemExit) as exc:
        provider_check.main(["--api-key", "sk-leaked"])
    assert exc.value.code == 2
    assert "sk-leaked" not in capsys.readouterr().out


def test_help_exits_zero(capsys: pytest.CaptureFixture) -> None:
    with pytest.raises(SystemExit) as exc:
        provider_check.main(["--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "用法" in out or "usage" in out
    assert "--base-url" in out and "--model" in out and "--json" in out


# --------------------------------------------------------------------------
# 请求体形状：和项目真实调用保持一致
# --------------------------------------------------------------------------
def test_request_payload_shapes(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    calls = install_fake(monkeypatch, all_pass_responses())
    provider_check.main([])
    assert len(calls) == 4
    ping = request_body(calls[0])
    assert "tools" not in ping and "tool_choice" not in ping
    assert ping["stream"] is False
    tool = request_body(calls[1])
    assert tool["tools"] and "tool_choice" not in tool
    choice = request_body(calls[2])
    assert choice["tool_choice"] == "auto"
    assert choice["tools"]
    stream = request_body(calls[3])
    assert stream["stream"] is True
    assert "tools" not in stream
    for req in calls:
        assert req.get_header("Authorization") == "Bearer " + FAKE_KEY
