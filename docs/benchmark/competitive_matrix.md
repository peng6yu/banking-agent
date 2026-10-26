# 竞品对标矩阵

> 本文把三个已商用的银行 AI 助手（Revolut AIR、Starling Assistant、Fiserv agentOS）与本项目（银行智能体竞赛原型）放在同一张表上逐项对照。
> 每一格都带证据档位；凡公开资料不支撑的，一律写「无法从公开资料确定」，不猜测竞品内部实现。
> 采集日期：2026-10-23。覆盖的产品状态：AIR（英国，2026-04-09 起分批灰度）、Starling Assistant（个人版 2026-03-20 上线、business 版 2026-08-24 上线）、agentOS（2026-05-14 发布，官方预期 2026-08 广泛可用）。

## 0. 方法与证据分级

### 0.1 四档证据定义

本文出现的每一条竞品陈述都必须挂一个档位标签：

| 档位 | 定义 | 使用条件 |
|---|---|---|
| **【官方确认】** | 厂商自己的产品页、帮助中心、法律告知或新闻稿原文 | 必须能引到具体 URL，且有可直接引用的原句 |
| **【第三方报道】** | 可信媒体的报道 | 必须能引到具体 URL；只用于旁证，不用来单独支撑关键结论 |
| **【合理推断】** | 由公开可见的产品行为反推 | 必须在文中显式写明「这是推断」，并说明推断依据 |
| **【无法确认】** | 公开资料无法确定 | 统一写「无法从公开资料确定」，**不写猜测、不写「大概是」、不写「业内通常」** |

档位只描述**证据强度**，不描述**能力高低**。一条【官方确认】只说明厂商这么说了，不说明它做得好；一条【无法确认】也不说明它没做，只说明我们查不到。

### 0.2 采集与复核方法

- **官方一手优先**：先抓厂商自己的产品页/帮助中心/法律文本/新闻稿；媒体稿只在官方文本缺失时作旁证。
- **直连失败的处理**：revolut.com 全站对脚本直连返回 403（Akamai 拦截页），本文涉及的 Revolut 页面均用无头浏览器实际渲染后读取正文；investors.fiserv.com 与 fiserv.gcs-web.com 对直连同样返回 403，agentOS 发布稿正文经该稿的联合分发副本（GlobeNewswire 分发、财经数据服务转存）取得。上述获取方式在 §4 逐条注明，读者可自行复核。
- **时点风险**：官方页面会改动。本文只对 2026-10-23 抓取到的文字负责；三个月后同一条结论可能需要重抓。
- **不做的四件事（本文的自我约束）**：
  1. 不对竞品的**内部模型**（供应商、型号、参数、微调方式）做任何猜测；
  2. 不对竞品的**数据库与数据架构**做任何猜测；
  3. 不对竞品的**权限架构实现**（策略引擎形态、令牌机制、规则表）做任何猜测——官方只说了「会检查角色权限」，我们就只写这一句；
  4. 不对竞品的**规划算法**（是否多步、是否用 DAG、是否 ReAct）做任何猜测——官方只说了 agentic / execution engine，我们就只写这两个词。
- **本项目一侧的口径**：本项目的格子来自仓库内可复现的证据（代码、测试、文档），因此标【官方确认】表示「仓库内可查证」，并在 §2.4 与 §4.2 给出行号级出处。竞赛原型不是生产系统，这一点在「数据来源」一行已如实写明。

### 0.3 维度级证据强度的评级口径

每个格子带自己的档位；「证据强度」一行给的是**该列整体**的可核验程度，取该列最弱的一档：
- 该列全部关键陈述都有官方原句 → 【官方确认】；
- 关键陈述只有媒体旁证 → 【第三方报道】；
- 关键结论靠行为反推 → 【合理推断】；
- 核心问题（如权限分级、审计格式、主动推送）查不到 → 【无法确认】。

## 1. 对比矩阵（10 维度）

