# 最终验收报告（阶段 7 · 2026-10-24 审计整改轮）

> 生成时间：2026-10-24 20:10（Asia/Shanghai），七项审计整改全部落地并重跑后更新。
> 仓库：`banking-agent/`，分支 `main`；本轮起点是 `563ead2`（上一轮公开提交），审计整改代码提交为 `61c5cf3`，
> 答辩包装提交为 `803e78e`（PPT/README/文档），收口提交为 `077e400`；`release_manifest.json` 以 `077e400` 的干净工作区生成，其后的清单提交只重生成清单。
> **尚未 push 远程**（任务书明确未经授权不得推送）；发布清单在干净工作区上生成。
> 本报告里的每个数字都来自本次实际执行，命令见 §3；与 `release_manifest.json` 冲突时以清单为准。
> 历史轮次数字（1336 passed / 33 工具 / 22 表 / 29 事件 / 62 测试文件）保留在 §11，标注为**历史基线**。

---

## 1. 结论摘要

| 维度 | 结论 |
|---|---|
| 代码 | 知识层、确定性资金安全执行、后台调度、持久化计划、人工接管队列均已接入真实调用链；规模见 §3 |
| 知识层 | 40 术语 / 25 实体 / 22 关系 / 11 意图规则 / 10 流程 / 34 来源，version `2c4fbe8d4be2906c`；参与意图修正、槽位补齐、追问与跨场景规划 |
| 本轮七项整改 | ①R07 改为黄/红分级 + 强验证 ②人工接管队列（7 态、并发领取、安全恢复、审计）③生日预算真实占用可支配金额 ④生日计划一次性授权（绑定计划版本与清单）⑤沙箱条款按「消除执行面」说明并给静态证据 ⑥A/B 配对统计 + 浏览器旅程如实化 ⑦交付材料统一 |
| A/B 实验 | 8 轮完整 80 例真实模型（5 轮 MiniMax + 3 轮 DeepSeek，`mock=false`）。意图 B−A 逐轮为 −1.25/+2.50/0/+2.50/+5.00pp（MiniMax）、+5.00/−2.50/+8.75pp（DeepSeek），**方向反复变号**；只有 `20261024T160805` 的 +8.75pp CI[+2.50,+16.25] 不含 0，但单轮单次采样。**不宣称知识层稳定提升准确率**（§7） |
| 主动财务 | 储蓄目标 → 计算 → 监测 → 提醒 → 处理 → 重算闭环；生日计划从「书签式预算」升级为「真实预留 + 到期重裁 + 可选一次性授权」；后台节拍单进程、只推进显式开启的监测与到期计划 |
| 安全 | 红队 43/43；e2e 13/13；执行层未授权写 0；`app/` 代码执行面 0（静态证明 75 文件 / 25383 行） |
| 人工接管 | 本地操作员队列（不对接坐席系统）：`pending → claimed / need_info → resumed / rejected / cancelled / failed`，并发领取唯一、恢复前重查授权/确认卡/账户/余额，已完成资金节点不重放 |
| 端到端 | `scripts/e2e.py --offline` **13/13 PASS**；七条浏览器旅程单次运行 **7/7 PASS**（`docs/交付/browser_journeys.json` 起止时间同一窗口，演示库 sha256 未变） |
| 交付物 | 文档、知识文件、测试、评估报告、演示手册、PPT 均已按本轮事实更新；限制见 §8 |

---

## 2. 验收环境

| 项 | 值 |
|---|---|
| 工作目录 | `/home/peng/peng_workspace/deepseek harness/银行agent/banking-agent` |
| Python | 仓库内 `.venv`（3.12.13）；脚本类用系统 `python3.12` |
| 数据库 | `data/bank.db`，验收前执行 `.venv/bin/python -m scripts.seed --force` 重建 |
| 种子 | `tables=30`；320 笔流水（credit 37 / debit 283）；储蓄卡尾号 1234 余额 **12860.50 元**；5 个在订订阅；5 个收款人（含一对同名）；5 个理财产品 / 2 笔持仓；`random_seed=20261018` |
| 模型 | DeepSeek `deepseek-flash`（OpenAI 兼容），temperature 0.0，max_tokens 512；MiniMax-M3.1-Flash-Preview 为历史轮（2026-10-23 后 429，复测仍 429） |
| 知识层版本 | `2c4fbe8d4be2906c`（8 轮 A/B 中 DeepSeek 轮使用的版本；MiniMax 历史轮 `59ff01419aac0b29`） |
| 鉴权 | `X-API-Token`（`data/api_token`），路由级 fail-closed；页面入口用 `#token=`（fragment 不进服务端日志） |

