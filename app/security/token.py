"""能力令牌（capability token）：签发与七步校验。

对应文档：docs/05_安全层实现规格.md 5.3。

令牌形如 body.sig：body 是 base64url(json)，sig 是 HMAC-SHA256(secret, body) 的十六进制。
七步校验顺序不可换：结构 → 签名 → 过期 → nonce 防重放 → tool → params_hash → account/amount。
第 4 步会消耗 nonce，所以 5—7 步失败时这个令牌已经不能再用，用户必须重新确认一次——
这是刻意的：宁可让人多确认一次，也不让一枚被改过的令牌反复试探。

与文档的一处有据偏差：verify() 多一个 conn 参数，因为第 4 步必须往 token_nonces 写一行。
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from app import audit
from app.clock import BankError, MissingCapability, TokenExpired, TokenMismatch, ValidationError, now_ms
from app.ids import nonce as new_nonce
from app.log import log
from app.models import Action, RiskLevel

TOKEN_VERSION: Final[int] = 1
SECRET_ENV: Final[str] = "CAPABILITY_SECRET"
#: 密钥文件位置覆盖项；未设置时用仓库约定路径（已在 .gitignore 的 data/token_secret*）。
SECRET_FILE_ENV: Final[str] = "CAPABILITY_SECRET_FILE"
DEFAULT_SECRET_FILE: Final[str] = "data/token_secret"
#: 自动生成的密钥文件权限：只给属主读写。
SECRET_FILE_MODE: Final[int] = 0o600
SECRET_HEX_LEN: Final[int] = 64
DEFAULT_TTL_MS: Final[int] = 120_000
NONCE_HEX_LEN: Final[int] = 16
_HEX_CHARS: Final[frozenset[str]] = frozenset("0123456789abcdef")
PAYLOAD_KEYS: Final[frozenset[str]] = frozenset(
    {"v", "tool", "params_hash", "account_id", "amount_fen", "level", "nonce", "exp_ms"}
)


@dataclass(frozen=True)
class TokenPayload:
    """令牌载荷；用 dataclass 而不是裸 dict，跨模块只传这个类型。"""

    tool: str
    params_hash: str
    account_id: str
    amount_fen: int | None
    level: RiskLevel
    nonce: str
    exp_ms: int
    version: int = TOKEN_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "v": self.version,
            "tool": self.tool,
            "params_hash": self.params_hash,
            "account_id": self.account_id,
            "amount_fen": self.amount_fen,
            "level": self.level,
            "nonce": self.nonce,
            "exp_ms": self.exp_ms,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "TokenPayload":
        if not PAYLOAD_KEYS.issubset(raw.keys()):
            raise TokenMismatch(reason="PAYLOAD_FIELDS_MISSING", missing=sorted(PAYLOAD_KEYS - set(raw)))
        if not isinstance(raw["tool"], str) or not isinstance(raw["params_hash"], str):
            raise TokenMismatch(reason="PAYLOAD_FIELDS_INVALID")
        if not isinstance(raw["account_id"], str):
            raise TokenMismatch(reason="PAYLOAD_FIELDS_INVALID")
        amount_fen = raw["amount_fen"]
        if amount_fen is not None and (isinstance(amount_fen, bool) or not isinstance(amount_fen, int)):
            raise TokenMismatch(reason="PAYLOAD_FIELDS_INVALID")
        if not isinstance(raw["exp_ms"], int) or isinstance(raw["exp_ms"], bool):
            raise TokenMismatch(reason="PAYLOAD_FIELDS_INVALID")
        if raw["level"] not in ("green", "yellow", "red"):
            raise TokenMismatch(reason="PAYLOAD_FIELDS_INVALID")
        return cls(
            tool=raw["tool"],
            params_hash=raw["params_hash"],
            account_id=raw["account_id"],
            amount_fen=amount_fen,
            level=raw["level"],
            nonce=str(raw["nonce"]),
            exp_ms=raw["exp_ms"],
            version=int(raw["v"]),
        )


def params_hash(params: dict[str, Any]) -> str:
    """参数指纹：键排序、紧凑分隔符，中文不转义，保证签发与校验算出同一个值。"""
    material = json.dumps(params, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def load_secret(raw: str | None = None) -> str:
    """取签名密钥：显式传入 > 环境变量 > 本地密钥文件（首次运行自动生成）。

    取值优先级刻意写成三段，语义各不相同：
    1. `raw` 显式传入：调用方自己负责，格式不对立刻抛（测试与多实例注入用）。
    2. `CAPABILITY_SECRET` **已设置**：只认 64 位十六进制。设成空串也照样 fail closed——
       「显式关掉」必须能被表达出来，否则运维清空变量会被悄悄兜底成自动生成。
    3. 变量**未设置**：读 `CAPABILITY_SECRET_FILE`（默认 `data/token_secret`），
       没有就生成一把 0600 的随机密钥落盘。首次 `make run` 不需要先配密钥，
       再次启动复用同一把；删掉文件等于轮换（已签出的令牌随即失效，TTL 本就只有 120 秒）。

    绝不退回硬编码默认密钥：任何一条路都走不到「所有人共用一个常量」。
    """
    if raw is not None:
        return _require_hex(raw, where="explicit")
    env = os.environ.get(SECRET_ENV)
    if env is not None:
        return _require_hex(env, where="env")
    return _secret_from_file()


def _require_hex(secret: str, *, where: str) -> str:
    """校验 64 位十六进制；不合格一律 fail closed，错误里带上来源便于排查。"""
    text = secret.strip()
    if len(text) != SECRET_HEX_LEN or any(char not in _HEX_CHARS for char in text.lower()):
        raise MissingCapability(reason="CAPABILITY_SECRET_MISSING", env=SECRET_ENV, source=where)
    return text.lower()


def secret_file_path() -> Path:
    """密钥文件位置；`CAPABILITY_SECRET_FILE` 可覆盖（相对路径按进程工作目录解析）。"""
    override = os.environ.get(SECRET_FILE_ENV, "").strip()
    return Path(override) if override else Path(DEFAULT_SECRET_FILE)


def _secret_from_file() -> str:
    """读本地密钥文件；不存在或内容不合法就重新生成一把并落盘（0600）。"""
    path = secret_file_path()
    try:
        existing = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        existing = ""
    except OSError as exc:  # 权限不足等：不静默兜底，让运维看见
        raise MissingCapability(
            reason="CAPABILITY_SECRET_FILE_UNREADABLE", env=SECRET_ENV, path=str(path)
        ) from exc
    if existing:
        try:
            return _require_hex(existing, where="file")
        except MissingCapability:
            log.warn("capability.secret.file_invalid", path=str(path), action="regenerate")
    return _write_new_secret(path)


def _write_new_secret(path: Path) -> str:
    """生成并原子写入密钥文件；先写临时文件再 replace，避免并发启动读到半截内容。"""
    secret = secrets.token_hex(SECRET_HEX_LEN // 2)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(secret + "\n", encoding="utf-8")
        os.chmod(tmp, SECRET_FILE_MODE)
        os.replace(tmp, path)
    except OSError as exc:
        raise MissingCapability(
            reason="CAPABILITY_SECRET_FILE_UNWRITABLE", env=SECRET_ENV, path=str(path)
        ) from exc
    log.warn("capability.secret.generated", path=str(path), mode=oct(SECRET_FILE_MODE))
    return secret


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64decode(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


def sign(body: str, secret: str) -> str:
    """HMAC-SHA256 十六进制签名。"""
    return hmac.new(bytes.fromhex(secret), body.encode("utf-8"), hashlib.sha256).hexdigest()


def issue(
    action: Action,
    *,
    level: RiskLevel = "yellow",
    ttl_ms: int = DEFAULT_TTL_MS,
    secret: str | None = None,
) -> str:
    """给一条已裁决的 Action 签发能力令牌，默认 120 秒有效。"""
    if ttl_ms <= 0:
        raise ValidationError("令牌有效期必须为正", ttl_ms=ttl_ms)
    key = load_secret(secret)
    payload = TokenPayload(
        tool=action.tool,
        params_hash=params_hash(action.params),
        account_id=action.account_id,
        amount_fen=action.amount_fen,
        level=level,
        nonce=new_nonce(NONCE_HEX_LEN),
        exp_ms=now_ms() + ttl_ms,
    )
    body = _b64encode(json.dumps(payload.to_dict(), sort_keys=True, separators=(",", ":")).encode("utf-8"))
    return f"{body}.{sign(body, key)}"


def issue_audited(
    conn: sqlite3.Connection,
    action: Action,
    *,
    session_id: str | None = None,
    task_id: str | None = None,
    level: RiskLevel = "yellow",
    ttl_ms: int = DEFAULT_TTL_MS,
    secret: str | None = None,
) -> str:
    """签发并写 token.issued 审计（docs/04 要求的签发时机）。"""
    token = issue(action, level=level, ttl_ms=ttl_ms, secret=secret)
    payload = decode(token, secret=secret)
    audit.append(
        conn,
        "token.issued",
        {"nonce": payload.nonce, "ttl": ttl_ms, "params_hash": payload.params_hash},
        session_id=session_id,
        task_id=task_id,
    )
    return token


def decode(token: str, *, secret: str | None = None) -> TokenPayload:
    """第 1、2 步：结构与签名。只做这两步，供「先看一眼令牌写了什么」的场景用。"""
    body, sig = _split(token)
    if not hmac.compare_digest(sign(body, load_secret(secret)), sig):
        raise TokenMismatch(reason="SIGNATURE_MISMATCH")
    try:
        raw = json.loads(_b64decode(body).decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise TokenMismatch(reason="PAYLOAD_NOT_JSON") from exc
    if not isinstance(raw, dict):
        raise TokenMismatch(reason="PAYLOAD_NOT_OBJECT")
    return TokenPayload.from_dict(raw)


def _split(token: str) -> tuple[str, str]:
    """第 1 步：两段结构 + body 必须是合法 base64url。"""
    parts = token.split(".") if isinstance(token, str) else []
    if len(parts) != 2 or not parts[0] or not parts[1]:
        raise TokenMismatch(reason="TOKEN_MALFORMED")
    try:
        _b64decode(parts[0])
    except (binascii.Error, ValueError) as exc:
        raise TokenMismatch(reason="TOKEN_MALFORMED") from exc
    return parts[0], parts[1]


def verify(
    conn: sqlite3.Connection,
    token: str,
    action: Action,
    *,
    secret: str | None = None,
) -> TokenPayload:
    """七步校验全跑一遍；任何一步不过都抛 TokenMismatch / TokenExpired 并写 token.rejected 审计。

    conn 必须在事务之外调用：第 4 步消耗 nonce 的写入要立刻落库，
    否则外层一旦回滚，令牌就能被无限重放。
    """
    payload = _verify_or_reject(conn, token, action, secret=secret, session_id=None)
    return payload


def _verify_or_reject(
    conn: sqlite3.Connection,
    token: str,
    action: Action,
    *,
    secret: str | None,
    session_id: str | None,
) -> TokenPayload:
    try:
        payload = decode(token, secret=secret)
        _check_expiry(payload)
        _consume_nonce(conn, payload)
        _check_tool(payload, action)
        _check_params(payload, action)
        _check_subject(payload, action)
    except TokenExpired:
        record_rejection(conn, "TOKEN_EXPIRED", session_id=session_id)
        raise
    except TokenMismatch as exc:
        record_rejection(conn, str(exc.fields.get("reason", "TOKEN_MISMATCH")), session_id=session_id)
        raise
    log.info("token.verified", tool=action.tool, account_id=action.account_id)
    return payload


def _check_expiry(payload: TokenPayload) -> None:
    """第 3 步：过期。"""
    if payload.exp_ms <= now_ms():
        raise TokenExpired(reason="TOKEN_EXPIRED", exp_ms=payload.exp_ms)


def _consume_nonce(conn: sqlite3.Connection, payload: TokenPayload) -> None:
    """第 4 步：nonce 一次性。主键冲突即重放，直接拒绝。"""
    stamp_ms = now_ms()
    conn.execute("DELETE FROM token_nonces WHERE expires_ms < ?", (stamp_ms,))
    try:
        conn.execute(
            "INSERT INTO token_nonces (nonce, used_ms, expires_ms) VALUES (?, ?, ?)",
            (payload.nonce, stamp_ms, payload.exp_ms),
        )
    except sqlite3.IntegrityError as exc:
        raise TokenMismatch(reason="NONCE_REPLAY", nonce=payload.nonce) from exc


def _check_tool(payload: TokenPayload, action: Action) -> None:
    """第 5 步：令牌只授权它签发时那一个工具。"""
    if payload.tool != action.tool:
        raise TokenMismatch(reason="TOOL_MISMATCH", token_tool=payload.tool, action_tool=action.tool)


def _check_params(payload: TokenPayload, action: Action) -> None:
    """第 6 步：参数指纹，换收款人就算换了一笔。"""
    if payload.params_hash != params_hash(action.params):
        raise TokenMismatch(reason="PARAMS_MISMATCH")


def _check_subject(payload: TokenPayload, action: Action) -> None:
    """第 7 步：账户与金额必须一模一样，差一分都不行。"""
    if payload.account_id != action.account_id:
        raise TokenMismatch(reason="ACCOUNT_MISMATCH")
    if payload.amount_fen != action.amount_fen:
        raise TokenMismatch(
            reason="AMOUNT_MISMATCH",
            token_amount_fen=payload.amount_fen,
            action_amount_fen=action.amount_fen,
        )


def record_rejection(conn: sqlite3.Connection, reason: str, *, session_id: str | None = None) -> None:
    """写 token.rejected 审计；审计失败必须炸出来，不许悄悄吞掉。"""
    audit.append(conn, "token.rejected", {"reason": reason}, session_id=session_id)


def require_token(token: str | None, conn: sqlite3.Connection, *, session_id: str | None = None) -> str:
    """没有令牌就是没有授权：记一笔审计再抛 MissingCapability（docs/05 5.4 步骤 1）。"""
    if token:
        return token
    record_rejection(conn, "TOKEN_MISSING", session_id=session_id)
    raise MissingCapability(reason="TOKEN_MISSING")


__all__ = [
    "DEFAULT_SECRET_FILE",
    "DEFAULT_TTL_MS",
    "SECRET_ENV",
    "SECRET_FILE_ENV",
    "SECRET_FILE_MODE",
    "SECRET_HEX_LEN",
    "TOKEN_VERSION",
    "TokenPayload",
    "decode",
    "issue",
    "issue_audited",
    "load_secret",
    "params_hash",
    "record_rejection",
    "require_token",
    "secret_file_path",
    "sign",
    "verify",
]
