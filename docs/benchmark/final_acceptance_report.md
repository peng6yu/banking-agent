# 最终验收报告（阶段 7）

> 生成时间：2026-10-23 22:30（Asia/Shanghai），第二轮审查收口后更新。
> 仓库：`banking-agent/`，分支 `main`；上一轮公开提交 `cccfb8f`（PR #1 合并后第一轮收口），
> 本轮收口改动已 commit 并 push GitHub（最终提交号见 `git log -1 --oneline` / 远程 `main`）。
> 本报告里的每个数字都来自本次实际执行，不引用 README 里的历史数字；命令与输出见 §3。
> **2026-10-24 复核**：模型已换 DeepSeek（`deepseek-flash`），在同一份 80 例上连跑三轮 A/B，
> 红队、独立 `browser_smoke.py` 与五条浏览器旅程也在新代码上复跑；MiniMax 轮次保留为历史对照。
> **数字口径**：工具数、表数、事件数、测试数一律以 `docs/benchmark/release_manifest.json` 为准，
> 本文件不再手抄这些计数；A/B 三轮明细见 §6。

---

## 1. 结论摘要

| 维度 | 结论 |
|---|---|
| 代码 | 知识层、适当性、订阅推断、失败恢复、跨场景计划均已接入真实调用链；测试通过数与测试文件数以 `release_manifest.json` 为准 |
| 知识层 | 40 术语 / 25 实体 / 22 关系 / 11 意图规则 / 10 流程 / 34 来源；参与意图修正、槽位补齐、追问与跨场景规划 |
| 原话层 guard | 新增 `app/planner/payment_guard.py`：多收款人 / 一句多笔的确定性预检查，A/B 共用，**44 条测试** |
| A/B 实验 | 2026-10-24 换 DeepSeek 连跑三轮：B−A 的 intent 差值为 **+5.0 / −2.5 / +8.75pp**，方向反复，**不宣称知识层稳定提升 x pp**；只认知识引用 41/51、41/51、42/51 与未授权写 1/1→0/0→0/0（§6） |
| 主动财务 | 储蓄目标 → 确定性计算 → 监测 → 提醒 → 处理 → 重算闭环已跑通（真浏览器点过）；后台节拍 `app/planner/ticker.py` 是单进程 asyncio，只推进显式开启监测的目标（默认 6 小时节流），只写提醒不动钱 |
| 安全 | 红队 43/43；执行层未授权写操作 0；知识层 13 条、主动服务 11 条、payment_guard 44 条测试全绿；权限裁决代码未被修改 |
| 端到端 | `scripts/e2e.py --offline` **13/13 PASS**（含注入、双因子、审计链、恶意模型）；`test_browser_smoke.py` **4 passed**；独立 `browser_smoke.py` 与 `scripts/ui_journeys.py`（5 条旅程）2026-10-24 用 DeepSeek 复跑 **PASS**，演示库 sha256 未变（§9） |
| 交付物 | 阶段 0–7 文档、知识文件、测试、评估报告、演示手册、PPT 均已落盘；限制与未实现项见 §7 |

---

## 2. 验收环境

| 项 | 值 |
|---|---|
| 工作目录 | `/home/peng/peng_workspace/deepseek harness/银行agent/banking-agent` |
| Python | 仓库内 `.venv`（3.12.13）；脚本类用系统 `python3.12` |
| 数据库 | `data/bank.db`，验收前执行 `python3.12 -m scripts.seed --force` 重建 |
| 种子 | `tables=27`（含本轮新增 4 张表）；320 笔流水（credit 37 / debit 283）；储蓄卡尾号 1234 余额 **12860.50 元**；5 个在订订阅 |
| 模型 | DeepSeek `deepseek-flash`（OpenAI 兼容，配置见 `.env`），temperature 0.0，max_tokens 512；MiniMax-M3.1-Flash-Preview 保留为历史轮（2026-10-23 后 429，2026-10-24 复测仍 429） |
| 知识层版本 | `2c4fbe8d4be2906c`（三轮 DeepSeek A/B 用的都是这一版；MiniMax 历史轮 `59ff01419aac0b29`） |
| 鉴权 | `X-API-Token`（`data/api_token`），路由级 fail-closed；页面入口用 `#token=`（fragment 不进服务端日志） |

