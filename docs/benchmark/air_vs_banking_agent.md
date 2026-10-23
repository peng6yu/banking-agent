# AIR ↔ banking-agent 竞品能力差距矩阵

> **用途**：2026 深圳国际金融科技大赛「人工智能赛道 · 银行 AI 智能体」阶段 0.3 / 0.4 交付物。把 Revolut AIR 的公开能力逐维度拆成可执行的改造项，回答三件事：差在哪、值不值得补、补完怎么验收。
> **对标对象**：Revolut AIR（AI by Revolut），证据全部复用 `docs/benchmark/revolut_air_research.md` 的台账编号（A1–A17 / B1–B15 / F1–F7）与四档证据状态，**不新增来源、不升级证据等级**。
> **本项目证据口径**：代码路径 + 测试。基线数字来自 `docs/改造实施基线.md`（改造前基线：**904 passed / 4 skipped**）；本文写作时（2026-10-23）一度处于并行改造中间态（916/935 passed），**已收口**。最终交付状态：**1131 passed / 4 skipped**（`.venv/bin/python -m pytest -q`，19.94s）、前端静态检查 **66/66**、红队 **PASS 43 / FAIL 0**、A/B 评估 **80 例**（详见 `docs/benchmark/final_acceptance_report.md`）。中间态数字只作过程记录，不作交付依据。
> **硬约束**：本矩阵不改代码；所有"本项目"结论都能回指真实文件；拿不准的一律写「待核实」，不用常识补齐。

---

## 〇、口径与摘要

### 0.1 AIR 证据状态（照抄研究文档 C.1，不升档）

| 标签 | 含义 | 本矩阵用法 |
|---|---|---|
| 官方资料明确证实 | revolut.com / help.revolut.com 原文 | 可直接作为对标事实 |
| 可信公开技术资料证实（原页未打开） | 第一方高管 LinkedIn（B1）或厂商材料 | 引用时必须写明"原页未打开" |
| 根据公开信息作出的合理推断 | 由已核实事实推导 | 只能写"推断"，不得当事实 |
| 尚未核实 | 无可靠证据 / 来源不可达 | 必须显式列为缺口，**不得用常识补齐** |

> 三条纪律（研究文档 F 节）：不称 AIR 用向量库/GraphRAG；不称 AIR 是主动型顾问；不照搬"投资只读"而不做取舍说明。

### 0.2 本项目证据状态

| 状态 | 判定标准 | 示例 |
|---|---|---|
| 已实现 | 生产链路可调用 + 有测试 | 转账、账单、卡片、订阅、理财申赎 |
| 部分实现 | 主路径可用，边界或形态缺失 | 多轮（仅收款人歧义续答）、订阅（仅查询/取消，无识别/提醒） |
| 部分实现（边界缺失） | 主路径可用，但缺后台调度/个性化推荐/延迟执行等形态 | 主动监测（仅手动触发）、理财（无推荐/横向比较）、跨场景（无持久化/延迟） |
| 未实现 | 无代码、无表、无工具 | 适当性问卷、真实银行连接、后台常驻调度 |
| 待核实 | 有线索但未跑通/未复跑 | 真实模型多话术对抗（红队 43/43 为规则级） |

### 0.3 缺口编号与优先级

- **缺口编号 G01–G17**：本文定义，`implementation_priorities.md` 直接引用。
- **优先级**：P0=核心业务可靠/参数校验/安全边界/执行结果核验/审计/赛题核心场景；P1=领域知识增强、主动财务管理、上下文理解、可解释提醒、能形成演示闭环的跨场景规划；P2=需大量新基础设施/外部服务/额外模型。

### 0.4 调研时的三处口径冲突（历史快照）

> 本节记录调研当时的判断，工具数当时为 26；最终交付为 33 个工具（见 `app/tools/tool_specs.py` 与 `/api/health`）。

| # | 流传说法 | 代码实际 | 处置 |
|---|---|---|---|
| 1 | README 称"AA 拆分未实现" | `app/planner/split_plan.py:42 build_split` + `app/tools/registry.py` 的 `split_transfer` + `tests/test_split.py`（8 条）**已实现** | 以代码为准，矩阵按"已实现"写；README 待改 |
| 2 | README/旧架构图称 14 个工具 | 调研时 `registry.TOOLS` 实测 **26**；最终交付为 **33**（新增知识层与主动财务工具） | 历史快照 26；当前以 33 为准 |
| 3 | `docs/提交物对照表` 称"多轮上下文缺失" | `app/planner/clarify.py` 有 Pending/remember/augment，但只服务收款人歧义续答，**不是通用多轮** | 写成"局部多轮"，缺口仍成立（G09） |

### 0.5 摘要矩阵（扫读用）

| 维度 | 缺口 | AIR 证据强度 | 本项目状态 | 优先级 | 估工（人日） |
|---|---|---|---|---|---|
| D01 产品定位与交互 | G01 | 官方证实 | 部分（PC 网页 5 面板 + SSE） | P1 | 2–3 |
| D02 个人财务洞察 | G02 G03 | 能力官方证实；可视化未核实 | 已实现（账单分析 + 储蓄测算/预算偏离，3 个只读工具 + 前端面板） | P1 | 5–7 |
| D03 转账与工具调用 | G16 G17 | 动作清单官方证实；工具协议未核实 | 已实现（33 工具 / 9 模板 / 动态 AA / 原话层付款 guard） | P2（G16/G17） | 6–10 |
| D04 卡片管理 | G04 | 官方证实 | 已实现（7 工具） | P1（G04） | 2–3 |
| D05 订阅代扣 | G05 | 查询/管理官方证实；识别/一键取消未核实 | 部分（查询+取消，无识别/提醒） | P0 | 5–7 |
| D06 理财与适当性 | G06 | 官方证实（读写边界） | 部分（申赎+风险提示，无适当性） | P0 | 4–6 |
| D07 跨场景规划 | G07 | 官方证实 + B1 | 部分（生日模板 + 知识层跨场景链路，非通用组合） | P0 | 6–8 |
| D08 主动服务 | G08 | **尚未核实（倾向否定）** | 部分（4 表 + 7 工具 + 前端闭环已注册；仅手动触发，无后台常驻调度） | P1 | 8–12 |
| D09 上下文与多轮 | G09 | B1（原页未打开）；窗口未核实 | 部分（4–6 轮 + 局部澄清） | P1 | 4–6 |
| D10 安全确认 | G10 | 官方证实（分级表未核实） | 已实现（R01–R12 + 令牌七步） | P0（已有） | 8–12（P2 部分） |
| D11 隐私与数据最小化 | G11 | 官方证实（三条承诺） | 已实现（信封 + 审计不落原文） | P1 | 1–2 |
| D12 可解释与来源引用 | G12 | 部分官方证实；"为什么"未核实 | 部分（审计已落知识 ids；explain 仍硬编码） | P0 | 3–5 |
| D13 评估与可观测性 | G13 | B1（原页未打开） | 部分（916 测试 + 43 红队 + 哈希链，无 NLU 评测） | P0 | 3–5 |
| D14 领域知识与术语 | G14 | B1 + A6 | 进行中（5 个 YAML 已齐、kb_bridge 已接 intent；测试未全绿） | P0 | 8–12 |
| D15 中断/回退/人工接管 | G15 | 仅"转人工"官方证实 | 部分（取消/超时/冲正有，转人工无） | P0（已有）/P2 | 5–8 |

