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

# 30 张表（改造前 27 + 本轮 3：用户维度日累计 daily_user_limits、计划一次性授权
# plan_authorizations、人工接管队列 handoffs；订阅推断字段在 subs、运行元数据在 dag_runs）
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
    "daily_user_limits",
    "denials",
    "fund_products",
    "fund_holdings",
    "scheduled_transfers",
    "savings_goals",
    "monitor_rules",
    "proactive_alerts",
    "monitor_runs",
    "factor_challenges",
    "investor_profiles",
    "plans",
    "plan_steps",
    "plan_budgets",
    "plan_authorizations",
    "handoffs",
    "subscription_verdicts",
    "chat_turns",
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
  -- 建 hold 时那次令牌裁决的等级。commit 只认这一列，不认调用方现传的参数：
  -- 否则「黄色确认卡先建好、另一笔并发把日累计推过阈值」时，旧黄卡还能把红操作放过去。
  risk_level      TEXT NOT NULL DEFAULT 'yellow' CHECK (risk_level IN ('green','yellow','red')),
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

-- 账户维度的日累计：账务口径，按账户记（谁家的哪张卡花了多少）。
-- 它**不再**直接喂给策略：赛题的「日累计」是用户维度（同一自然人的所有账户合起来算），
-- 那个口径在下面那张表里。保留这张表是为了账户级账单与历史兼容。
CREATE TABLE IF NOT EXISTS daily_limits (
  account_id TEXT NOT NULL REFERENCES accounts(id),
  day        TEXT NOT NULL,
  spent_fen  INTEGER NOT NULL DEFAULT 0,
  tx_count   INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (account_id, day)
);

-- 用户维度的日累计：策略判级的唯一口径（赛题：日累计 ≤1000 元为黄，>1000 元为红）。
-- 与 daily_limits 在同一个事务里一起更新，两者永远同源。
CREATE TABLE IF NOT EXISTS daily_user_limits (
  user_id   TEXT NOT NULL REFERENCES users(id),
  day       TEXT NOT NULL,
  spent_fen INTEGER NOT NULL DEFAULT 0,
  tx_count  INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (user_id, day)
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
  -- 到点复判后升级成红灯、但这次预约没有有效强验证记录：停在这里等用户再验一次，
  -- 不进执行、也不假装执行过。0 = 正常排队。
  needs_verification INTEGER NOT NULL DEFAULT 0 CHECK (needs_verification IN (0,1)),
  idempotency_key TEXT NOT NULL UNIQUE
);
CREATE INDEX IF NOT EXISTS idx_sched_due ON scheduled_transfers(status, due_ms);
CREATE INDEX IF NOT EXISTS idx_sched_user ON scheduled_transfers(user_id, status);

-- 阶段 5 主动财务管理：目标 → 监测规则 → 提醒 → 监测运行记录。
-- 这四张表只存「计划与提醒」，不碰 accounts/transactions 的余额：主动服务可以算、
-- 可以提醒，但转账/申购/扣款一律不在这里发生。
-- 金额仍用 INTEGER 分；target_date 是「日期」不是时刻，按本地日历日存 YYYY-MM-DD，
-- 避免把一个 12-31 的截止日按 UTC 推成 1-1。
CREATE TABLE IF NOT EXISTS savings_goals (
  id          TEXT PRIMARY KEY,
  user_id     TEXT NOT NULL REFERENCES users(id),
  name        TEXT NOT NULL DEFAULT '',
  target_fen  INTEGER NOT NULL CHECK (target_fen > 0),
  target_date TEXT NOT NULL,
  saved_fen   INTEGER NOT NULL DEFAULT 0 CHECK (saved_fen >= 0),
  status      TEXT NOT NULL CHECK (status IN ('active','achieved','cancelled')),
  created_ms  INTEGER NOT NULL,
  updated_ms  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_savings_goals_user ON savings_goals(user_id, status);

CREATE TABLE IF NOT EXISTS monitor_rules (
  id          TEXT PRIMARY KEY,
  goal_id     TEXT NOT NULL REFERENCES savings_goals(id),
  kind        TEXT NOT NULL CHECK (kind IN ('progress','budget')),
  -- progress：允许落后计划的百分点；budget：本期支出上限（分）
  threshold   INTEGER NOT NULL CHECK (threshold > 0),
  period      TEXT NOT NULL CHECK (period IN ('monthly','weekly')),
  enabled     INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0,1)),
  created_ms  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_monitor_rules_goal ON monitor_rules(goal_id, enabled);