---

## 3. 最终实测（本报告的唯一数字来源）

所有命令均在仓库根执行，全部 exit 0。

| # | 命令 | 结果 |
|---|---|---|
| 1 | `.venv/bin/python -m pytest -q tests/` | **1415 passed, 4 skipped, 1 warning**（196.86s，2026-10-24 20:10）；4 个 skip 是浏览器冒烟默认关闭 |
| 2 | `RUN_BROWSER_SMOKE=1 .venv/bin/python -m pytest tests/test_browser_smoke.py -q` | **4 passed**（固定模型） |
| 3 | `.venv/bin/python scripts/check_frontend.py` | **前端检查通过：66/66** |
| 4 | `.venv/bin/python -m scripts.redteam` | **43 条：PASS 43 / FAIL 0** |
| 5 | `.venv/bin/python -m scripts.e2e --offline` | **13 步：PASS 13 / FAIL 0** |
| 6 | `.venv/bin/python -m scripts.seed --force` | `tables=30`，320 笔流水，自检全 passed；知识库 version=`2c4fbe8d4be2906c` |
| 7 | `.venv/bin/python scripts/ui_journeys.py` | **7/7 PASS**，失败 0，`demo_db_unchanged=true`，10 张旅程截图 |

本轮新增的 8 个整改测试文件单独跑：`.venv/bin/python -m pytest -q tests/test_daily_risk_tiers.py tests/test_daily_upgrade_flow.py tests/test_handoff.py tests/test_birthday_budget.py tests/test_birthday_authorization.py tests/test_birthday_api_authorization.py tests/test_no_exec_surface.py tests/test_db_migration.py` → **79 passed**（14 / 1 / 13 / 15 / 15 / 4 / 14 / 3）。

补充规模数据（实测，不是文档抄写）：测试文件 **88** 个（`tests/test_*.py`；`tests/` 下 `.py` 共 93 个，含 5 个夹具/辅助）；`app/*.py` + `app/*/*.py` **75** 个；数据库表 **30** 张；工具 **35** 个；审计事件类型 **61** 类；策略规则 **13** 条；HTTP 路由 **39** 条（38 条 `/api/*` + 根路径 `/`）。

知识层补充数据：terms 40 / entities 25 / relations 22 / intents 11 / workflows 10 / sources 34 / warnings **5**（1 superseded + 4 unverified）；verified 29 / unverified 4 / superseded 1。

---

## 4. 本轮七项整改的逐项验收

### 4.1 任务一（P0）日累计 1000 元的黄/红分级

**问题**：R07 把「日累计超过 1000 元」当成硬拒绝且不给因子；`funds._bump_daily_limit()` 用 SQL 条件把累计锁死在 `limit_fen` 内。这与赛题「小额转账日累计 ≤ 1000 元黄色确认、> 1000 元红色人脸/短信/盾多因子」直接冲突。

**改动**：
- `app/policy/rules.py`：`DAILY_RISK_THRESHOLD_FEN=100_000`（只决定黄/红）、`ABSOLUTE_DAILY_CAP_FEN=None`（可选绝对上限，无依据不启用）、新增 R13 可选绝对上限规则（置于 R05 之后、R06 之前，否则不可达）；R07 改为红色 + `required_factors=('face','sms')`；日累计口径按**用户维度**；新增 `SYSTEM_SPEND_TOOLS={'scheduled_transfer'}`，`_DAILY_RULE_EXEMPT_TOOLS` 只保留创建期的 `schedule_transfer`（教训：把到点工具也豁免会让到期重裁永远黄灯）。
- `app/funds.py`：`spent_today_fen()` 读 `daily_user_limits`（用户维度，跨该用户多个账户合计），`account_spent_today_fen()` 读 `daily_limits`（账户账单视图）；结算事务内 `_assert_daily_tier()` 先查绝对上限、再查风险阈值——只有 `holds.risk_level='red'` 的 hold 才能跨过 1000 元线，黄 hold 越线抛 `DAILY_RISK_UPGRADE_REQUIRED`；`_bump_daily_counters()` 在**同一事务**更新两本账。
- `app/planner/executor.py`：新增 `_retry_after_upgrade()`——黄确认后因他人提交导致越线时，重新裁决 R07、审计 `policy.upgraded`、要求强验证，再执行；`_existing_confirmation()` 拒绝复用与当前裁决级别不符的旧确认卡。
- `app/tools/scheduled.py`：预约转账**到点**用当前日累计重新裁决；红且无有效因子 → `needs_verification=1` 并暂停（不再后台自动扣）；新增 `challenge_scheduled()` / `verify_and_execute()`，路由 `POST /api/scheduled/{id}/challenge|verify`；验证通过后一次性授权立即烧掉。
- `app/db.py`：新增 `daily_user_limits` 表；`plan_budgets` 增 `account_id`/`hold_id`；表数 27→30；`tests/test_db_migration.py` 3 条验证老库可加列。