---

## 一、逐维度能力差距矩阵

> 每张表固定 10 行，对应任务要求的 10 列。第 7 行给出人日与代码量级；第 9 行给出可执行的验收方法。

### D01 产品定位与交互

| 字段 | 内容 |
|---|---|
| ① AIR 公开能力 | 把多标签页操作压缩成一次对话：App 内下拉或 Profile→Chats→AIR；官方自述 "an execution engine"、"moves beyond the traditional chatbot"、"One chat. Total control."；英国 1300 万客户起量 |
| ② AIR 依据与证据状态 | A1 https://www.revolut.com/air-ai-by-revolut/ 、A3 https://www.revolut.com/news/revolut_enters_new_era_of_money_intelligence_with_launch_of_ai_assistant/ 、A4 https://www.revolut.com/blog/post/air-ai-by-revolut/ 、A5 https://help.revolut.com/help/profile-and-plan/getting-started-with-air/ ——**官方资料明确证实**；语音入口只在 B1 提到，**尚未核实** |
| ③ 本项目真实实现状态 | 部分：PC 单页 `web/index.html`（1173 行，无框架），右侧 5 面板"意图/计划/权限灯/多因子/审计链"（`web/index.html:297-340`），SSE 固定 8 个事件 `start/intent/trace/confirm/factor/tool/done/error`（`app/planner/events.py:1-8`、`app/api/routes.py:106`）；无移动端、无手势入口、无语音 |
| ④ 代码位置与测试证据 | `app/api/routes.py:330 _events`（SSE 编排）、`app/planner/events.py trace_event`；`scripts/check_frontend.py` **66/66**（2026-10-23 实测）、`scripts/browser_smoke.py` + `tests/test_browser_smoke.py:42`（2 场景真实浏览器截图，断言截图互不相同） |
| ⑤ 能力缺口 | **G01**：缺"一句一屏"的演示级交互收敛（当前是对话 + 工程看板并置，评审第一眼更像监控台，不是银行助手）；无移动端形态与语音 |
| ⑥ 可借鉴设计 | E1 手势单入口（难度低）、E13 "单步完成复杂任务"作为验收标准；**不照搬**独立 App，赛题演示用响应式 Web + 大字号对话区即可 |
| ⑦ 改造复杂度与风险 | 2–3 人日 / 约 200–300 行前端改动：新增"演示模式"开关、对话区优先布局、手势/快捷键唤起、移动端断点；风险：改坏 66 项前端硬检查（R10 前端单文件膨胀），须先跑 `check_frontend.py` 再改 |
| ⑧ 对应赛题要求 | 30% 创新性与交互体验；演示视频 ≤5 分钟；3.3 "模糊表达"体验 |
| ⑨ 验收方法 | `python scripts/check_frontend.py` 66/66；`pytest tests/test_browser_smoke.py -q` 通过且截图差异断言不变；人工走查 5 条口语句（含 1 条模糊句）均一屏内看到"意图→权限灯→结果" |
| ⑩ 优先级 | **P1**（演示加分项，不阻塞核心场景；主演示前必须完成） |

### D02 个人财务洞察

| 字段 | 内容 |
|---|---|
| ① AIR 公开能力 | 消费洞察（"instant spending insights"、"Where's my money going?"）、分类与趋势、预算辅助、储蓄测算（"How much can I save this month?"）、投资收益查询；官方**没有**说明做异常交易识别或月/年报 |
| ② AIR 依据与证据状态 | A3、A4、A5、A6 https://www.revolut.com/legal/rita-disclaimer/ ——消费/趋势/预算/储蓄测算**官方资料明确证实**；"消费者端图表形态"、"异常交易识别"、"月/年报"**尚未核实**（研究文档 Q3） |
| ③ 本项目真实实现状态 | 部分：`analyze_bill` 支持 `period=month/year`（`app/tools/bill.py:43`、`:89 _period_range`、`:85 _range_label`），分类统计（`:145 _by_category`）、Top 商户（`:164 _top_merchants`）、3 类异常（`:34` 常量 `split_orders/odd_hour_overseas/subscription_hike`、`:192 _detect_anomalies`）；无储蓄/预算测算、无图表、无月报导出。**复核补记（2026-10-23 19:00）**：储蓄测算 compute_savings_plan 与预算偏离 budget_deviation 已在 app/proactive.py 实现，但未注册为工具，DAG 里没有入口 |
| ④ 代码位置与测试证据 | 工具在 `registry.TOOLS`（只读，R01 直接放行）；DAG 模板 `dag.py:43-71` 的 `bill_analysis→analyze_bill`；测试 `tests/test_tools.py`、`tests/test_acceptance.py`、`tests/test_planner.py` 覆盖账单意图与统计 |
| ⑤ 能力缺口 | **G02** 储蓄/预算测算（"这个月能存多少"）缺失；**G03** 洞察无可视化（前端无图表，只有文本摘要） |
| ⑥ 可借鉴设计 | E2 最小可见性（账单只算本人账户）；E3 查询不打扰；测算类输出必须给"口径+区间"，不能给一个裸数字 |
| ⑦ 改造复杂度与风险 | G02 3–4 人日：新增只读工具 `compute_savings_plan`（收入−固定支出−近 3 月均值），金额用 int 分；G03 2–3 人日：前端加 2 张图（分类占比、月度趋势），不引入图表库可用内联 SVG；风险：测算被误读为理财建议，文案必须限定"基于本人历史流水的估算" |
| ⑧ 对应赛题要求 | 场景 2 账单分析（分类统计、异常识别、月/年报）；40% 任务完成率 |
| ⑨ 验收方法 | 固定种子库里"上月餐饮支出"与 SQL 手算一致；断言"分类金额之和 = 总支出"；异常三类各有 1 条正例 + 1 条反例；"今年一共花了多少"返回年报表且区间标签来自 `bill._range_label` |
| ⑩ 优先级 | **P1**（赛题场景 2 的主体已达标，G02/G03 是增强） |

### D03 转账与工具调用

