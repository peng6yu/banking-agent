# 改动前测试基线报告（banking-agent）

本文件是 **大型重构前**的可复现基线快照。所有数字均为本机实际执行观测所得，未经观测的数值一律不写。

- 仓库路径：`/home/peng/peng_workspace/deepseek harness/银行agent/banking-agent`
- 采集时间：2026-10-23 23:16 ~ 23:27（Asia/Shanghai）
- 采集方式：仓库根目录 `.venv/bin/python`（不使用系统 Python）
- 本次唯一新增文件：`docs/audit/baseline_report.md`（未改动 app/ web/ tests/ scripts/ docs/benchmark/ 及任何既有文档）

---

## 1. 基线环境

| 项目 | 观测值 |
|---|---|
| 分支 | `main` |
| HEAD commit | `b769da120d0d2d054ef8bc4f454cff314e6c0f1b` |
| commit 标题 / 时间 | `docs: 修正测试增量 1131-904=+227` / 2026-10-23 22:32:10 +0800 |
| Python | `Python 3.12.13` (main, Apr 14 2026, 14:29:00) [Clang 22.1.3] |
| 平台 | `Linux-7.0.0-38-generic-x86_64-with-glibc2.43` |
| pytest | `pytest 9.1.1` |
| 解释器路径 | 仓库根 `.venv/bin/python` |
| `.env` | **存在**，486 字节，权限 0600；键名（值已脱敏）：`MINIMAX_API_KEY`、`MINIMAX_API_HOST`、`MODEL_BRAIN`、`MODEL_FAST`、`MODEL_BACKUP`、`LLM_TEMPERATURE`、`LLM_TIMEOUT`、`MOCK_MODE`、`CAPABILITY_SECRET` |
| `MOCK_MODE` | 实际生效为 **False**（`app.smoke` 打印 `mock: False`），即走真实模型 |
| 真实 LLM 是否可达 | **是（Y）**。见 §2 命令 10：`python -m app.smoke` 退出码 0，4/4 通过，累计真实调用 2 次 |
| 模型厂商 / 端点 | `minimax` / `https://api.minimaxi.com/v1/chat/completions` |
| 主脑 / 备脑 | `MiniMax-M3.1-Flash-Preview` / `MiniMax-M2.5` |
| 配额状态 | **未耗尽**。未出现 HTTP 429，未出现 `rate_limit_error` |

### 1.1 工作区状态（采集期间在变化，如实记录）

| 时间点 | `git status --porcelain` |
|---|---|
| 约 23:16（首次全量 pytest 之前） | **空**（0 行），树干净 |
| 23:27:18（第二次全量 pytest 之前） | 9 行（见下） |
| 23:27:25 / 23:27:49 | 9 行（见下） |

23:27 时刻的未提交改动（**同事并发改动，未做任何清理 / stash / commit**）：

```
 M app/main.py
 M app/planner/events.py
 M app/planner/executor.py
 M app/planner/runner.py
 M app/planner/ticker.py
 M app/tools/bill.py
 M app/tools/queries.py
?? app/api/business.py
?? docs/benchmark/competitive_matrix.md
```

> 因此本报告同时给出 **两次全量 pytest 结果**：一次在干净树（commit `b769da1` 状态），一次在上述未提交改动之上。重构应以后者之外的干净树数值为比较基准。

---

## 2. 命令结果表

全部命令的工作目录均为仓库根 `.../banking-agent/`，解释器均为 `.venv/bin/python`。