**测试命令与结果**：`.venv/bin/python -m pytest -q tests/test_daily_risk_tiers.py tests/test_daily_upgrade_flow.py` → **15 passed**；`tests/test_policy.py` 45 条、`tests/test_funds_invariants.py` 11 条同批通过。

**验收点覆盖**（`tests/test_daily_risk_tiers.py`）：500→400 仍黄且两笔都结算；500→500 正好在线上仍黄；500→800 升级红且旧黄卡不能结算、重写确认卡后可结算；今日未转、单笔 >1000 直接红；红色无验证不可用、令牌只绑定该收款人/金额/账户；两个黄操作并发合计跨线时只有一个能提交、另一个必须重裁；因余额不足/策略失败回滚的操作不增加累计；预约创建时黄、到点执行时红。

**剩余风险**：绝对上限默认未启用，若评审要求一个硬性日封顶，需要业务给出依据后显式配置；用户维度累计依赖 `daily_user_limits`，历史库首次运行只补表不回填过去流水（当日数据从启用时刻开始）。

**对应赛题条款**：3.2 权限分级（黄/红、日累计口径）；3.3 复杂任务的中断与恢复（升级后不得沿用旧确认）。

### 4.2 任务二（P0）最小人工接管机制

**问题**：只有用户确认、任务取消、冲正与恢复，无法证明「任务可以转交人工」。

**改动**：新增 `app/handoffs.py` + `app/api/handoffs_api.py` + `handoffs` 表。
- 状态机：`pending / claimed / need_info / rejected / cancelled / resumed / failed`；同一 run 仅一张活跃单（DB 部分唯一索引）。
- 工单只搬运已落库事实：`run_id`、原始用户请求、DAG、已完成/失败节点、失败原因、裁决、授权状态。
- 操作员动作：列表（受保护，`require_api_token`）、领取、拒绝接管、要求补充信息、取消处理、恢复；领取用条件 UPDATE，只有一个赢家。
- 安全恢复：恢复前依次检查新轮次、确认卡是否过期、计划一次性授权是否过期、账户是否可用、可支配余额是否足够；不足/不可用返回 409（`HANDOFF_INSUFFICIENT_FUNDS` / `HANDOFF_ACCOUNT_UNAVAILABLE`）。
- 不重放写工具：交给 `app.planner.recover.resume` 按原图继续，执行器跳过 `done/failed/skipped`；恢复失败写 `failed` + note + `handoff.resume_failed`，绝不把 run 标成功。
- 操作员不能替用户点确认卡：缺的是用户决定时（`PAYEE_AMBIGUOUS` / `PAYEE_NOT_FOUND`）只能退回用户（`HANDOFF_NEEDS_USER_DECISION`）。
- 审计：`handoff.created/claimed/rejected/need_info/cancelled/resumed/resume_failed` 全部进同一条哈希链；用户回答澄清后旧单自动关闭（`closed_by=run.superseded`）。

**测试命令与结果**：`.venv/bin/python -m pytest -q tests/test_handoff.py` → **13 passed**。

**验收点覆盖**：模糊收款人生成 pending；服务重启后仍待处理；两操作员并发领取恰好一个赢家；拒绝后不能恢复；need_info 必须写备注；过期确认卡/过期计划授权不能恢复；恢复跳过已完成节点且只扣一次钱；失败节点不被报告为成功；收款人选择权留在用户；每个操作员动作都有审计事件；API 受保护且能列出待处理。

**剩余风险**：`operator` 只是审计里的字符串，没有 RBAC / 坐席账号体系；队列是进程内 + SQLite，没有分布式锁；不对接真实客服系统（按任务书要求）。

**对应赛题条款**：3.3「执行过程中支持中断、回退和人工接管」；3.2 操作审计。

### 4.3 任务三（P0）生日预算真正影响可支配金额

**问题**：`plan_budgets` 只是 `bookkeeping_reservation`——记录预算但不扣余额、不建冻结、不阻止其他交易动用这笔钱。

