"""app/db.py 单测：15 张表、PRAGMA、审计触发器、事务与过期 hold 清扫。"""

from __future__ import annotations

import sqlite3

import pytest

from app import audit, db
from app.clock import now_ms

EXPECTED_TABLES = frozenset(
    {
        "users", "accounts", "cards", "payees", "transactions", "holds", "audit_log",
        "sessions", "dag_runs", "dag_nodes", "confirmations", "token_nonces",
        "subs", "daily_limits", "denials",
    }
)


def test_table_names_15(conn: sqlite3.Connection) -> None:
    assert len(db.table_names(conn)) == 15
    assert set(db.table_names(conn)) == EXPECTED_TABLES
    assert set(db.TABLE_NAMES) == EXPECTED_TABLES


def test_init_db_is_idempotent(conn: sqlite3.Connection) -> None:
    db.init_db(conn)
    db.init_db(conn)
    assert len(db.table_names(conn)) == 15


def test_pragmas(conn: sqlite3.Connection) -> None:
    assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000


def test_audit_triggers_exist(conn: sqlite3.Connection) -> None:
    names = {row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'trigger'"
    ).fetchall()}
    assert {"audit_no_update", "audit_no_delete"} <= names


def test_foreign_keys_enforced(conn: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO accounts (id, user_id, name, tail, type, balance_fen, credit_limit_fen)"
            " VALUES ('a_1', 'u_not_exist', '卡', '0000', 'debit', 0, 0)"
        )


def test_tx_rolls_back(conn: sqlite3.Connection) -> None:
    with pytest.raises(ValueError):
        with db.tx(conn):
            conn.execute("INSERT INTO users (id, name, phone, created_ms)"
                         " VALUES ('u_1', '张伟', '13800001234', ?)", (now_ms(),))
            raise ValueError("boom")
    assert conn.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"] == 0


def test_tx_nested_savepoint_keeps_outer(conn: sqlite3.Connection) -> None:
    with db.tx(conn):
        conn.execute("INSERT INTO users (id, name, phone, created_ms)"
                     " VALUES ('u_1', '张伟', '13800001234', ?)", (now_ms(),))
        with pytest.raises(ValueError):
            with db.tx(conn):
                conn.execute("INSERT INTO users (id, name, phone, created_ms)"
                             " VALUES ('u_2', '李四', '13900001234', ?)", (now_ms(),))
                raise ValueError("boom")
        conn.execute("INSERT INTO users (id, name, phone, created_ms)"
                     " VALUES ('u_3', '王五', '13700001234', ?)", (now_ms(),))
    phones = {row["name"] for row in conn.execute("SELECT name FROM users").fetchall()}
    assert phones == {"张伟", "王五"}


def _insert_hold(conn: sqlite3.Connection, hold_id: str, expires_ms: int) -> None:
    now = now_ms()
    conn.execute("INSERT OR IGNORE INTO users (id, name, phone, created_ms)"
                 " VALUES ('u_1','张伟','138',?)", (now,))
    conn.execute("INSERT OR IGNORE INTO accounts (id, user_id, name, tail, type, balance_fen,"
                 " credit_limit_fen) VALUES ('a_1','u_1','卡','1234','debit',100000,0)", ())
    conn.execute("INSERT OR IGNORE INTO payees (id, user_id, name, bank, card_tail, alias)"
                 " VALUES ('p_1','u_1','张三','招行','4417',NULL)", ())
    conn.execute(
        "INSERT INTO holds (id, account_id, payee_id, amount_fen, note, status, idempotency_key,"
        " session_id, created_ms, expires_ms, settled_ms)"
        " VALUES (?,'a_1','p_1',1000,'','held',?,'s_1',?,?,NULL)",
        (hold_id, f"idem-{hold_id}", now, expires_ms),
    )


def test_sweep_expired_holds(conn: sqlite3.Connection) -> None:
    _insert_hold(conn, "h_old", now_ms() - 1_000)
    _insert_hold(conn, "h_live", now_ms() + 600_000)
    assert db.sweep_expired_holds(conn) == 1
    statuses = {row["id"]: row["status"]
                for row in conn.execute("SELECT id, status FROM holds").fetchall()}
    assert statuses == {"h_old": "expired", "h_live": "held"}
    released = conn.execute("SELECT COUNT(*) AS n FROM audit_log"
                            " WHERE event_type = 'transfer.released'").fetchone()["n"]
    assert released == 1
    assert audit.verify_chain(conn)
