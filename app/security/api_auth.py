"""HTTP 访问令牌：把「谁能调这台 API」从部署假设变成真校验。

为什么需要它：路由层原先没有任何身份校验，session_id 由客户端随便传，
任何能连上端口的人都能以任意会话发起转账、读审计、甚至代替用户点确认。
只绑 `127.0.0.1` 压住了远程风险，但那是部署习惯，不是代码保证。

取值优先级（从高到低）：
1. 环境变量 `API_TOKEN`（非空）—— 显式指定；
2. 文件 `API_TOKEN_FILE`（默认 `data/api_token`，0600）—— 首次启动自动生成；
3. 都没有 —— 返回空串，所有受保护端点 401（fail closed），不会悄悄放行。

唯一的口子是 `API_AUTH_DISABLED=1`：只给「端口确实只对本机可信进程开放」的
排障场景用，启动时会打警告；它不影响 capability token 的独立校验。

模块只用标准库，待在安全层最底下，被 API 层与入口引用。
"""

from __future__ import annotations

import hmac
import os
import secrets
from pathlib import Path
from typing import Final

#: 显式指定令牌的环境变量
TOKEN_ENV: Final[str] = "API_TOKEN"
#: 令牌落盘路径的覆盖项
TOKEN_FILE_ENV: Final[str] = "API_TOKEN_FILE"
#: 关掉 HTTP 鉴权的逃生口（只给排障用）
DISABLED_ENV: Final[str] = "API_AUTH_DISABLED"
DEFAULT_TOKEN_FILE: Final[str] = "data/api_token"
TOKEN_FILE_MODE: Final[int] = 0o600
#: token_urlsafe(32) 约 43 个字符，足够抗枚举
TOKEN_BYTES: Final[int] = 32
DISABLED_VALUES: Final[frozenset[str]] = frozenset({"1", "true", "yes", "on"})


def auth_disabled() -> bool:
    """是否显式关掉 HTTP 鉴权；只认 1/true/yes/on，其余一律当作没关。"""
    return os.environ.get(DISABLED_ENV, "").strip().lower() in DISABLED_VALUES


def token_path() -> Path:
    """令牌文件路径：API_TOKEN_FILE 优先，默认 data/api_token。"""
    return Path(os.environ.get(TOKEN_FILE_ENV) or DEFAULT_TOKEN_FILE)


def load_token() -> str:
    """读当前生效的令牌；没有 / 读不了就返回空串，由调用方 fail closed。"""
    raw = os.environ.get(TOKEN_ENV, "").strip()
    if raw:
        return raw
    try:
        return token_path().read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def ensure_token() -> str:
    """确保有一把可用令牌：env 优先，其次读文件，最后生成并落盘（0600）。

    写盘失败会抛 OSError 给启动流程——宁可启动日志里报错并让请求全 401，
    也不能因为「写不了文件」就悄悄关掉鉴权。
    """
    raw = os.environ.get(TOKEN_ENV, "").strip()
    if raw:
        return raw
    current = load_token()
    if current:
        return current
    path = token_path()
    token = secrets.token_urlsafe(TOKEN_BYTES)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(token + "\n", encoding="utf-8")
    os.chmod(tmp, TOKEN_FILE_MODE)
    os.replace(tmp, path)
    return token


def is_authorized(presented: str | None, *, token: str | None = None) -> bool:
    """常数时间比较：没配令牌、没带令牌、带错令牌，一律 False。"""
    if auth_disabled():
        return True
    expected = (token if token is not None else load_token()).strip()
    given = (presented or "").strip()
    if not expected or not given:
        return False
    return hmac.compare_digest(given.encode("utf-8"), expected.encode("utf-8"))


def mask(token: str) -> str:
    """日志/审计用：只留头 4 尾 2，整串不落盘。"""
    text = (token or "").strip()
    if len(text) <= 8:
        return "***"
    return f"{text[:4]}…{text[-2:]}"


def bootstrap_url(token: str, *, host: str = "127.0.0.1", port: int = 8000) -> str:
    """带令牌的浏览器地址；令牌放在 URL fragment（#）里，不会随 HTTP 请求发到服务端。

    为什么不用 ?token=：查询串会进服务端访问日志、代理日志和浏览器历史，
    长期令牌一旦落到日志里就等于泄露。fragment 只存在于浏览器，前端读一次存进
    sessionStorage 后立刻从地址栏抹掉；服务端也不再接受查询参数里的令牌。
    """
    return f"http://{host}:{port}/#token={token}"


__all__ = [
    "DEFAULT_TOKEN_FILE",
    "DISABLED_ENV",
    "TOKEN_BYTES",
    "TOKEN_ENV",
    "TOKEN_FILE_ENV",
    "TOKEN_FILE_MODE",
    "auth_disabled",
    "bootstrap_url",
    "ensure_token",
    "is_authorized",
    "load_token",
    "mask",
    "token_path",
]