**改动**：`plan_budgets` 增 `account_id`/`hold_id`（老库自动加列）；`app/funds.py` 新增 `reserved_budget_fen()` / `held_fen()` / `available_fen()`，可支配金额 = 账面余额 − 在途 hold − 有效预算预留（`status='reserved'`；`paying` 不计，避免与 hold 重复占用）。`_create_hold()` 的余额校验读这个可支配金额，因此即时转账、预约出账、AA、商户付款、理财申购走同一层限制；`app/birthday.py` 建计划前校验账户能承担预留（不足 `BUDGET_NOT_COVERED` 整体回滚），取消/拒绝/失败释放预留，`reconcile()` 把「崩在支付中间」的行对回真实 hold 与流水；`app/api/business.py` 的余额视图与写操作读同一个 `funds.available_fen`。

**测试命令与结果**：`.venv/bin/python -m pytest -q tests/test_birthday_budget.py` → **15 passed**。

**验收点覆盖**：预留使可支配金额减少 1000 而账面余额不变；用这笔钱发起新转账被阻止；部分动用只按预留守住缺口；付款时不重复占用预算与 hold；付款后 hold/流水/审计对齐；拒绝释放；取消计划释放全部；服务重启仍保留预留；`reconcile` 各自恢复 interrupted/promoted/无凭证三种情形；并发领取同一预算项目只付一次；预算不足整个计划回滚；老库 `plan_budgets` 迁移后仍计数；边界文案与新语义一致。

**剩余风险**：这是系统自建的可用额度约束（不是银行侧真实预授权/商户冻结）；外部直接改库仍能绕过；`reconcile` 需后台节拍驱动。

**对应赛题条款**：场景 6 跨场景联动「当月锁定 1000 元活期」；3.2 资金安全。

### 4.4 任务四（P1）生日计划一次性预约授权

**改动**：新增 `plan_authorizations` 表（`plan_version`、`scope_json`、`decision_json`、`status ∈ active/used/revoked/expired`、`expires_ms`）。`app/birthday.py` 新增 `authorize_plan()` / `revoke_authorization()` / `expire_authorizations()` / `run_due_authorized()`：授权冻结当时的清单、金额、账户、窗口；到点先按最新日累计重新裁决，只有「绿灯或黄灯 + 参数完全未变 + 预算与账户仍够」才自动付；红/参数漂移/额度不足一律暂停计划待确认；用一次即 `used`，用户拒绝/取消/修改计划即作废；后台不得在取消后再启动付款。API：`POST /api/plans/birthday` 增加 `account_id` / `save_authorization`，`POST /api/plans/run` 先跑授权到期任务。

**测试命令与结果**：`.venv/bin/python -m pytest -q tests/test_birthday_authorization.py tests/test_birthday_api_authorization.py` → **19 passed**（15 + 4）。

**验收点覆盖**：授权带冻结范围落库；不勾选则保持手工；窗口前不执行；窗口内只付一次并标 `used`；重启不重复付；红灯暂停不扣款；用户拒绝作废；修改计划作废；取消计划作废并释放；用户暂停与后台暂停区分；可支配不足先暂停；过期窗口标 `expired`；范围漂移暂停并作废；`reconcile` 撤掉死计划的授权；ticker 走授权路径且保留既有幂等键。

**剩余风险**：默认路径仍是「到期提醒 + 用户确认」（安全限制，已明确标注）；一次性授权不是「无限授权未来所有扣款」，参数变化必须重新确认。

**对应赛题条款**：3.3 复杂任务的延后执行与中断恢复；3.2 授权边界。

### 4.5 任务五（P1）沙箱要求的合规说明与安全验证

**审查结论**：主 Agent 不执行模型生成的代码。工具是静态白名单函数（`app/tools/*.py` 注册表），任务由结构化 DAG 节点驱动；`app/` 下无 `eval`/`exec`/`compile`、无 shell 调用、无把模型输出当脚本执行的路径；13 处动态 SQL 只拼接表名/列名常量，取值一律参数化；出网只在 `app/llm.py`（模型 API）。

**证据**：`docs/security_sandbox.md`（149 行，逐跳调用链 file:line + 与赛题 3.2 的对应表 + 残余风险）；`scripts/prove_no_exec_surface.py` 实测 75 文件 / 25383 行、代码执行面 **0**、出网 3 处（`app/llm.py:7/:297/:303`）、动态 SQL 13 处全人工复核、离线命令 9 条、自检 15/15；`tests/test_no_exec_surface.py` **14 passed**，覆盖：扫描覆盖 `app/` 全部模块、零执行面、探测器能发现植入违规、不会误报良性近似、出网只在 LLM 模块、目标缺失时显式失败、证明脚本可跑、两份文档口径一致、工具处理函数为静态模块函数、注册表只含白名单工具、API schema 无代码字符串字段、动态 SQL 有报告与说明、离线脚本清单、文档覆盖 3.2。

