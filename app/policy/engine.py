"""三色裁决引擎：decide() 是纯函数。

对应文档：docs/05_安全层实现规格.md 5.2。

纯函数约束（评审会现场查）：本模块不 import llm、不读环境变量、不写数据库、不自己取时钟。
所有外部事实由调用方查好后放进 PolicyContext，所以同一条 Action 任何时候重放结果一致。
"""

from __future__ import annotations

from app.clock import AppError
from app.models import Action, Decision
from app.money import fen_to_yuan_str
from app.policy.rules import PolicyContext, RULES

_LEVEL_BADGE: dict[str, str] = {"green": "🟢 直接执行", "yellow": "🟡 待确认", "red": "🔴 已拦截"}

_BASIS: dict[str, str] = {
    "READONLY": "依据：只读查询不改变账户状态，按 R01 直接放行",
    "INVALID_AMOUNT": "依据：金额缺失或不是正数，按 R02 一律拦截",
    "UNKNOWN_TOOL": "依据：工具不在白名单里，按 R03 一律拦截",
    "UNTRUSTED_SOURCE": "依据：请求字段检出注入特征，金额来源不可信，按 R04 升级为人脸+短信",
    "CONFIRM_ONLY": "依据：写操作但不涉及金额，按 R05 先跟你确认一句",
    "TRANSFER_YELLOW": "依据：单笔未超过限额，且今日累计仍在限额内，按 R06 待确认",
    "TRANSFER_DAILY_RED": "依据：今日累计加上本笔超过日累计分级线（大额转账），按 R07 要求人脸+短信强验证",
    "TRANSFER_SINGLE_RED": "依据：单笔已超过限额，按 R08 拦截并要求人脸+短信",
    "ABSOLUTE_DAILY_CAP": "依据：超过本部署显式配置的绝对每日限额，按 R13 硬拒绝（因子不能突破绝对限额）",
    "SENSITIVE_TOOL": "依据：账户安全类操作，按 R09 需要人脸+短信+动态口令三重验证",
    "INVEST_TOOL": "依据：理财类操作，按 R10 需要人脸+动态口令",
    "DEFAULT_DENY": "依据：既不在已登记的规则里也不在金额规则内，按 R11 默认拒绝",
}


def decide(action: Action, ctx: PolicyContext | None = None) -> Decision:
    """按表顺序短路求值，第一条命中的规则产出裁决。"""
    context = ctx if ctx is not None else PolicyContext()
    for rule in RULES:
        if rule.when(action, context):
            return rule.emit(action, context)
    # 走到这里说明规则表被改坏了（例如 R11 被删）：宁可炸掉也不能默认放行
    raise AppError("RULES_BROKEN")


def explain(decision: Decision, action: Action | None = None) -> str:
    """给人看的三行解释：颜色+规则号 / 这笔到底要干什么 / 为什么。"""
    badge = _LEVEL_BADGE.get(decision.level, decision.level)
    return "\n".join([f"{badge} · {decision.rule_id}", decision.reason_text, _basis(decision, action)])


def _basis(decision: Decision, action: Action | None) -> str:
    """依据行；带了 Action 才能把「单笔多少」写进依据里。"""
    base = _BASIS.get(decision.reason_code, "依据：按既定规则处理")
    if action is None:
        return base
    if decision.reason_code in {"TRANSFER_YELLOW", "TRANSFER_DAILY_RED", "TRANSFER_SINGLE_RED"}:
        limit_fen = decision.limit_fen or 0
        spent_fen = decision.spent_today_fen or 0
        detail = f"单笔 {fen_to_yuan_str(action.amount_fen or 0)} 元，限额 {fen_to_yuan_str(limit_fen)} 元"
        return f"{base}（{detail}，今日已用 {fen_to_yuan_str(spent_fen)} 元）"
    return base


__all__ = ["decide", "explain"]
