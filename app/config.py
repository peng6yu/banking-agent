from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

_ENV_LINE = re.compile(r"^\s*([A-Z0-9_]+)\s*=\s*(.*)$")
_TRUTHY: Final[frozenset[str]] = frozenset({"1", "true", "yes", "on"})
# MOCK_MODE 的另一个取值：协作回放桩（离线但正常办事），与上面的对抗演示桩不是一回事
MOCK_SCRIPT: Final[str] = "script"

# --------------------------------------------------------------------------
# 默认厂商：MiniMax。但代码只依赖 OpenAI 兼容协议
# （POST {base}/chat/completions + Bearer 鉴权 + tool_calls 结构化输出），
# 换任何兼容厂商都只要改 .env 里的 LLM_BASE_URL / LLM_MODEL / LLM_API_KEY。
# 详见 docs/模型配置说明.md
# --------------------------------------------------------------------------
DEFAULT_BASE_URL: Final[str] = "https://api.minimaxi.com"
DEFAULT_MODEL_BRAIN: Final[str] = "MiniMax-M3.1-Flash-Preview"
DEFAULT_MODEL_FAST: Final[str] = "MiniMax-M2.5-highspeed"
DEFAULT_MODEL_BACKUP: Final[str] = "MiniMax-M2.5"

# 末尾带版本号（/v1、/v4…）的 base url 只补 /chat/completions，否则补 /v1/chat/completions
_VERSION_TAIL = re.compile(r"/v\d+$")

# 厂商粗判表：只用于自检输出和文档提示，不参与任何路由决策
_PROVIDER_MARKERS: Final[tuple[tuple[str, str], ...]] = (
    ("minimax", "minimaxi.com"),
    ("minimax", "minimax.io"),
    ("deepseek", "deepseek.com"),
    ("zhipu", "bigmodel.cn"),
    ("dashscope", "dashscope.aliyuncs.com"),
    ("moonshot", "moonshot.cn"),
    ("openai", "openai.com"),
    ("ollama", "11434"),
    ("lmstudio", "1234"),
    ("vllm", "8000"),
)