| 字段 | 内容 |
|---|---|
| ① AIR 公开能力 | "代客执行动作"：卡片冻结、单次虚拟卡、限额、暂停周期付款、屏蔽商户、买 eSIM；投资写侧被法律禁止；官方未公开工具协议/端点/参数 schema |
| ② AIR 依据与证据状态 | A4、A5、A6 ——动作清单**官方资料明确证实**；"工具调用面 = 写操作白名单"属**合理推断**（研究文档 C.3-4）；协议与端点**尚未核实**（C.4-3） |
| ③ 本项目真实实现状态 | 已实现：26 个受管工具（`app/tools/registry.py TOOLS`），9 条固定模板 + AA 动态拆图（`app/planner/split_plan.py:42 build_split`）；人名/手机号/卡尾号收款人（`resolve_payee`，24 条关系词 + 消歧测试）；定时转账（`app/tools/scheduled.py:48` 仅 `once/monthly`）；金额两阶段 hold→commit（`app/funds.py`） |
| ④ 代码位置与测试证据 | `app/planner/dag.py:43-71`、`:180 split`；`app/tools/scheduled.py:71 schedule_transfer`、`:193 execute_due`；`app/policy/rules.py` R06/R07/R08；测试 `tests/test_e2e_transfer.py`、`tests/test_split.py`（8 条含"分不丢一分钱"）、`tests/test_scheduled.py`、`tests/test_phone_payee.py`、`tests/test_relation_payee.py`、`tests/test_reverse.py` |
| ⑤ 能力缺口 | **G16** 无对外工具协议（MCP/OpenAPI），第三方无法接入；**G17** 定时仅一次性/每月，缺季付、每周、指定工作日 |
| ⑥ 可借鉴设计 | E5 知识库+产品生态直连（我们已有工具面，缺知识面）；E13 一句指令一步到位；工具返回必须结构化 + 可核验 |
| ⑦ 改造复杂度与风险 | G17 1–2 人日（扩 `RECURRENCES` + 下期时间计算 + 迁移）；G16 5–8 人日（只读工具先开放，写工具必须走令牌与人工确认，不能直接暴露）；风险：开放协议 = 越权面放大，须复用 E2/E3；R3 `KNOWN_TOOLS` 白名单测试会拦新增工具 |
| ⑧ 对应赛题要求 | 场景 1 智能转账（人名/手机号/备注、定时、AA 拆分）；3.3 工具调用链路 |
| ⑨ 验收方法 | `pytest tests/test_e2e_transfer.py tests/test_split.py tests/test_scheduled.py -q`；AA 总额守恒断言；定时转账到点后余额与流水一致；重复提交幂等（同一 idempotency_key 只扣一次） |
| ⑩ 优先级 | **P0 已有主体（不新增）**；G17 **P2**、G16 **P2** |

### D04 卡片管理

| 字段 | 内容 |
|---|---|
| ① AIR 公开能力 | 冻结/解冻卡、申请单次虚拟卡、消费限额、屏蔽商户与未来支付；卡片控制与订阅管理是产品页两个展示 Tab |
| ② AIR 依据与证据状态 | A1、A4、A5 ——**官方资料明确证实**；"逐动作敏感度分级表"**尚未核实**（C.4-7） |
| ③ 本项目真实实现状态 | 已实现：7 个卡片工具 `list_cards / apply_virtual_card / set_card_limit / freeze_card / unfreeze_card / report_card_loss / change_card_pin / close_card`（`app/tools/cards.py`，6 个写 + 1 读）；`card_manage` 由 `app/planner/card_actions.py:52 CARD_TEMPLATES` 按原话路由；虚拟卡黄灯（R05），限额/冻结/挂失/改密/销卡红灯（R09 三因子） |
| ④ 代码位置与测试证据 | `app/tools/cards.py`（挂失终态、幂等 summary）；`app/policy/rules.py` `CONFIRM_ONLY_TOOLS`/`SENSITIVE_TOOLS`；测试 `tests/test_tools_cards.py`、`tests/test_cards_close_pin.py`、`tests/test_card_intent.py` |
| ⑤ 能力缺口 | **G04** 缺"交易限制"维度：只有卡级冻结与额度，没有商户/品类黑名单（AIR 的 "Block a merchant"）；实体卡申请/补卡流程无（虚拟卡只能新开） |
| ⑥ 可借鉴设计 | E3 敏感动作强验证；E4 写操作白名单化；状态机式卡片生命周期（active/frozen/lost/closed） |
| ⑦ 改造复杂度与风险 | 2–3 人日 / 约 300 行 + 1 张表：`card_merchant_blocks` + 工具 `block_merchant`(黄) / `unblock_merchant`(黄) + R05；风险：`EVENT_TYPES` 是闭集（R2），新增审计事件要同步 `app/audit.py` 与测试；"商户"在模拟环境是文本匹配，需说明是演示能力 |
| ⑧ 对应赛题要求 | 场景 4 卡片管理（卡申请、额度调整、交易限制/解锁、挂失） |
| ⑨ 验收方法 | 每个动作断言最终状态与审计事件；挂失后再交易被拒；虚拟卡只可单次使用；新增 `block_merchant` 后该商户支付被拒且可解禁 |
| ⑩ 优先级 | **P1**（G04；现有四类动作已覆盖赛题主路径） |

### D05 订阅代扣

| 字段 | 内容 |
|---|---|
| ① AIR 公开能力 | 查看订阅、管理月度支出、暂停经常性支付；**未见**"自动识别订阅扣费 + 一键取消"的官方证据 |
| ② AIR 依据与证据状态 | A1、A4、A5 ——查询与管理**官方资料明确证实**；"一键取消"**尚未核实**（C.4-8）；"暂停"≠"取消"，AIR 的暂停语义未公开 |
| ③ 本项目真实实现状态 | 部分：`list_subs` 返回订阅列表 + 月度合计 + 下次扣款日（`app/tools/subs.py:29`），`cancel_subscription` 幂等取消（`:55`）；订阅是**种子表** `subs`（`app/db.py:193-201`，字段 `merchant/amount_fen/period/next_charge_ms/status`，`scripts/seed.py:295` 写入），不是从流水识别；账单侧只做"订阅涨价"异常（`bill.py:294 _subscription_hike_anomalies`）；无到期提醒 |
| ④ 代码位置与测试证据 | `app/tools/subs.py:29/55/92/_cancel_payload`；`app/planner/dag.py:43-71` 的 `subscription→list_subs`；测试 `tests/test_subscription_cancel.py`、`tests/test_tools.py`、`tests/test_acceptance.py` |
| ⑤ 能力缺口 | **G05**：①从流水自动识别周期性扣费（同一商户、金额稳定、间隔≈月/年）未实现；②续费提醒只在返回字段里显示日期，无主动提醒；③无"暂停"状态（只有 active/cancelled） |
| ⑥ 可借鉴设计 | E3 查询/取消走黄灯；E14 提醒定位为"基于本人流水的自研能力"，不得写成 AIR 对标；识别结果必须可解释（命中哪几笔） |
| ⑦ 改造复杂度与风险 | 5–7 人日 / 约 500–700 行 + 2 张表：识别器（按商户聚合 ≥3 笔、间隔方差阈值、金额波动阈值）产出候选；提醒表复用 G08 的 `proactive_alerts`；风险：误报会让"自动识别"变成骚扰，必须给置信度与"忽略此订阅"入口；`subs.status` CHECK 约束改动要迁移 |
| ⑧ 对应赛题要求 | 场景 5 订阅代扣（自动识别、续费提醒、一键取消）——**赛题原文比 AIR 更进一步**，AIR 无证据，需自研 |
| ⑨ 验收方法 | 构造 4 个月同商户扣款 → 识别为候选并给出证据笔；取消后下一周期不再扣；到期前 3 天产生提醒且只提醒一次；误报样本（同商户但金额/间隔不稳）不识别 |
| ⑩ 优先级 | **P0**（若主演示包含场景 5，识别与提醒必须做；否则场景 5 只能算"查询+取消"半个场景） |