CREATE TABLE IF NOT EXISTS proactive_alerts (
  id            TEXT PRIMARY KEY,
  goal_id       TEXT REFERENCES savings_goals(id),
  rule_id       TEXT REFERENCES monitor_rules(id),
  reason        TEXT NOT NULL,
  evidence_json TEXT NOT NULL,
  suggestion    TEXT NOT NULL DEFAULT '',
  created_ms    INTEGER NOT NULL,
  status        TEXT NOT NULL CHECK (status IN ('open','ignored','resolved')),
  resolved_ms   INTEGER
);
CREATE INDEX IF NOT EXISTS idx_alerts_goal_status ON proactive_alerts(goal_id, status, created_ms);
CREATE INDEX IF NOT EXISTS idx_alerts_created ON proactive_alerts(created_ms);

CREATE TABLE IF NOT EXISTS monitor_runs (
  id             TEXT PRIMARY KEY,
  started_ms     INTEGER NOT NULL,
  finished_ms    INTEGER,
  status         TEXT NOT NULL CHECK (status IN ('ok','failed')),
  alerts_created INTEGER NOT NULL DEFAULT 0,
  detail         TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_monitor_runs_started ON monitor_runs(started_ms);

-- 多因子挑战：服务端签发、服务端校验、一次性使用（app/security/factors.py）
-- code_hash 只存摘要；used_ms 非空即已用过；attempts 防暴力猜码。
CREATE TABLE IF NOT EXISTS factor_challenges (
  id              TEXT PRIMARY KEY,
  session_id      TEXT NOT NULL,
  confirmation_id TEXT NOT NULL,
  factor          TEXT NOT NULL CHECK (factor IN ('sms','face','otp')),
  code_hash       TEXT NOT NULL,
  created_ms      INTEGER NOT NULL,
  expires_ms      INTEGER NOT NULL,
  used_ms         INTEGER,
  attempts        INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0)
);
CREATE INDEX IF NOT EXISTS idx_factor_challenges_conf ON factor_challenges(confirmation_id, factor);

-- 投资者适当性：问卷答案 + 能力/意愿两个分数，买基金前必须先有这张表里的画像
CREATE TABLE IF NOT EXISTS investor_profiles (
  user_id       TEXT PRIMARY KEY REFERENCES users(id),
  score         INTEGER NOT NULL CHECK (score BETWEEN 0 AND 100),
  level         TEXT NOT NULL CHECK (level IN ('C1','C2','C3','C4','C5')),
  capacity      INTEGER NOT NULL CHECK (capacity BETWEEN 1 AND 5),
  horizon_years INTEGER NOT NULL CHECK (horizon_years >= 0),
  loss_tolerance TEXT NOT NULL,
  answers_json  TEXT NOT NULL,
  created_ms    INTEGER NOT NULL,
  updated_ms    INTEGER NOT NULL
);