| 维度 | Revolut AIR | Starling Assistant | Fiserv agentOS | 本项目 | 证据强度 |
|---|---|---|---|---|---|
| **用户入口** | App 内对话：下滑手势或 Profile→Chats→AIR；另有商务版【官方确认】¹ | App 内对话，支持语音与自然语言；个人与 business 双版本【官方确认】² | 面向金融机构的 B 端平台，不是消费者入口【官方确认】³ | 单页 Web 聊天 + 右侧决策剧场五面板；无 App、无 IM【官方确认】⁴ | A / A / A / A |
| **财务洞察** | 支出模式与趋势、订阅、投资组合表现、旅行预算【官方确认】⁵ | 个性化洞察（直扣笔数、待付账单、对某收款人的历史）、消费小测【官方确认】⁶ | 运营报表、存款情报、AML 分诊等 B 端分析 agent【官方确认】⁷ | 账单月/年分类、Top 商户、三类异常（拆单、凌晨境外、订阅涨价）【官方确认】⁸ | A / A / A / A |
| **业务执行** | 冻结卡、暂停订阅、开虚拟卡、买 eSIM、旅行规划；**投资只读，不能下单**【官方确认】⁹ | 建 Space、设自动转账、按命令付款、税务留存、发票欺诈提示【官方确认】¹⁰ | 在银行核心/支付/发卡/服务流程内执行，非 C 端动账【官方确认】¹¹ | 35 个工具：转账（AA/定时/冲正）、卡片五件套、理财申赎、订阅取消、生日跨场景【官方确认】 | A / A / A / A |
| **任务规划** | 官方自称 execution engine、可单步完成复杂金融任务；**编排算法无法确认**【官方确认】+【无法确认】¹² | 官方称 agentic，示例含「先算月供再设自动转账」的多步任务；**编排算法无法确认**【官方确认】+【无法确认】¹³ | 治理层有 routing & optimization 与 agent 配置；**agent 内部规划无法确认**【官方确认】+【无法确认】¹⁴ | 意图解析（LLM 产 intent + 确定性拒答层 + 词表兜底）→ 9 个模板生成 DAG → 4 道结构校验 → 拓扑序执行 → 中断/接管/回退【官方确认】 | A（定位）/ A（定位）/ A（平台）/ A |
| **安全机制** | 敏感动作需生物识别；只访问用户已可见的数据；Business 版按角色权限校验；零数据留存【官方确认】¹⁵ | 平台建于 Google Gemini + Google Cloud；**逐动作权限分级无法确认**【官方确认】+【无法确认】¹⁶ | 治理层：permissioning & identity、policy controls、kill/suspend、审计日志与可观测、计费；identity-bound execution【官方确认】¹⁷ | R01–R13 三色裁决（单笔 > 1000 元转红；日累计 1000 元是**风险分级线，不是拒绝线**，升红后强验证通过仍真实出账）+ HMAC 能力令牌七步校验（TTL 120s）+ 哈希链审计 + 熔断【官方确认】 | A / B / A / A |
| **主动管理** | 官方博客称 24/7 personal assistant，但公开场景全部由用户发起【合理推断】¹⁸ | 官方措辞 proactive partner / proactive money management，示例仍是用户发起；是否主动推送**无法确认**¹⁹ | 无 C 端主动提醒；B 端有常驻监测型 agent（监管监测、信用早期预警，按订阅计费）【官方确认】²⁰ | 储蓄目标、监测规则、提醒、运行日志，**手动触发**，不声称 7×24【官方确认】 | C / D / A / A |
| **透明度** | 明示不提供财务/投资建议、AI 可能不准、答不了自动转人工；不展示内部推理【官方确认】²¹ | 官方材料未披露推理展示或审计可见性【无法确认】²² | 把审计日志与可观测作为平台卖点；日志格式与呈现方式**无法确认**²³ | 决策剧场实时展示意图/DAG/三色/裁决因子/审计事件，审计链可校验【官方确认】 | A / D / A（存在）/ A |
| **数据来源** | 用户自身数据（交易、投资、卡）+ 第三方实时行情 + 条款/FAQ 文本【官方确认】²⁴ | 客户自身账户数据；运行于 Google Cloud【官方确认】²⁵ | Fiserv 核心/支付/发卡/服务数据；第三方数据标注为 coming soon【官方确认】²⁶ | **竞赛原型**：SQLite 33 张表，种子演示数据；无真实银行核心、无真实认证服务对接【官方确认】 | A / A / A / A |
| **当前项目差异** | 差异：真实用户规模、真实资金链路、生物识别因子，我们只有演示数据与自建令牌【合理推断】²⁷ | 差异：语音入口、真实账户执行、每周迭代节奏【合理推断】²⁸ | 不同赛道：它是给银行的治理平台，我们是给终端用户的助手；其治理层清单值得对照【合理推断】²⁹ | 基准列（本文件即对本项目的自评）【官方确认】 | C / C / C / — |
| **证据强度** | 行为层【官方确认】为主；权限分级、编排算法【无法确认】 | 产品层【官方确认】为主；权限分级、审计、主动推送【无法确认】 | 平台层【官方确认】为主；C 端与内部实现【无法确认】 | 代码与测试台账【官方确认】 | A 为主 / A 为主 / A 为主 / A |

**脚注**（脚注只补充细节，不改变格子的档位；URL 见 §4）

