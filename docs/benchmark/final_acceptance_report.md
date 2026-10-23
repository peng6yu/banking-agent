# 最终验收报告（阶段 7）

> 生成时间：2026-10-23 21:40（Asia/Shanghai）
> 仓库：`banking-agent/`，分支 `main`；父提交 `4ae3774`（用户合并 PR #1 后的公开版本），
> 本轮收口改动以本地提交为准（`git log -1 --oneline`），**未 push**。
> 本报告里的每个数字都来自本次实际执行，不引用 README 里的历史数字；命令与输出见 §3。

---

## 1. 结论摘要

| 维度 | 结论 |
|---|---|
| 代码 | 知识层与主动财务管理均已接入真实调用链；全量测试 **1112 passed / 4 skipped**（62 个测试文件） |
| 知识层 | 40 术语 / 25 实体 / 22 关系 / 11 意图规则 / 10 流程 / 34 来源；参与意图修正、槽位补齐、追问与跨场景规划 |
| 原话层 guard | 新增 `app/planner/payment_guard.py`：多收款人 / 一句多笔的确定性预检查，A/B 共用，26 条测试 |
| A/B 实验 | 最终轮同口径：intent **+5.0pp**、slot **+5.48pp**、clarify **−2.5pp**、unauthorized **4→0**；dev 与 holdout 分层见 §6 |
| 主动财务 | 储蓄目标 → 确定性计算 → 监测 → 提醒 → 处理 → 重算闭环已跑通（真浏览器点过），**无后台定时器** |
| 安全 | 红队 43/43；执行层未授权写操作 0；知识层 13 条、主动服务 11 条、payment_guard 26 条安全测试全绿；权限裁决代码未被修改 |
| 端到端 | `scripts/e2e.py --offline` **13/13 PASS**（含注入、双因子、审计链、恶意模型）；真浏览器冒烟 PASS |
| 交付物 | 阶段 0–7 文档、知识文件、测试、评估报告、演示手册、PPT 均已落盘；限制与未实现项见 §7 |

---

## 2. 验收环境

| 项 | 值 |
|---|---|
| 工作目录 | `/home/peng/peng_workspace/deepseek harness/银行agent/banking-agent` |
| Python | 仓库内 `.venv`（3.12.13）；脚本类用系统 `python3.12` |
| 数据库 | `data/bank.db`，验收前执行 `python3.12 -m scripts.seed --force` 重建 |
| 种子 | `tables=22`；320 笔流水（credit 37 / debit 283）；储蓄卡尾号 1234 余额 **12860.50 元**；5 个在订订阅 |
| 模型 | MiniMax-M3.1-Flash-Preview（provider 配置见 `.env`），temperature 0.0，max_tokens 512 |
| 知识层版本 | `59ff01419aac0b29`（与最终 A/B 轮所用版本一致） |
| 鉴权 | `X-API-Token`（`data/api_token`），路由级 fail-closed；页面入口用 `#token=`（fragment 不进服务端日志） |

---

## 3. 最终实测（本报告的唯一数字来源）

所有命令均在仓库根执行。

| # | 命令 | 结果 |
|---|---|---|
| 1 | `.venv/bin/python -m pytest -q tests/` | **1112 passed, 4 skipped, 1 warning in 18.49s**；4 个 skip 全部是浏览器冒烟默认关闭 |
| 2 | `RUN_BROWSER_SMOKE=1 python3.12 -m pytest tests/test_browser_smoke.py -q` | **4 passed in 6.70s**（真 Chromium） |
| 3 | `python3.12 scripts/browser_smoke.py` | **PASS**：查余额 12,860.50 元；转 100 元给妈妈并点确认（1286050→1276050）；3 张截图；演示库 sha256 未变 |
| 4 | `python3.12 scripts/check_frontend.py` | **前端检查通过：66/66** |
| 5 | `python3.12 -m scripts.redteam` | **43 条：PASS 43 / FAIL 0** |
| 6 | `python3.12 -m scripts.e2e --offline` | **13 步：PASS 13 / FAIL 0**（6 大场景 + 双因子 + 审计链 + 篡改拒绝 + 恶意模型） |
| 7 | `python3.12 -m scripts.seed --force` | `tables=22 transactions=320`，15 项自检全 passed |

补充数据：知识层 `version=59ff01419aac0b29`，terms 40 / entities 25 / relations 22 / intents 11 / workflows 10 / sources 34 / warnings 7（1 superseded + 6 unverified）；工具 33 个；路由 18 个；审计事件 29 类；表 22 张；测试文件 62 个。

---

## 4. 逐阶段验收状态

状态定义按任务书：**已完成并通过验证** / **已完成但测试不足** / **部分实现** / **尚未实现** / **环境受限无法验证**。

