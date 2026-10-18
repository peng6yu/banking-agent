"""SQLite 连接管理、事务、建表与过期 hold 清扫。

对应文档：docs/03_数据库与种子数据.md 3.1/3.2、docs/04_核心模块实现规格.md 4.6。

底层约定：金额 INTEGER 分、时间 INTEGER 毫秒、主键 TEXT、布尔 INTEGER 0/1、
外键一律声明且连接时 PRAGMA foreign_keys=ON，审计表只增不改由触发器兜底。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Final

from app import audit
from app.clock import now_ms

ROOT: Final[Path] = Path(__file__).resolve().parent.parent
DEFAULT_DB_PATH: Final[Path] = ROOT / "data" / "bank.db"

PRAGMAS: Final[tuple[str, ...]] = (
    "PRAGMA journal_mode=WAL;",
    "PRAGMA foreign_keys=ON;",
    "PRAGMA busy_timeout=5000;",
)

# 15 张表，顺序即依赖顺序（被引用的先建）
TABLE_NAMES: Final[tuple[str, ...]] = (
    "users",
    "accounts",
    "cards",
    "payees",
    "holds",
    "transactions",
    "audit_log",
    "sessions",
    "dag_runs",
    "dag_nodes",
    "confirmations",
    "token_nonces",
    "subs",
    "daily_limits",
    "denials",
)

SCHEMA_SQL: Final[str] = """
CREATE TABLE IF NOT EXISTS users (
  id          TEXT PRIMARY KEY,
  name        TEXT NOT NULL,
  phone       TEXT NOT NULL UNIQUE,
  created_ms  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS accounts (
  id               TEXT PRIMARY KEY,
  user_id          TEXT NOT NULL REFERENCES users(id),
  name             TEXT NOT NULL,
  tail             TEXT NOT NULL,
  type             TEXT NOT NULL CHECK (type IN ('debit','credit')),
  balance_fen      INTEGER NOT NULL CHECK (balance_fen >= 0),
  credit_limit_fen INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_accounts_user ON accounts(user_id);

CREATE TABLE IF NOT EXISTS cards (
  id               TEXT PRIMARY KEY,
  account_id       TEXT NOT NULL REFERENCES accounts(id),
  tail             TEXT NOT NULL,
  kind             TEXT NOT NULL CHECK (kind IN ('physical','virtual')),
  status           TEXT NOT NULL CHECK (status IN ('active','frozen','lost')),
  per_tx_limit_fen INTEGER NOT NULL DEFAULT 500000
);
CREATE INDEX IF NOT EXISTS idx_cards_account ON cards(account_id);

CREATE TABLE IF NOT EXISTS payees (
  id        TEXT PRIMARY KEY,
  user_id   TEXT NOT NULL REFERENCES users(id),
  name      TEXT NOT NULL,
  bank      TEXT NOT NULL,
  card_tail TEXT NOT NULL,
  alias     TEXT
);
CREATE INDEX IF NOT EXISTS idx_payees_user ON payees(user_id);
CREATE INDEX IF NOT EXISTS idx_payees_name ON payees(name);

CREATE TABLE IF NOT EXISTS holds (
  id              TEXT PRIMARY KEY,
  account_id      TEXT NOT NULL REFERENCES accounts(id),
  payee_id        TEXT NOT NULL REFERENCES payees(id),
  amount_fen      INTEGER NOT NULL CHECK (amount_fen > 0),
  note            TEXT NOT NULL DEFAULT '',
  status          TEXT NOT NULL CHECK (status IN ('held','committed','released','expired')),
  idempotency_key TEXT NOT NULL UNIQUE,
  session_id      TEXT NOT NULL,
  created_ms      INTEGER NOT NULL,
  expires_ms      INTEGER NOT NULL,
  settled_ms      INTEGER
);
CREATE INDEX IF NOT EXISTS idx_holds_sweep ON holds(status, expires_ms);

CREATE TABLE IF NOT EXISTS transactions (
  id                TEXT PRIMARY KEY,
  account_id        TEXT NOT NULL REFERENCES accounts(id),
  direction         TEXT NOT NULL CHECK (direction IN ('in','out')),
  amount_fen        INTEGER NOT NULL CHECK (amount_fen > 0),
  counterparty      TEXT NOT NULL,
  category          TEXT NOT NULL,
  note              TEXT NOT NULL DEFAULT '',
  ts_ms             INTEGER NOT NULL,
  balance_after_fen INTEGER NOT NULL,
  hold_id           TEXT REFERENCES holds(id),
  channel           TEXT NOT NULL DEFAULT 'app'
);
CREATE INDEX IF NOT EXISTS idx_tx_account_ts ON transactions(account_id, ts_ms DESC);
CREATE INDEX IF NOT EXISTS idx_tx_category ON transactions(category);
CREATE INDEX IF NOT EXISTS idx_tx_counterparty ON transactions(counterparty);

CREATE TABLE IF NOT EXISTS audit_log (
  seq          INTEGER PRIMARY KEY AUTOINCREMENT,
  ts_ms        INTEGER NOT NULL,
  session_id   TEXT,
  task_id      TEXT,
  event_type   TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  prev_hash    TEXT NOT NULL,
  hash         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_audit_session ON audit_log(session_id, seq);

CREATE TABLE IF NOT EXISTS sessions (
  id         TEXT PRIMARY KEY,
  created_ms INTEGER NOT NULL,
  last_ms    INTEGER NOT NULL,
  title      TEXT NOT NULL DEFAULT '',
  status     TEXT NOT NULL CHECK (status IN ('active','done','cancelled'))
);

CREATE TABLE IF NOT EXISTS dag_runs (
  id         TEXT PRIMARY KEY,
  session_id TEXT NOT NULL REFERENCES sessions(id),
  dag_json   TEXT NOT NULL,
  status     TEXT NOT NULL CHECK (status IN ('running','waiting','done','failed','cancelled')),
  created_ms INTEGER NOT NULL,
  updated_ms INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_dag_runs_session ON dag_runs(session_id);

CREATE TABLE IF NOT EXISTS dag_nodes (
  id            TEXT PRIMARY KEY,
  run_id        TEXT NOT NULL REFERENCES dag_runs(id),
  node_id       TEXT NOT NULL,
  tool          TEXT NOT NULL,
  params_json   TEXT NOT NULL,
  state         TEXT NOT NULL CHECK (state IN
                 ('pending','running','waiting_confirm','waiting_factor','done','failed','skipped')),
  decision_json TEXT,
  result_json   TEXT,
  ts_ms         INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_dag_nodes_run ON dag_nodes(run_id);
CREATE INDEX IF NOT EXISTS idx_dag_nodes_state ON dag_nodes(state);

CREATE TABLE IF NOT EXISTS confirmations (
  id          TEXT PRIMARY KEY,
  session_id  TEXT NOT NULL,
  run_id      TEXT NOT NULL,
  node_id     TEXT NOT NULL,
  action_json TEXT NOT NULL,
  status      TEXT NOT NULL CHECK (status IN ('pending','approved','rejected','expired')),
  created_ms  INTEGER NOT NULL,
  expires_ms  INTEGER NOT NULL,
  resolved_ms INTEGER
);
CREATE INDEX IF NOT EXISTS idx_confirmations_session ON confirmations(session_id, status);

CREATE TABLE IF NOT EXISTS token_nonces (
  nonce      TEXT PRIMARY KEY,
  used_ms    INTEGER NOT NULL,
  expires_ms INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_nonces_expires ON token_nonces(expires_ms);

CREATE TABLE IF NOT EXISTS subs (
  id             TEXT PRIMARY KEY,
  account_id     TEXT NOT NULL REFERENCES accounts(id),
  merchant       TEXT NOT NULL,
  amount_fen     INTEGER NOT NULL CHECK (amount_fen > 0),
  period         TEXT NOT NULL CHECK (period IN ('monthly','yearly')),
  next_charge_ms INTEGER NOT NULL,
  status         TEXT NOT NULL CHECK (status IN ('active','cancelled'))
);
CREATE INDEX IF NOT EXISTS idx_subs_account ON subs(account_id);

CREATE TABLE IF NOT EXISTS daily_limits (
  account_id TEXT NOT NULL REFERENCES accounts(id),
  day        TEXT NOT NULL,
  spent_fen  INTEGER NOT NULL DEFAULT 0,
  tx_count   INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (account_id, day)
);

CREATE TABLE IF NOT EXISTS denials (
  id          TEXT PRIMARY KEY,
  session_id  TEXT NOT NULL,
  reason      TEXT NOT NULL,
  action_json TEXT NOT NULL,
  ts_ms       INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_denials_session ON denials(session_id, ts_ms);

-- 审计只增不改：UPDATE/DELETE 一律由 SQLite 层拦掉（docs/03 3.2 第 7 张表）
CREATE TRIGGER IF NOT EXISTS audit_no_update BEFORE UPDATE ON audit_log
BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;

CREATE TRIGGER IF NOT EXISTS audit_no_delete BEFORE DELETE ON audit_log
BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;
"""


def connect(path: str | Path | None = None) -> sqlite3.Connection:
    """打开连接并设置 PRAGMA；isolation_level=None 让事务完全由 tx() 掌控。"""
    db_path = Path(path) if path is not None else DEFAULT_DB_PATH
    if db_path.parent and str(db_path) != ":memory:":
        db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    for pragma_sql in PRAGMAS:
        conn.execute(pragma_sql)
    return conn


@contextmanager
def tx(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """事务：顶层 BEGIN IMMEDIATE，嵌套用 SAVEPOINT，异常一律回滚后原样抛出。"""
    if conn.in_transaction:
        savepoint = f"sp_{now_ms()}"
        conn.execute(f"SAVEPOINT {savepoint}")
        try:
            yield conn
        except BaseException:
            conn.execute(f"ROLLBACK TO {savepoint}")
            conn.execute(f"RELEASE {savepoint}")
            raise
        conn.execute(f"RELEASE {savepoint}")
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def init_db(conn: sqlite3.Connection) -> None:
    """执行 DDL 建全部表与触发器，幂等（IF NOT EXISTS）。"""
    conn.executescript(SCHEMA_SQL)


def sweep_expired_holds(conn: sqlite3.Connection) -> int:
    """把超时未落账的 hold 置为 expired 并写 transfer.released 审计，返回处理条数。"""
    now = now_ms()
    rows = conn.execute(
        "SELECT id, account_id, amount_fen FROM holds WHERE status = 'held' AND expires_ms < ?",
        (now,),
    ).fetchall()
    if not rows:
        return 0
    with tx(conn):
        conn.execute(
            "UPDATE holds SET status = 'expired', settled_ms = ? WHERE status = 'held' AND expires_ms < ?",
            (now, now),
        )
        for row in rows:
            audit.append(
                conn,
                "transfer.released",
                {"hold_id": row["id"], "reason": "HOLD_TIMEOUT", "amount_fen": row["amount_fen"]},
            )
    return len(rows)


def table_names(conn: sqlite3.Connection) -> tuple[str, ...]:
    """库里实际存在的业务表名（按建表顺序）。"""
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    found = {row["name"] for row in rows}
    return tuple(name for name in TABLE_NAMES if name in found)


__all__ = [
    "DEFAULT_DB_PATH",
    "PRAGMAS",
    "TABLE_NAMES",
    "SCHEMA_SQL",
    "connect",
    "tx",
    "init_db",
    "sweep_expired_holds",
    "table_names",
]