| # | 命令 | 退出码 | 关键输出（摘要） | 耗时 |
|---|---|---|---|---|
| 1 | `.venv/bin/python -m pytest -q tests/` （干净树） | **0** | `1131 passed, 4 skipped, 1 warning in 18.78s` | pytest 自报 18.78s / 墙钟 ≈19.2s |
| 1b | `.venv/bin/python -m pytest -q tests/ -rs` （干净树，取跳过原因） | **0** | `1131 passed, 4 skipped, 1 warning in 18.34s` | 18.34s / 墙钟 18.6s |
| 1c | `.venv/bin/python -m pytest -q tests/` （含 §1.1 未提交改动） | **1** | `2 failed, 1129 passed, 4 skipped, 1 warning in 19.93s` | 19.93s / 墙钟 20.2s |
| 2 | `.venv/bin/python scripts/check_frontend.py` | **0** | `前端检查通过：66/66 项全部达标` | <1s（墙钟 0s） |
| 3 | `.venv/bin/python -m scripts.e2e --offline` （**单独运行**） | **0** | `合计 13 步，PASS 13，FAIL 0`；`健康检查 ok=True 模型=MiniMax-M3.1-Flash-Preview 工具数=33` | 墙钟 1s |
| 3b | `.venv/bin/python -m scripts.e2e --offline` （**与 redteam 并发**） | **1** | `sqlite3.OperationalError: no such table: denials` / `no such table: holds` / `app.clock.ValidationError: 账户不存在` → 崩溃于 `scripts/e2e.py:339 step_bill_anomaly` | 墙钟 1s |
| 4 | `.venv/bin/python -m scripts.redteam` | **0** | `用例 43 条：PASS 43 / FAIL 0` | 墙钟 25s（单条最慢 E05 = 15.1s） |
| 5 | `.venv/bin/python -m pytest -q tests/eval/test_eval_harness.py` | **0** | `5 passed in 0.44s` | 0.44s / 墙钟 0.6s |
| 6 | `.venv/bin/python -m pytest -q tests/test_browser_smoke.py` | **0** | `4 skipped in 0.01s` | 0.01s / 墙钟 0.2s |
| 7a | `.venv/bin/python -m pytest -q tests/test_no_exec_surface.py` | **0** | `8 passed in 0.20s` | 0.20s / 墙钟 0.4s |
| 7b | `.venv/bin/python scripts/prove_no_exec_surface.py` | **0** | `命中（代码执行面）: 0`；`命中（网络出网面）: 3`（全在 `app/llm.py` 的 `urllib.request`）；`扫描器自检: 15/15 段植入样本被报出` | 墙钟 0.5s |
| 8 | 计数脚本（见 §3） | **0** | 见 §3 | — |
| 9 | `.venv/bin/python -m app.smoke` | **0** | `PASS  工具调用可用` / `PASS  思维链剥离` / `PASS  流式可用` / `PASS  Mock 兜底可用`；`累计真实调用: 2  通过: 4 / 4` | 墙钟 11s |
| 10 | `git log -1 --format='%H %ci %s'` / `git branch --show-current` / `git rev-parse HEAD` | **0** | 见 §1 / §1.1 | <1s |
| 11 | `.venv/bin/python -m pytest -q tests/test_token.py tests/test_policy.py tests/test_payee_discriminator.py` | **0** | `75 passed in 0.24s` | 0.24s / 墙钟 0.9s |
| 12 | `python -m scripts.seed`（**临时库**，见 §5） | **0** | `种子数据自检未通过` 未触发；13 项 `seed.check` 全部 `passed:true`；`seed.ok tables=22 random_seed=20261018 window_days=180` | ≈0.01s |

### 2.1 命令 8 观测到的原始输出

```
$ find tests -name 'test_*.py' | wc -l
63
$ grep -rhoE '^[[:space:]]*def test_[A-Za-z0-9_]+' tests --include='*.py' | wc -l
798
$ .venv/bin/python -m pytest --collect-only -q tests/ | tail
1135 tests collected in 0.31s

$ .venv/bin/python -c "from app.tools.registry import TOOLS; from app.policy.rules import RULES; \
    from app.planner.dag import INTENT_TEMPLATES; from app.audit import EVENT_TYPES; ..."
TOOLS 33
RULES 12
INTENT_TEMPLATES 9
EVENT_TYPES 29

$ .venv/bin/python -c "import sqlite3; c=sqlite3.connect('data/bank.db'); \
    print(c.execute("select count(*) from sqlite_master where type='table' and name not like 'sqlite_%'").fetchone()[0])"
22
```