### D06 理财与适当性

| 字段 | 内容 |
|---|---|
| ① AIR 公开能力 | 只读查看持仓、收益、行情；**明确禁止**下单/改单/撤单；声明不构成投资建议；未见推荐排序、产品对比、风险测评问卷 |
| ② AIR 依据与证据状态 | A1、A4、A6 https://www.revolut.com/legal/rita-disclaimer/ ——读写边界与免责**官方资料明确证实**（A6 原文 "You will not be able to place, modify, or cancel any order..."）；推荐/对比/适当性问卷**尚未核实** |
| ③ 本项目真实实现状态 | 部分：`list_products` 按风险/类别筛选（`app/tools/products.py:77`）、`estimate_risk` 返回 `risk_level/risk_label/summary/warnings/advice`（`:115`、`:162`、`:175`）；`buy_fund/redeem_fund` 走 R10 红灯 + 三因子（`app/tools/invest.py:44/83`）；持仓 `fund_holdings`。**无**风险承受能力问卷、无"承受能力↔产品风险"匹配、无产品对比表、无收益报表 |
| ④ 代码位置与测试证据 | `app/policy/rules.py` `INVEST_TOOLS` + R10；`app/planner/dag.py:35-40 INVEST_TEMPLATES`；测试 `tests/test_invest.py`、`tests/test_invest_intent_dag.py`、`tests/test_policy.py` |
| ⑤ 能力缺口 | **G06**：赛题场景 3 要求"产品推荐与对比、风险评估"，当前"风险评估"只是把产品风险等级翻译成一段文案，"对比"要用户自己问两遍 |
| ⑥ 可借鉴设计 | E4 的"声明偏离 + 补偿控制"：AIR 禁下单是法律边界，赛题要求一键申赎，我们**有意偏离**，补偿=红级三因子 + 冷静期文案 + 低承受能力拒售 + 全程审计；对比表用确定性字段生成，不让模型编收益率 |
| ⑦ 改造复杂度与风险 | 4–6 人日 / 约 400 行 + 1 张表：`risk_profiles(user_id, score, level, assessed_at)` + 只读 `assess_suitability` + `compare_products`；风险：合规话术（"不构成投资建议"必须与推荐同时出现）、红队"诱导买高风险"用例、R10 规则不可放宽 |
| ⑧ 对应赛题要求 | 场景 3 理财操作（推荐对比、风险评估、一键申购/赎回）；30% 安全合规 |
| ⑨ 验收方法 | 5 题问卷映射 R1–R5；低承受买 R4 被拒或转人工；对比表含期限/起购/风险/费率且字段来自 DB；申购必过三因子，审计含 `product_id+risk_level`；`pytest tests/test_invest.py tests/test_policy.py -q` 不回归 |
| ⑩ 优先级 | **P0**（场景 3 是六大场景之一，缺此项目前只能演示"申购"，演示不出"适当性"） |

### D07 跨场景规划

| 字段 | 内容 |
|---|---|
| ① AIR 公开能力 | 官方只证实"自有生态内串联"（旅行 eSIM、Stays、Experiences、RevPoints 一站完成）；**无**公开证据支持跨任意场景、跨天、带条件触发的长周期编排 |
| ② AIR 依据与证据状态 | A2、A4、A5——生态内串联**官方资料明确证实**；平台化多 Agent 编排来自 B1（**可信公开技术资料证实，原页未打开**）；"通用跨场景规划"**尚未核实** |
| ③ 本项目真实实现状态 | 部分：9 条固定模板（`app/planner/dag.py:43-71`），其中 `birthday_plan` 是**写死的单场景模板**；AA 可动态造图（`split_plan.py:42`）；`validate` 做四校验。能演示"生日锁 1000 + 订花"是因为模板预先写死，不是运行时组合。**复核补记**：dag.py 已新增 CROSS_TEMPLATES（redeem_then_transfer：赎回→确认收款人→查余额→转账），知识层识别后可传给建图；仍是白名单链路，不是通用组合 |
| ④ 代码位置与测试证据 | `dag.py:43-71`、`:180 split`、`validate`；`tests/test_birthday_intent.py`；`tests/test_invest_intent_dag.py`（意图→DAG 节点断言） |
| ⑤ 能力缺口 | **G07**：缺"目标→里程碑→定时→条件触发→预算占用"的通用组合层；新场景必须改代码加模板 |
| ⑥ 可借鉴设计 | E13 单步完成复杂任务作为验收；E6 平台化思路但**不引入多 Agent 框架**（README 已明确不用 LangGraph）；用现有 DAG+定时器做"计划"而不是新增编排引擎 |
| ⑦ 改造复杂度与风险 | 6–8 人日 / 约 500–700 行：在 `dag.build_or_single` 前加一层"复合计划解析"（识别 2 个以上目标 → 复用既有模板节点 → 生成联合 DAG），新增 `plan_goals` 表；风险：R1 `SYSTEM_PROMPT` 被测试钉死（不能靠改 prompt 实现，只能靠规则+槽位）、模板爆炸、`EVENT_TYPES` 闭集需扩 |
| ⑧ 对应赛题要求 | 场景 6 跨场景联动；3.3 DAG 拆解、中断/回退；40% 完成率 |
| ⑨ 验收方法 | 一句复合指令生成 ≥4 节点 DAG（查询余额→预留 1000→取消订阅→申购→定时支付），每节点权限灯正确；1000 元预留后超额转账被余额校验拒绝并可回滚；中断后从 `dag_runs` 可恢复到未完成节点 |
| ⑩ 优先级 | **P0**（场景 6 是"鼓励全覆盖"里最能体现创新的一项，且现有 birthday 模板可平滑升级） |

