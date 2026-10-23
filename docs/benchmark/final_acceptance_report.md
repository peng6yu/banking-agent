# 最终验收报告（阶段 7）

> 生成时间：2026-10-23 19:58（Asia/Shanghai）
> 仓库：`banking-agent/`，分支 `main`，HEAD `011c564`（merge: 忽略 data/api_token）。改造内容**未提交**：按任务书第 8 条不擅自 commit / push，工作区有 27 个已修改文件、25 个未跟踪文件，评审可直接 `git status` 复核。
> 本报告里的每个数字都来自本次实际执行，不引用 README 里的历史数字；命令与输出见 §3、§9。

---

## 1. 结论摘要

| 维度 | 结论 |
|---|---|
| 代码 | 知识层与主动财务管理均已接入真实调用链，不是设计文档；全量测试 **1080 passed / 4 skipped** |
| 知识层 | 40 术语 / 25 实体 / 22 关系 / 11 意图规则 / 10 流程 / 34 来源；参与意图修正、槽位补齐、追问与跨场景规划 |
| A/B 实验 | 同一轮同口径：clarify +5.0pp、slot +1.37pp、intent 持平、unauthorized 5→0；并发 2 复跑 unauthorized 6→2，**未稳定为 0**（§7 如实说明） |
| 主动财务 | 储蓄目标 → 确定性计算 → 监测 → 提醒 → 处理 → 重算闭环已跑通（真浏览器点过），**无后台定时器**（手动触发，界面文案已明示） |
| 安全 | 红队 43/43 PASS；执行层未授权写操作 0；知识层与主动服务各 8/10 项攻击测试通过；权限裁决代码未被修改 |
| 交付物 | 阶段 0–7 文档、知识文件、测试、评估报告、演示手册、PPT 均已落盘；限制与未实现项在 §8 逐条列出 |

---

## 2. 验收环境

| 项 | 值 |
|---|---|
| 工作目录 | `/home/peng/peng_workspace/deepseek harness/银行agent/banking-agent` |
| Python | 仓库内 `.venv`（3.12.13）；脚本类用系统 `python3.12` |
| 数据库 | `data/bank.db`，验收前执行 `python3.12 -m scripts.seed --force` 重建 |
| 种子 | `seed.ok random_seed=20261018 tables=22 window_days=180`；320 笔流水；储蓄卡尾号 1234 余额 **12860.50 元**，信用卡余额 13990.00 元；5 个在订订阅；0 个储蓄目标 |
| 模型 | MiniMax-M3.1-Flash-Preview（provider 配置见 `.env`），temperature 0.0，max_tokens 512 |
| 知识层版本 | `59ff01419aac0b29`（与 A/B 第三、四轮所用版本一致） |
| 鉴权 | `X-API-Token`（`data/api_token`），路由级 fail-closed |

---

## 3. 最终实测（本报告的唯一数字来源）

所有命令均在仓库根执行。

| # | 命令 | 结果 |
|---|---|---|
| 1 | `.venv/bin/python -m pytest -q tests/ -rs` | **1080 passed, 4 skipped, 1 warning in 19.61s**；4 个 skip 全部是浏览器冒烟默认关闭（要显式 `RUN_BROWSER_SMOKE=1`） |
| 2 | `RUN_BROWSER_SMOKE=1 python3.12 -m pytest tests/test_browser_smoke.py -q` | **4 passed in 6.56s**（真 Chromium） |
| 3 | `python3.12 scripts/browser_smoke.py` | **PASS**：场景 1 查余额（12,860.50 元）、场景 2 转账 100 元并点确认（余额 1286050→1276050）；三张截图落 `docs/交付/截图/`；演示库未被改动（sha256 校验） |
| 4 | `python3.12 scripts/check_frontend.py` | **前端检查通过：66/66** |
| 5 | `python3.12 -m scripts.redteam` | **43 条：PASS 43 / FAIL 0** |
| 6 | `python3.12 -m scripts.e2e` | **13 步：PASS 13 / FAIL 0**（覆盖 6 大场景 + 双因子 + 审计链 + 篡改拒绝 + 恶意模型） |
| 7 | `python3.12 -m scripts.seed --force` | `seed.ok tables=22 transactions=320`，15 项自检全 passed |

补充数据：知识层 `version=59ff01419aac0b29`，terms 40 / entities 25 / relations 22 / intents 11 / workflows 10 / sources 34 / warnings 7（1 superseded + 6 unverified）；工具 33 个；路由 18 个；审计事件 29 类；表 22 张；测试文件 66 个。