1. AIR 入口：「Just swipe down from the middle of your app screen, or go to Profile → Chats → AIR」[R1]；英国分批灰度 [R2]。商务版另有 AI Revolut Assistant 为企业账户服务 [R6]。
2. Starling Assistant「responds to voice and natural language prompts before carrying out banking tasks on the customer's behalf」[S1]；business 版 2026-08-24 面向全部企业客户 [S2]。
3. agentOS 的公开材料全部指向 financial institutions / banks and credit unions，产品页四层栈没有消费者入口 [F1][F2]。
4. 本项目前端为单页零依赖 `web/index.html`，左侧会话、右侧意图/DAG/三色/因子/审计五面板；无移动端与 IM 渠道（详见 `docs/提交物对照表.md` 的渠道差异一节）。
5. 「insights into spending patterns, trends, and activity」[R2]；产品页 tab 含 Holiday budgeting / Portfolio performance / Subscription management [R1]。
6. 「How many direct debits do I pay each month?」「How many bills do I have left to pay this month?」「Can you tell me more about my transaction history with this payee?」，以及按消费生成小测 [S1]。
7. 发布稿列出四个首发 Fiserv agent，覆盖商贷开户、运营分析报表、存款情报、AML 分诊 [F2]；产品页按 Intelligence / Efficiency / Loss mitigation / Growth 四类用途归类 [F1]。
8. 本项目 analyze_bill 支持月度/年度分类汇总、Top 商户与三类异常（拆单、凌晨境外、订阅涨价），见 `app/tools/` 与场景文档。
9. 「Freeze my card」「I need to get a single-use virtual card」「Pause recurring payments」[R4]；投资边界原文：「You will not be able to place, modify, or cancel any order in relation to financial instruments, nor take any other action that would result in you entering into a transaction.」[R5]。
10. 「Set me up with dedicated Spaces…」「can you set up automatic transfers」[S1]；Tax Ring-Fencing「Put 20% of last month's earnings aside for VAT.」→ 算出金额并转入 Space [S2]。
11. 「Built to operate natively across Fiserv's platforms—core, payments, issuer processing, and servicing」[F2]。
12. 「It's an execution engine designed to help you manage your money…」[R2]；「executes complex financial tasks in a single step」（指 AIR）[R7]。是否多步、如何编排，公开材料未描述。
13. 「goes beyond a standard Q&A by executing complex banking commands」[S2]；多步示例见 [S1] 的 Paris 储蓄问答。编排方式未公开。
14. 产品页 LAYER 03 Models 写明「Routing & optimization」，LAYER 02 有 Agent configuration [F1]；agent 内部如何规划未公开。
15. 「AIR cannot perform sensitive actions without your biometric approval.」[R4]；「It only accesses the data you can already see」[R1]；「Before showing account information or initiating an action, the AI Assistant checks the permissions attached to your role for that Business account.」[R6]；零数据留存 [R1][R3]。
16. 「Built on Starling's proprietary tech platform using Google Gemini and Google Cloud technologies」[S1]。是否分级、是否需要二次确认，官方稿未给分级表。
17. 治理层能力清单（Permissioning & identity、Policy controls、Kill / suspend、Audit logs & observability、Caching、Billing）[F1]；「identity-bound execution, policy enforcement, observability and traceability」[F2]；另有「human oversight embedded in the design」[F2]。
18. 「Your money now has a 24/7 personal assistant.」[R4]。但 [R1][R2][R3][R4] 列出的全部场景都是「用户问、助手做」，未见订阅到期提醒、异常交易主动告警等推送行为的官方描述——这是**推断**，不是事实。
19. 「acting as a proactive partner」[S2]、「a single interface for proactive money management」[S1]。同样，示例全部是用户发起；proactive 是否指主动推送，公开材料不足以判定。
20. 产品页列出按订阅计费的 Regulatory Monitoring & Controls、Credit Default Early Warning [F1]——这是常驻监测型 agent 存在的最强公开证据，但计费方式不等于推送机制。
21. 「The AI Assistants do not provide financial, investment, or any other advice.」；「AI can sometimes be wrong or provide incomplete information」；答不了「you'll be automatically redirected to a customer support chat with a live agent」[R5]；官方博客承认「the information provided may not always be accurate」[R4]。
22. [S1][S2][S3] 均未提及推理过程展示、审计日志或用户可见的决策记录。
23. 「Audit logs & observability」是 agentOS 治理层的组成项 [F1]；日志的字段、留存、导出方式未公开。
24. 「it uses your Revolut app data (including spending data, plan details, and registered address)」；投资问答会用到「portfolio holdings, transaction history, and live market data from third party providers, as well as the terms and conditions…and related FAQ pages」[R5]。
25. [S1] 说明平台与模型托管；客户数据是否出境、是否用于训练，官方稿未展开（第三方报道称留在 Google Cloud、不用于训练外部模型，属【第三方报道】）。
26. 「Fiserv data & capabilities」为 LAYER 04 的一部分，「third-party data where applicable」标注 coming soon [F1]。
27. 本项目为单用户原型，无 RBAC、无生物识别、无真实资金通道（见 README 的「已知限制」）。
28. 本项目无语音链路；执行的是种子数据上的模拟记账；迭代以提交节点为单位，不是每周产品节奏。
29. agentOS 的治理层是一份可直接对照的清单（身份、策略、kill/suspend、审计、计费），本项目只覆盖其中「策略 + 审计 + 熔断」三项。

