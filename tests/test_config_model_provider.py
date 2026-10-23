"""模型配置（换厂商/换模型）的回归测试。

覆盖三件事：
  1. endpoint 拼接规则对各家 base url 都成立（OpenAI 兼容协议的通用约定）；
  2. 新的通用环境变量名（LLM_*）与旧的 MiniMax 专用名（MINIMAX_* / MODEL_*）都生效，新名优先；
  3. key 缺失时 fail closed，错误信息能告诉人怎么配。
"""

from __future__ import annotations

import pytest

from app.config import (
    DEFAULT_BASE_URL,
    DEFAULT_MODEL_BRAIN,
    Settings,
    build_endpoint,
    get_settings,
)
from app.llm import LLMAdapter, LLMError, MockAdapter, ScriptAdapter, build_adapter

_MODEL_ENV_KEYS = (
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
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """本机 .env 里可能有真 key，先全部清干净再断言。"""
    for key in _MODEL_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


# ---------------------------------------------------------------- endpoint


@pytest.mark.parametrize(
    "base,expected",
    [
        ("https://api.minimaxi.com", "https://api.minimaxi.com/v1/chat/completions"),
        ("https://api.deepseek.com", "https://api.deepseek.com/v1/chat/completions"),
        ("https://api.openai.com/v1", "https://api.openai.com/v1/chat/completions"),
        ("https://api.moonshot.cn/v1", "https://api.moonshot.cn/v1/chat/completions"),
        ("https://dashscope.aliyuncs.com/compatible-mode/v1", "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"),
        ("https://open.bigmodel.cn/api/paas/v4", "https://open.bigmodel.cn/api/paas/v4/chat/completions"),
        ("http://localhost:11434/v1", "http://localhost:11434/v1/chat/completions"),
        ("http://127.0.0.1:8000/v1/", "http://127.0.0.1:8000/v1/chat/completions"),
        ("https://x.com/v1/chat/completions", "https://x.com/v1/chat/completions"),
        ("", DEFAULT_BASE_URL + "/v1/chat/completions"),
    ],
)
def test_build_endpoint_rules(base, expected):
    assert build_endpoint(base) == expected


def test_endpoint_override_wins():
    override = "https://open.bigmodel.cn/api/paas/v4/chat/completions"
    assert build_endpoint("https://api.deepseek.com", override) == override


# ------------------------------------------------------------------ 键名


def test_generic_env_names_are_read(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "sk-new")
    monkeypatch.setenv("LLM_BASE_URL", "https://api.deepseek.com")
    monkeypatch.setenv("LLM_MODEL", "deepseek-chat")
    s = Settings()
    assert s.api_key == "sk-new"
    assert s.base_url == "https://api.deepseek.com"
    assert s.model_brain == "deepseek-chat"
    assert s.endpoint == "https://api.deepseek.com/v1/chat/completions"


def test_legacy_env_names_still_work(monkeypatch):
    """老 .env 不用重写：MINIMAX_* / MODEL_* 仍然生效。"""
    monkeypatch.setenv("MINIMAX_API_KEY", "sk-legacy")
    monkeypatch.setenv("MINIMAX_API_HOST", "https://api.minimaxi.com")
    monkeypatch.setenv("MODEL_BRAIN", "MiniMax-M3.1-Flash-Preview")
    s = Settings()
    assert s.api_key == "sk-legacy"
    assert s.base_url == "https://api.minimaxi.com"
    assert s.model_brain == "MiniMax-M3.1-Flash-Preview"


def test_generic_names_take_precedence_over_legacy(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "sk-new")
    monkeypatch.setenv("MINIMAX_API_KEY", "sk-legacy")
    monkeypatch.setenv("LLM_MODEL", "deepseek-chat")
    monkeypatch.setenv("MODEL_BRAIN", "MiniMax-M3.1-Flash-Preview")
    s = Settings()
    assert s.api_key == "sk-new"
    assert s.model_brain == "deepseek-chat"


def test_host_alias_keeps_old_field_working(monkeypatch):
    monkeypatch.setenv("LLM_BASE_URL", "https://api.deepseek.com")
    s = Settings()
    assert s.host == s.base_url


def test_timeout_legacy_alias(monkeypatch):
    monkeypatch.setenv("LLM_TIMEOUT_S", "90")
    assert Settings().timeout == 90
    monkeypatch.setenv("LLM_TIMEOUT", "30")
    assert Settings().timeout == 30


def test_defaults_without_any_env():
    s = Settings()
    assert s.base_url == DEFAULT_BASE_URL
    assert s.model_brain == DEFAULT_MODEL_BRAIN
    assert s.api_key == ""
    assert s.temperature == 0.0
    assert s.timeout == 60


def test_get_settings_matches_settings():
    assert get_settings() == Settings()


# ---------------------------------------------------------------- 厂商识别


@pytest.mark.parametrize(
    "base,provider",
    [
        ("https://api.minimaxi.com", "minimax"),
        ("https://api.deepseek.com", "deepseek"),
        ("https://open.bigmodel.cn/api/paas/v4", "zhipu"),
        ("https://dashscope.aliyuncs.com/compatible-mode/v1", "dashscope"),
        ("https://api.moonshot.cn/v1", "moonshot"),
        ("https://api.openai.com/v1", "openai"),
        ("http://localhost:11434/v1", "ollama"),
        ("https://my-gateway.internal/v1", "openai-compatible"),
    ],
)
def test_provider_detection(monkeypatch, base, provider):
    monkeypatch.setenv("LLM_BASE_URL", base)
    assert Settings().provider == provider


# ---------------------------------------------------------------- fail closed


def test_missing_key_fails_closed_with_actionable_message():
    adapter = LLMAdapter(Settings())
    with pytest.raises(LLMError) as exc:
        adapter._headers()
    msg = str(exc.value)
    assert "LLM_API_KEY" in msg
    assert "模型配置说明" in msg


def test_headers_use_bearer_scheme(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "sk-abc")
    headers = LLMAdapter(Settings())._headers()
    assert headers["Authorization"] == "Bearer sk-abc"
    assert headers["Content-Type"] == "application/json"


# ---------------------------------------------------------------- mock 模式


def test_mock_mode_true_gives_adversarial_stub(monkeypatch):
    monkeypatch.setenv("MOCK_MODE", "true")
    assert isinstance(build_adapter(Settings()), MockAdapter)


def test_mock_mode_script_gives_offline_stub(monkeypatch):
    monkeypatch.setenv("MOCK_MODE", "script")
    assert isinstance(build_adapter(Settings()), ScriptAdapter)