---

## 3. 最终实测（本报告的唯一数字来源）

所有命令均在仓库根执行。

| # | 命令 | 结果 |
|---|---|---|
| 1 | `.venv/bin/python -m pytest -q tests/` | **1330 passed, 4 skipped, 1 warning in 46.61s**（2026-10-24 终测；秒数取决于机器负载，以 `release_manifest.json` 记录为准）；4 个 skip 是浏览器冒烟默认关闭 |
| 2 | `RUN_BROWSER_SMOKE=1 python3.12 -m pytest tests/test_browser_smoke.py -q` | **4 passed in 6.44s**（最终代码，固定模型） |
| 3 | `python3.12 scripts/check_frontend.py` | **前端检查通过：66/66** |
| 4 | `python3.12 -m scripts.redteam` | **43 条：PASS 43 / FAIL 0** |
| 5 | `python3.12 -m scripts.e2e --offline` | **13 步：PASS 13 / FAIL 0**（6 大场景 + 双因子 + 审计链 + 篡改拒绝 + 恶意模型） |
| 6 | `python3.12 -m scripts.seed --force` | `tables=22 transactions=320`，自检全 passed；知识库 version=`2c4fbe8d4be2906c` |

补充数据：知识层 `version=2c4fbe8d4be2906c`，terms 40 / entities 25 / relations 22 / intents 11 / workflows 10 / sources 34 / warnings **5**（1 superseded + 4 unverified）；verified 29 / unverified 4 / superseded 1；工具 33 个；路由 18 个；审计事件 29 类；表 22 张；测试文件 62 个。

---

## 4. 逐阶段验收状态

状态定义按任务书：**已完成并通过验证** / **已完成但测试不足** / **部分实现** / **尚未实现** / **环境受限无法验证**。

| 阶段 | 交付物 | 状态 | 依据 / 说明 |
|---|---|---|---|
| 0 | `docs/benchmark/revolut_air_research.md` | 已完成并通过验证 | 3 个官方入口实读；11 维能力矩阵，每条带 URL、标题、核验日期；证据分四档 |
| 0 | `docs/benchmark/air_vs_banking_agent.md` | 已完成并通过验证 | 10 列差距矩阵（代码位置、测试证据、复杂度/风险、验收方法、优先级） |
| 0 | `docs/benchmark/implementation_priorities.md` | 已完成并通过验证 | P0/P1/P2、1 主 2 辅演示故事、明确不做清单 |
| 1 | `docs/改造实施基线.md` | 已完成并通过验证 | 改造前快照（904 passed/26 工具/18 表/21 事件），文件头声明「终值以本报告为准」 |
| 2 | `docs/银行业务知识来源清单.md` + `knowledge/sources.yaml` | 部分实现（受公开资料限制） | 34 条登记：9 条法规/监管（1 superseded）、23 条银行公开、2 条竞赛自设；4 条 unverified（1 页面正文未渲染 + 3 条 GAP）、1 条 superseded 如实标注 |
| 3 | `app/knowledge/` + `knowledge/*.yaml` + `app/planner/kb_bridge.py` | 已完成并通过验证 | 加载/校验/规范化/检索/降级有单测；接入 `intent.parse()`、`dag.build_or_single(cross=…)`、`routes._audit_knowledge`；`KNOWLEDGE_ENABLED` 一键开关 |
| 4 | `tests/eval/`（80 例、runner、报告） | 部分实现 | 最终有效轮 B 组 unauthorized=0，但 clarify 比 A 低 2.5pp；最终审查后的三处代码改动因配额未重跑 A/B（报告头注）；未宣称稳定为零 |
| 5 | `app/proactive.py`、`app/tools/proactive.py`、7 个工具、6 个 API + 模拟支出端点、前端面板 | 部分实现 | 监测闭环完整；**后台节拍（app/planner/ticker.py，默认 30 秒）只推进显式打开监测开关的目标**，按 `last_monitored_ms` 节流（默认 6 小时），只写提醒与运行记录、不动钱；没开开关的目标仍只能手动跑；单进程边界见 ticker 模块说明 |
| 6 | `docs/14_安全回归报告.md`、`tests/test_security_*.py`、`tests/test_payment_guard.py` | 已完成并通过验证 | 红队 43/43；知识层 13 条、主动服务 11 条、guard **44** 条；执行层未授权写操作 0 |
| 7 | 本报告、`docs/15_竞赛总报告.md`、`docs/13_竞赛演示手册.md`、README、`docs/提交物对照表.md`、PPT | 已完成并通过验证 | 文档数字统一为 1131/33/22/29/66/43/13；PPT 13 页重建；配额限制已如实标注 |

