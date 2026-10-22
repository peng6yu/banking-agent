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

from app.api import routes
from app.clock import BankError
from app.llm import LLMError
from app.planner import ticker

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

@asynccontextmanager
async def _lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """启动时起到点执行节拍，关闭时收掉；SCHEDULED_TICK_MS=0 可关。"""
    task = ticker.start()
    log.info("app.startup ticker=%s", "on" if task is not None else "off")
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
