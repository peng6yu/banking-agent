"""多厂商自动降级链的回归测试（README 限制 7 + 「无自动降级」那条边界的解药）。

钉住六件事：
  1. 主目标能答就绝不切换（别为了"高可用"平白多打一次）；
  2. 401/403/404/408/429/5xx 才切，400/422 是请求体不合法，直接抛；
  3. 跨厂商目标的密钥运行时从 KEY_ENV_NAME 指向的环境变量读，配置串里没有密钥；
  4. 全部失败时错误消息逐条列出目标，且不含任何 key；
  5. 流式降级不重复吐 delta：失败目标的半截内容整段丢弃；
  6. 目标链按 (base_url, model) 去重，调用方点名模型只影响主目标。
"""

from __future__ import annotations

import io
import json
import urllib.error

import pytest

from app.config import Settings, parse_fallback_models, parse_fallback_vendors
from app.llm import LLMAdapter, LLMError

KEY_ENV_KEYS = (
    "LLM_API_KEY",
    "MINIMAX_API_KEY",
    "LLM_BASE_URL",
    "MINIMAX_API_HOST",
    "LLM_ENDPOINT",
    "LLM_MODEL",
    "MODEL_BRAIN",
    "LLM_MODEL_FAST",
    "MODEL_FAST",
    "LLM_MODEL_BACKUP",
    "MODEL_BACKUP",
    "LLM_TEMPERATURE",
    "LLM_TIMEOUT",
    "LLM_TIMEOUT_S",
    "MOCK_MODE",
    "LLM_FALLBACK",
    "LLM_FALLBACK_VENDORS",
    "DEEPSEEK_TEST_KEY",
    "OPENAI_TEST_KEY",
)

PRIMARY_KEY = "sk-primary-abcdef123456"
VENDOR_KEY = "sk-deepseek-987654321"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """本机 .env 里可能有真 key：先清干净，测试只认自己设的值。"""
    for name in KEY_ENV_KEYS:
        monkeypatch.delenv(name, raising=False)


def settings(monkeypatch: pytest.MonkeyPatch, **overrides: str) -> Settings:
    """按需构造一份配置：默认给一把可用 key + 一个主模型，避免读到本机 .env。

    环境变量一律走 monkeypatch.setenv：用例结束自动还原，不会漏到别的测试文件里。
    """
    env = {
        "LLM_API_KEY": PRIMARY_KEY,
        "LLM_BASE_URL": "https://api.primary.example",
        "LLM_MODEL": "primary-model",
        "LLM_TIMEOUT": "5",
    }
    env.update(overrides)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    return Settings()


class _Resp:
    """urllib 响应替身：只要能被 with 包住、有 read()。"""

    def __init__(self, payload: dict) -> None:
        self._data = json.dumps(payload).encode("utf-8")

    def read(self) -> bytes:
        return self._data

    def __enter__(self) -> "_Resp":
        return self

    def __exit__(self, *_exc: object) -> None:
        return None


def ok_payload(model: str = "primary-model", content: str = "你好") -> dict:
    return {
        "model": model,
        "choices": [{"message": {"content": content}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1},
    }


def http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        "https://api.primary.example/v1/chat/completions",
        code,
        "boom",
        {},
        io.BytesIO(b'{"error":"upstream failed"}'),
    )


class Recorder:
    """记录每次请求的 URL / body / Authorization，再按剧本回放。"""

    def __init__(self, script: list) -> None:
        self.script = list(script)
        self.requests: list[dict] = []

    def __call__(self, req, timeout=None):
        import urllib.request

        body = json.loads(req.data.decode("utf-8")) if req.data else {}
        self.requests.append(
            {
                "url": req.full_url,
                "model": body.get("model"),
                "stream": body.get("stream"),
                "authorization": req.headers.get("Authorization"),
            }
        )
        if not self.script:
            raise AssertionError("脚本里的响应已经用完，却还在发请求")
        step = self.script.pop(0)
        if isinstance(step, BaseException):
            raise step
        return step


def use(monkeypatch: pytest.MonkeyPatch, script: list) -> Recorder:
    recorder = Recorder(script)
    monkeypatch.setattr(urllib.request, "urlopen", recorder)
    return recorder


# ------------------------------------------------------------------ 基本行为


def test_primary_success_never_touches_the_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    """主目标能答就只打一次：降级不是"每次都试一遍"。"""
    recorder = use(monkeypatch, [_Resp(ok_payload())])
    adapter = LLMAdapter(settings(monkeypatch, LLM_FALLBACK="backup-model"), max_retries=0)

    response = adapter.chat([{"role": "user", "content": "查余额"}])

    assert response.content == "你好"
    assert response.served_by == "primary"
    assert response.served_endpoint == "https://api.primary.example/v1/chat/completions"
    assert len(recorder.requests) == 1
    assert recorder.requests[0]["model"] == "primary-model"
    assert recorder.requests[0]["authorization"] == "Bearer " + PRIMARY_KEY