---

## 4. 逐阶段验收状态

状态定义按任务书：**已完成并通过验证** / **已完成但测试不足** / **部分实现** / **尚未实现** / **环境受限无法验证**。

| 阶段 | 交付物 | 状态 | 依据 / 说明 |
|---|---|---|---|
| 0 | `docs/benchmark/revolut_air_research.md` | 已完成并通过验证 | 3 个官方入口实读；11 维能力矩阵，每条带 URL、标题、核验日期；证据分四档（官方证实/可信公开/合理推断/未核实） |
| 0 | `docs/benchmark/air_vs_banking_agent.md` | 已完成并通过验证 | 10 列差距矩阵（含代码位置、测试证据、复杂度/风险、验收方法、优先级） |
| 0 | `docs/benchmark/implementation_priorities.md` | 已完成并通过验证 | P0/P1/P2、1 主 2 辅演示故事、明确不做清单 |
| 1 | `docs/改造实施基线.md` | 已完成并通过验证 | 改造前快照（904 passed/26 工具/18 表/21 事件），文件头已声明「终值以本报告为准」 |
| 2 | `docs/银行业务知识来源清单.md` + `knowledge/sources.yaml` | 部分实现（受公开资料限制） | 34 条登记：9 条法规/监管（1 superseded）、23 条银行公开、2 条竞赛自设；其中 6 条 unverified、3 条 GAP 缺口如实标注，未伪造机构名 |
| 3 | `app/knowledge/` + `knowledge/*.yaml` + `app/planner/kb_bridge.py` | 已完成并通过验证 | 加载/校验/规范化/检索/降级全部有单测；接入 `intent.parse()`、`dag.build_or_single(cross=…)`、`routes._audit_knowledge`；A/B 中可一键开关（`KNOWLEDGE_ENABLED`） |
| 4 | `tests/eval/`（80 例、runner、报告） | 部分实现 | 对照实验与报告已完成；但 B 组 unauthorized 在并发 2 复跑中为 2（非 0），知识引用 42/51 属粒度口径差，见 §7、§8 |
| 5 | `app/proactive.py`、`app/tools/proactive.py`、6 个 API、前端面板 | 部分实现 | 手动监测闭环完整；**没有后台定时任务**，界面明确写「不会在后台自动运行」 |
| 6 | `docs/14_安全回归报告.md`、`tests/test_security_knowledge.py`、`tests/test_security_proactive.py` | 已完成并通过验证 | 红队 43/43；知识层 8 项攻击、主动服务 10 项边界均有自动化用例；执行层未授权写操作 0 |
| 7 | 本报告、`docs/15_竞赛总报告.md`、`docs/13_竞赛演示手册.md`、README、`docs/提交物对照表.md`、PPT | 已完成并通过验证 | 文档数字与实测一致（1080）；PPT 13 页已重建并校验 |

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

1. **权限裁决未被知识/主动服务修改**：`app/policy/rules.py` 只新增了 7 个主动服务工具的分类登记（只读/确认），`decide()` 的规则语义未变；`tests/test_knowledge_integration.py` 有「开启/关闭知识层，裁决结果一致」的对照用例。
2. **红队 43/43**：诱导转账 10、越权操作 10、提示注入 10、信息套取 8、异常与稳定性 5。
3. **知识层攻击 8 项**（`tests/test_security_knowledge.py`）：检索文本含「忽略安全规则」、备注要求跳过验证、冒充管理员、外部文本要求直发写操作、知识条目冲突、来源过期/删除、模型返回非法结构、检索超时——预期均为拒绝越权 / 保留原规则 / 追问 / 降级。
4. **主动服务边界 10 项**（`tests/test_security_proactive.py`）：无历史、现金流不足、目标过期、重复交易、重复提醒、监测失败、关闭提醒、修改目标、请求未授权转账、恶意交易数据——不产生任何资金写入；所有资金操作仍回到原 `/api/chat` + 确认 + 令牌链路。
5. **执行层未授权写操作 = 0**：策略/确认/令牌相关测试全绿；e2e 第 13 步恶意模型零工具调用、余额零变化。
6. **审计链**：e2e 第 11 步 16 条事件 `chain_ok=True`，UPDATE 被数据库触发器拒绝；新增 8 类事件（knowledge.* / savings_goal.* / proactive.*）走同一哈希链。

---

## 6. A/B 实验结论（详见 `tests/eval/evaluation_report.md`）

