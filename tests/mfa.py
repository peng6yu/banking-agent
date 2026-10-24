"""测试辅助：用服务端签发的真挑战换证明。

生产代码里没有任何「自己给自己开证明」的入口——证明必须由 /api/factor/challenge
（或 factors.issue）签发，再拿一次验证码回来。测试要过红灯，就得走同一条路。
"""

from __future__ import annotations

import json
from typing import Any

from app import db
from app.security import factors


def required_factors(conn: Any, session_id: str, confirmation_id: str) -> list[str]:
    """从落库的裁决里读出这张卡要求的因子。"""
    row = conn.execute(
        "SELECT n.decision_json AS decision_json FROM confirmations c"
        " JOIN dag_nodes n ON n.run_id = c.run_id AND n.node_id = c.node_id"
        " WHERE c.id = ? AND c.session_id = ?",
        (confirmation_id, session_id),
    ).fetchone()
    if row is None:
        return []
    try:
        decision = json.loads(row["decision_json"] or "{}")
    except ValueError:
        return []
    return [str(item) for item in (decision.get("required_factors") or [])]


def proofs(conn: Any, session_id: str, confirmation_id: str,
           required: list[str] | None = None) -> list[dict[str, str]]:
    """签发挑战并立刻「收到」验证码，返回 /api/confirm 需要的 proofs。"""
    wanted = list(required) if required is not None else required_factors(conn, session_id, confirmation_id)
    out: list[dict[str, str]] = []
    for factor in wanted:
        challenge = factors.issue(
            conn,
            session_id=session_id,
            confirmation_id=confirmation_id,
            factor=factor,
        )
        out.append({
            "factor": factor,
            "challenge_id": str(challenge["challenge_id"]),
            "code": str(challenge["code"]),
        })
    return out


def solve(session_id: str, confirmation_id: str, required: list[str] | None = None) -> list[dict[str, str]]:
    """开一条自己的连库连接拿证明；调用方不必操心连接生命周期。"""
    conn = db.connect()
    try:
        return proofs(conn, session_id, confirmation_id, required)
    finally:
        conn.close()
