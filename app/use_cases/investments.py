"""风险测评：把 5 题答案算成分数并落画像（只写画像，不碰持仓）。"""

from __future__ import annotations

from typing import Any

import app.proactive as proactive
import app.suitability as suitability
from app.use_cases.base import write_case


@write_case
def assessment_submit(conn: Any, *, answers: dict[str, str]) -> dict[str, Any]:
    """提交 5 题答案：算分、落画像、写一条 suitability.assessed 审计。"""
    return {"profile": suitability.save(conn, proactive.default_user_id(conn), answers)}


__all__ = ["assessment_submit"]