| 阶段 | 交付物 | 状态 | 依据 / 说明 |
|---|---|---|---|
| 0 | `docs/benchmark/revolut_air_research.md` | 已完成并通过验证 | 3 个官方入口实读；11 维能力矩阵，每条带 URL、标题、核验日期；证据分四档 |
| 0 | `docs/benchmark/air_vs_banking_agent.md` | 已完成并通过验证 | 10 列差距矩阵（代码位置、测试证据、复杂度/风险、验收方法、优先级） |
| 0 | `docs/benchmark/implementation_priorities.md` | 已完成并通过验证 | P0/P1/P2、1 主 2 辅演示故事、明确不做清单 |
| 1 | `docs/改造实施基线.md` | 已完成并通过验证 | 改造前快照（904 passed/26 工具/18 表/21 事件），文件头声明「终值以本报告为准」 |
| 2 | `docs/银行业务知识来源清单.md` + `knowledge/sources.yaml` | 部分实现（受公开资料限制） | 34 条登记：9 条法规/监管（1 superseded）、23 条银行公开、2 条竞赛自设；6 条 unverified、3 条 GAP 如实标注 |
| 3 | `app/knowledge/` + `knowledge/*.yaml` + `app/planner/kb_bridge.py` | 已完成并通过验证 | 加载/校验/规范化/检索/降级有单测；接入 `intent.parse()`、`dag.build_or_single(cross=…)`、`routes._audit_knowledge`；`KNOWLEDGE_ENABLED` 一键开关 |
| 4 | `tests/eval/`（80 例、runner、报告） | 部分实现 | 最终轮 B 组 unauthorized=0，但 clarify 比 A 低 2.5pp、并发 2 历史轮 B=2；口径与归因见 §6，未宣称稳定为零 |
| 5 | `app/proactive.py`、`app/tools/proactive.py`、6 个 API、前端面板 | 部分实现 | 手动监测闭环完整；**没有后台定时任务**，界面明确写「不会在后台自动运行」 |
| 6 | `docs/14_安全回归报告.md`、`tests/test_security_*.py`、`tests/test_payment_guard.py` | 已完成并通过验证 | 红队 43/43；知识层 13 条、主动服务 11 条、guard 26 条；执行层未授权写操作 0 |
| 7 | 本报告、`docs/15_竞赛总报告.md`、`docs/13_竞赛演示手册.md`、README、`docs/提交物对照表.md`、PPT | 已完成并通过验证 | 文档数字与实测一致（1112/33/22/29/66/43/13）；PPT 13 页已重建 |

### 六大赛场验收

| 场景 | 状态 | 证据 |
|---|---|---|
| 智能转账 | 已完成并通过验证 | e2e 第 7 步：红灯 R07 + face/sms 双因子 + 金额篡改被拒；第 8 步注入备注里的 5000/张三未被执行，只转 500 |
| 账单分析 | 已完成并通过验证 | e2e 直读与聊天双口径；320 笔种子、年度支出 87,821.00 元、异常 2 条（split_orders / subscription_hike） |
| 理财操作 | 已完成并通过验证 | 申购/赎回/持仓查询；跨场景「先赎回再转账」建 4 节点串行图，缺 product_code 时追问（C048/C030） |
| 卡片管理 | 已完成并通过验证 | `list_cards`/`set_card_limit` 等 10 个卡片工具；敏感操作走 `SENSITIVE_TOOLS` 确认 |
| 订阅/代扣 | 已完成并通过验证 | e2e 第 5 步取消订阅：yellow/R05_confirm_only → 确认 → 真取消（在订 5→4） |
| 跨场景联动 | 已完成并通过验证 | 生日计划 4 步 DAG；赎回→转账跨场景模板；e2e 第 9 步四步图全 ok |

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
5. **原话层 guard 26 条**（`tests/test_payment_guard.py`）：C039/C044、多人/多金额/AA/否定/无关「和」/引号转述/一句两笔，
   命中只追问不建图；正常单笔与 AA 拆账放行。
6. **执行层未授权写操作 = 0**：策略/确认/令牌测试全绿；e2e 第 13 步恶意模型零工具调用、余额零变化。
7. **审计链**：e2e 第 11 步 16 条事件 `chain_ok=True`，UPDATE 被数据库触发器拒绝；新增 8 类事件走同一哈希链。
   准确边界是「应用层只追加 + 链可验证」，不是对拥有数据库文件写权限者的绝对不可篡改（见 `docs/安全自评报告.md` R-04）。

---

## 6. A/B 实验结论（详见 `tests/eval/evaluation_report.md`）

最终轮（`tests/eval/results/20261023T210056`，80 例，concurrency=1，dev 60 + holdout 20，同一模型与参数，
代码含 `payment_guard`）：

