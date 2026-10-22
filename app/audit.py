"""审计哈希链：只追加、不可改、可验证。

对应文档：docs/03_数据库与种子数据.md 3.2 第 7 张表、docs/04_核心模块实现规格.md 4.7。

哈希材料必须逐字节一致（否则演示时校验会挂）：
    material = prev_hash + '|' + str(ts_ms) + '|' + event_type + '|' + payload_json
    hash     = sha256(material.encode('utf-8')).hexdigest()
payload_json 固定 json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(',',':'))。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any, Final

from app.clock import ValidationError, now_ms
from app.log import log

GENESIS: Final[str] = "GENESIS"

# docs/03 3.2：event_type 是闭集，写错名字说明调用方有 bug，当场拦
EVENT_TYPES: Final[frozenset[str]] = frozenset(
    {
        "session.start",
        "intent.parsed",
        "dag.built",
        "policy.decided",
        "confirm.requested",
        "confirm.approved",
        "confirm.rejected",
        "token.issued",
        "token.rejected",
        "transfer.initiated",
        "transfer.committed",
        "transfer.released",
        "transfer.reversed",
        "scheduled.created",
        "scheduled.cancelled",
        "scheduled.executed",
        "tool.executed",
        "injection.detected",
        "breaker.open",
        "session.end",
    }
)


def _material(prev_hash: str, ts_ms: int, event_type: str, payload_json: str) -> str:
    """拼哈希材料，字段顺序固定不要动。"""
    return prev_hash + "|" + str(ts_ms) + "|" + event_type + "|" + payload_json


def _digest(material: str) -> str:
    """材料的 sha256 十六进制摘要。"""
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def payload_to_json(payload: dict[str, Any]) -> str:
    """审计内容的唯一序列化方式；不可序列化就报错，绝不用 default=str 蒙混。"""
    try:
        return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise ValidationError("审计内容无法序列化", error=str(exc)) from exc


def append(
    conn: sqlite3.Connection,
    event_type: str,
    payload: dict[str, Any],
    *,
    session_id: str | None = None,
    task_id: str | None = None,
) -> int:
    """追加一条审计记录，返回自增 seq。"""
    if event_type not in EVENT_TYPES:
        raise ValidationError("未知的审计事件类型", event_type=event_type)
    payload_json = payload_to_json(payload)
    prev_hash = chain_head(conn)
    ts_ms = now_ms()
    hash_value = _digest(_material(prev_hash, ts_ms, event_type, payload_json))
    cursor = conn.execute(
        "INSERT INTO audit_log (ts_ms, session_id, task_id, event_type, payload_json, prev_hash, hash)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (ts_ms, session_id, task_id, event_type, payload_json, prev_hash, hash_value),
    )
    return int(cursor.lastrowid)


def verify_chain(conn: sqlite3.Connection) -> bool:
    """逐行重算整条链；任一处对不上就 log 出断点 seq 并返回 False。"""
    prev_hash = GENESIS
    rows = conn.execute(
        "SELECT seq, ts_ms, event_type, payload_json, prev_hash, hash FROM audit_log ORDER BY seq"
    ).fetchall()
    for row in rows:
        if row["prev_hash"] != prev_hash:
            log.error(
                "audit.chain_break",
                seq=row["seq"],
                reason="PREV_HASH_MISMATCH",
                expect=prev_hash,
                actual=row["prev_hash"],
            )
            return False
        expect = _digest(_material(prev_hash, row["ts_ms"], row["event_type"], row["payload_json"]))
        if expect != row["hash"]:
            log.error("audit.chain_break", seq=row["seq"], reason="HASH_MISMATCH", expect=expect)
            return False
        prev_hash = row["hash"]
    return True


def chain_head(conn: sqlite3.Connection) -> str:
    """最新一条的 hash，空表返回 GENESIS。"""
    row = conn.execute("SELECT hash FROM audit_log ORDER BY seq DESC LIMIT 1").fetchone()
    if row is None:
        return GENESIS
    return str(row["hash"])


def count_events(conn: sqlite3.Connection) -> int:
    """审计条数，给演示与自检用。"""
    row = conn.execute("SELECT COUNT(*) AS n FROM audit_log").fetchone()
    return int(row["n"])


__all__ = [
    "GENESIS",
    "EVENT_TYPES",
    "append",
    "verify_chain",
    "chain_head",
    "payload_to_json",
    "count_events",
]