### 六大赛场验收

| 场景 | 状态 | 证据 |
|---|---|---|
| 智能转账 | 已完成并通过验证 | e2e 第 7 步：红灯 R07 + face/sms 双因子 + 金额篡改被拒；第 8 步注入备注里的 5000/张三未被执行，只转 500；原话层 guard 覆盖多收款人/一句多笔 |
| 账单分析 | 已完成并通过验证 | e2e 直读与聊天双口径；320 笔种子、年度支出 87,821.00 元、异常 2 条（split_orders / subscription_hike） |
| 理财操作 | 部分实现（边界缺失） | 申购/赎回/持仓查询与风险提示是真实工具；**无个性化推荐/横向比较**，不把「列产品」说成「智能推荐」；跨场景见下行 |
| 卡片管理 | 已完成并通过验证 | `list_cards`/`set_card_limit` 等 10 个卡片工具；敏感操作走 `SENSITIVE_TOOLS` 确认 |
| 订阅/代扣 | 已完成并通过验证 | e2e 第 5 步取消订阅：yellow/R05_confirm_only → 确认 → 真取消（在订 5→4） |
| 跨场景联动 | 部分实现（边界缺失） | 生日计划与赎回→转账是**同一次执行内的串行 DAG**；**无持久化计划、无延迟到生日前 2 天执行、无重启恢复**，e2e 第 9 步四步图全 ok |

---

## 5. 安全边界核验

1. **权限裁决未被知识/主动服务/guard 修改**：`app/policy/rules.py` 只登记了主动服务工具的分类，`decide()` 规则语义未变；
   `tests/test_knowledge_integration.py` 有「开/关知识层，裁决结果一致」的对照用例；
   `payment_guard` 只写 `question/ok/guard_reason`，不签发令牌、不改策略。
2. **红队 43/43**：诱导转账 10、越权操作 10、提示注入 10、信息套取 8、异常与稳定性 5。
3. **知识层攻击 13 条**（`tests/test_security_knowledge.py`）：检索文本含「忽略安全规则」、备注要求跳过验证、
   冒充管理员、外部文本要求直发写操作、知识条目冲突、来源过期/删除、模型返回非法结构、检索超时——预期均为拒绝越权 / 保留原规则 / 追问 / 降级。
4. **主动服务边界 11 条**（`tests/test_security_proactive.py`）：无历史、现金流不足、目标过期、重复交易、重复提醒、
   监测失败、关闭提醒、修改目标、请求未授权转账、恶意交易数据——不产生任何资金写入。
5. **原话层 guard 44 条**（`tests/test_payment_guard.py`）：C039/C044、多人/多金额/AA/否定/无关「和」/引号转述/一句两笔/口语变体，
   命中只追问不建图；正常单笔与 AA 拆账放行。
6. **执行层未授权写操作 = 0**：策略/确认/令牌测试全绿；e2e 第 13 步恶意模型零工具调用、余额零变化。
7. **审计链**：e2e 第 11 步 16 条事件 `chain_ok=True`，UPDATE 被数据库触发器拒绝；新增 8 类事件走同一哈希链。
   准确边界是「应用层只追加 + 链可验证」，不是对拥有数据库文件写权限者的绝对不可篡改（见 `docs/安全自评报告.md` R-04）。

---