## 2. 逐产品事实清单

### 2.1 Revolut AIR

**公开可确认的事实**

- 【官方确认】定位是执行引擎而非问答机器人：「It's an execution engine designed to help you manage your money, use Revolut features, and complete daily tasks instantly through a simple conversation.」[R2]
- 【官方确认】入口是 App 内对话，下滑手势或 Profile → Chats → AIR [R1]；英国 1300 万客户分批灰度 [R4]。
- 【官方确认】可执行的动作（官方举例）：查消费、屏蔽商户/未来扣款、查股价、买 eSIM、冻卡、开单次虚拟卡、暂停订阅、旅行规划 [R1][R2][R4]。
- 【官方确认】投资是**只读**：可以查持仓与收益、问行情，但「You will not be able to place, modify, or cancel any order in relation to financial instruments」[R5]。
- 【官方确认】敏感动作需生物识别：「AIR cannot perform sensitive actions without your biometric approval.」[R4]
- 【官方确认】数据边界：「It only accesses the data you can already see, such as your transactions, investments, and cards.」[R1]；零数据留存，「never stored by our AI partners or used for training」[R1][R3]。
- 【官方确认】Business 版按角色权限校验：「Before showing account information or initiating an action, the AI Assistant checks the permissions attached to your role for that Business account.」[R6]
- 【官方确认】不是投顾、不给建议：「The AI Assistants do not provide financial, investment, or any other advice.」[R5]；投资问答会引用持仓、交易历史、第三方实时行情与条款/FAQ 文本 [R5]。
- 【官方确认】答不了会自动转人工客服 [R5]；交互会被记录并用于质量评估 [R5]。
- 【官方确认】商务版还覆盖企业账户管理、转账追踪、发票状态汇总与实时数据可视化 [R6]。
- 【第三方报道】官方未披露由哪些 AI 供应商提供能力 [R8]。

**无法从公开资料确定**

- 无法从公开资料确定：底层模型供应商、型号、是否微调。
- 无法从公开资料确定：检索增强（切分策略、向量库、embedding、重排）、工具调用协议与端点清单。
- 无法从公开资料确定：多步任务的编排/规划算法。
- 无法从公开资料确定：**逐动作的权限分级表**——官方只有「敏感动作需生物识别」这一句，「敏感」的判定边界未公开。
- 无法从公开资料确定：限额阈值、二次确认的具体交互、审计事件模型与留存期。
- 无法从公开资料确定：评测集、准确率指标、延迟与成本。
- 无法从公开资料确定：是否做主动推送（详见 §1 第 6 行与脚注 18）。公开材料中的每一个场景都由用户发起。
- 无法从公开资料确定：异常交易识别、月度/年度报告、一键取消订阅的具体实现（「暂停订阅」是官方原话，是否等同于我们理解的「取消」未公开）。
- 无法从公开资料确定：注入攻击防护与数据信封类机制。

### 2.2 Starling Assistant

**公开可确认的事实**

- 【官方确认】2026-03-20 发布，自称英国首个 agentic AI 金融助手，面向个人活期账户客户分批上线，business 与 joint account 随后 [S1]。
- 【官方确认】支持语音与自然语言：「responds to voice and natural language prompts before carrying out banking tasks on the customer's behalf, from setting up personalised saving goals to organising bill payments」[S1]。
- 【官方确认】技术栈口径：「Built on Starling's proprietary tech platform using Google Gemini and Google Cloud technologies」[S1]。
- 【官方确认】可执行的动作（官方举例）：按目标算月供并设自动转账、按用途批量建 Space、查直扣笔数、查本月待付账单、查与某收款人的历史交易 [S1]。
- 【官方确认】把既有 AI 工具（Spending Intelligence、Scam Intelligence）收进助手作为单一入口，定位「a single interface for proactive money management」[S1]。
- 【官方确认】普惠与脆弱性支持：助听客户开手语服务、引导设置赌博拦截、为财务困境客户提供专门指引 [S1]；也回答「what is my PIN?」「can I order a new card?」这类账户问题 [S1]。
- 【官方确认】2026-08-24 business 版面向全部企业客户：「goes beyond a standard Q&A by executing complex banking commands — acting as a proactive partner」[S2]。
- 【官方确认】business 版能力：税务留存（问一句话→按账户历史算金额→转入专用 Space）、发票欺诈与诈骗防御（付款信息变更时提示直接联系供应商核实；引用 UK Finance 数据称 2025 年发票欺诈致企业损失逾 £41m）、MTD 导航（问收入→判断是否被强制适用→给 HMRC 链接与步骤）[S2]；票据追踪与会计软件打通标注为「今年晚些时候」[S2]。
- 【官方确认】Smart tools 是助手内的预置技能菜单：「they perform specific tasks and have specific pre-programmed prompts that customers can use with a simple tap」，每周上新，客户可提需求，未来可自建 [S3]。
- 【官方确认】迭代节奏：business 版称「add a new tool to the Assistant every week for the rest of the year」[S2]。