22 张表的清单（`sqlite_master` 实测）：
`accounts`、`audit_log`、`cards`、`confirmations`、`dag_nodes`、`dag_runs`、`daily_limits`、`denials`、`fund_holdings`、`fund_products`、`holds`、`monitor_rules`、`monitor_runs`、`payees`、`proactive_alerts`、`savings_goals`、`scheduled_transfers`、`sessions`、`subs`、`token_nonces`、`transactions`、`users`

---

## 3. 测试套件构成

### 3.1 规模

| 指标 | 数值 | 来源 |
|---|---|---|
| 测试文件数（`tests/**/test_*.py`） | **63** | `find tests -name 'test_*.py'` |
| `tests/` 下 `.py` 总数 | 67 | 含 `conftest.py`、`__init__.py`、`tool_fixtures.py`、`eval/run_eval.py` |
| `def test_*` 函数定义数 | **798** | `grep -rhoE '^\s*def test_[A-Za-z0-9_]+' tests` |
| 实际收集到的用例数 | **1135** | `pytest --collect-only -q tests/` |
| 差额来源 | 1135 > 798 = **parametrize 展开**（例如 `test_tools_write.py` 1 个函数参数化出 8 个用例） | — |
| `app/` 模块数 | 64 | `find app -name '*.py'` |
| `scripts/` 脚本数 | 14 | `find scripts -name '*.py'` |

### 3.2 按功能域分布（按文件名前缀归类，规则与结果如下，合计 = 1135）

| 功能域 | 用例数 | 文件数 | 归属文件 |
|---|---|---|---|
| 意图 / 规划 / DAG | 208 | 15 | test_planner(60)、test_card_intent(22)、test_intent_question(15)、test_invest(15)、test_clarify_resume(12)、test_invest_intent_dag(11)、test_relation_payee(11)、test_birthday_intent(9)、test_payee_discriminator(9)、test_scheduled_fires(9)、test_subscription_cancel(9)、test_phone_payee(7)、test_scheduled(6)、test_reverse(5)、test_split(8) |
| 导入面 / 静态检查 | 151 | 3 | test_imports(135)、test_no_exec_surface(8)、test_setup_script(8) |
| 提示注入 / 对抗 | 132 | 8 | test_injection_integration(36)、test_redteam_product_defects(20)、test_injection(17)、test_refusal_guard(17)、test_security_knowledge(13)、test_injection_classifier(12)、test_security_proactive(11)、test_injection_session_taint(6) |
| 风控 / 熔断 / 策略 | 122 | 6 | test_payment_guard(44)、test_policy(43)、test_breaker(11)、test_proactive_safety(9)、test_capability_secret(8)、test_breaker_production(7) |
| LLM / 模型 / 厂商 | 97 | 4 | test_config_model_provider(30)、test_provider_check(29)、test_llm_fallback(21)、test_models(17) |
| 资金 / 账务 / 支付 | 84 | 8 | test_funds(25)、test_money(24)、test_confirm_timeout_config(9)、test_funds_invariants(9)、test_e2e_transfer(7)、test_confirmations_persist(4)、test_confirm_early_answer(3)、test_confirm_timeout(3) |
| 工具层 | 84 | 3 | test_tools_write(36)、test_tools(26)、test_tools_cards(22) |
| API / HTTP 层 | 83 | 5 | test_api_auth(32)、test_acceptance(17)、test_api(14)、test_envelope(14)、test_proactive_api(6) |
| 知识库 | 74 | 4 | test_knowledge_integration(33)、test_knowledge_normalize(15)、test_knowledge_loader(13)、test_knowledge_retrieve(13) |
| 主动预计算 | 41 | 1 | test_proactive_calc(41) |
| DB / 审计 / 令牌 | 39 | 3 | test_token(23)、test_audit(8)、test_db(8) |
| 卡片销户改密（未归入上列） | 11 | 1 | test_cards_close_pin(11) |
| 评测 | 5 | 1 | test_eval_harness(5) |
| 浏览器冒烟 | 4 | 1 | test_browser_smoke(4) |
| **合计** | **1135** | **63** | — |

### 3.3 红队用例数（按类别）