### D08 主动服务

| 字段 | 内容 |
|---|---|
| ① AIR 公开能力 | **没有**公开证据表明 AIR 会主动推送提醒或做财务监测；A9 的"tailored ads"是广告定向，不是财务顾问服务 |
| ② AIR 依据与证据状态 | A1、A3、A4、A5、A9——均无主动服务表述，**尚未核实，倾向否定**（研究文档 C.3-6）；不得把"主动"写成 AIR 对标能力 |
| ③ 本项目真实实现状态 | **部分实现**：`app/proactive.py`（确定性 int 分计算 + 仓储 + 两段事务的 `run_monitor`）、`app/tools/proactive.py`（7 工具）、`app/db.py` 四表（savings_goals / monitor_rules / proactive_alerts / monitor_runs）、`app/api/routes.py` 六端点 + 模拟支出端点、`web/index.html` 面板。**监测仅手动/脚本触发**，`app/planner/ticker.py` 仍只跑预约转账，不是后台常驻服务。 |
| ④ 代码位置与测试证据 | `app/proactive.py`、`app/tools/proactive.py`、`app/db.py`、`app/api/routes.py`、`web/index.html`；`tests/test_proactive_calc.py`、`test_proactive_api.py`、`test_proactive_safety.py`、`test_security_proactive.py`（共 30+ 条），浏览器冒烟已走通建目标→模拟支出→监测→提醒→处理→重算 |
| ⑤ 能力缺口 | **G08 收窄为**：无后台常驻调度与推送通道（仅页面/脚本手动触发）；其余规则化监测、提醒生命周期（产生/去重/忽略/已处理）、手动触发入口均已实现 |
| ⑥ 可借鉴设计 | E14：定位为**自研增强**，对外话术只说"打开助手就能看到提醒"，不宣称 7×24 推送；设计上"手动可重复触发"优先于后台常驻（与基线建议一致） |
| ⑦ 改造复杂度与风险 | 已落地 4 张表 + 7 工具（3 只读、4 黄灯确认）+ 六端点；风险：误报、重复提醒、无推送通道（单机演示只能页面轮询）、审计事件闭集、R7 单用户 |
| ⑧ 对应赛题要求 | 无直接场景；支撑场景 5"续费提醒"与场景 2"异常识别"的主动化表达 |
| ⑨ 验收方法 | 规则命中生成 alert 且幂等（同 rule+周期只一条）；`resolve` 后不再出现；每次扫描写 `proactive.monitor_run` 审计；前端徽标数 = 未解决数；连续两次扫描不产生重复条目 |
| ⑩ 优先级 | **P1**（G05 的最小提醒可先复用其表，但完整主动服务不阻塞六场景） |

### D09 上下文与多轮

| 字段 | 内容 |
|---|---|
| ① AIR 公开能力 | 自建上下文与记忆、每客户隔离 profile、直连内部知识；官方未公开上下文窗口、记忆保留期与清除方式 |
| ② AIR 依据与证据状态 | B1 Pavel Nesterov LinkedIn（**可信公开技术资料证实，原页未打开**）；窗口/记忆机制/保留期**尚未核实**（研究文档 C.4-4） |
| ③ 本项目真实实现状态 | 部分：路由层 4 轮（`app/api/routes.py:85 HISTORY_TURNS`）、意图层 6 轮（`app/planner/intent.py:153`）、单会话上限 16（`HISTORY_PER_SESSION`）；`clarify.py` 的 Pending 只处理"收款人歧义/序号"续答，TTL 5 分钟且**只在内存**（`:26 CLARIFY_TTL_MS`、`:38 Pending`、`:84 augment`）；无偏好记忆、无跨会话记忆、无"那笔/上次那个"指代 |
| ④ 代码位置与测试证据 | `intent.py:210-219 build_messages`（最近 6 轮+末尾信封）、`routes.py:519 _remember`；测试 `tests/test_clarify_resume.py`、`tests/test_api.py` |
| ⑤ 能力缺口 | **G09**：省略主语/指代（"再转 100"、"上次那个收款人"）不能稳定续答；用户偏好（常用收款人、默认账户、风险偏好）不落库 |
| ⑥ 可借鉴设计 | E9 隔离 profile：我们用单机 SQLite + 用户可清除，不把账户明细塞进 prompt，只放结构化槽位与偏好 |
| ⑦ 改造复杂度与风险 | 4–6 人日 / 约 400 行 + 1 张表 `user_prefs`；指代解析用"最近 N 笔 + 最近收款人"规则，不让模型自由发挥；风险：上下文进 prompt 的隐私边界（R7 单用户）、R1 prompt 不可改、澄清状态不落库导致重启丢失 |
| ⑧ 对应赛题要求 | 3.3 模糊/省略/隐含意图；30% 创新交互 |
| ⑨ 验收方法 | "转 50 给小王"→"再转 100"能命中同一收款人；"上次那个收款人"解析正确；重启后偏好仍在；提供清除偏好端点并写审计 |
| ⑩ 优先级 | **P1**（现有 clarify 已能演示"追问一次"，通用多轮是加分） |

### D10 安全确认

| 字段 | 内容 |
|---|---|
| ① AIR 公开能力 | 敏感动作要求生物识别；Business 版动作前校验角色权限；写操作范围受限（投资写侧整类禁止） |
| ② AIR 依据与证据状态 | A4（生物识别）、A7 https://www.revolut.com/legal/assistant-business-disclaimer/ （角色权限）——**官方资料明确证实**；逐动作权限分级表**尚未核实**（C.4-7） |
| ③ 本项目真实实现状态 | 已实现（本项目最强项）：12 条规则 R01–R12（`app/policy/rules.py`）、令牌七步（`app/policy/token.py`）、`params_hash` 绑定、日累计 1000 元（`SINGLE_LIMIT_FEN=DAILY_LIMIT_FEN=100_000`）、熔断（5 分钟 3 次→60 秒）、三因子（mock）、哈希链审计 + `/api/audit/verify`。缺口：多因子是模拟（R6）、无登录/多用户/RBAC（R7）、额度固定不可配置 |
| ④ 代码位置与测试证据 | `app/policy/rules.py`、`token.py`、`breaker.py`、`app/audit.py`；测试 `tests/test_policy.py`、`test_token.py`、`test_breaker.py`、`test_confirm_*`、`test_injection*.py`、`tests/redteam/cases.yaml`（43 条，**本轮未复跑**） |
| ⑤ 能力缺口 | **G10**：真实认证通道（短信/UKey/人脸）与多用户 RBAC、分级额度配置——属"需外部服务"的 P2；安全内核本身不缺 |
| ⑥ 可借鉴设计 | E3/E12：动作前二次校验、权限先于执行；多因子做成**可插拔 provider**，演示用桩、生产可换 |
| ⑦ 改造复杂度与风险 | 8–12 人日（外部短信/UKey 接入 + 用户体系 + 迁移）；风险：演示环境无法接真实银行认证，不能宣称"已对接"；R7 改动面大（所有工具都要 user_id） |
| ⑧ 对应赛题要求 | 3.1 三色权限、30% 安全合规、安全自评 |
| ⑨ 验收方法 | 现有安全测试全绿；新增"同一用户 24h 第 11 笔 100 元被拒"；未认证请求 401；只读角色无法发起转账（若做 RBAC） |
| ⑩ 优先级 | 现状 **P0（保持，不打折）**；G10 新增部分 **P2**，本次只做"可插拔 provider 接口 + 说明" |

