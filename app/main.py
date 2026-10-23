"""FastAPI 入口：注册路由与三个异常处理器。

对应文档：docs/07 7.5 的异常 → HTTP 映射表。
硬规矩：任何异常到浏览器那里都只剩 {code, user_text} 两个字段，
堆栈、英文异常名、路径一律不出现在响应体里。
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app import db
from app.api import routes
from app.clock import BankError
from app.llm import LLMError
from app.planner import ticker
from app.security import api_auth

log = logging.getLogger(__name__)

API_TITLE = "银行 AI 智能体"
API_VERSION = "0.1.0"
INTERNAL_CODE = "INTERNAL"
INTERNAL_TEXT = "系统开小差了，已经记录。"
MODEL_CODE = "MODEL_UNAVAILABLE"
MODEL_TEXT = "模型暂时没响应，请稍后再试一次。"
VALIDATION_CODE = "VALIDATION"
VALIDATION_TEXT = "请求参数不对，我没法理解你发的内容。"
HTTP_CODE = "HTTP_ERROR"
HTTP_TEXT = "这个请求我没找到对应的处理方式。"


def _announce_api_token() -> None:
    """启动时把访问令牌打到日志：本机唯一一次「拿到令牌」的入口。

    令牌整串出现在本机控制台是有意的——它要能被人复制去开浏览器；
    日志采集不该收它，所以 mask() 留给审计/远端日志用。
    """
    if api_auth.auth_disabled():
        log.warning("api_auth.disabled /api/* 不再校验令牌，仅在「端口只对本机可信进程开放」时使用")
        return
    try:
        token = api_auth.ensure_token()
    except OSError as exc:
        log.error(
            "api_auth.token_unwritable file=%s error=%s 受保护端点将一律 401；"
            "请修好目录权限，或者显式设置 API_TOKEN",
            api_auth.token_path(),
            exc,
        )
        return
    log.info("api_auth.ready file=%s token=%s", api_auth.token_path(), token)
    log.info("api_auth.open_url %s", api_auth.bootstrap_url(token))


def _ensure_schema() -> None:
    """启动时把表补齐：DDL 幂等，老库（上一版留下的 data/bank.db）自动多出新增的表。

    为什么放在启动：新增的 savings_goals / monitor_rules / proactive_alerts /
    monitor_runs 四张表只写在本版本的 DDL 里，而线上库是旧版本建的。
    没有这一步，升级后第一次调用主动财务接口就是 "no such table"。
    失败不阻塞启动（模型/静态页仍可用），但会留一条 warn 级日志。
    """
    try:
        connection = db.connect()
        try:
            db.init_db(connection)
            tables = db.table_names(connection)
        finally:
            connection.close()
        log.info("app.startup schema_ok tables=%d", len(tables))
    except Exception as exc:  # noqa: BLE001 - 起不来也要先把服务拉起来，错误留日志
        log.warning("app.startup schema_failed %s: %s", type(exc).__name__, exc)


@asynccontextmanager
async def _lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """启动时补齐表结构、起到点执行节拍，关闭时收掉；SCHEDULED_TICK_MS=0 可关。"""
    _ensure_schema()
    task = ticker.start()
    log.info("app.startup ticker=%s", "on" if task is not None else "off")
    _announce_api_token()
    try:
        yield
    finally:
        await ticker.stop(task)


app = FastAPI(title=API_TITLE, version=API_VERSION, lifespan=_lifespan)
app.include_router(routes.router)


@app.exception_handler(BankError)
async def _bank_error(_request: Request, exc: BankError) -> JSONResponse:
    """业务异常：自带 code 与中文话，直接映射成状态码。"""
    return JSONResponse(status_code=exc.http_status, content=exc.to_dict())


@app.exception_handler(LLMError)
async def _llm_error(_request: Request, exc: LLMError) -> JSONResponse:
    """模型不可用是 503，不是 500——它会恢复，值得用户再试一次。"""
    log.warning("api.llm_error %s", str(exc)[:200])
    return JSONResponse(status_code=503, content={"code": MODEL_CODE, "user_text": MODEL_TEXT})


@app.exception_handler(RequestValidationError)
async def _validation_error(_request: Request, _exc: RequestValidationError) -> JSONResponse:
    """入参不合法回 400；不把 pydantic 的英文字段名回传给浏览器。"""
    return JSONResponse(status_code=400, content={"code": VALIDATION_CODE, "user_text": VALIDATION_TEXT})


@app.exception_handler(StarletteHTTPException)
async def _http_error(_request: Request, exc: StarletteHTTPException) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code, content={"code": HTTP_CODE, "user_text": HTTP_TEXT})


@app.exception_handler(Exception)
async def _unhandled(_request: Request, exc: Exception) -> JSONResponse:
    """兜底 500：日志里留全堆栈，响应体只有一句人话。"""
    log.exception("api.unhandled %s", type(exc).__name__)
    return JSONResponse(status_code=500, content={"code": INTERNAL_CODE, "user_text": INTERNAL_TEXT})


# 根路径的静态页挂在 app/api/routes.py，这里只管异常映射
__all__ = ["app"]