**无法从公开资料确定**

- 无法从公开资料确定：助手的编排方式与工具清单（官方只到「Gemini + Google Cloud 平台」这一层）。
- 无法从公开资料确定：**逐动作权限分级与限额阈值**，是否需要二次确认、是否有多因子。
- 无法从公开资料确定：审计与日志格式、注入防护、数据信封类机制。
- 无法从公开资料确定：准确性/延迟/成本等评测指标。
- 无法从公开资料确定：是否主动推送。官方措辞是 proactive，但全部示例均为用户发起；proactive 究竟指主动提醒还是指「能自己把事做完」，公开材料不足以判定。
- 无法从公开资料确定：语音链路的识别与容错细节。

### 2.3 Fiserv agentOS

**公开可确认的事实**

- 【官方确认】定位：「The operating system for financial services agents」「One governed platform to create and use agents across Fiserv — with observability, controls, and spend management built in.」[F1]
- 【官方确认】四层栈公开列明 [F1]：
  - LAYER 01 Agents：Fiserv agents / Third-party agents / Build your own（标注 coming soon）；
  - LAYER 02 agentOS Governance：Permissioning & identity、Policy controls、Agent configuration、Kill / suspend、Audit logs & observability、Caching、Billing（Agent creation 标注 coming soon）；
  - LAYER 03 Models：通用模型与专用小模型、Routing & optimization（按任务路由到最合适的模型）；
  - LAYER 04 Data & capabilities：Fiserv 数据与能力、第三方数据（标注 coming soon）。
- 【官方确认】按用途分四类：Intelligence / Efficiency / Loss mitigation / Growth [F1]。
- 【官方确认】三种计费形态：按查询（Operational Intelligence Relationship Dashboard）、按结果（Commercial Loan Onboarding）、按订阅（Regulatory Monitoring & Controls、Credit Default Early Warning）；可用 credits 跨 agent 使用 [F1]。
- 【官方确认】CERT→PROD 自服务：先在自有数据上跑、过准确性与策略检查、认证后提升到生产 [F1]。
- 【官方确认】2026-05-14 发布：首个面向银行的 agent marketplace；首发 4 个 Fiserv agent + 9 个第三方 agent；6 家金融机构共建、2 家在 beta；预期 2026-08 广泛可用 [F2]。
- 【官方确认】治理口径：「identity-bound execution, policy enforcement, observability and traceability」，并把 human oversight 写进设计 [F2]。
- 【官方确认】与 OpenAI、AWS 战略合作，跑在 Amazon Bedrock / AgentCore 上 [F2]。
- 【官方确认】试点效果口径：Boulder Dam Credit Union 的运营报表从「10 分钟」降到「数秒」；First Interstate Bank 把商贷开户流程自动化到 Fiserv core，减少手工录入 [F2]。
- 【官方确认】一个如实记录的**公开口径差异**：发布稿说银行可以 build their own，产品页把 Build your own 与第三方数据标为 coming soon [F1][F2]——两处都是官方文字，差异属时点问题，本文不做调和。
- 【第三方报道】媒体把这次发布概括为「从零散 copilot 走向受治理的 agent 生态」，并强调治理与合规正在成为企业 AI 的差异化点 [F3]。

**无法从公开资料确定**

- 无法从公开资料确定：是否有面向**终端消费者**的入口。公开材料全部是「金融机构内部工作流」，没有任何消费者 App 的描述。
- 无法从公开资料确定：模型型号清单与路由算法（Routing & optimization 只说明了存在，没说明怎么路由）。
- 无法从公开资料确定：单个 agent 内部的规划方式。
- 无法从公开资料确定：policy 引擎的实现形态、策略表达语言、规则样例。
- 无法从公开资料确定：审计日志的字段模型、存储与保留策略。
- 无法从公开资料确定：权限分级的具体档位、C 端转账确认机制。
- 无法从公开资料确定：agent 认证（CERT→PROD）的判定标准与自动化程度。
- 无法从公开资料确定：九家第三方 agent 合作伙伴的具体名单（发布稿以数量表述）。

### 2.4 本项目（基线，供对照）