### D11 隐私与数据最小化

| 字段 | 内容 |
|---|---|
| ① AIR 公开能力 | 三条承诺：不拿用户数据训练、交互结束不留存、只访问用户已可见的数据；另称交互记录会留存用于质量评估（与"不留存"存在口径张力，研究文档已标注） |
| ② AIR 依据与证据状态 | A3、A4、A8 隐私政策、A6——**官方资料明确证实**（承诺本身）；"零留存"与"留存做评估"如何并存**尚未核实** |
| ③ 本项目真实实现状态 | 已实现（强项）：`envelope.py:19 ALLOWED_SOURCES` 白名单 + `MAX_TEXT_LEN` 截断；`_audit_intent` 只落 `text_len` **不落原文**（`routes.py:386`）；账户数据不进模型上下文（工具结果由 `events.summarize` 确定性生成）；澄清状态内存 TTL 不落库。缺口：无数据地图/保留期文档、无用户导出/删除入口、知识层接入后 63KB 法规文本会进 prompt（需边界） |
| ④ 代码位置与测试证据 | `app/planner/envelope.py`、`app/api/routes.py:386`、`app/planner/events.py summarize`；测试 `tests/test_injection*.py`、`tests/test_audit.py` |
| ⑤ 能力缺口 | **G11**：隐私口径未文档化（"我们存什么/存多久/怎么删"），知识文本外发边界未定义 |
| ⑥ 可借鉴设计 | E10 三条隐私承诺 + 一页"数据地图"；对知识层明确"只发送命中的法规片段，不发送账户数据" |
| ⑦ 改造复杂度与风险 | 1–2 人日（文档 + 一条断言测试 + 数据地图）；风险：低；但写错会成合规把柄，必须与代码逐条对齐 |
| ⑧ 对应赛题要求 | 3.2 安全（数据安全）、安全自评 |
| ⑨ 验收方法 | 审计 payload 无原文的断言测试；数据地图覆盖每类数据的存储位置/保留期/删除路径；prompt 内容断言不含账户明细（可用 message 快照测试） |
| ⑩ 优先级 | **P1**（成本极低、答辩加分，建议与 P0 同批做） |

### D12 可解释与来源引用

| 字段 | 内容 |
|---|---|
| ① AIR 公开能力 | AI 透明度声明、输出"as is"可能不准确、对 AI 生成内容做标识；未见"每个结论给出来源链接/条款号" |
| ② AIR 依据与证据状态 | A6、A4——透明度与免责**官方资料明确证实**；"逐结论来源引用"**尚未核实**；RAG/知识直连来自 B1（**可信公开，原页未打开**） |
| ③ 本项目真实实现状态 | 部分：`explain` 固定三行——颜色+规则号、`reason_text`、`_basis`（`app/planner/engine.py:43`、`:18-30` 共 11 条硬编码依据）；`trace` 事件带 explain；**无**知识来源 ID、无"为什么推荐这个产品"的产品级解释；`Retrieved.knowledge_ids` 已设计但未接线。**复核补记**：routes.py 的 _audit_knowledge 已把 knowledge.retrieved 审计落库（含 ids/sources/version/degraded/cross），但 explain 的 _BASIS 仍是 11 条硬编码，尚未引用知识来源 |
| ④ 代码位置与测试证据 | `engine.py:18-30 _BASIS`、`:43 explain`；`events.py trace_event`；`app/knowledge/models.py Retrieved`；测试 `tests/test_api.py`（trace 事件）、`tests/test_audit.py` |
| ⑤ 能力缺口 | **G12**：解释只解释"权限/规则"，不解释"业务结论依据"；无来源编号进入审计与前端 |
| ⑥ 可借鉴设计 | E11 交互记录可审计 + 知识 ID/来源 ID 入审计（基线接入建议）；来源分级复用三档，**不得把"未核实"显示成"官方"** |
| ⑦ 改造复杂度与风险 | 3–5 人日：`_BASIS` 改为规则号+知识命中（知识未命中则说明"仅基于规则"）；审计 payload 增 `knowledge_ids/source_ids`；前端 trace 卡展示来源与核验状态；风险：R2 事件闭集、来源等级被误标 |
| ⑧ 对应赛题要求 | 3.2 幻觉防护、可解释；30% 安全合规 |
| ⑨ 验收方法 | 任一转账/理财决策的 trace 含规则号；知识命中时含 ≥1 个来源 ID 与核验状态；问"为什么是红灯"能引用规则原文；来源为"未核实"时前端显式标注 |
| ⑩ 优先级 | **P0**（与 G14 同批交付，否则知识层"有知识、说不出来源"） |

### D13 评估与可观测性

| 字段 | 内容 |
|---|---|
| ① AIR 公开能力 | 官方未公开评测方法；B1 称有多 Agent 平台、guardrails、evals、monitoring、标注与 QA/QC 流程、集中式 AI 网关 |
| ② AIR 依据与证据状态 | B1（**可信公开技术资料证实，原页未打开**）；具体指标/阈值/数据集规模**尚未核实** |
| ③ 本项目真实实现状态 | **已补强**：`tests/eval/` 80 例中文评估集 + 可复现脚本 + A/B 报告（真模型，6 项指标：intent/slot/clarify/dag/knowledge_citation/unauthorized）；全量 **1131 passed / 4 skipped**（2026-10-23 终测）、60 个测试文件、43 条红队 **PASS 43 / FAIL 0**、前端 66 项、哈希链审计可验证。**仍无**延迟/token/成本指标与人工质量抽样 |
| ④ 代码位置与测试证据 | `tests/`、`tests/eval/{intent_cases.yaml,run_eval.py,evaluation_report.md,test_eval_harness.py}`、`scripts/redteam.py`、`scripts/check_frontend.py`、`app/api/routes.py /api/audit/verify` |
| ⑤ 能力缺口 | **G13**：40% 完成率需要"可量化的意图理解准确率"，当前只有"测试全绿"这一间接证据 |
| ⑥ 可借鉴设计 | E7：小步引入 evals + 标注集，不建平台；指标只做三项（意图准确率、工具成功率、P95 延迟） |
| ⑦ 改造复杂度与风险 | 3–5 人日：`eval/` 目录 + 80–100 句标注（六场景+模糊+拒绝样本）+ 脚本输出准确率/混淆矩阵 + CI 阈值；风险：R4 模型随机性（temperature=0 + 3 次多数投票）、不要把评测写成"刷分" |
| ⑧ 对应赛题要求 | 40% 任务完成率、技术文档 |
| ⑨ 验收方法 | `python -m eval.run` 输出 ≥80 句的总准确率与分场景召回，低于阈值退出码非 0；报告可附进技术文档；重跑结果稳定（同输入两次差异 ≤2%） |
| ⑩ 优先级 | **P0**（成本低、直接支撑 40% 评分与答辩"证据链"） |