-- 跨场景计划：一份计划下面挂若干步骤，按 due_ms 由调度器推进
CREATE TABLE IF NOT EXISTS plans (
  id          TEXT PRIMARY KEY,
  user_id     TEXT NOT NULL REFERENCES users(id),
  title       TEXT NOT NULL,
  status      TEXT NOT NULL CHECK (status IN ('active','paused','completed','cancelled','failed')),
  meta_json   TEXT NOT NULL DEFAULT '{}',
  created_ms  INTEGER NOT NULL,
  updated_ms  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_plans_user ON plans(user_id, status);

CREATE TABLE IF NOT EXISTS plan_steps (
  id          TEXT PRIMARY KEY,
  plan_id     TEXT NOT NULL REFERENCES plans(id),
  seq         INTEGER NOT NULL,
  kind        TEXT NOT NULL CHECK (kind IN ('check','action','reminder')),
  title       TEXT NOT NULL,
  payload_json TEXT NOT NULL DEFAULT '{}',
  state       TEXT NOT NULL CHECK (state IN ('pending','running','done','failed','skipped')),
  due_ms      INTEGER NOT NULL,
  attempts    INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
  max_attempts INTEGER NOT NULL DEFAULT 3 CHECK (max_attempts > 0),
  last_error  TEXT NOT NULL DEFAULT '',
  updated_ms  INTEGER NOT NULL,
  UNIQUE (plan_id, seq)
);
CREATE INDEX IF NOT EXISTS idx_plan_steps_due ON plan_steps(state, due_ms);

-- 生日计划的项目预算：账本上的预算占用，**不是资金冻结**。
-- 不建 holds 行、不扣余额，所以也不会像 5 分钟临时 hold 那样「过期自动释放」——
-- 拿短 hold 冒充长期预留，等于把一个月的计划写成 5 分钟的资金占用，语义完全不对。
CREATE TABLE IF NOT EXISTS plan_budgets (
  id          TEXT PRIMARY KEY,
  plan_id     TEXT NOT NULL REFERENCES plans(id),
  item_key    TEXT NOT NULL,
  label       TEXT NOT NULL,
  merchant    TEXT NOT NULL,
  amount_fen  INTEGER NOT NULL CHECK (amount_fen > 0),
  -- 这笔预留占的是哪个账户的可支配金额。历史数据为 NULL（当时只是记账），
  -- 新数据必须带上，否则「预留」和「账户」对不上，别的写操作不可能被它拦住。
  account_id  TEXT REFERENCES accounts(id),
  -- 转成付款 hold 后指向那份 hold：一笔钱只能有一个占用者，避免预算与 hold 重复扣。
  hold_id     TEXT,
  extra_json  TEXT NOT NULL DEFAULT '{}',
  status      TEXT NOT NULL CHECK (status IN ('reserved','paying','committed','declined','failed','released')),
  reason      TEXT NOT NULL DEFAULT '',
  created_ms  INTEGER NOT NULL,
  updated_ms  INTEGER NOT NULL,
  UNIQUE (plan_id, item_key)
);
CREATE INDEX IF NOT EXISTS idx_plan_budgets_plan ON plan_budgets(plan_id, status);
CREATE INDEX IF NOT EXISTS idx_plan_budgets_account ON plan_budgets(account_id, status);

-- 生日计划的一次性预约授权（任务四）：绑定**具体计划版本 + 逐项商户/金额/账户/执行时间**。
-- 「用户批准了生日计划」只对这一个版本、这几项、这个金额上限有效；
-- 计划被改、被取消、金额变了、风险升级成红灯，这条授权就不再成立。
CREATE TABLE IF NOT EXISTS plan_authorizations (
  id            TEXT PRIMARY KEY,
  plan_id       TEXT NOT NULL REFERENCES plans(id),
  plan_version  TEXT NOT NULL,
  account_id    TEXT NOT NULL DEFAULT '',
  scope_json    TEXT NOT NULL DEFAULT '{}',
  decision_json TEXT NOT NULL DEFAULT '{}',
  status        TEXT NOT NULL CHECK (status IN ('active','used','revoked','expired')),
  session_id    TEXT NOT NULL DEFAULT '',
  created_ms    INTEGER NOT NULL,
  used_ms       INTEGER,
  revoked_ms    INTEGER,
  expires_ms    INTEGER
);
CREATE INDEX IF NOT EXISTS idx_plan_auth_plan ON plan_authorizations(plan_id, status);

-- 人工接管队列（任务二）：任务卡在需要人判断的地方，把「原始请求 + DAG + 已完成节点 +
-- 失败节点 + 裁决结果 + 授权状态」整包交给操作员，而不是把会话交给运气。
-- 同一次 run 同时只允许一条活着的接管单（下面那个部分唯一索引），
-- 避免两个入口各开一张、操作员各处理一半。
CREATE TABLE IF NOT EXISTS handoffs (
  id             TEXT PRIMARY KEY,
  run_id         TEXT NOT NULL,
  session_id     TEXT NOT NULL,
  user_id        TEXT NOT NULL DEFAULT '',
  account_id     TEXT NOT NULL DEFAULT '',
  status         TEXT NOT NULL CHECK (
                   status IN ('pending','claimed','need_info','rejected','cancelled','resumed','failed')
                 ),
  reason         TEXT NOT NULL DEFAULT '',
  error_code     TEXT NOT NULL DEFAULT '',
  request_text   TEXT NOT NULL DEFAULT '',
  dag_json       TEXT NOT NULL DEFAULT '{}',
  done_nodes_json TEXT NOT NULL DEFAULT '[]',
  failed_node_json TEXT NOT NULL DEFAULT '{}',
  failure_json   TEXT NOT NULL DEFAULT '{}',
  decision_json  TEXT NOT NULL DEFAULT '{}',
  auth_json      TEXT NOT NULL DEFAULT '{}',
  operator       TEXT NOT NULL DEFAULT '',
  note           TEXT NOT NULL DEFAULT '',
  claimed_ms     INTEGER NOT NULL DEFAULT 0,
  created_ms     INTEGER NOT NULL,
  updated_ms     INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_handoffs_status ON handoffs(status, created_ms);
CREATE UNIQUE INDEX IF NOT EXISTS idx_handoffs_active_run
  ON handoffs(run_id) WHERE status IN ('pending','claimed','need_info');

-- 疑似订阅的核对结论：流水推断出来的「疑似订阅」只能由用户自己定性。
-- 说「这不是订阅」之后要能记住，否则每次打开页面同一条又冒出来，
-- 用户既不能转正也不能排除，那这块就只是块只读广告。
CREATE TABLE IF NOT EXISTS subscription_verdicts (
  user_id     TEXT NOT NULL,
  merchant    TEXT NOT NULL,
  verdict     TEXT NOT NULL CHECK (verdict IN ('not_subscription','confirmed')),
  note        TEXT NOT NULL DEFAULT '',
  created_ms  INTEGER NOT NULL,
  updated_ms  INTEGER NOT NULL,
  PRIMARY KEY (user_id, merchant)
);

-- 会话记忆：每一轮对话按会话落库，服务重启也接得上。
-- 为什么不用 dag_nodes 代替：一个会话里多数轮次根本不会建 DAG（查余额、问提醒、
-- 定性订阅都是直答），只记 DAG 就等于只记住了「执行过的那几句」。
CREATE TABLE IF NOT EXISTS chat_turns (
  session_id TEXT NOT NULL,
  seq        INTEGER NOT NULL,
  role       TEXT NOT NULL CHECK (role IN ('user','assistant')),
  content    TEXT NOT NULL,
  ts_ms      INTEGER NOT NULL,
  PRIMARY KEY (session_id, seq)
);
CREATE INDEX IF NOT EXISTS idx_chat_turns_time ON chat_turns(session_id, ts_ms);

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


# 后加的列：CREATE TABLE IF NOT EXISTS 不会改已存在的表，老库要单独补（docs/03 3.2「老库升级顺序」）
_ADDED_COLUMNS: Final[tuple[tuple[str, str, str], ...]] = (
    ("cards", "pin_hash", "TEXT"),
    ("payees", "phone", "TEXT"),
    ("transactions", "reversal_of", "TEXT"),
    ("transactions", "reversed_by", "TEXT"),
    ("scheduled_transfers", "factor_verified_ms", "INTEGER NOT NULL DEFAULT 0"),
    ("scheduled_transfers", "defer_count", "INTEGER NOT NULL DEFAULT 0"),
    # 领取租约：多进程同时 tick 时，只有抢到租约的那一个执行（不是靠「谁先读到」）
    ("scheduled_transfers", "claim_token", "TEXT NOT NULL DEFAULT ''"),
    ("scheduled_transfers", "claimed_ms", "INTEGER NOT NULL DEFAULT 0"),
    ("scheduled_transfers", "needs_verification", "INTEGER NOT NULL DEFAULT 0"),
    ("plan_budgets", "account_id", "TEXT"),
    ("plan_budgets", "hold_id", "TEXT"),
    # 账户状态：money path 真的要看它（冻结/销户后不能再出账）
    ("accounts", "status", "TEXT NOT NULL DEFAULT 'active'"),
    # hold 建卡时的裁决等级：commit 据此决定能不能跨过日累计风险阈值（见 SCHEMA_SQL 注释）
    ("holds", "risk_level", "TEXT NOT NULL DEFAULT 'yellow'"),
    # 订阅来源：recorded=银行代扣记录，inferred=从流水推断（必须带证据与置信度）
    ("subs", "source", "TEXT NOT NULL DEFAULT 'recorded'"),
    ("subs", "confidence", "REAL NOT NULL DEFAULT 1.0"),
    ("subs", "evidence_json", "TEXT NOT NULL DEFAULT '{}'"),
    # 运行元数据：重建 RunState 需要 account_id / user_text
    ("dag_runs", "meta_json", "TEXT NOT NULL DEFAULT '{}'"),
    # 自动监测的节流时间：同一时间窗内不重复跑，重启后也不会重复
    ("savings_goals", "last_monitored_ms", "INTEGER NOT NULL DEFAULT 0"),
    # 这次监测是谁触发的：manual=用户点按钮，scheduler=后台节拍器
    ("monitor_runs", "trigger", "TEXT NOT NULL DEFAULT 'manual'"),
    # 提醒的归属：储蓄目标提醒由 goal_id 推出用户；跨场景计划的提醒没有 goal_id，
    # 必须自己带 user_id，否则按用户查提醒时会整条看不见。
    ("proactive_alerts", "user_id", "TEXT NOT NULL DEFAULT ''"),
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
    """执行 DDL 建全部表与触发器，幂等（IF NOT EXISTS），老库顺带补列。

    顺序不能反过来：SCHEMA_SQL 里的索引（如 idx_plan_budgets_account）引用
    后加的列，老库还没有这些列时先 executescript 会直接 no such column。
    先补列、再建表和索引，新库与老库走同一条路径。
    """
    # 两次补列，方向不同：
    # - 之前一次：老库的索引（如 idx_plan_budgets_account）引用后加的列，
    #   必须先把列补出来，否则 executescript 直接 no such column；
    # - 之后一次：全新库的表由 SCHEMA_SQL 建出来，少数历史表（accounts.status、
    #   dag_runs.meta_json 等）的 DDL 仍只靠 _ADDED_COLUMNS 补，所以建表后还要再补一遍。
    # 两次都是幂等的：列已存在就什么都不做。
    _ensure_columns(conn)
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
