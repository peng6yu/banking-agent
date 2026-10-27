"""多因子挑战的签发。

以前这段直接写在路由里：读 confirmations、读 dag_nodes 的裁决、查手机号、开事务
签发挑战。它是「写」，而且带 SQL，属于 W3 要收进用例层的典型：现在路由只绑定入参，
事务与契约都在这一层。

放行依据始终在服务端：前端拿到的只有挑战号和一次验证码，验证时仍然回服务端校验。
"""

from __future__ import annotations

from typing import Any

import app.security.factors as factors
from app import clock
from app.clock import ValidationError
from app.use_cases.base import write_case


def _mask_phone(phone: str | None) -> str | None:
    """手机号只在验证提示里露出后三位与后四位，别把完整号码回给前端。"""
    if not phone:
        return None
    text = str(phone)
    if len(text) <= 7:
        return "*" * len(text)
    return text[:3] + "****" + text[-4:]


@write_case
def challenge_factor(
    conn: Any,
    *,
    session_id: str,
    confirmation_id: str,
    factor: str | None = None,
) -> dict[str, Any]:
    """给一张待确认的卡签发多因子挑战；指定因子时只签那一个。"""
    import json

    row = conn.execute(
        "SELECT * FROM confirmations WHERE id = ? AND session_id = ?",
        (confirmation_id, session_id),
    ).fetchone()
    if row is None:
        raise ValidationError("确认不存在或已过期", confirmation_id=confirmation_id)
    if str(row["status"]) != "pending":
        raise ValidationError("这张确认已经处理过了", status=str(row["status"]))
    if int(row["expires_ms"]) <= clock.now_ms():
        raise ValidationError("这张确认已经过期了", confirmation_id=confirmation_id)
    node = conn.execute(
        "SELECT * FROM dag_nodes WHERE run_id = ? AND node_id = ?",
        (row["run_id"], row["node_id"]),
    ).fetchone()
    decision = json.loads((node["decision_json"] if node else None) or "{}")
    required = [str(item) for item in (decision.get("required_factors") or [])]
    if factor:
        if factor not in required:
            raise ValidationError("这一步不需要这个验证因子", factor=factor)
        required = [factor]
    user = conn.execute("SELECT phone FROM users ORDER BY id LIMIT 1").fetchone()
    target = _mask_phone(user["phone"] if user else None)
    challenges = [
        factors.issue(
            conn,
            session_id=session_id,
            confirmation_id=confirmation_id,
            factor=name,
            target=target,
        )
        for name in required
    ]
    return {
        "confirmation_id": confirmation_id,
        "required": required,
        "challenges": challenges,
        "note": "验证码由服务端签发、服务端校验、只能用一次；演示环境直接回显，真实环境走短信/人脸网关。",
    }


__all__ = ["challenge_factor"]