数据集文件：`tests/redteam/cases.yaml`（43 条），执行器 `scripts/redteam.py`（离线 `ScriptedAdapter`，不联网）。

| 类别 | 用例数 | 本次 PASS | 本次 FAIL |
|---|---|---|---|
| 诱导转账（A01–A10） | 10 | 10 | 0 |
| 信息套取（B01–B08） | 8 | 8 | 0 |
| 越权操作（C01–C10） | 10 | 10 | 0 |
| 提示注入（D01–D10） | 10 | 10 | 0 |
| 异常与稳定性（E01–E05） | 5 | 5 | 0 |
| **合计** | **43** | **43** | **0** |

- 红队退出码 **0**（全部通过时为 0；按设计，部分失败应为 1）。
- 失败清单文件 `tmp/redteam_failures.txt` 实测内容为：`总数 43，PASS 43，FAIL 0`（无失败条目）。
- E05 运行时出现 `executor.run.busy code=SYSTEM_BUSY`（耗时 15.1s），用例仍判 PASS —— 该路径被正常覆盖到。
- 每条用例都会 **重置 `data/bank.db`** 并重新播种（`scripts/redteam.py` 头部注释 + `purge` 逻辑），因此红队是破坏性运行。

### 3.4 评测数据集规模

| 指标 | 数值 |
|---|---|
| 数据集文件 | `tests/eval/intent_cases.yaml` |
| 用例总数 | **80**（`dev` 60 + `holdout` 20，ID 唯一 80/80） |
| 安全标签分布 | `none` 41 / `ask` 34 / `refuse` 3 / `unsupported` 2 |
| 意图分布 | transfer 31、invest 10、subscription 8、bill_analysis 7、card_manage 6、query_balance 5、unknown 4、schedule_transfer 3、reversal 3、split_bill 2、birthday_plan 1 |
| 已有运行结果 | `tests/eval/results/` 下 7 个时间戳目录；最近一次 `20261023T210056` 的 `cases_a.jsonl` / `cases_b.jsonl` 各 80 行；`latest_summary.json` 12,630 字节 |
| 说明 | `tests/eval/run_eval.py` **不是** `test_*.py`，不会被默认 pytest 收集；评测需单独运行 |
| 离线自测 | `tests/eval/test_eval_harness.py` 5 passed（用 Scripted adapter 回放，不发网络请求） |

---

## 4. 已知失败与跳过

### 4.1 失败用例

**干净树（commit `b769da1`）：0 个失败。** 全量套件为 `1131 passed, 4 skipped`，无失败。

**含 §1.1 未提交改动的工作区：2 个失败**（均与并发改动直接相关，非基线既有缺陷）：

| 用例 | 首行错误 | 归因 |
|---|---|---|
| `tests/test_imports.py::test_every_app_module_is_registered` | `AssertionError: 这些模块没登记进 tests/test_imports.py 的允许依赖表：['app.api.business']`（断言位置 `tests/test_imports.py:472`） | 新增未跟踪文件 `app/api/business.py` 未登记进 `ALLOWED` 依赖表（docs/02 §2.4 的登记动作尚未做） |
| `tests/test_tools.py::test_analyze_bill_detects_all_three_anomalies` | `AssertionError: assert {'amount_fen', ...} == {'amount_fen', 'detail', 'kind', 'title'}`，左侧多出 `window_start_ms`、`window_end_ms`、`occurrence_count`、`tx_ids`、`merchant`（断言位置 `tests/test_tools.py:262`） | `app/tools/bill.py` 被修改，`analyze_bill` 返回的 anomaly 字典新增了 5 个字段，而测试用的是 **全等** 断言（集合相等），字段一多即失败 |

> 这两条在 **干净树上不失败**（干净树全量跑通过）。重构前请先确认基线取自哪一侧。

### 4.2 跳过用例（4 个，全部为环境跳过，非真实能力缺口）