**边界**：这是「消除执行面」架构，不是容器沙箱；不宣称部署了沙箱。若评审必须看运行时代码隔离，需要另建独立、无网络、无资金库访问的模拟计算环境（当前未做，已如实记录）。

**对应赛题条款**：3.2「生成的代码在受限、可监控的逻辑隔离环境中运行」——本项目的回答是：不生成也不执行代码。

### 4.6 任务六（P1）A/B 评估与浏览器回归

**A/B**：`scripts/ab_stats.py`（803 行）从 `tests/eval/results/` 原始 JSONL 复算，逐轮与 `summary.json` 对齐；纳入 8 轮完整 80 例（5 MiniMax + 3 DeepSeek），按 case id 配对，10000 次配对 bootstrap（seed 20261024），区分 60 开发 / 20 留出。结论与限制见 §7 与 `docs/benchmark/ab_paired_stats.md`。**未补跑新轮次**（配额/网络不足），未使用 MOCK_MODE。

**浏览器**：七条旅程全部由当前 `scripts/ui_journeys.py` 定义并执行；`j1/j2/j4/j5/j6/j7` 共用 `MOCK_MODE=script` 后端，`j3` 单独以真模型（`MOCK_MODE=false`）后端跑，起止时间在同一窗口（20:04:46–20:06:03），演示库 sha256 前后一致；J6/J7 点击级，J4/J5 是 API 驱动流程级（文档已如实标注）；J3 歧义拦截现在会核验服务端状态，解析不到 session 直接判失败而不是静默通过。截图/旅程 JSON/清单来自同一次运行。

**对应赛题条款**：评测证据与演示可复现性；不夸大 A/B 结论。

### 4.7 任务七（P0）统一交付材料

本轮统一了：`README.md`、`docs/benchmark/release_manifest.json`、`docs/benchmark/final_acceptance_report.md`（本文件）、`docs/benchmark/implementation_priorities.md`、`docs/benchmark/air_vs_banking_agent.md`、`docs/交付/browser_journeys.json`、`docs/提交物对照表.md`、`PROGRESS.md`、答辩 PPT 大纲与演示手册。统一的事实包括：最终 Git SHA 与生成时间、测试通过/失败/跳过数、工具数、真实表数、旅程数与执行状态、R07 黄红语义、生日预算是否影响可支配金额、预约是否需到期重新确认、人工接管范围、沙箱设计、A/B 结论与统计限制、真实 MFA 未接入。旧数字保留并标为历史基线（§11）。

---

## 5. 逐阶段验收状态

状态定义：**已完成并通过验证** / **已完成但测试不足** / **部分实现** / **尚未实现** / **环境受限无法验证**。

| 阶段 | 交付物 | 状态 | 依据 / 说明 |
|---|---|---|---|
| 0 | `docs/benchmark/revolut_air_research.md`、`air_vs_banking_agent.md`、`implementation_priorities.md` | 已完成并通过验证 | 3 个官方入口实读；11 维能力矩阵；本轮更新 G15 为「已实现收窄版」并新增 §1.6 落地状态 |
| 1 | `docs/改造实施基线.md` | 已完成并通过验证 | 改造前快照（904 passed / 26 工具 / 18 表 / 21 事件），标注历史 |
| 2 | `docs/银行业务知识来源清单.md` + `knowledge/sources.yaml` | 部分实现（受公开资料限制） | 34 条登记；4 条 unverified、1 条 superseded 如实标注 |
| 3 | `app/knowledge/` + `knowledge/*.yaml` + `app/planner/kb_bridge.py` | 已完成并通过验证 | 加载/校验/规范化/检索/降级有单测；接入 `intent.parse()`、`dag.build_or_single(cross=…)`、`routes._audit_knowledge`；`KNOWLEDGE_ENABLED` 一键开关 |
| 4 | `tests/eval/`（80 例、runner、报告、配对统计） | 部分实现 | 8 轮真实配对统计；方向反复，不宣称提升；未补跑 |
| 5 | `app/proactive.py`、`app/tools/proactive.py`、7 个工具、6 个 API + 模拟支出端点、前端面板 | 部分实现 | 监测闭环完整；后台节拍单进程、默认 30 秒、只推进显式开启目标；只写提醒不动钱 |
| 6 | `docs/14_安全回归报告.md`、`tests/test_security_*.py`、`tests/test_payment_guard.py` | 已完成并通过验证 | 红队 43/43；知识层 13 条、主动服务 11 条、guard 44 条；执行层未授权写 0 |
| 7 | 本报告、`docs/15_竞赛总报告.md`、`docs/13_竞赛演示手册.md`、README、`docs/提交物对照表.md`、PPT | 已完成并通过验证 | 本轮重新生成；数字统一为 1415/4、43/43、13/13、7/7、30 表/35 工具/61 事件/13 规则/39 路由/88 测试文件（`tests/test_*.py`）；未 push 已声明 |