def load_env(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = _ENV_LINE.match(line)
        if not m:
            continue
        key, val = m.group(1), m.group(2).strip().strip('"').strip("'")
        os.environ.setdefault(key, val)


ROOT = Path(__file__).resolve().parent.parent
load_env(ROOT / ".env")


def _env(*names: str, default: str = "") -> str:
    """按优先级取第一个非空环境变量（真实环境变量优先于 .env，见 load_env）。"""
    for name in names:
        val = (os.getenv(name) or "").strip()
        if val:
            return val
    return default


def _env_float(*names: str, default: float) -> float:
    raw = _env(*names)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_int(*names: str, default: int) -> int:
    raw = _env(*names)
    if not raw:
        return default
    try:
        return int(float(raw))
    except ValueError:
        return default


def _env_mock() -> str:
    return (os.getenv("MOCK_MODE") or "false").strip().lower()


def build_endpoint(base_url: str, endpoint_override: str = "") -> str:
    """把 base url 拼成 chat/completions 地址。

    规则（与 docs/模型配置说明.md 一致）：
      1. 给了 LLM_ENDPOINT 就直接用它，别的都不管；
      2. base 已以 /chat/completions 结尾 → 原样返回；
      3. base 以 /v1、/v4 等版本号结尾 → 只补 /chat/completions；
      4. 其余 → 补 /v1/chat/completions。
    """
    override = (endpoint_override or "").strip()
    if override:
        return override
    base = (base_url or "").strip().rstrip("/")
    if not base:
        base = DEFAULT_BASE_URL
    if base.endswith("/chat/completions"):
        return base
    if _VERSION_TAIL.search(base):
        return base + "/chat/completions"
    return base + "/v1/chat/completions"


# --------------------------------------------------------------------------
# 降级链：主目标不可用时按配置顺序切到下一个目标
# --------------------------------------------------------------------------
# LLM_FALLBACK          逗号分隔的**同厂商**模型名（与主目标同 base、同 key）
# LLM_FALLBACK_VENDORS  逗号分隔的**跨厂商**条目：base_url::模型名::KEY_ENV_NAME
#   第三段 KEY_ENV_NAME 是**环境变量名**，运行时才从那个环境变量里读密钥；
#   配置串里永远不出现密钥本身。
# --------------------------------------------------------------------------
# 跨厂商条目的分隔符
_FALLBACK_VENDOR_SEP = "::"


@dataclass(frozen=True)
class LLMTarget:
    """一个可服务的模型目标（主目标或降级目标）。

    api_key 是运行时读到的真实密钥：只允许待在内存里，
    不许写进配置文件、日志或错误消息（错误消息只报 base / 模型 / 错误类别）。
    label 是给审计看的短标识，同样不含密钥。
    """

    base_url: str
    api_key: str
    model: str
    endpoint: str
    label: str


def parse_fallback_models(raw: str) -> tuple[str, ...]:
    """LLM_FALLBACK：逗号分隔的同厂商模型名，保序去重。"""
    names: list[str] = []
    for chunk in (raw or "").split(","):
        name = chunk.strip()
        if name and name not in names:
            names.append(name)
    return tuple(names)


def parse_fallback_vendors(raw: str) -> tuple[tuple[str, str, str], ...]:
    """LLM_FALLBACK_VENDORS：逗号分隔的 base_url::模型名::KEY_ENV_NAME。

    格式不对的条目直接跳过（少一个降级目标好过整个进程起不来）。
    第三段是**环境变量名**，不是密钥。
    """
    vendors: list[tuple[str, str, str]] = []
    for chunk in (raw or "").split(","):
        parts = [part.strip() for part in chunk.strip().split(_FALLBACK_VENDOR_SEP)]
        if len(parts) != 3 or not all(parts):
            continue
        vendors.append((parts[0], parts[1], parts[2]))
    return tuple(vendors)


@dataclass(frozen=True)
class Settings:
    """运行时配置。

    字段默认值走 default_factory：**每次实例化时读环境变量**，
    所以测试里 monkeypatch.setenv 立刻生效，.env 或环境变量改了无需重启进程。

    键名兼容：新的通用名（LLM_API_KEY / LLM_BASE_URL / LLM_MODEL…）优先，
    旧的 MiniMax 专用名（MINIMAX_API_KEY / MINIMAX_API_HOST / MODEL_BRAIN…）
    仍然生效，老 .env 不用重写。
    """

    api_key: str = field(default_factory=lambda: _env("LLM_API_KEY", "MINIMAX_API_KEY"))
    base_url: str = field(
        default_factory=lambda: _env("LLM_BASE_URL", "MINIMAX_API_HOST", default=DEFAULT_BASE_URL)
    )
    endpoint_override: str = field(default_factory=lambda: _env("LLM_ENDPOINT"))
    model_brain: str = field(
        default_factory=lambda: _env("LLM_MODEL", "MODEL_BRAIN", default=DEFAULT_MODEL_BRAIN)
    )
    model_fast: str = field(
        default_factory=lambda: _env("LLM_MODEL_FAST", "MODEL_FAST", default=DEFAULT_MODEL_FAST)
    )
    model_backup: str = field(
        default_factory=lambda: _env("LLM_MODEL_BACKUP", "MODEL_BACKUP", default=DEFAULT_MODEL_BACKUP)
    )
    temperature: float = field(default_factory=lambda: _env_float("LLM_TEMPERATURE", default=0.0))
    timeout: int = field(default_factory=lambda: _env_int("LLM_TIMEOUT", "LLM_TIMEOUT_S", default=60))
    mock: bool = field(default_factory=lambda: _env_mock() in _TRUTHY)
    # MOCK_MODE=script：协作回放桩。纯规则、确定性、零网络，断网也能演主链路；
    # 上面那个 mock 是对抗演示专用（恶意模型），两者行为完全不同，别混用。
    mock_script: bool = field(default_factory=lambda: _env_mock() == MOCK_SCRIPT)

    @property
    def host(self) -> str:
        """兼容旧字段名：老代码/老脚本读 settings.host。"""
        return self.base_url

    @property
    def endpoint(self) -> str:
        return build_endpoint(self.base_url, self.endpoint_override)

    @property
    def provider(self) -> str:
        """从 base url 粗判厂商名，只用于自检输出和文档提示。"""
        base = (self.base_url or "").lower()
        for name, marker in _PROVIDER_MARKERS:
            if marker in base:
                return name
        return "openai-compatible"

    @property
    def fallback_targets(self) -> tuple[LLMTarget, ...]:
        """目标链：第 0 位永远是主目标，后面按配置顺序排降级目标。

        - 主目标：当前 base_url / api_key / model_brain；
        - LLM_FALLBACK：同厂商降级模型（同一个 base、同一把 key）；
        - LLM_FALLBACK_VENDORS：跨厂商条目，密钥在运行时从 KEY_ENV_NAME 指向的
          环境变量里读，配置串里不出现密钥。

        这里做成 property 而不是字段：每次访问都重新读环境变量，
        改完 .env 不用重启进程；同时显式构造 Settings(api_key=...) 也能生效。
        """
        primary = LLMTarget(
            base_url=self.base_url,
            api_key=self.api_key,
            model=self.model_brain,
            endpoint=self.endpoint,
            label="primary",
        )
        targets: list[LLMTarget] = [primary]
        for name in parse_fallback_models(_env("LLM_FALLBACK")):
            targets.append(
                LLMTarget(
                    base_url=self.base_url,
                    api_key=self.api_key,
                    model=name,
                    endpoint=self.endpoint,
                    label="fallback:" + name,
                )
            )
        for base, model, key_env in parse_fallback_vendors(_env("LLM_FALLBACK_VENDORS")):
            targets.append(
                LLMTarget(
                    base_url=base,
                    # 跨厂商的密钥运行时才读，且只来自环境变量
                    api_key=(os.getenv(key_env) or "").strip(),
                    model=model,
                    endpoint=build_endpoint(base),
                    label="vendor:" + base,
                )
            )
        return tuple(targets)

    @classmethod
    def from_env(cls) -> "Settings":
        """显式入口，语义与 Settings() 相同（都从环境变量读）。"""
        return cls()


def get_settings() -> Settings:
    return Settings()