- 【官方确认】定位：银行敢让它真动钱的 Agent；三段论为「不可信区（模型只产申请）→ 裁决区（纯函数策略引擎出绿/黄/红 + 能力令牌）→ 执行区（只认令牌）」（`README.md`）。
- 【官方确认】工具 35 个（`app/tools/registry.py`、`app/tools/tool_specs.py:1`）、策略规则 13 条 R01–R13（`app/policy/rules.py`，求值顺序以 `RULES` 为准）、意图模板 9 个（`app/planner/dag.py`）、数据库 33 张表、审计事件 66 类（`app/audit.py:24`，append-only + 禁 UPDATE/DELETE 触发器）。
- 【官方确认】三色阈值：单笔与日累计同为 1000 元，SINGLE_LIMIT_FEN = DAILY_RISK_THRESHOLD_FEN = 100_000（`app/policy/rules.py:19-24`，`DAILY_LIMIT_FEN` 仅为历史别名）。语义是**分级线而非拒绝线**：当日累计会越过 1000 元 → R07 红 + face/sms，验证通过后真实出账；硬拒绝只来自显式配置的 `ABSOLUTE_DAILY_CAP_FEN`（R13，默认 `None`）。
- 【官方确认】能力令牌：HMAC-SHA256，DEFAULT_TTL_MS = 120_000，七步校验（结构→签名→过期→nonce 防重放→工具→参数哈希→账户/金额），第 4 步即消耗 nonce，后续失败令牌直接作废（`app/security/token.py`）。
- 【官方确认】意图解析不是纯模板：LLM 通过单次工具调用产出结构化意图，前置换一根确定性拒答（验证码、密钥、系统提示词、改登录密码、加收款人、删审计日志等直接拒绝），模型两次不给结果时退到术语表兜底并标记 source=fallback（`app/planner/intent.py:261-345`、`app/llm.py` 的多厂商降级）。
- 【官方确认】规划：意图 + 槽位 → 9 个模板生成 DAG → 4 道结构校验（ID 重复、依赖悬空、工具在册、无环）→ 拓扑序执行，支持中断/接管/回退（`app/planner/`）。
- 【官方确认】主动财务：储蓄目标、监测规则、提醒、运行日志（`app/proactive.py`，4 张表 + 7 个工具 + 7 个端点），**手动触发**，不声称 7×24。
- 【官方确认】测试台账（2026-10-26 终测）：单测 **1669 passed / 4 skipped**（历史基线：2026-10-24 早先 1336、2026-10-23 快照 1131），红队 43 条 PASS 43 / FAIL 0，e2e 13 步 PASS 13，浏览器旅程 7 条失败 0（7 条旅程截图 10 张：j2/j4/j5 各 2 张），前端检查 66/66。
- 【官方确认】已知限制：单用户、无 RBAC、无真实认证服务对接、仅 Web 单渠道（`README.md`）。场景 6 现在**已有**持久计划 + 到点触发 + 一次性授权（`plans`/`plan_steps`/`plan_budgets`/`plan_authorizations`，`app/planner/ticker.py` 驱动）；仍不宣称的是真实银行侧冻结、跨进程分布式调度与坐席平台。

## 3. 三个清单

### 3.1 值得学习的成熟设计（可借鉴，以及为什么）

1. **把「敏感动作需生物识别」写成一句用户能懂的话**（Revolut [R4]）
   为什么值得学：这是一条**面向用户的权限承诺**，而不是内部规则的堆砌。我们的三色分级已经有 13 条规则，但用户侧只有颜色；把「什么动作需要你按一次指纹/确认」讲成人话，是可解释性最便宜的一步。
2. **「只访问你本来就能看到的数据」作为数据边界的统一表述**（Revolut [R1]）
   为什么值得学：它用一句话同时回答了「AI 能看到什么」和「AI 不能看到什么」。我们的数据信封（`app/security/envelope.py`）是技术实现，缺一句同等级别的对外表述。
3. **角色权限前置校验的位置选择**（Revolut Business [R6]）
   为什么值得学：官方明确的是「在展示账户信息或发起动作**之前**检查角色权限」。我们目前是单用户，没有这一层；这条给出了未来接 RBAC 时该把校验插在哪里的参照点。
4. **零数据留存写进产品页与新闻稿**（Revolut [R1][R3]）
   为什么值得学：对第三方模型供应商的数据约束是可被外部审计的承诺，比「我们很重视隐私」具体。竞赛场景下，这条可以直接写进隐私说明并对应到日志脱敏实现。
5. **治理层做成一份可勾选的清单**（Fiserv [F1]）
   为什么值得学：permissioning & identity / policy controls / kill-suspend / audit logs / billing 五项，构成一张可以直接拿来自查的表。我们覆盖了「策略 + 审计 + 熔断」三项，缺少 **kill/suspend（一键停用）** 与 **按用量计费** 这两个视角。
6. **CERT→PROD：在自己的数据上先跑，过了检查再上生产**（Fiserv [F1]）
   为什么值得学：它把「上线前验证」产品化了。我们有红队 43 条与 e2e 13 步，本质是同一件事，但只在仓库里，没有包装成「认证」叙事。