### 六大赛场验收

| 场景 | 状态 | 证据 |
|---|---|---|
| 智能转账 | 已完成并通过验证 | e2e 第 7 步：红灯 R07 + face/sms 双因子 + 金额篡改被拒；第 8 步注入备注里的 5000/张三未被执行，只转 500；日累计按用户维度；并发越线必须重裁 |
| 账单分析 | 已完成并通过验证 | e2e 直读与聊天双口径；320 笔种子、年度支出 87,821.00 元、异常 2 条（split_orders / subscription_hike） |
| 理财操作 | 部分实现（边界缺失） | 申购/赎回/持仓查询与风险提示是真实工具；**无个性化推荐/横向比较**，不把「列产品」说成「智能推荐」 |
| 卡片管理 | 已完成并通过验证 | `list_cards`/`set_card_limit` 等 10 个卡片工具；敏感操作走 `SENSITIVE_TOOLS` 确认 |
| 订阅/代扣 | 已完成并通过验证 | e2e 第 5 步取消订阅：yellow/R05_confirm_only → 确认 → 真取消（在订 5→4） |
| 跨场景联动 | 已完成并通过验证（本轮升级） | 生日计划持久化在 `plans`/`plan_steps`/`plan_budgets`；预算预留真实占用可支配金额；生日前两天由 ticker 触发待确认付款；可选一次性授权（绑定计划版本）到点重裁后自动付；重启后计划与预留仍在；限制：默认仍需用户确认，红/参数漂移必须重新验证 |

---

## 6. 安全边界核验

1. **权限裁决本轮有受控变更**：R07 由「硬拒绝」改为「红色 + face/sms」，新增可选 R13 绝对上限；13 条规则顺序与文案由 `tests/test_policy.py` 45 条 + `tests/test_daily_risk_tiers.py` 14 条固定。**不变的是边界**：知识层、主动服务、`payment_guard` 不参与裁决——`tests/test_knowledge_integration.py` 仍有「开/关知识层，裁决结果一致」的对照；`payment_guard` 只写 `question/ok/guard_reason`，不签发令牌、不改策略。
2. **资金执行层原子性**：hold 创建时按 `available_fen`（余额 − hold − 预算预留）校验；结算在单事务内 `_assert_daily_tier()` → 扣款 `UPDATE … WHERE balance_fen >= ?` → 写流水 → 双账本累计 → 审计；黄 hold 越线抛 `DAILY_RISK_UPGRADE_REQUIRED`，绝不放行。
3. **能力令牌不可复制**：令牌绑定 session/node/参数哈希/nonce；红色令牌额外绑定收款人、金额、账户（`test_red_token_binds_the_exact_payee_amount_and_account`）；重试走 nonce 派生幂等键，重复提交只结算一次。
4. **红队 43/43**：诱导转账 10、越权操作 10、提示注入 10、信息套取 8、异常与稳定性 5。
5. **知识层攻击 13 条**（`tests/test_security_knowledge.py`）：检索文本含「忽略安全规则」、备注要求跳过验证、冒充管理员、外部文本要求直发写操作、知识条目冲突、来源过期/删除、模型返回非法结构、检索超时——预期均为拒绝越权 / 保留原规则 / 追问 / 降级。
6. **主动服务边界 11 条**（`tests/test_security_proactive.py`）：无历史、现金流不足、目标过期、重复交易、重复提醒、监测失败、关闭提醒、修改目标、请求未授权转账、恶意交易数据——不产生任何资金写入。
7. **原话层 guard 44 条**（`tests/test_payment_guard.py`）：多人/多金额/AA/否定/无关「和」/引号转述/一句两笔/口语变体，命中只追问不建图。
8. **执行层未授权写操作 = 0**：e2e 第 13 步恶意模型零工具调用、余额零变化；本轮新增接管恢复同样不重放已完成的写节点。
9. **审计链**：e2e 第 11 步 16 条事件 `chain_ok=True`，UPDATE 被数据库触发器拒绝；新增 `policy.upgraded`、`scheduled.needs_verification/verified/verification_failed/verification_waived`、`handoff.*` 全部进同一哈希链。准确边界是「应用层只追加 + 链可验证」，不是对拥有数据库文件写权限者的绝对不可篡改（见 `docs/安全自评报告.md` R-04）。
10. **MFA 挑战不能由客户端自称通过**：挑战由服务端签发、只存哈希、一次性使用、最多 5 次尝试；生产模式下未声明真实渠道直接 503 fail-closed。演示模式回显验证码是为了现场可演示，已如实标注。