### D14 领域知识与术语

| 字段 | 内容 |
|---|---|
| ① AIR 公开能力 | 直连内部知识库与产品生态，能回答产品/条款类问题；未见术语表或法规知识库的公开细节 |
| ② AIR 依据与证据状态 | B1（**可信公开技术资料证实，原页未打开**）；A6（AI 透明度）——**官方资料明确证实**；知识库结构**尚未核实** |
| ③ 本项目真实实现状态 | **已完成**：`app/knowledge/` 6 个模块 + 5 个 YAML（terms 40 / entities 25 / relations 22 / intents 11 / workflows 10 / sources 34），`app/planner/kb_bridge.py` 接入 intent + dag + routes，`knowledge.retrieved` 审计落库；知识集成测试 7 条失败已收口，全量 1131 passed；加载失败降级为「空知识 + warning + 追问」并有专门测试 |
| ④ 代码位置与测试证据 | `app/knowledge/loader.py`、`retrieve.py`（docstring 明确"不引入向量检索"）、`app/planner/kb_bridge.py`、`app/api/routes.py:_audit_knowledge`；测试 `tests/test_knowledge_{loader,normalize,retrieve,integration}.py`、`tests/test_security_knowledge.py`；43 条红队已复跑 PASS 43 / FAIL 0 |
| ⑤ 能力缺口 | **G14 已关闭**（原缺口：缺 4 个数据文件、缺接线、缺审计与降级策略）；残余：知识仅参与「问不问/填什么/怎么解释」，不进模型上下文（受 SYSTEM_PROMPT 逐字测试约束），语义泛化仍靠别名表 |
| ⑥ 可借鉴设计 | E5 知识库+产品生态直连；知识是**数据不是指令**（基线原则），冲突时降级并审计；检索用结构化/关键词即可，**不上向量库** |
| ⑦ 改造复杂度与风险 | 8–12 人日：补 terms 80–120 条、ontology、intent_rules 同义词、workflows 与 6 场景对齐；接线 4 处（parse 前后、build_messages、dag 校验、policy 只收窄）；风险：R1（不改 SYSTEM_PROMPT，知识只能从 user 消息注入）、注入攻击（知识文本按 DATA 处理）、加载失败拖垮主链路（需降级为空知识+告警）、R2 事件闭集 |
| ⑧ 对应赛题要求 | 3.3 领域理解；技术文档"知识来源可追溯"；40% 完成率 |
| ⑨ 验收方法 | 80 条术语命中/未命中测试；缺文件时降级不崩且写 `knowledge.warning`；现有 916 条测试零回归；`knowledge.retrieved` 审计含 ids 与版本 |
| ⑩ 优先级 | **P0**（阶段 0 已投入，不接线等于白做；也是 G12 的前置） |

### D15 中断/回退/人工接管

| 字段 | 内容 |
|---|---|
| ① AIR 公开能力 | 有转人工/live agent 的表述（法律声明中出现）；任务取消、步骤回退、人工接管的产品化细节未公开 |
| ② AIR 依据与证据状态 | A6——"转人工"**官方资料明确证实**（仅限声明语境）；取消/回退/接管机制**尚未核实** |
| ③ 本项目真实实现状态 | 部分：`/api/cancel` 取消进行中请求；确认超时（`tests/test_confirm_timeout.py`）；两阶段资金 hold 超时释放；`reverse_transfer` 冲正 + 审计；熔断降级；执行器失败降级（`executor`）。**无**转人工队列/工单、无"从断点继续未完成 DAG"的产品化入口 |
| ④ 代码位置与测试证据 | `app/api/routes.py`（cancel 端点）、`app/tools/funds.py`、`app/tools/reverse.py`、`app/policy/breaker.py`；测试 `test_reverse.py`、`test_confirm_timeout.py`、`test_breaker.py`、`test_funds.py` |
| ⑤ 能力缺口 | **G15**：无人工接管通道与工单号；DAG 中断后只能重新发起，不能续跑 |
| ⑥ 可借鉴设计 | 赛题 3.3 明确要求"中断/回退/人工接管"；最小实现=转人工卡片（生成 ticket_id + 原因 + 上下文快照）+ DAG 断点表 |
| ⑦ 改造复杂度与风险 | 5–8 人日（真正接坐席系统则更高）；风险：无外部工单系统 → 只能做占位；DAG 续跑要处理幂等与已执行节点 |
| ⑧ 对应赛题要求 | 3.3 中断/回退/人工接管；30% 安全合规 |
| ⑨ 验收方法 | 取消后 hold 释放、无副作用；冲正后余额复原且审计两条（原交易+冲正）；转人工生成 ticket 并带上下文快照；中断的 DAG 可续跑且不重复扣款 |
| ⑩ 优先级 | 取消/冲正 **P0 已有**；转人工与断点续跑 **P2** |

---

## 二、差距汇总与依赖

### 2.1 按优先级归并

| 优先级 | 缺口 | 判定理由 |
|---|---|---|
| **P0** | G14 知识层、G12 来源引用、G05 订阅识别+提醒、G06 适当性、G07 通用跨场景、G13 评测集 | 直接对应赛题六场景与 40% 完成率；G14 已投入沉没成本；G12 是"可解释"的硬证据 |
| **P1** | G01 交互、G02 储蓄测算、G03 可视化、G04 商户限制、G08 主动监测、G09 通用多轮、G11 隐私文档 | 提升完成度与观感，但不做也能跑通六场景；G11 成本极低应提前 |
| **P2** | G10 真实认证/RBAC、G15 转人工、G16 MCP、G17 定时周期 | 需外部服务/账号体系/协议面，10 天内不可控 |

### 2.2 依赖关系（关键路径）