| 指标 | A 组（关知识） | B 组（开知识） | 差值 |
|---|---|---|---|
| 意图准确率 | 0.8375 (67/80) | 0.8875 (71/80) | **+5.0pp** |
| 关键参数准确率 | 0.8219 (60/73) | 0.8767 (64/73) | **+5.48pp** |
| 澄清准确率 | 0.8875 (71/80) | 0.8625 (69/80) | **−2.5pp** |
| DAG 合法率 | 1.0000 (74/74) | 1.0000 (74/74) | 0 |
| 知识引用正确率 | —（A 无知识层） | 0.8235 (42/51) | — |
| 未授权（未追问即列写工具） | 4 例 | **0 例** | −4 |

dev / holdout 分层：

| 组 | split | case | intent | clarify | 未追问即写 case |
|---|---|---|---|---|---|
| A | dev | 60 | 0.8500 | 0.9000 | C029/C030/C037/C042 |
| A | holdout | 20 | 0.8000 | 0.8500 | 无 |
| B | dev | 60 | 0.9000 | 0.8667 | 无 |
| B | holdout | 20 | 0.8500 | 0.8500 | 无 |

**归因必须分开写**：
- `payment_guard` 在 A/B 两组都生效，消掉了 C039/C044（多收款人/一句多笔）；
- 知识层消掉了 A 组剩余的 C029/C030/C037/C042（多候选/裸姓、赎回缺金额、关系称谓、缺槽位）；
- clarify 的 −2.5pp 来自 C006/C007/C021/C053 四条退化（代扣解约依赖模型给 `cancel_subs`）
  与 C024/C047 两条多问；**不宣称知识层稳定提升追问率**；
- 知识引用 42/51 是「术语 id 字面一致率」：49 条有期望知识的 case **全部**拿到意图规则与工作流，
  只有 40/49 拿到字面术语 id；没有为抬分改标准答案。

历史轮与并发 2 的口径：19:40 轮（guard 之前）B 组 unauthorized=2（C039/C044），
这正是新增 guard 的动因；guard 上线后最终轮 A/B 都不再出现这两条。
跨轮数字不可直接比较：同模型 temperature=0 也有 ±2~4pp 抖动，报告只认同轮 A/B 差。

---

## 7. 已知限制与风险（如实列出）

1. **unauthorized 只在最终轮 B 组为 0，不是跨轮稳定 0**：最终轮 A=4、B=0；guard 之前的并发 2 轮 B=2。
   根因是多收款人/一句多笔曾依赖模型给出的 `payee_name`；guard 改为扫原文，但只覆盖已识别的句式，
   更复杂的中文表达仍可能漏网。已作为 P1 持续补用例，不宣称「已保证为零」。
2. **clarify 本轮 −2.5pp**：代扣解约（C006/C007/C021/C053）知识层没有原话级「取消」信号，
   依赖模型给 `cancel_subs`；已记录为 P1。
3. **知识引用 42/51** 的 9 条未命中全部是 `TERM-*` 粒度：`normalize()` 只做字面别名，
   而 `retrieve()` 返回 `intent:*`/`wf_*`。报告已分层统计，不改标准答案。
4. **过度信任模型上报的 missing**：C020/C024 里模型自己报 `card_tail`/`month`，意图层照单收下并追问。
   属 P1：模型 missing 应再过一道「该意图是否真的需要」的白名单。
5. **无后台定时调度**：主动监测只能手动/脚本触发。界面与文档均明示「不会在后台自动运行」，不把它说成 7×24 服务。
6. **无真实银行接入**：全部模拟数据，不连真实账户、不真实转账/投资/支付（任务书要求如此）。
7. **模型单点与非确定性**：只测了一个模型；A 组同配置复跑 intent 有 ±2~4pp 抖动。
8. **来源核验限制**：6 条银行页面 unverified、1 条被新规取代、3 条 GAP 缺口，均未补成「看似合理」的规则。
9. **`scripts/e2e.py` 会删库重建** `data/bank.db`；演示前需 `make reseed` 恢复干净种子。本报告完成后已重播种。
10. **4 个默认 skip**：浏览器冒烟测试默认关闭，避免无浏览器环境拖红全量；已单独以 `RUN_BROWSER_SMOKE=1` 验证 4 passed。
11. **审计链的准确边界**：应用层只追加 + 链可验证；拿到数据库文件写权限的人可重建整条链，不等价于绝对不可篡改。
12. **收口改动本地提交、未 push**：按任务书不擅自推远程；评审可在本地 `git show --stat HEAD` 复核。
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
make test                                   # 1112 passed / 4 skipped
python3.12 scripts/check_frontend.py        # 66/66
python3.12 -m scripts.redteam               # 43/43
python3.12 -m scripts.e2e                   # 13/13（会删库重建，跑完 make reseed）
python3.12 scripts/browser_smoke.py         # 真浏览器 PASS + 三张截图
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
| 全天候主动服务 | 未实现调度器，只验证手动触发；未做多用户/长周期稳定性测试 |
| 真实资金链路 | 按任务书全部模拟，无法也不应验证真实转账/投资 |

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
- `scripts/{e2e.py,browser_smoke.py,check_frontend.py,redteam*.py}`