---

## 7. A/B 实验结论（8 轮配对统计与口径）

数据来源：`tests/eval/results/` 原始 JSONL（未改写），复算脚本 `scripts/ab_stats.py`，明细 `docs/benchmark/ab_paired_stats.md`。8 轮完整 80 例、`mock=false`。

### 7.1 意图 B−A（配对，80 例）

| 轮次 | 模型 | A | B | B−A | 95% CI | 60 dev | 20 holdout |
|---|---|---|---|---|---|---|---|
| 20261023T185338 | MiniMax | 86.25% | 85.00% | −1.25pp | [−10.00, +6.25] | −1.67pp | +0.00pp |
| 20261023T191048 | MiniMax | 86.25% | 88.75% | +2.50pp | [−3.75, +8.75] | +5.00pp | −5.00pp |
| 20261023T192756 | MiniMax | 85.00% | 85.00% | +0.00pp | [−6.25, +6.25] | +0.00pp | +0.00pp |
| 20261023T194022 | MiniMax | 82.50% | 85.00% | +2.50pp | [−3.75, +8.75] | +3.33pp | +0.00pp |
| 20261023T210056 | MiniMax | 83.75% | 88.75% | +5.00pp | [+0.00, +11.25] | +5.00pp | +5.00pp |
| 20261024T134524 | deepseek-flash | 85.00% | 90.00% | +5.00pp | [−2.50, +12.50] | +8.33pp | −5.00pp |
| 20261024T135754 | deepseek-flash | 87.50% | 85.00% | −2.50pp | [−10.00, +3.75] | +0.00pp | −10.00pp |
| 20261024T160805 | deepseek-flash | 82.50% | 91.25% | +8.75pp | [+2.50, +16.25] | +6.67pp | +15.00pp |

槽位同向：+1.37 / +4.11 / +1.37 / +8.22 / +5.48 / +5.48 / −2.74 / +6.85pp；追问：−5.00 / +3.75 / +5.00 / +3.75 / −2.50 / +2.50 / +1.25 / −2.50pp。

### 7.2 能宣称与不能宣称

- **不能**宣称知识层稳定提升准确率：8 轮里 B−A 符号翻转；只有 `20261024T160805` 的意图 CI 不含 0，且是单轮单次采样。
- **不能**把 bootstrap CI 当模型稳定性区间：它只重采样 case。同配置复跑的点估计抖动与差值同量级——MiniMax 意图 B−A 极差 **6.25pp**，DeepSeek **11.25pp**。
- **可以**宣称：B 组确实产生了知识引用（41–42/51，A 组恒为 0）；全部纳入轮次 `mock=false`；未追问即动钱的每个 case 可在原始 JSONL 逐条回溯（但该计数历史口径变过，不能跨轮比较，见统计文档 §5.1）。
- 未补跑新轮次：配额与网络不足，已在 `ab_paired_stats.md` §7 明确声明，未使用 MOCK_MODE 生成任何结果。

---

## 8. 已知限制与风险（如实列出）