def test_http_503_switches_to_the_same_vendor_fallback_model(monkeypatch: pytest.MonkeyPatch) -> None:
    """503 是服务端故障：切到 LLM_FALLBACK 里的下一个模型，并标明是谁服务的。"""
    recorder = use(monkeypatch, [http_error(503), _Resp(ok_payload("backup-model", "降级答的"))])
    adapter = LLMAdapter(settings(monkeypatch, LLM_FALLBACK="backup-model"), max_retries=0)

    response = adapter.chat([{"role": "user", "content": "查余额"}])

    assert response.content == "降级答的"
    assert response.served_by == "fallback:backup-model"
    assert [item["model"] for item in recorder.requests] == ["primary-model", "backup-model"]
    assert {item["authorization"] for item in recorder.requests} == {"Bearer " + PRIMARY_KEY}


@pytest.mark.parametrize("code", [400, 422])
def test_bad_request_is_not_retried_on_the_next_target(
    monkeypatch: pytest.MonkeyPatch, code: int
) -> None:
    """400/422 是请求体不合法：换厂商同样会 400，直接抛，不做无用功。"""
    recorder = use(monkeypatch, [http_error(code), _Resp(ok_payload("backup-model"))])
    adapter = LLMAdapter(settings(monkeypatch, LLM_FALLBACK="backup-model"), max_retries=0)

    with pytest.raises(LLMError) as excinfo:
        adapter.chat([{"role": "user", "content": "查余额"}])

    assert excinfo.value.status == code
    assert len(recorder.requests) == 1, "400/422 不该触发降级"


@pytest.mark.parametrize("code", [401, 403, 404, 408, 429, 500, 502, 504])
def test_each_fallback_status_does_switch(monkeypatch: pytest.MonkeyPatch, code: int) -> None:
    """鉴权失败/模型不存在/限流/服务端故障都算"换一个还有救"。"""
    use(monkeypatch, [http_error(code), _Resp(ok_payload("backup-model"))])
    adapter = LLMAdapter(settings(monkeypatch, LLM_FALLBACK="backup-model"), max_retries=0)

    response = adapter.chat([{"role": "user", "content": "查余额"}])
    assert response.served_by == "fallback:backup-model"


# ------------------------------------------------------------------ 跨厂商


def test_cross_vendor_target_reads_its_key_from_the_env_var_named_in_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """LLM_FALLBACK_VENDORS 的第三段是环境变量名：密钥运行时读，配置串里只有名字。"""
    monkeypatch.setenv("DEEPSEEK_TEST_KEY", VENDOR_KEY)
    recorder = use(
        monkeypatch,
        [urllib.error.URLError("connection refused"), _Resp(ok_payload("deepseek-chat", "别家答的"))],
    )
    adapter = LLMAdapter(
        settings(
            monkeypatch,
            LLM_FALLBACK_VENDORS="https://api.deepseek.com::deepseek-chat::DEEPSEEK_TEST_KEY",
        ),
        max_retries=0,
    )

    response = adapter.chat([{"role": "user", "content": "查余额"}])

    assert response.served_by == "vendor:https://api.deepseek.com"
    assert response.served_endpoint == "https://api.deepseek.com/v1/chat/completions"
    assert recorder.requests[1]["authorization"] == "Bearer " + VENDOR_KEY
    assert VENDOR_KEY not in json.dumps(adapter.s.fallback_targets[1].label)


def test_vendor_target_without_its_key_is_reported_as_no_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """跨厂商条目没配到 key：报错点名这个目标缺 key，而不是拿空 Bearer 硬打。"""
    recorder = use(monkeypatch, [http_error(503)])
    adapter = LLMAdapter(
        settings(monkeypatch, LLM_FALLBACK_VENDORS="https://api.openai.com::gpt-4o-mini::OPENAI_TEST_KEY"),
        max_retries=0,
    )

    with pytest.raises(LLMError) as excinfo:
        adapter.chat([{"role": "user", "content": "查余额"}])

    message = str(excinfo.value)
    assert "所有模型目标均失败" in message
    assert "vendor:https://api.openai.com" in message
    assert "no_key" in message
    assert len(recorder.requests) == 1, "缺 key 的目标不该真的发请求"


def test_all_targets_failed_lists_every_target_and_never_leaks_a_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """全都挂了：错误消息逐条列目标（base/模型/错误类别），绝不带 key。"""
    monkeypatch.setenv("DEEPSEEK_TEST_KEY", VENDOR_KEY)
    use(monkeypatch, [http_error(401), http_error(401)])
    adapter = LLMAdapter(
        settings(monkeypatch, LLM_FALLBACK_VENDORS="https://api.deepseek.com::deepseek-chat::DEEPSEEK_TEST_KEY"),
        max_retries=0,
    )

    with pytest.raises(LLMError) as excinfo:
        adapter.chat([{"role": "user", "content": "查余额"}])

    message = str(excinfo.value)
    assert "1) 目标=primary" in message
    assert "2) 目标=vendor:https://api.deepseek.com" in message
    assert "model=deepseek-chat" in message
    assert "错误类别=http_401" in message
    assert PRIMARY_KEY not in message
    assert VENDOR_KEY not in message