| 用例位置 | 跳过原因（pytest 原话） |
|---|---|
| `tests/test_browser_smoke.py:42` | 浏览器冒烟默认不跑（要真起后端 + 真开浏览器）；设 `RUN_BROWSER_SMOKE=1` 显式开启 |
| `tests/test_browser_smoke.py:50` | 同上 |
| `tests/test_browser_smoke.py:57` | 同上 |
| `tests/test_browser_smoke.py:71` | 同上 |

单独执行 `.venv/bin/python -m pytest -q tests/test_browser_smoke.py` → `4 skipped in 0.01s`，退出码 0。

**性质区分**：这 4 条属于 **环境跳过**（缺真实浏览器 + 真实后端进程的运行条件），**不是**产品逻辑缺口；代码本身从未被执行过。

### 4.3 已知缺陷与未完成能力清单

1. **浏览器端到端从未在自动化中执行**（4 条用例常驻跳过）。真实浏览器渲染路径、`web/index.html` 的 DOM 行为只有 `scripts/check_frontend.py` 的 **静态** 检查（66/66 通过）背书，没有运行期证据。
2. **红队与 E2E 不可并发执行**。两者都写同一个 `data/bank.db`：`scripts/redteam.py` 每条用例前重置该库，`scripts/e2e.py` 在 `scripts/e2e.py:217` 也删除同名库重建。并发时 e2e 立刻炸 `no such table: denials` / `no such table: holds` / `账户不存在`（本次已实测，命令 3b）。**该库是 gitignore 的运行时数据，被这两条命令重写属预期行为。**
3. **`cancel_subscription` 没有意图模板**。e2e 演示步骤 5 的备注原文：「无 cancel_subscription 意图模板，走 executor.run 直驱（docs/02 遗留 2）」。当前该能力只能绕过规划器直达执行器。
4. **资金流水口径不全**。e2e 步骤 10 备注原文：「funds._settle 的 category 固定 transfer、counterparty 取收款人名，商户名不进流水；在鲜花店花了多少需按 hold_id 关联 pay_merchant 的返回值」。商户维度无法从流水直接聚合。
5. **`app/api/business.py` 未登记进依赖白名单**，导致 `test_imports.py` 失败（见 §4.1）—— 新增模块的「三动作之一」登记流程未完成。
6. **`analyze_bill` 的 anomaly 字典结构已变但测试未同步**（见 §4.1），测试用全等断言，对字段增减零容错，重构改动返回值极易踩雷。
7. **评测流水线不在默认测试内**。80 条意图评测需手工跑 `tests/eval/run_eval.py`，且 `tests/eval/results/` 里最新一次结果的时间戳是 `20261023T210056`，晚于基线 commit（22:32）之前，属独立产物。
8. **模型不可达时的行为未纳入基线**。本次 `MOCK_MODE=False` 且真实模型可达（4/4 通过），因此 **没有** 观测到 429 / `rate_limit_error`；配额耗尽场景本基线不覆盖。
9. **`sqlite3` 命令行工具本机不存在**（`bash: sqlite3: 未找到命令`），表计数改用 Python 的 `sqlite3` 模块执行等价 SQL。
10. **1 条警告**：`StarletteDeprecationWarning: Using `httpx` with `starlette.testclient` is deprecated; install `httpx2` instead.`（来自 `fastapi/testclient.py:1`），非失败，但升级 `httpx` 时会成为阻塞项。

---

## 5. 环境限制

1. **`scripts/seed.py` 不接受 `--db` 参数**，只认 `--force`，且 `--force` 会删除 `data/bank.db`（含 -wal/-shm）后重播。本报告 **未使用 `--force`**，改为在子进程内把 `app.db.DEFAULT_DB_PATH` 指向临时库后调用 `scripts.seed.main([])`：
   - 临时库：`/tmp/bank_seed_baseline_*/bank.db`
   - 结果：返回码 **0**，13 项 `seed.check` 全 `passed:true`，22 表、320 笔交易、2 账户、5 收款人、2 卡、5 订阅
   - **`data/bank.db` 的 mtime 全程未变**（已比对），真实库未被覆盖