1. **真实 MFA 渠道未接入**：短信/人脸是服务端挑战 + 演示回显，没有真实网关与活体服务；关闭 `FACTOR_DEMO_ECHO` 且未声明 `FACTOR_PROVIDER` 时 503 fail-closed。
2. **人工接管没有 RBAC**：`operator` 只是审计字段，没有坐席账号、权限分级与工单分配；队列是进程内 + SQLite，不是分布式任务系统。
3. **沙箱是「消除执行面」而非容器**：`app/` 无代码执行、出网仅 LLM；不宣称部署了运行时沙箱，残余风险见 `docs/security_sandbox.md`。
4. **预算预留是系统内约束**：不是银行侧预授权，不改账面余额，外部直接改库可绕过。
5. **A/B 单次采样**：8 轮都是每组一次，无重复采样方差；结论只到「方向不稳、引用确实发生」。
6. **未授权写不是跨轮稳定 0**：历史 MiniMax 轮 A=4/B=0，DeepSeek r1 两组各 1 例（已修并补测试），r2/r3 为 0；guard 只覆盖已识别表达，不宣称「保证为零」。
7. **澄清率方向也不一致**：不宣称提升追问率。
8. **知识引用 41–42/51 的漏项是 `TERM-*` 粒度**：`normalize()` 只做字面别名，`retrieve()` 返回规则/工作流粒度；不改标准答案。
9. **后台节拍是单进程 asyncio**：进程退出即停，多 worker 未验证；界面区分「计划已保存」与「任务已安排」，不说成 7×24 服务。
10. **无真实银行接入**：全部模拟数据，符合任务书要求。
11. **模型单点与非确定性**：只测两个模型、无第三方交叉验证；温度 0.0 仍抖。
12. **来源核验限制**：4 条 unverified、1 条被新规取代；未补成「看似合理」的规则。
13. **`scripts/e2e.py` 会删库重建** `data/bank.db`；演示前需 `make reseed`。
14. **4 个默认 skip**：浏览器冒烟默认关闭，已单独以 `RUN_BROWSER_SMOKE=1` 验证 4 passed。
15. **审计链边界**：应用层只追加 + 链可验证；拿到数据库文件写权限者可重建整条链。
16. **MiniMax 429**：历史轮保留，不与 DeepSeek 轮混算；DeepSeek 是单一厂商。
17. **未做 RAG/向量库对照**：结构化匹配已够用，刻意不引入。

---

## 9. 从干净环境复现

```bash
cd banking-agent
python3.12 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env            # 填 LLM key；不填则 MOCK_MODE=1 离线演示
python3.12 -m scripts.seed --force
python3.12 -m uvicorn app.main:app --port 8000
# 浏览器打开启动日志打印的 http://127.0.0.1:8000/#token=…
```

验证与演示：

```bash
make test                                    # 通过/失败/跳过数见 release_manifest.json
python3.12 scripts/check_frontend.py         # 66/66
python3.12 -m scripts.redteam                # 43/43
python3.12 -m scripts.e2e                    # 13/13（会删库重建，跑完 make reseed）
python3.12 scripts/ui_journeys.py            # 7/7（真浏览器；需已起服务）
python3.12 scripts/prove_no_exec_surface.py  # 代码执行面 0 的静态证明
python3.12 scripts/ab_stats.py --results tests/eval/results --iters 10000 --seed 20261024
```

三段演示的逐步脚本见 `docs/13_竞赛演示手册.md`（演示 A 转账/歧义/权限变化，演示 B 知识增强与歧义追问，演示 C 储蓄目标闭环 + 生日预算与一次性授权）。

---

## 10. 环境受限与无法验证项

| 项 | 情况 |
|---|---|
| Revolut AIR 内部架构 | 官方未公开模型、工具链、知识库实现；一律标「未核实/合理推断」 |
| 部分银行页面 | 需登录或改版，6 条来源保持 `unverified` |
| 监管文件逐条比对 | 已核验发布机构、文号、生效状态与关键条款；未做法律意见级逐字解释 |
| 全天候主动服务 | 进程内节拍（预约转账 / 开启监测目标 / 到期计划）；单进程、多 worker 未验证 |
| 真实资金链路 | 按任务书全部模拟 |
| 真实 MFA 渠道 | 未接短信网关与人脸活体；演示回显，生产模式 fail-closed |
| 代码沙箱 | 未部署运行时容器；采用「消除执行面」设计并给静态证据 |
| 真实模型复跑 | MiniMax 仍 429；本轮未补跑新 A/B 轮次 |

---

## 11. 历史基线（保留，不再作为当前事实）

| 指标 | 历史基线（2026-10-23 / 上一轮） | 本轮实测（2026-10-24） |
|---|---|---|
| pytest | 1336 passed / 4 skipped | **1415 passed / 4 skipped** |
| 工具 | 33 | **35** |
| 数据表 | 22 | **30** |
| 审计事件类型 | 29 | **61** |
| 策略规则 | 12 | **13** |
| 测试文件 | 62 | **88**（`tests/test_*.py`；`tests/` 下 `.py` 共 93，含夹具/辅助） |
| HTTP 路由 | 18 | **39**（含根路径） |
| 浏览器旅程 | 5 条（后补到 7 条） | **7 条，单次运行 7/7** |

> 历史数字仅用于说明演进，不与本轮数字混算；`release_manifest.json` 只记录本轮一次干净运行的实测值。
