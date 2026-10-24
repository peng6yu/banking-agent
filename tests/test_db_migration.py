"""旧库迁移：init_db 自己就能补列，不要求调用方先执行 ensure_columns。

历史实现先 executescript(SCHEMA_SQL) 再补列，而 SCHEMA_SQL 里
idx_plan_budgets_account(account_id, status) 引用后加的列，老库会直接
"no such column: account_id" 启动失败。这组测试锁定修复后的顺序。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from app import db


def _legacy_plan_budgets(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE plan_budgets ("
        " id TEXT PRIMARY KEY, plan_id TEXT NOT NULL, item_key TEXT NOT NULL,"
        " label TEXT NOT NULL, merchant TEXT NOT NULL,"
        " amount_fen INTEGER NOT NULL CHECK (amount_fen > 0),"
        " extra_json TEXT NOT NULL DEFAULT '{}', status TEXT NOT NULL,"
        " reason TEXT NOT NULL DEFAULT '', created_ms INTEGER NOT NULL,"
        " updated_ms INTEGER NOT NULL, UNIQUE(plan_id, item_key))"
    )
    conn.execute(
        "INSERT INTO plan_budgets (id, plan_id, item_key, label, merchant,"
        " amount_fen, status, created_ms, updated_ms)"
        " VALUES ('pb_old', 'pl_old', 'flowers', '鲜花', 'flower_shop',"
        " 40000, 'reserved', 1, 1)"
    )


def _legacy_accounts(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE accounts ("
        " id TEXT PRIMARY KEY, user_id TEXT NOT NULL, name TEXT NOT NULL,"
        " tail TEXT NOT NULL, type TEXT NOT NULL,"
        " balance_fen INTEGER NOT NULL DEFAULT 0, credit_limit_fen INTEGER NOT NULL DEFAULT 0)"
    )
    conn.execute(
        "INSERT INTO accounts (id, user_id, name, tail, type, balance_fen)"
        " VALUES ('acc_old', 'u_old', '旧账户', '0001', 'debit', 123456)"
    )


def test_init_db_migrates_legacy_plan_budgets_without_pre_adding_columns(
    tmp_path: Path,
) -> None:
    """老库直接 init_db（不先 ensure_columns）必须成功并补出 account_id/hold_id。"""
    path = tmp_path / "legacy_plan_budgets.db"
    conn = db.connect(str(path))
    try:
        _legacy_plan_budgets(conn)
        conn.commit()

        db.init_db(conn)  # 关键：调用方没有先补列

        columns = {row["name"] for row in conn.execute("PRAGMA table_info(plan_budgets)")}
        assert {"account_id", "hold_id"} <= columns
        indexes = {row["name"] for row in conn.execute("PRAGMA index_list(plan_budgets)")}
        assert "idx_plan_budgets_account" in indexes
        row = conn.execute("SELECT * FROM plan_budgets WHERE id = 'pb_old'").fetchone()
        assert row is not None and int(row["amount_fen"]) == 40_000
        assert row["account_id"] is None
    finally:
        conn.close()


def test_init_db_is_idempotent_on_an_already_migrated_database(tmp_path: Path) -> None:
    path = tmp_path / "legacy_twice.db"
    conn = db.connect(str(path))
    try:
        _legacy_plan_budgets(conn)
        conn.commit()
        db.init_db(conn)
        db.init_db(conn)  # 第二次不应报错，也不应重复 ALTER
        columns = [row["name"] for row in conn.execute("PRAGMA table_info(plan_budgets)")]
        assert columns.count("account_id") == 1
    finally:
        conn.close()


def test_init_db_keeps_legacy_rows_and_adds_account_columns(tmp_path: Path) -> None:
    """账户表也走同一条迁移路径：老行不丢，status 补成 active。"""
    path = tmp_path / "legacy_accounts.db"
    conn = db.connect(str(path))
    try:
        _legacy_accounts(conn)
        conn.commit()

        db.init_db(conn)

        columns = {row["name"] for row in conn.execute("PRAGMA table_info(accounts)")}
        assert "status" in columns
        row = conn.execute("SELECT * FROM accounts WHERE id = 'acc_old'").fetchone()
        assert row is not None and int(row["balance_fen"]) == 123_456
        assert row["status"] == "active"
        assert "handoffs" in db.table_names(conn)
    finally:
        conn.close()
