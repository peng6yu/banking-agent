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
from app.clock import SystemBusy, now_ms

ROOT: Final[Path] = Path(__file__).resolve().parent.parent
DEFAULT_DB_PATH: Final[Path] = ROOT / "data" / "bank.db"

PRAGMAS: Final[tuple[str, ...]] = (
    "PRAGMA journal_mode=WAL;",
    "PRAGMA foreign_keys=ON;",
    "PRAGMA busy_timeout=5000;",
)

# 18 张表，顺序即依赖顺序（被引用的先建）
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
    "fund_products",
    "fund_holdings",
    "scheduled_transfers",
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
  status           TEXT NOT NULL CHECK (status IN ('active','frozen','lost','closed')),
  per_tx_limit_fen INTEGER NOT NULL DEFAULT 500000,
  pin_hash         TEXT
);
CREATE INDEX IF NOT EXISTS idx_cards_account ON cards(account_id);

CREATE TABLE IF NOT EXISTS payees (
  id        TEXT PRIMARY KEY,
  user_id   TEXT NOT NULL REFERENCES users(id),
  name      TEXT NOT NULL,
  bank      TEXT NOT NULL,
  card_tail TEXT NOT NULL,
  alias     TEXT,
  phone     TEXT
);
CREATE INDEX IF NOT EXISTS idx_payees_user ON payees(user_id);
CREATE INDEX IF NOT EXISTS idx_payees_name ON payees(name);
CREATE INDEX IF NOT EXISTS idx_payees_phone ON payees(phone);

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
  channel           TEXT NOT NULL DEFAULT 'app',
  reversal_of       TEXT,
  reversed_by       TEXT
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

-- 理财场景三：产品库与用户持仓。金额/净值/份额一律整数，份额必须为正，
-- 赎回全部时删行而不是留 0，从库层面就不可能出现负持仓或 0 份僵尸持仓。
CREATE TABLE IF NOT EXISTS fund_products (
  code           TEXT PRIMARY KEY,
  name           TEXT NOT NULL,
  category       TEXT NOT NULL CHECK (category IN ('money','bond','mixed','equity')),
  risk_level     INTEGER NOT NULL CHECK (risk_level BETWEEN 1 AND 5),
  min_buy_fen    INTEGER NOT NULL CHECK (min_buy_fen > 0),
  nav_micro      INTEGER NOT NULL CHECK (nav_micro > 0),
  nav_updated_ms INTEGER NOT NULL,
  description    TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_fund_products_risk ON fund_products(risk_level);
CREATE INDEX IF NOT EXISTS idx_fund_products_category ON fund_products(category);

CREATE TABLE IF NOT EXISTS fund_holdings (
  user_id      TEXT NOT NULL REFERENCES users(id),
  product_code TEXT NOT NULL REFERENCES fund_products(code),
  shares_milli INTEGER NOT NULL CHECK (shares_milli > 0),
  cost_fen     INTEGER NOT NULL DEFAULT 0 CHECK (cost_fen >= 0),
  PRIMARY KEY (user_id, product_code)
);
CREATE INDEX IF NOT EXISTS idx_fund_holdings_user ON fund_holdings(user_id);

-- 定时转账（场景 1 补齐）：预约指令本身不动钱，到点执行时才走 hold/commit
CREATE TABLE IF NOT EXISTS scheduled_transfers (
  id              TEXT PRIMARY KEY,
  user_id         TEXT NOT NULL REFERENCES users(id),
  account_id      TEXT NOT NULL REFERENCES accounts(id),
  payee_id        TEXT NOT NULL REFERENCES payees(id),
  amount_fen      INTEGER NOT NULL CHECK (amount_fen > 0),
  note            TEXT NOT NULL DEFAULT '',
  due_ms          INTEGER NOT NULL,
  recurrence      TEXT NOT NULL CHECK (recurrence IN ('once','monthly')),
  status          TEXT NOT NULL CHECK (status IN ('scheduled','executed','cancelled','failed')),
  created_ms      INTEGER NOT NULL,
  executed_ms     INTEGER,
  -- 创建期验过人脸+短信的时刻：0 = 没验过。到点执行凭它放行日累计线，
  -- 否则「预约一笔大额」会在到点那天被额度线拦死，用户以为产品坏了。
  factor_verified_ms INTEGER NOT NULL DEFAULT 0,
  -- 撞了当日额度被顺延的次数：顺延有上限，超过上限就明确失败，不无限期挂着。
  defer_count        INTEGER NOT NULL DEFAULT 0,
  idempotency_key TEXT NOT NULL UNIQUE
);
CREATE INDEX IF NOT EXISTS idx_sched_due ON scheduled_transfers(status, due_ms);
CREATE INDEX IF NOT EXISTS idx_sched_user ON scheduled_transfers(user_id, status);

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


#: SQLite 的「写锁没抢到」措辞；都在 OperationalError 里，靠文本来分类。
_BUSY_MARKERS: Final[tuple[str, ...]] = (
    "database is locked",
    "database table is locked",
    "database schema is locked",
    "database is busy",
)


def as_busy_error(exc: BaseException) -> SystemBusy | None:
    """把「数据库忙」从一堆异常里分出来；不是忙就返回 None。

    busy_timeout 用尽后 sqlite3 抛的是 OperationalError，和「SQL 写错了」
    「表不存在」同属一类，只能按文本区分。分出来之后调用方可以当业务异常
    走（标失败 + 回一句人话），而不是落进 INTERNAL 兜底。
    """
    if not isinstance(exc, sqlite3.OperationalError):
        return None
    text = str(exc).lower()
    if not any(marker in text for marker in _BUSY_MARKERS):
        return None
    return SystemBusy(None, sqlite_error=str(exc)[:120])


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


# 后加的列：CREATE TABLE IF NOT EXISTS 不会改已存在的表，老库要单独补（docs/03 3.4）
_ADDED_COLUMNS: Final[tuple[tuple[str, str, str], ...]] = (
    ("cards", "pin_hash", "TEXT"),
    ("payees", "phone", "TEXT"),
    ("transactions", "reversal_of", "TEXT"),
    ("transactions", "reversed_by", "TEXT"),
    ("scheduled_transfers", "factor_verified_ms", "INTEGER NOT NULL DEFAULT 0"),
    ("scheduled_transfers", "defer_count", "INTEGER NOT NULL DEFAULT 0"),
)


def _ensure_columns(conn: sqlite3.Connection) -> None:
    """给老库补后加的列；已经是新库时全部命中、不发任何 ALTER。"""
    for table, column, ddl in _ADDED_COLUMNS:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
        if not rows:
            continue
        if column not in {str(row["name"]) for row in rows}:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


def ensure_columns(conn: sqlite3.Connection) -> None:
    """只补后加的列，不动已有表结构。

    老库（演示库里那份 data/bank.db 就是）第一次被这个版本碰到时用它补列，
    不用重建库、也不用手工迁移；补完列的库与新建的库完全一样。
    """
    _ensure_columns(conn)


def init_db(conn: sqlite3.Connection) -> None:
    """执行 DDL 建全部表与触发器，幂等（IF NOT EXISTS），老库顺带补列。"""
    conn.executescript(SCHEMA_SQL)
    _ensure_columns(conn)


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
    "SCHEMA_SQL",
    "TABLE_NAMES",
    "connect",
    "ensure_columns",
    "init_db",
    "sweep_expired_holds",
    "table_names",
    "tx",
]
