"""统一时间源与业务异常层级。

对应文档：docs/04_核心模块实现规格.md 4.1/4.2、docs/08_编码规范.md 8.4。

全项目的时间只能从这里取（now_ms / now_s），业务异常也只能从这里抛：
API 层只要认识一个基类，就能把任何失败映射成 HTTP 状态码 + 给用户看的中文话。
"""

from __future__ import annotations

import re
import time
from typing import Any, Final

# 形如 RULES_BROKEN 的入参按错误码处理而不是按人话处理，
# 免得英文常量被原样甩到界面上（docs/08 8.4「把英文异常名直接返给用户」要扣分）。
_CODE_LIKE: Final[re.Pattern[str]] = re.compile(r"^[A-Z][A-Z0-9_]*$")

_frozen_ms: int | None = None


def now_ms() -> int:
    """当前 Unix 毫秒；冻结之后不再随真实时间前进。"""
    if _frozen_ms is not None:
        return _frozen_ms
    # docs/04 4.1：全项目唯一允许直接读系统时钟的地方
    return int(time.time() * 1000)


def now_s() -> int:
    """当前 Unix 秒。"""
    return now_ms() // 1000


def freeze(ts_ms: int) -> None:
    """冻结时间，仅供测试与可复现演示使用。"""
    global _frozen_ms
    _frozen_ms = int(ts_ms)


def unfreeze() -> None:
    """解冻时间，恢复真实时钟。"""
    global _frozen_ms
    _frozen_ms = None


def is_frozen() -> bool:
    """当前是否处于冻结状态。"""
    return _frozen_ms is not None


class BankError(Exception):
    """业务异常基类：带 code、http_status 与中文 user_text。

    入参形如 'RULES_BROKEN' 的大写常量会覆盖 code，形如 '没听懂金额' 的中文
    会覆盖 user_text，其余关键字参数原样收进 fields 供日志与审计使用。
    """

    code: str = "BANK_ERROR"
    http_status: int = 500
    user_text: str = "操作没成功，稍后我再试一次。"

    def __init__(self, message: str | None = None, **fields: Any) -> None:
        self.detail: str = message or ""
        self.fields: dict[str, Any] = fields
        if message:
            if _CODE_LIKE.match(message):
                self.code = message
            else:
                self.user_text = message
        super().__init__(self.user_text)

    def to_dict(self) -> dict[str, Any]:
        """给 API 层用的最小结构：只给 code 与人话，不带堆栈。"""
        return {"code": self.code, "user_text": self.user_text}


class ValidationError(BankError):
    code = "VALIDATION"
    http_status = 400
    user_text = "我没理解这个说法，换个说法再试一次。"


class MissingCapability(BankError):
    code = "MISSING_CAPABILITY"
    http_status = 403
    user_text = "这一步没有授权凭证，我不能直接动账户。"


class TokenExpired(BankError):
    code = "TOKEN_EXPIRED"
    http_status = 403
    user_text = "凭证已经过期了，我重新走一遍确认流程。"


class TokenMismatch(BankError):
    code = "TOKEN_MISMATCH"
    http_status = 403
    user_text = "凭证和你刚说的对不上，我不敢动，先跟你核对一下。"


class PolicyDenied(BankError):
    code = "POLICY_DENIED"
    http_status = 403
    user_text = "这笔操作超出了安全范围，我先不做了。"


class InsufficientFunds(BankError):
    code = "INSUFFICIENT_FUNDS"
    http_status = 409
    user_text = "账户余额不够这笔，要不要换个金额或者看看别的额度。"


class HoldExpired(BankError):
    code = "HOLD_EXPIRED"
    http_status = 409
    user_text = "刚才那笔预留已经超时释放了，需要重新发起一次。"


class NotFound(BankError):
    """记录不存在：404，仍然是一句人话，不把 id 回显成英文错误。

    W3 起放在内核里：用例层（app/use_cases）和路由层都要用它抛「没这条记录」，
    以前它定义在 app/api/routes.py，用例层要用就得反过来 import api 层。
    """

    code = "NOT_FOUND"
    http_status = 404
    user_text = "没找到这条记录，可能已经删掉，或者不在这份演示数据里。"


class InjectionDetected(BankError):
    code = "INJECTION_DETECTED"
    http_status = 400
    user_text = "内容里夹带了指令，我把它当普通文本忽略了。"


class BreakerOpen(BankError):
    code = "BREAKER_OPEN"
    http_status = 429
    user_text = "连续几次请求没通过，我先歇一会儿，稍后再试。"


class ToolNotFound(BankError):
    code = "TOOL_NOT_FOUND"
    http_status = 400
    user_text = "这个操作我还不会，先不做了。"


class SystemBusy(BankError):
    """数据库忙（SQLite 写锁没抢到）：可重试的临时故障，不是业务失败。

    2026-10-23 加：红队 E05 用另一条连接攥住 BEGIN EXCLUSIVE，busy_timeout
    5 秒后 sqlite3 抛 OperationalError。以前它一路冒到最外层，被当成
    「未预期异常」落成 INTERNAL 兜底——对用户是句空话，排查时也分不清是
    「系统忙」还是「代码有 bug」。忙不忙是一等一的可分类状态，单独给码。
    """

    code = "SYSTEM_BUSY"
    http_status = 503
    user_text = "系统正忙，刚才这笔没有执行，过一会儿再试一次。"


# docs/02 2.x 把异常清单挂在 app/errors.py 名下，docs/04 4.2 的树也写作 AppError。
# 这里以 docs/08 8.4 为准（异常定义在 clock.py），同时留别名给按旧名字 import 的调用方。
AppError = BankError

__all__ = [
    "BankError",
    "AppError",
    "ValidationError",
    "PolicyDenied",
    "MissingCapability",
    "TokenExpired",
    "TokenMismatch",
    "InsufficientFunds",
    "HoldExpired",
    "NotFound",
    "InjectionDetected",
    "BreakerOpen",
    "ToolNotFound",
    "SystemBusy",
    "now_ms",
    "now_s",
    "freeze",
    "unfreeze",
    "is_frozen",
]