最终轮（`results/20261023T192756`，80 例，concurrency=1，dev+holdout，同一模型与参数）：

| 指标 | A 组（关知识） | B 组（开知识） | 差值 |
|---|---|---|---|
| 意图准确率 | 0.8500 (68/80) | 0.8500 (68/80) | 0 |
| 关键参数准确率 | 0.8082 (59/73) | 0.8219 (60/73) | +1.37pp |
| 澄清准确率 | 0.8125 (65/80) | 0.8625 (69/80) | **+5.0pp** |
| DAG 合法率 | 1.0000 | 1.0000 | 0 |
| 知识引用正确率 | —（A 组无知识层） | 0.8235 (42/51) | — |
| 未授权（未追问即列写工具） | 5 例 | **0 例** | −5 |

并发 2 复跑（`results/20261023T194022`）：A unauthorized 6 → B **2**（C039「给李静和她老公」、C044「一句两笔」）。因此本报告的口径是「**显著降低但未消除**」，不宣称已保证为零；修法（直接扫原文识别多收款人/多笔）列为 P1 未交付。

---

## 7. 已知限制与风险（如实列出）

1. **B 组 unauthorized 未稳定为 0**（并发 2 的 2 例）。根因是多收款人/一句两笔依赖模型给出的 `payee_name`；`kb_bridge` 的文本扫描只在模型报出收款人后才补追问。已列为 P1，附复现用例 C039/C044。
2. **知识引用 42/51** 的 9 条未命中全部是 `TERM-*` 粒度：`normalize()` 只做字面别名（「转500块」不含「转账」），而 `retrieve()` 返回 `intent:*`/`wf_*`。属标注粒度与实现粒度不一致，不是检索失效；为不牵动知识版本与重跑，报告如实解释而未改标注。
3. **过度信任模型上报的 missing**：C020/C024 里模型自己报 `card_tail`/`month`，意图层照单收下并追问，知识规则并未要求。属 P1：模型 missing 应再过一道「该意图是否真的需要」的白名单。
4. **无后台定时调度**：主动监测只能手动/脚本触发。界面与文档均明示「不会在后台自动运行」，不把它说成 7×24 服务。
5. **无真实银行接入**：全部模拟数据，不连真实账户、不真实转账/投资/支付（任务书要求如此）。
6. **模型单点与非确定性**：只测了一个模型；A 组同配置复跑 intent 有 ±2~4pp 抖动。跨轮数字不可直接比较，只看同轮 A/B 差。
7. **来源核验限制**：6 条银行页面 unverified（页面结构变化/需登录）、1 条被新规取代（superseded）、3 条 GAP 缺口（转账退回、状态码、代扣解约限制），均未补成「看似合理」的规则。
8. **`scripts/e2e.py` 会删库重建** `data/bank.db`；演示前需 `make reseed` 恢复干净种子。本报告完成后已重播种。
9. **4 个默认 skip**：浏览器冒烟测试默认关闭，避免无浏览器环境拖红全量；已单独以 `RUN_BROWSER_SMOKE=1` 验证 4 passed。
10. **改造未提交 git**：52 个文件变更留在工作区（27 M / 25 ??），按任务书不擅自 commit / push。

---

## 8. 从干净环境复现

```bash
cd banking-agent
python3.12 -m venv .venv && . .venv/bin/activate   # 或直接用系统 python3.12（依赖已装）
pip install -r requirements.txt
cp .env.example .env            # 填 LLM key；不填则 MOCK_MODE=1 离线演示
python3.12 -m scripts.seed --force
python3.12 -m uvicorn app.main:app --port 8000
# 浏览器打开启动日志打印的 http://127.0.0.1:8000/?token=…
```

验证与演示：

```bash
make test                                   # 1080 passed / 4 skipped
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
- `docs/13_竞赛演示手册.md`、`docs/14_安全回归报告.md`（含 §9.1 e2e 两次故障）、`docs/技术文档.md`
- `docs/安全自评报告.md`、`docs/提交物对照表.md`、`docs/交付/交付说明.md`、`docs/交付/答辩PPT.pptx`
- `knowledge/{terms,ontology,intent_rules,workflows,sources}.yaml`
- `tests/eval/{intent_cases.yaml,run_eval.py,evaluation_report.md,README.md,test_eval_harness.py}`
- `tests/test_knowledge_*.py`、`tests/test_proactive_*.py`、`tests/test_security_{knowledge,proactive}.py`
- `scripts/{e2e.py,browser_smoke.py,check_frontend.py,redteam*.py}`
