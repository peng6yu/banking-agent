"""共用口令校验：/api/* 的每个端点都挂在它后面（第 5 层，只认 app.security.api_auth）。"""

from __future__ import annotations

from typing import Any, Final
from fastapi import APIRouter, Depends, Request

from app.clock import BankError, NotFound, ValidationError
from app.security import api_auth



class Unauthorized(BankError):
    """没带令牌 / 令牌不对：401，响应体仍然只有 code 与一句人话。"""

    code = "UNAUTHORIZED"
    http_status = 401
    user_text = "这个请求没有带访问令牌，未执行。用启动日志里带 token 的地址打开页面再试。"


OPEN_PATHS: Final[frozenset[str]] = frozenset({"/", "/api/health", "/favicon.ico"})


def _presented_token(request: Request) -> str | None:
    """令牌只从请求头取：Authorization、X-API-Token 或 token。

    刻意**不接受查询参数**：?token= 会随首个请求写进访问日志与代理日志，
    长期令牌不该出现在那里。浏览器首次打开用 URL fragment（#token=），
    前端读一次存进 sessionStorage，再由 fetch 自动加 Authorization 头。
    """
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        return header[7:].strip()
    for name in ("x-api-token", "token"):
        value = request.headers.get(name)
        if value:
            return value.strip()
    return None


async def require_api_token(request: Request) -> None:
    """所有 /api/* 都校验访问令牌；关掉鉴权或开放路径直接放行。

    这一层只回答「请求从哪来」，不回答「你是谁」——那是登录体系的事，
    单用户原型没有，README 里照实写着。
    """
    if request.url.path in OPEN_PATHS or api_auth.auth_disabled():
        return
    if not api_auth.is_authorized(_presented_token(request)):
        raise Unauthorized()