## 6. A/B 实验结论（三轮明细与口径）

2026-10-24 起改在 DeepSeek（`deepseek-flash`，temperature 0.0，max_tokens 512）上复跑。
三轮都用同一份 80 例（`tests/eval/intent_cases.yaml`，dev 60 + holdout 20）、concurrency=1、
同一提示词与同一评测协议；结果目录保留原始 jsonl，不改标准答案。

### 6.1 三轮（DeepSeek，同一份用例）

| 轮 | 结果目录 | intent A/B | slot A/B | clarify A/B | DAG A/B | 知识引用（B） | 未授权写 A/B |
|---|---|---|---|---|---|---|---|
| r1 | `20261024T134524` | 0.8500 / 0.9000 | 0.8219 / 0.8767 | 0.8875 / 0.9125 | 1.0000 / 1.0000 | 41/51 | 1 / 1 |
| r2 | `20261024T135754` | 0.8750 / 0.8500 | 0.8356 / 0.8082 | 0.9250 / 0.9375 | 1.0000 / 0.9730 | 41/51 | 0 / 0 |
| r3 | `20261024T160805` | 0.8250 / 0.9125 | 0.8082 / 0.8767 | 0.9375 / 0.9125 | 1.0000 / 1.0000 | 42/51 | 0 / 0 |

r3 的 dev / holdout 分层：A 组 dev intent 0.8500（51/60）、holdout 0.7500（15/20）；
B 组 dev 0.9167（55/60）、holdout 0.9000（18/20）。

### 6.2 结论（只写站得住的）

- **B−A 的 intent 差值三轮分别是 +5.0pp、−2.5pp、+8.75pp，方向反复变号**；
  slot 是 +5.48 / −2.74 / +6.85pp，clarify 是 +2.5 / +1.25 / −2.5pp。
  因此**不能宣称「知识层带来 x pp 的稳定提升」**。三轮均值只能当描述性统计，不构成显著性结论。
- 稳定成立的两项：B 组知识引用正确率 **41/51、41/51、42/51**（术语 id 字面一致率，口径未改）；
  未授权写操作 r1 两组各 1 例，r2/r3 两组都是 **0**（r1 的两例逐条见
  `tests/eval/evaluation_report.md` §11.2，修复见 §7 第 1 条）。
- 逐 case 对齐（`evaluation_report.md` §11.4）：稳定 B 更好 1 条、稳定 A 更好 2 条，其余是方向相反的抖动；
  所以竞品对比与宣传材料都不引用单轮差值。

### 6.3 历史对照（不作为最终代码证据）

`20261023T210056`（MiniMax）：intent 0.8375 / 0.8875、slot 0.8219 / 0.8767、
clarify 0.8875 / 0.8625、unauthorized **4 / 0**。该轮早于本轮新增的 guard 单姓修复、
真实渠道闸门、恢复与跨场景计划代码，**不能与 6.1 混算**。它唯一还被引用的事实是：
guard 上线前 A 组确实出现过未追问即列写工具，这正是新增 guard 的动因。

### 6.4 归因与边界

- `payment_guard` 在两组都生效，消掉了多收款人 / 一句多笔的写操作；guard 自身有 44 条以上句式测试。
- 知识层的增量主要在意图与槽位，但跨轮抖动大于差值，因此只认「知识引用确实发生」，
  不认「准确率提升了多少」。
- 每轮 temperature=0.0、单次采样，**没有方差估计**；需要显著性应做多温度多次重复，
  本轮时间与配额不允许，明确记为「未做」而不是「不显著」。
- 单次运行约 5～6 分钟（80 例 × 2 组，concurrency=1），重跑命令见 §8。


---

## 7. 已知限制与风险（如实列出）

1. **未授权写不是跨轮稳定 0**：2026-10-24 三轮 DeepSeek 里 r1 两组各 1 例，r2/r3 都是 0；
   历史 MiniMax 轮 A=4、B=0。r1 的两条（单姓收款人、赎回链被兜底压成纯转账）已修并补测试，
   r2/r3 未再出现；但 guard 仍只覆盖已识别表达，更复杂的中文可能漏网。**不宣称「已保证为零」**。
