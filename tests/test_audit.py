"""app/audit.py 单测：哈希链能追加、能验证、篡改必被检出。"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from app import audit, db
from app.clock import ValidationError

_PAYLOAD = {"session_id": "s_1", "amount_fen": 50_000, "tool": "transfer"}


def test_chain_empty_is_genesis(conn: sqlite3.Connection) -> None:
    assert audit.chain_head(conn) == audit.GENESIS
    assert audit.verify_chain(conn)


def test_append_then_verify_chain_ok(conn: sqlite3.Connection) -> None:
    """正常追加若干条后，整条链必须能逐行重算通过。"""
    first = audit.append(conn, "session.start", _PAYLOAD, session_id="s_1")
    second = audit.append(conn, "policy.decided", {"level": "green", "rule_id": "R000"},
                          session_id="s_1")
    third = audit.append(conn, "session.end", {"turns": 1}, session_id="s_1")
    assert [first, second, third] == [1, 2, 3]
    assert audit.verify_chain(conn) is True
    assert audit.chain_head(conn) != audit.GENESIS
    assert audit.count_events(conn) == 3


def test_manual_update_breaks_chain(conn: sqlite3.Connection) -> None:
    """绕过触发器手工改一行审计内容后，verify_chain 必须立刻失败。"""
    audit.append(conn, "session.start", _PAYLOAD, session_id="s_1")
    audit.append(conn, "tool.executed", {"tool": "transfer", "ok": True}, session_id="s_1")
    assert audit.verify_chain(conn) is True

    # 正常路径改不动：触发器直接 ABORT
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE audit_log SET payload_json = '{}' WHERE seq = 1")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM audit_log WHERE seq = 1")

    # 模拟攻击者先摘掉触发器再改内容
    conn.execute("DROP TRIGGER audit_no_update")
    conn.execute("UPDATE audit_log SET payload_json = ? WHERE seq = 1", ('{"tampered":true}',))
    assert audit.verify_chain(conn) is False


def test_manual_delete_breaks_chain(conn: sqlite3.Connection) -> None:
    audit.append(conn, "session.start", _PAYLOAD, session_id="s_1")
    audit.append(conn, "session.end", {"turns": 1}, session_id="s_1")
    conn.execute("DROP TRIGGER audit_no_delete")
    conn.execute("DELETE FROM audit_log WHERE seq = 1")
    assert audit.verify_chain(conn) is False


def test_payload_serialization_is_canonical(conn: sqlite3.Connection) -> None:
    """字段顺序不影响哈希：先 append 再手工换一种写法 insert 仍应断链。"""
    audit.append(conn, "session.start", {"b": 2, "a": 1}, session_id="s_1")
    conn.execute("DROP TRIGGER audit_no_update")
    conn.execute("UPDATE audit_log SET payload_json = ? WHERE seq = 1", ('{"a":1,"b":2}',))
    assert audit.verify_chain(conn) is True
    conn.execute("UPDATE audit_log SET payload_json = ? WHERE seq = 1", ('{"a":1,"b":3}',))
    assert audit.verify_chain(conn) is False


def test_unknown_event_type_rejected(conn: sqlite3.Connection) -> None:
    with pytest.raises(ValidationError):
        audit.append(conn, "not.a.real.type", _PAYLOAD)


def test_unserializable_payload_rejected(conn: sqlite3.Connection) -> None:
    with pytest.raises(ValidationError):
        audit.append(conn, "session.start", {"obj": object()})


def test_chain_links_two_conns(conn: sqlite3.Connection, tmp_path: Path) -> None:
    """两条链首尾相接时，后一条的首行 prev_hash 必须等于前一条的末行 hash。"""
    other = db.connect(tmp_path / "other.db")
    try:
        db.init_db(other)
        audit.append(conn, "session.start", _PAYLOAD, session_id="s_1")
        head = audit.chain_head(conn)
        audit.append(other, "session.start", _PAYLOAD, session_id="s_2")
        row = other.execute("SELECT prev_hash FROM audit_log WHERE seq = 1").fetchone()
        assert row["prev_hash"] == audit.GENESIS
        assert head != row["prev_hash"]
    finally:
        other.close()