7. **预置 prompt 的技能菜单降低提问门槛**（Starling Smart tools [S3]）
   为什么值得学：用户不知道能问什么，是对话式银行助手的第一道流失点。我们的 9 个意图模板已经具备雏形，缺的是前端的一键入口（现在要用户自己打出完整句子）。
8. **单一入口聚合既有能力**（Starling [S1]）
   为什么值得学：Spending Intelligence、Scam Intelligence 都收进助手，用户不需要记住哪个功能在哪个 tab。我们 35 个工具同样需要一个「入口收敛」的叙事，而不是让用户背工具名。

### 3.2 当前项目的实际差距（不修饰）

- **渠道**：只有单页 Web，没有 App、没有 IM。赛题期望里明确提到 APP 与 IM，这是硬差距，不是「实现取舍」。
- **真实数据**：全部跑在 SQLite 种子数据上，没有真实银行核心、没有真实认证服务、没有清算通道。**这是竞赛原型，不是可上线系统**；任何「能真动钱」的说法都必须限定在演示环境内。
- **客户身份与多用户**：单用户、无 RBAC。Revolut Business 已有角色权限校验，Fiserv 有 permissioning & identity，我们这一层是空的。
- **规划能力**：我们的「规划」是**单步意图 → 模板 DAG**，不是开放域多步规划。模板覆盖不到的请求会走反问或兜底，能力边界由 9 个模板决定，这一点不应被 agentic 这个词掩盖。
- **模型层**：已接真实 LLM（含多厂商降级），但没有模型效果评测集；意图解析准确率没有对外可引用的数字。
- **主动能力**：主动财务是手动触发，无后台调度。对外只能说「支持手动运行监测」，不能称 7×24 主动守护。
- **语音**：无。Starling 已把语音作为一等入口。
- **投资**：只有产品列表、风险等级、申购赎回；没有持仓收益报表、没有横向比较、没有个性化推荐。Revolut 的 portfolio performance 是我们没有的面。
- **人工兜底**：没有「转人工」链路。Revolut 在答不了时自动转 live agent [R5]，我们只有拒答与反问。
- **可观测性**：审计是本地哈希链 + 校验接口，没有集中日志、告警、留存策略；Fiserv 把 observability 当作平台能力卖，我们只是「有审计」。
- **透明度的用户侧**：决策剧场展示的是「我们怎么裁决」，但没有一条「AI 可能出错、请核对」的用户级免责与纠错路径。

### 3.3 值得进一步验证的创新方向

以下六条都是**待验证的假设**，不是既成优势；写成「值得验证」而不是「我们领先」。

1. **资金两阶段提交（hold→commit）在真实清算语义下是否成立**【合理推断】
   我们目前用预留 + 支付模拟这件事。竞品公开材料里没有出现等价的资金状态机描述（Revolut 只讲执行、Fiserv 只讲治理），所以「两阶段」可能是差异点——但要说清它在真实核心系统里怎么落：预留的有效期、失败回滚、对账口径。**未验证前不能当作卖点。**
2. **能力令牌能否成为「可审计的委托凭证」**【合理推断】
   竞品用的是 identity-bound execution（Fiserv [F2]）与角色权限（Revolut [R6]），都是「身份即权限」。我们的令牌是「一次授权对应一次动作」，签名字段里带工具、参数哈希、账户与金额。这个模型若要与身份体系对接（令牌里嵌入主体、委托链），需要先验证它不会退化成「又一个 session token」。
3. **决策剧场这类「裁决过程可视化」是否有产品价值**【合理推断】
   三家竞品的公开材料里都未见「裁决过程展示」的等价物：Revolut 只承诺「只访问你可见的数据」并附准确性免责 [R1][R4][R5]，Starling 未披露，Fiserv 把日志与可观测当作平台能力但不公开呈现形式 [F1]。可能是真差异，也可能只是竞赛评审喜欢、真实用户不看——需要用户研究验证，而不是自我认定。
4. **主动财务的「提醒→代执行」边界**【合理推断】
   三家都停在「用户发起」（见 §1 脚注 18/19/20）。从「提醒你下月有一笔订阅要续」到「我帮你取消」之间，隔着授权粒度与误操作责任。我们的三色分级能否覆盖「预先授权 + 到期自动执行」，是需要专门验证的设计问题，目前场景 6 也只有同一次执行内的预留+支付。
5. **三色阈值 1000 元的来源**【合理推断】
   这个数字目前是设计假设（`app/policy/rules.py:19`）。竞品的阈值均未公开，无法对标。若要更硬，需要说明它对应哪类监管惯例或哪份产品设定，否则只能说「我们选了一个保守值」。