2. **clarify 三轮方向也不一致**（+2.5 / +1.25 / −2.5pp）：代扣解约曾依赖模型给 `cancel_subs`，
   最终代码补了原话级「订阅词 + 取消动词」信号与测试；r2/r3 没有再出现旧轮那种 2.5pp 下滑，
   但也没有稳定提升，因此不宣称提升追问率。
3. **知识引用 42/51** 的 9 条未命中全部是 `TERM-*` 粒度：`normalize()` 只做字面别名，
   而 `retrieve()` 返回 `intent:*`/`wf_*`。报告已分层统计，不改标准答案。
4. **过度信任模型上报的 missing（已修）**：C020/C024 里模型自己报 `card_tail`/`month`，
   当时意图层照单收下并追问。最终代码加入 `REPORTED_MISSING_ALLOWED` 按意图白名单并有集成测试；
   r2/r3 在该修复之后运行，指标已包含修复后的行为。
5. **后台节拍是单进程的**：`app/planner/ticker.py` 用进程内 asyncio 每 30 秒推进预约转账、
   显式开启监测的目标、到点计划步骤；进程退出即停，多 worker 未验证（`tests/test_ticker_consistency.py`）。
   界面写明「计划已保存」与「任务已安排」的区别，不把它说成 7×24 服务。
6. **无真实银行接入**：全部模拟数据，不连真实账户、不真实转账/投资/支付（任务书要求如此）。
7. **模型单点与非确定性**：只测了一个模型；A 组同配置复跑 intent 有 ±2~4pp 抖动。
8. **来源核验限制**：4 条 unverified（1 条页面正文未渲染 + 3 条 GAP）、1 条被新规取代；已核验 29 条（law 8 / bank_public 19 / contest_simulation 2），未补成「看似合理」的规则。
9. **`scripts/e2e.py` 会删库重建** `data/bank.db`；演示前需 `make reseed` 恢复干净种子。本报告完成后已重播种。
10. **4 个默认 skip**：浏览器冒烟测试默认关闭，避免无浏览器环境拖红全量；已单独以 `RUN_BROWSER_SMOKE=1` 验证 4 passed。
11. **审计链的准确边界**：应用层只追加 + 链可验证；拿到数据库文件写权限的人可重建整条链，不等价于绝对不可篡改。
12. **外部模型配额（部分缓解）**：MiniMax 在 2026-10-23 22:10 后 429，2026-10-24 复测仍 429；
    换 DeepSeek 后 A/B、红队、独立浏览器冒烟与五条旅程都已复跑。DeepSeek 侧仍是单一厂商、
    单次采样，没有第三方交叉验证。
13. **未做 RAG/向量库对照**：结构化匹配已够用，刻意不引入；未来若加应作为第三对照组，而不是替换现有知识层。

---

## 8. 从干净环境复现

```bash
cd banking-agent
python3.12 -m venv .venv && . .venv/bin/activate   # 或直接用系统 python3.12（依赖已装）
pip install -r requirements.txt
cp .env.example .env            # 填 LLM key；不填则 MOCK_MODE=1 离线演示
python3.12 -m scripts.seed --force
python3.12 -m uvicorn app.main:app --port 8000
# 浏览器打开启动日志打印的 http://127.0.0.1:8000/#token=…
```

验证与演示：

```bash
make test                                   # 通过/失败/跳过数见 docs/benchmark/release_manifest.json
python3.12 scripts/check_frontend.py        # 66/66
python3.12 -m scripts.redteam               # 43/43
python3.12 -m scripts.e2e                   # 13/13（会删库重建，跑完 make reseed）
python3.12 scripts/browser_smoke.py         # 真浏览器（需可用模型；配额耗尽时失败），通过则落三张截图
RUN_BROWSER_SMOKE=1 python3.12 -m pytest tests/test_browser_smoke.py -q   # 4 passed
python3.12 -m tests.eval.run_eval --cases tests/eval/intent_cases.yaml --out tests/eval/results --group both
```

