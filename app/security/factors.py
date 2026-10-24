"""多因子验证：服务端签发挑战、服务端校验、一次性使用。

为什么重做这一层：旧实现里 POST /api/confirm 只要在 factor 字段里写
"face sms"，后端拿字符串做子集判断就通过——能调接口的人自己宣布「我验过了」，
这等于没有验证。现在的约定是：

1. 客户端先 POST /api/factor/challenge 申请挑战；
2. 服务端生成随机码，只把 sha256(secret|challenge_id|code) 落库（明文不落库）；
3. 校验在服务端做：挑战必须属于同一个会话/同一张确认卡、没过期、没用过、
   尝试次数没超；通过后立刻标记 used_ms，重放同一份证明不再生效；
4. POST /api/confirm 必须带 proofs；缺一个必需因子就仍然是 waiting_factor。

局限（照实写，不夸大）：这是一个竞赛演示环境，没有接真实短信网关和人脸活体
服务，演示模式（FACTOR_DEMO_ECHO=1，默认开）会把验证码原样回给前端，方便评审
现场演示。它真正防住的是「客户端自己宣称已经通过验证」这一类绕过，不防
「拿到演示验证码的人」。短信/人脸的真实投递与比对不在本仓库范围内。
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import sqlite3
from typing import Any, Final

from app import audit, clock
from app.clock import ValidationError
from app.ids import new_id

#: 认可的因子种类；和 policy 层的 required_factors 是同一套名字。
FACTOR_KINDS: Final[tuple[str, ...]] = ("sms", "face", "otp")

#: 挑战有效期：5 分钟。确认卡本身 120 秒，挑战比它长，给「先申请再想一下」留余量。
CHALLENGE_TTL_MS: Final[int] = 300_000

#: 同一张挑战最多允许错几次；超过就作废，必须重新申请。
MAX_ATTEMPTS: Final[int] = 5

#: 演示回显开关：只有显式设成 0/false 才关掉，默认开（现场没短信网关）。
ECHO_ENV: Final[str] = "FACTOR_DEMO_ECHO"
SECRET_ENV: Final[str] = "FACTOR_SECRET"
_ECHO_OFF: Final[frozenset[str]] = frozenset({"0", "false", "no", "off"})


def echo_enabled() -> bool:
    """演示模式是否回显验证码（默认开）。"""
    raw = os.getenv(ECHO_ENV, "").strip().lower()
    return raw not in _ECHO_OFF


def _secret(explicit: str | None) -> str:
    """挑战码的哈希密钥；没配就用固定串（演示库，明说不当生产密钥用）。"""
    if explicit:
        return explicit
    return os.getenv(SECRET_ENV, "").strip() or "demo-factor-secret"


def _digest(secret: str, challenge_id: str, code: str) -> str:
    """验证码摘要：绑定 challenge_id，换一张挑战同一个码就不成立。"""
    return hashlib.sha256(f"{secret}|{challenge_id}|{code}".encode("utf-8")).hexdigest()


def _new_code() -> str:
    """6 位数字验证码（服务端生成，客户端不能指定）。"""
    return f"{secrets.randbelow(10**6):06d}"


def issue(
    conn: sqlite3.Connection,
    *,
    session_id: str,
    confirmation_id: str,
    factor: str,
    target: str | None = None,
    secret: str | None = None,
    now: int | None = None,
) -> dict[str, Any]:
    """签发一张挑战：明文码只在返回值里出现一次，库里只有摘要。"""
    kind = str(factor).strip().lower()
    if kind not in FACTOR_KINDS:
        raise ValidationError("不认识的验证因子", factor=factor)
    current = int(now if now is not None else clock.now_ms())
    challenge_id = new_id("fc")
    code = _new_code()
    expires_ms = current + CHALLENGE_TTL_MS
    conn.execute(
        "INSERT INTO factor_challenges"
        " (id, session_id, confirmation_id, factor, code_hash, created_ms, expires_ms, used_ms, attempts)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, NULL, 0)",
        (
            challenge_id,
            session_id,
            confirmation_id,
            kind,
            _digest(_secret(secret), challenge_id, code),
            current,
            expires_ms,
        ),
    )
    audit.append(
        conn,
        "factor.challenge_issued",
        {
            "challenge_id": challenge_id,
            "confirmation_id": confirmation_id,
            "factor": kind,
            "expires_ms": expires_ms,
            # 审计里只记「发给了哪个尾号」，不记验证码本身
            "target": target,
        },
        session_id=session_id,
    )
    echo = echo_enabled()
    return {
        "challenge_id": challenge_id,
        "factor": kind,
        "expires_ms": expires_ms,
        "expires_in_s": max(expires_ms - current, 0) // 1000,
        "attempts_left": MAX_ATTEMPTS,
        "target": target,
        "delivery": "demo_echo" if echo else "out_of_band",
        "code": code if echo else None,
        "note": (
            "演示模式：验证码由本进程生成并原样回显，校验仍在服务端完成。"
            if echo
            else "验证码已通过外部渠道发送；本服务不接收明文，只校验摘要。"
        ),
        "provider": "demo_stub",
        "limitation": "没有接真实短信网关/人脸活体服务；这里证明的是「客户端不能自己宣布验证通过」。",
    }


def _normalize(proofs: Any) -> list[dict[str, str]]:
    """把入参整理成 [{factor, challenge_id, code}]；坏数据当没给。"""
    if not isinstance(proofs, list):
        return []
    cleaned: list[dict[str, str]] = []
    for item in proofs:
        if isinstance(item, dict):
            cleaned.append(
                {
                    "factor": str(item.get("factor") or "").strip().lower(),
                    "challenge_id": str(item.get("challenge_id") or "").strip(),
                    "code": str(item.get("code") or "").strip(),
                }
            )
    return cleaned


def verify(
    conn: sqlite3.Connection,
    *,
    session_id: str,
    confirmation_id: str,
    required: list[str] | tuple[str, ...],
    proofs: Any,
    secret: str | None = None,
    now: int | None = None,
    audit_result: bool = True,
) -> dict[str, Any]:
    """校验一组证明；必需因子全部通过才算 ok。

    单张挑战失败一次就 attempts+1，达到 MAX_ATTEMPTS 后即使码对也拒绝。
    返回结构固定：{ok, verified, missing, invalid, reason}。
    """
    current = int(now if now is not None else clock.now_ms())
    need = [str(f).strip().lower() for f in required or ()]
    verified: list[str] = []
    missing: list[str] = []
    invalid: list[dict[str, str]] = []
    for proof in _normalize(proofs):
        factor = proof["factor"]
        if factor not in need or factor in verified:
            invalid.append({"factor": factor, "reason": "UNEXPECTED_FACTOR"})
            continue
        row = None
        if proof["challenge_id"]:
            row = conn.execute(
                "SELECT * FROM factor_challenges WHERE id = ?", (proof["challenge_id"],)
            ).fetchone()
        if row is None:
            invalid.append({"factor": factor, "reason": "CHALLENGE_NOT_FOUND"})
            continue
        if str(row["session_id"]) != str(session_id) or str(row["confirmation_id"]) != str(confirmation_id):
            invalid.append({"factor": factor, "reason": "CHALLENGE_NOT_BOUND"})
            continue
        if str(row["factor"]) != factor:
            invalid.append({"factor": factor, "reason": "FACTOR_MISMATCH"})
            continue
        if row["used_ms"] is not None:
            invalid.append({"factor": factor, "reason": "CHALLENGE_USED"})
            continue
        if int(row["expires_ms"]) <= current:
            invalid.append({"factor": factor, "reason": "CHALLENGE_EXPIRED"})
            continue
        if int(row["attempts"]) >= MAX_ATTEMPTS:
            invalid.append({"factor": factor, "reason": "TOO_MANY_ATTEMPTS"})
            continue
        expected = str(row["code_hash"])
        actual = _digest(_secret(secret), str(row["id"]), proof["code"])
        if not proof["code"] or not hmac.compare_digest(expected, actual):
            conn.execute(
                "UPDATE factor_challenges SET attempts = attempts + 1 WHERE id = ?", (row["id"],)
            )
            invalid.append({"factor": factor, "reason": "CODE_MISMATCH"})
            continue
        conn.execute(
            "UPDATE factor_challenges SET used_ms = ? WHERE id = ?", (current, row["id"])
        )
        verified.append(factor)
    missing = [factor for factor in need if factor not in verified]
    ok = bool(need) and not missing
    reason = "ok" if ok else ("missing" if not invalid else "invalid")
    if audit_result and session_id:
        audit.append(
            conn,
            "factor.verified" if ok else "factor.rejected",
            {
                "confirmation_id": confirmation_id,
                "required": need,
                "verified": verified,
                "missing": missing,
                "invalid": invalid,
            },
            session_id=session_id,
        )
    return {"ok": ok, "verified": verified, "missing": missing, "invalid": invalid, "reason": reason}


__all__ = [
    "CHALLENGE_TTL_MS",
    "ECHO_ENV",
    "FACTOR_KINDS",
    "MAX_ATTEMPTS",
    "echo_enabled",
    "issue",
    "verify",
]
