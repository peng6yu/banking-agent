"""策略层：三色权限裁决（纯函数）。

对应文档：docs/05_安全层实现规格.md 5.1/5.2。
"""

from __future__ import annotations

from app.policy.engine import decide, explain
from app.policy.rules import (
    ABSOLUTE_DAILY_CAP_FEN,
    DAILY_LIMIT_FEN,
    DAILY_RISK_THRESHOLD_FEN,
    PolicyContext,
    Rule,
    RULES,
    SINGLE_LIMIT_FEN,
)

__all__ = [
    "ABSOLUTE_DAILY_CAP_FEN",
    "DAILY_LIMIT_FEN",
    "DAILY_RISK_THRESHOLD_FEN",
    "PolicyContext",
    "RULES",
    "Rule",
    "SINGLE_LIMIT_FEN",
    "decide",
    "explain",
]