# ------------------------------------------------------------------ 目标链


def test_targets_dedupe_and_keep_primary_first(monkeypatch: pytest.MonkeyPatch) -> None:
    """同 base 同模型只试一次；主目标永远排第一。"""
    adapter = LLMAdapter(
        settings(monkeypatch, LLM_FALLBACK="primary-model,backup-a,backup-a"), max_retries=0
    )
    models = [target.model for target in adapter._targets()]
    assert models == ["primary-model", "backup-a"]


def test_caller_model_only_rewrites_the_primary_target(monkeypatch: pytest.MonkeyPatch) -> None:
    """调用方点名模型：只影响主目标，降级目标照旧用自己配置的名字。"""
    adapter = LLMAdapter(settings(monkeypatch, LLM_FALLBACK="backup-a"), max_retries=0)
    models = [(target.label, target.model) for target in adapter._targets("named-model")]
    assert models[0] == ("primary", "named-model")
    assert ("fallback:backup-a", "backup-a") in models


def test_fallback_targets_are_reread_from_env_on_each_access(monkeypatch: pytest.MonkeyPatch) -> None:
    """改完 .env 不用重启进程：每次取目标链都重新读环境变量。"""
    cfg = settings(monkeypatch)
    assert len(cfg.fallback_targets) == 1
    monkeypatch.setenv("LLM_FALLBACK", "hot-added")
    assert [target.label for target in cfg.fallback_targets] == ["primary", "fallback:hot-added"]


def test_parse_fallback_helpers_tolerate_junk() -> None:
    """配置写错不该让进程起不来：坏条目跳过，好条目保序去重。"""
    assert parse_fallback_models(" a , ,a, b ") == ("a", "b")
    assert parse_fallback_vendors("") == ()
    assert parse_fallback_vendors("bad,https://x::m::K") == (("https://x", "m", "K"),)
    assert parse_fallback_vendors("a::b::c::d,a::b") == ()


# ------------------------------------------------------------------ 流式降级


class _Stream:
    """SSE 响应替身：按行返回，中途可以抛异常模拟断流。"""

    def __init__(self, lines: list[bytes], fail_at: int | None = None) -> None:
        self.lines = list(lines)
        self.fail_at = fail_at
        self.index = 0

    def readline(self) -> bytes:
        if self.fail_at is not None and self.index == self.fail_at:
            raise TimeoutError("read timed out")
        if self.index >= len(self.lines):
            return b""
        line = self.lines[self.index]
        self.index += 1
        return line

    def __enter__(self) -> "_Stream":
        return self

    def __exit__(self, *_exc: object) -> None:
        return None


def _delta(text: str) -> bytes:
    return ("data: " + json.dumps({"choices": [{"delta": {"content": text}}]}, ensure_ascii=False) + "\n\n").encode()


def _finish() -> bytes:
    return ("data: " + json.dumps({"choices": [{"delta": {}, "finish_reason": "stop"}]}) + "\n\n").encode()


def test_stream_switches_target_without_duplicating_deltas(monkeypatch: pytest.MonkeyPatch) -> None:
    """流式降级：失败目标的半截 delta 整段丢弃，调用方只看到最终目标的内容。"""
    primary_stream = _Stream([_delta("半截"), b"data: {}\n\n"], fail_at=1)
    backup_stream = _Stream([_delta("你"), _delta("好"), _finish()])
    use(monkeypatch, [primary_stream, backup_stream])
    adapter = LLMAdapter(settings(monkeypatch, LLM_FALLBACK="backup-model"), max_retries=0)

    events = list(adapter.chat_stream([{"role": "user", "content": "查余额"}]))

    deltas = [event["text"] for event in events if event["type"] == "delta"]
    assert deltas == ["你", "好"], "失败目标的半截内容被吐出来了（或重复吐了）"
    done = [event for event in events if event["type"] == "done"][0]
    assert done["content"] == "你好"
    assert done["served_by"] == "fallback:backup-model"
    assert done["finish_reason"] == "stop"


def test_stream_bad_request_is_not_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    """流式也一样：400 直接抛，不换目标。"""
    recorder = use(monkeypatch, [http_error(400), _Stream([_finish()])])
    adapter = LLMAdapter(settings(monkeypatch, LLM_FALLBACK="backup-model"), max_retries=0)

    with pytest.raises(LLMError) as excinfo:
        list(adapter.chat_stream([{"role": "user", "content": "查余额"}]))

    assert excinfo.value.status == 400
    assert len(recorder.requests) == 1