```
G14 知识层（补 4 YAML + 接线 + 降级 + 审计）
 ├── G12 来源引用（_BASIS 接知识 + 审计 source_ids + 前端展示）
 ├── G06 适当性（产品对比/风险匹配需要术语与规则知识）
 └── G09 多轮（知识命中可用于省略意图补全，非强依赖）

G05 订阅识别+提醒 ──(最小共享表 proactive_alerts)── G08 主动服务（完整版）
G07 通用跨场景 ──依赖已有工具（转账/订阅/理财/定时）── 无前置缺口
G13 评测集 ──独立，可并行
G01 交互 ──演示前最后做（避免中途改前端影响联调）
```

### 2.3 与 AIR 的"有意偏离"声明（必须在技术文档写清）

| AIR 边界 | 我们的选择 | 补偿控制 |
|---|---|---|
| 投资写侧整类禁止（A6） | 按赛题实现一键申购/赎回 | 红级三因子 + 适当性校验 + 冷静期文案 + 全程审计 |
| 敏感动作生物识别（A4） | 三因子为 mock | 明确标注"演示桩"，接口可插拔，不宣称已对接银行 |
| 只访问已可见数据（A4） | 相同 | 信封白名单 + 审计不落原文 |
| 无主动服务证据 | 自研主动提醒（G08） | 对外不称"对标 AIR"，只称"自研增强" |

---

## 三、工作量与关键路径

### 3.1 人日汇总

| 分组 | 缺口 | 人日 | 代码量级 |
|---|---|---|---|
| 知识+解释 | G14 8–12、G12 3–5 | 11–17 | ~1200 行 + 4 个 YAML（约 1500 行数据） |
| 场景补全 | G05 5–7、G06 4–6、G07 6–8 | 15–21 | ~1600 行 + 4 张表 |
| 评测 | G13 3–5 | 3–5 | ~400 行 + 80 句标注 |
| **P0 合计** | 6 项 | **29–43** | — |
| P1 合计 | 7 项 | 16–27 | — |
| P2 合计 | 4 项 | 19–30 | 需外部服务 |

### 3.2 时间现实（2026-10-23 → 截止 2026-11-02，约 10 天）

- P0 全量 29–43 人日，单人窗口不可能完成；按 **3 人并行 × 8.5 天 ≈ 25 人日** 规划，必须做"最小可交付 P0"（口径见 `implementation_priorities.md` §2.2）：G14 收窄为 6 人日（术语 60 条 + 接线 3 处 + 降级）、G12 2 人日、G05 4 人日（只识别+一次性提醒，不做暂停）、G06 3 人日（问卷+对比表）、G07 4 人日（生日复合计划）、G13 2 人日（80 句）= **21 人日**；再加 3 项低成本 P1（G01 2、G03 1、G11 1）= **本次 25 人日**。
- 关键路径：G14 → G12 / G06（串行 11 人日）为最长链；G07 与 G13 可并行；G01 放最后 2 天。
- 若只剩 1–2 人：砍 G07 与 G06 到"演示脚本固定路径"，保 G14+G12+G13（可解释与评测是答辩最容易被追问的）。

---

## 四、未核实清单

### 4.1 AIR 侧（沿用研究文档，禁止在答辩中当事实讲）

| # | 未核实项 | 研究文档位置 |
|---|---|---|
| 1 | LLM 供应商与具体型号 | C.4-1（B8 明确"未披露"） |
| 2 | RAG/向量库实现细节 | C.4-2（B1 只提 pipelines） |
| 3 | 工具协议与端点（是否 MCP/OpenAPI） | C.4-3 |
| 4 | 上下文窗口与记忆保留期 | C.4-4 |
| 5 | 意图识别/任务完成率等评测指标 | C.4-5 |
| 6 | 防幻觉与 prompt 注入的具体机制 | C.4-6 |
| 7 | 逐动作权限分级表 | C.4-7 |
| 8 | 订阅"一键取消"、自动识别扣费 | C.4-8 |
| 9 | 端到端延迟与成本 | C.4-9 |
| 10 | 主动提醒/财务监测能力 | C.3-6（倾向否定） |
| 11 | 消费者端图表形态、异常交易识别、月/年报 | Q3 |
| 12 | 语音入口 | 仅 B1 提及，原页未打开 |

### 4.2 本项目侧（写作时状态；除注明外已在 2026-10-23 交付时收口）

| # | 事项 | 现状 |
|---|---|---|
| 1 | 43 条红队用例 | **已复跑：PASS 43 / FAIL 0**（`python3.12 -m scripts.redteam`，约 12 秒） |
| 2 | 知识层加载 | **已收口**：5 个 YAML 可加载（version 59ff01419aac0b29）；集成测试全绿；降级策略有测试（`KNOWLEDGE_ENABLED=0` 与加载失败两路） |
| 3 | 多因子认证 | 代码为 mock（基线 R6），未接真实短信/UKey |
| 4 | 季付/周付定时 | README 称未实现，代码 `RECURRENCES=("once","monthly")` 一致，确认缺失 |
| 5 | 前端移动端适配 | 未做断点验证，未测真机 |

---

## 五、来源对照（只列本文引用到的编号）

| 编号 | URL / 来源 | 用途 | 状态 |
|---|---|---|---|
| A1 | https://www.revolut.com/air-ai-by-revolut/ | 产品定位、卡片/订阅 Tab | 官方证实 |
| A2 | revolut.com en-NO 产品页 | 生态内串联 | 官方证实 |
| A3 | https://www.revolut.com/news/revolut_enters_new_era_of_money_intelligence_with_launch_of_ai_assistant/ | 执行引擎、1300 万、隐私承诺 | 官方证实 |
| A4 | https://www.revolut.com/blog/post/air-ai-by-revolut/ | 能力清单、生物识别、as-is | 官方证实 |
| A5 | https://help.revolut.com/help/profile-and-plan/getting-started-with-air/ | 入口、"execution engine" | 官方证实 |
| A6 | https://www.revolut.com/legal/rita-disclaimer/ | 投资写侧禁止、免责、留存评估、转人工 | 官方证实 |
| A7 | https://www.revolut.com/legal/assistant-business-disclaimer/ | Business 角色权限 | 官方证实 |
| A8 | Revolut 隐私政策 | 数据承诺 | 官方证实 |
| A9 | tailored-ads 页 | 广告≠主动服务 | 官方证实（用于否定推断） |
| B1 | Pavel Nesterov LinkedIn | 多 Agent、RAG、evals、记忆、知识直连 | 可信公开技术资料（原页未打开） |
| B8 | fintechweekly | 未披露模型供应商 | 可信公开 |
| F1–F7 | 抓取失败记录 | 说明为何有"未核实" | 过程证据 |

> 引用规则：B1 的任何结论在正文中必须带"原页未打开"限定；"尚未核实"项不得在演示话术里说成能力。