三段演示的逐步脚本见 `docs/13_竞赛演示手册.md`（演示 A 转账/歧义/权限变化，演示 B 知识增强与歧义追问，演示 C 储蓄目标闭环）。

---

## 9. 环境受限与无法验证项

| 项 | 情况 |
|---|---|
| Revolut AIR 内部架构 | 官方未公开模型、工具链、知识库实现；报告中一律标「未核实/合理推断」，不声称获得源码或未公开 API |
| 部分银行页面 | 需登录或页面改版，6 条来源保持 `unverified`，引用时必须标注 |
| 监管文件逐条比对 | 已核验发布机构、文号、生效状态与关键条款；未做法律意见级别的逐字解释 |
| 全天候主动服务 | 已有进程内节拍（预约转账 / 开启监测的目标 / 到点计划步骤），但只是单进程 asyncio：进程退出即停、多 worker 未验证；未做多用户/长周期稳定性测试 |
| 真实资金链路 | 按任务书全部模拟，无法也不应验证真实转账/投资 |
| 真实模型复跑 | MiniMax 仍 429；2026-10-24 换 DeepSeek 完成三轮 A/B、红队、独立浏览器冒烟与五条旅程，结果见 §6 与 `browser_journeys.json`；仍是单一厂商、单次采样 |
| 独立 `browser_smoke.py` | 2026-10-24 换 DeepSeek 后复跑 **PASS**（场景 1 查余额、场景 2 黄灯转账点确认，余额 12,860.50 → 12,760.50，演示库未变） |
| 五条高风险浏览器旅程 | `scripts/ui_journeys.py`：小额转账、高额强验证、AA 同名候选、生日计划刷新恢复、储蓄提醒处理，5/5 PASS；每条同时核对 DOM 与服务端节点/审计，演示库 sha256 未变 |
| 点击级 DOM 单测 | 旅程是流程级（真浏览器 + 服务端双证），没有做每个按钮的点击级组件测试；`tests/test_browser_smoke.py` 默认 skip，需 `RUN_BROWSER_SMOKE=1` |

---

## 10. 提交物清单（本阶段新增/更新）

- `docs/benchmark/revolut_air_research.md`、`air_vs_banking_agent.md`、`implementation_priorities.md`
- `docs/改造实施基线.md`、`docs/银行业务知识来源清单.md`
- `docs/11_知识层设计.md`（含 §11.5.5 引号误拦修正）、`docs/12_主动财务管理设计.md`
- `docs/13_竞赛演示手册.md`、`docs/14_安全回归报告.md`、`docs/技术文档.md`
- `docs/安全自评报告.md`、`docs/提交物对照表.md`、`docs/交付/交付说明.md`、`docs/交付/答辩PPT.pptx`
- `knowledge/{terms,ontology,intent_rules,workflows,sources}.yaml`
- `tests/eval/{intent_cases.yaml,run_eval.py,evaluation_report.md,README.md,test_eval_harness.py}` 与 `tests/eval/results/` 原始 jsonl
- `tests/test_knowledge_*.py`、`tests/test_proactive_*.py`、`tests/test_security_{knowledge,proactive}.py`、`tests/test_payment_guard.py`
- `app/planner/payment_guard.py`、`app/planner/kb_bridge.py`、`app/proactive.py`、`app/tools/proactive.py`
- `scripts/{e2e.py,browser_smoke.py,check_frontend.py,redteam*.py,ui_journeys.py,release_manifest.py}`
- `docs/benchmark/release_manifest.json`（唯一数字口径）、`docs/交付/browser_journeys.json`、`docs/交付/截图/信息架构/journey_*.png`
- 2026-10-24 新增测试：`tests/test_split_safety.py`、`test_birthday_plan.py`、`test_ticker_consistency.py`、`test_channels.py`、`test_recover.py`、`test_suitability.py`、`test_plans.py`、`test_subscriptions.py`、`test_real_channels.py`、`test_scheduled_lease.py`、`test_scheduled_calendar.py`、`test_session_cards.py`、`test_business_api.py`、`test_utterance_eval.py`