2. **无 GPU / 本任务不涉及推理硬件**，不适用。
3. **真实模型调用会消耗配额**：`app.smoke` 本次发起 2 次真实调用（端点 `https://api.minimaxi.com/v1/chat/completions`），延迟 3630ms / 1137ms，11s 结束。`MOCK_MODE` 为 False 时 `scripts/e2e` 亦会真实调用模型（本次 e2e 走 `--offline` 端口随机化）。
4. **`data/bank.db`、`data/token_secret`、`data/api_token`、`data/audit_fallback.log`、`tmp/` 全部在 `.gitignore` 内**，跑测试/脚本会改写它们，但 `git status --porcelain` 不受影响。
5. **工作区存在并发未提交改动**（§1.1），任何"基线"数值都必须绑定到当时的 commit 与当时的工作区状态，否则不可复现。

---

## 6. 复现步骤

前置：在仓库根 `.../banking-agent/` 执行；解释器一律用 `.venv/bin/python`；**第 3 步与第 4 步不可并发**。

```bash
cd '/home/peng/peng_workspace/deepseek harness/银行agent/banking-agent'

# 0) 记录起点
git branch --show-current
git rev-parse HEAD
git log -1 --format='%H %ci %s'
git status --porcelain
.venv/bin/python --version
.venv/bin/python -m pytest --version

# 1) 全量后端套件（带跳过原因）
.venv/bin/python -m pytest -q tests/ -rs
# 期望（干净树）：1131 passed, 4 skipped, 1 warning

# 2) 前端静态检查
.venv/bin/python scripts/check_frontend.py
# 期望：前端检查通过：66/66 项全部达标

# 3) 端到端演示（单独跑；不要与第 4 步并发）
.venv/bin/python -m scripts.e2e --offline
# 期望：合计 13 步，PASS 13，FAIL 0

# 4) 红队（离线；破坏性，会重置 data/bank.db）
.venv/bin/python -m scripts.redteam
# 期望：用例 43 条：PASS 43 / FAIL 0

# 5) 评测离线自测
.venv/bin/python -m pytest -q tests/eval/test_eval_harness.py
# 期望：5 passed

# 6) 浏览器冒烟（默认跳过）
.venv/bin/python -m pytest -q tests/test_browser_smoke.py
# 期望：4 skipped

# 7) 无代码执行面证明
.venv/bin/python -m pytest -q tests/test_no_exec_surface.py
.venv/bin/python scripts/prove_no_exec_surface.py
# 期望：8 passed；命中（代码执行面）: 0

# 8) 计数
.venv/bin/python -m pytest --collect-only -q tests/ | tail -1
find tests -name 'test_*.py' | wc -l
grep -rhoE '^[[:space:]]*def test_[A-Za-z0-9_]+' tests --include='*.py' | wc -l
.venv/bin/python - <<'PY'
from app.tools.registry import TOOLS
from app.policy.rules import RULES
from app.planner.dag import INTENT_TEMPLATES
from app.audit import EVENT_TYPES
print(len(TOOLS), len(RULES), len(INTENT_TEMPLATES), len(EVENT_TYPES))   # 33 12 9 29
PY
.venv/bin/python - <<'PY'
import sqlite3
c = sqlite3.connect("data/bank.db")
print(c.execute("select count(*) from sqlite_master where type='table' and name not like 'sqlite_%'").fetchone()[0])  # 22
PY

# 9) 真实模型可达性（会真实消耗配额，失败时不重试）
.venv/bin/python -m app.smoke
# 期望：通过: 4 / 4，累计真实调用: 2

# 10) 安全相关子集
.venv/bin/python -m pytest -q tests/test_token.py tests/test_policy.py tests/test_payee_discriminator.py
# 期望：75 passed

# 11) seed.py 可运行性（临时库，不动 data/bank.db）
.venv/bin/python - <<'PY'
import tempfile, pathlib
from app import db
from scripts import seed
tmp = pathlib.Path(tempfile.mkdtemp()) / "bank.db"
db.DEFAULT_DB_PATH = tmp
print("rc =", seed.main([]))
PY
```

---

*本报告只描述观测到的行为，不含推断值。所有"期望"均为本机同环境下已实际观测到的输出。*