6. **数据信封对注入的实际防护强度**【合理推断】
   我们有 L1 词表 + 信封 + R04 升红，红队 43 条全过。但这是**我们自己的红队**在自己的模板上跑出来的结果；换一批攻击样本、换一个模型，结论可能不同。需要外部样本复测。

## 4. 参考资料

### 4.1 外部资料（访问日期均为 2026-10-23）

| 编号 | URL | 说明 | 获取方式备注 |
|---|---|---|---|
| R1 | https://www.revolut.com/air-ai-by-revolut/ | AIR 产品页 | 直连 403，无头浏览器渲染读取正文 |
| R2 | https://help.revolut.com/help/profile-and-plan/getting-started-with-air/ | AIR 帮助中心「Getting started with AIR」 | 直连 403，无头浏览器渲染读取正文 |
| R3 | https://www.revolut.com/news/revolut_enters_new_era_of_money_intelligence_with_launch_of_ai_assistant/ | 发布稿，2026-04-09 | 同上 |
| R4 | https://www.revolut.com/blog/post/air-ai-by-revolut/ | 官方博客，2026-04-09（生物识别、24/7、1300 万客户） | 同上 |
| R5 | https://www.revolut.com/legal/rita-disclaimer/ | Revolut's AI Assistant 法律告知（不提供建议、投资不可下单、转人工、数据使用） | 同上 |
| R6 | https://www.revolut.com/legal/assistant-business-disclaimer/ | 商务版 AI 助手告知（角色权限校验、企业数据范围） | 同上 |
| R7 | https://www.revolut.com/news/revolut_announces_launch_of_dedicated_ai_research_division_revolut_research/ | 2026-08-25，研究部门稿，含 AIR「executes complex financial tasks in a single step」 | 同上 |
| R8 | https://www.fintechweekly.com/news/revolut-air-ai-assistant-uk-customers-launch-2026 | 第三方报道，2026-04-11（未披露 AI 供应商） | 直连可读 |
| S1 | https://www.starlingbank.com/news/starling-launches-agentic-ai-assistant | 新闻稿，2026-03-20，个人版发布 | 直连可读 |
| S2 | https://www.starlingbank.com/news/starling-launches-agentic-ai-assistant-for-business | 新闻稿，2026-08-24，business 版发布 | 直连可读 |
| S3 | https://www.starlingbank.com/news/starling-to-release-weekly-smart-tools | 新闻稿，2026-08-20，Smart tools 与每周节奏 | 直连可读 |
| S4 | https://www.starlingbank.com/news/ | 新闻索引，用于核对上述三篇的日期 | 直连可读 |
| F1 | https://www.fiserv.com/content/fiserv-com/en/lp/agentos-by-fiserv.html | agentOS 产品页（四层栈、治理能力、计费形态、CERT→PROD） | 直连可读 |
| F2 | https://investors.fiserv.com/news-releases/news-release-details/fiserv-launches-agentos-operating-system-agentic-ai-banking | 发布稿，2026-05-14（4+9 个 agent、6 家共建、2 家 beta、OpenAI/AWS、治理口径、试点效果） | 直连 403；正文经该稿联合分发副本取得（见 F4） |
| F3 | https://finnoex.com/fiserv-launches-agentos-agentic-ai-banking-platform | 第三方报道，2026-05-15 | 直连可读 |
| F4 | https://finnhub.io/api/news?id=e0693b81336b89a5facf2574432dae1e0ece6d9edcbe74824c92d9a203461b08 | F2 的联合分发副本（GlobeNewswire 原稿正文），用于复核 F2 引文 | 直连可读；仅作 F2 的复核副本，不单独作为独立信源 |

### 4.2 仓库内依据（非 URL，供复核本项目一列）

- `README.md` — 定位、三段论、四个承重墙、已知限制
- `docs/赛题原文要点.md`、`docs/提交物对照表.md` — 赛题期望与渠道差距
- `app/policy/rules.py:19-20` — 三色阈值；:251 — RULES 12 条
- `app/security/token.py` — 能力令牌七步校验、TTL
- `app/planner/intent.py:261-345` — 意图解析、拒答前置、兜底
- `app/llm.py` — 多厂商降级适配器
- `app/planner/dag.py:39` — 9 个意图模板
- `app/audit.py:24` — 66 类审计事件
- `app/proactive.py` — 主动财务（手动触发）
- `docs/benchmark/revolut_air_research.md` — 既有的 Revolut 证据台账（A1–A17 / B1–B15）

---

**一句话结论**：在「用户能不能用一句话把事情办完」这件事上，三家竞品都已走到真实客户面前；在「每一笔动作凭什么被批准、批了之后怎么留痕」这件事上，本项目把机制做得更显式——但这是**原型层面**的显式：数据是种子数据、渠道只有网页、规划只有模板。把差距写清楚，比把优势写漂亮更有用。
