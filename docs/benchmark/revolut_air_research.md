# Revolut AIR 能力矩阵研究

> **用途**：2026 深圳国际金融科技大赛「人工智能赛道 · 银行 AI 智能体」阶段 0.1 / 0.2 的行业对标研究。
> **对标对象**：Revolut AIR（AI by Revolut），Revolut 推出的 App 内 AI 金融助手。
> **核验方式**：① 用真实浏览器（ego，默认 space）逐页打开 `revolut.com` / `help.revolut.com` 官方页面并提取正文原文；② 用 `web_search` 补充检索官方工程材料、厂商案例与第三方分析，并按来源类型标注可信度。
> **核验日期**：2026-10-23（实际抓取并逐页核对正文的完成日）
> **历史说明**：初稿曾把 2026-09-04 写成「任务书指定核验日期」，但任务书并未指定该日期，那是初稿自设的名义值；该错误已在本版更正，全文不再以 2026-09-04 作为证据时点。
> **作者**：air-research（teammate，阶段 0.1 + 0.2 专项）

---

## 0. 阅读指南与口径声明

### 0.1 为什么必须走浏览器

直接用 `curl` / `web_fetch` 访问 `revolut.com` 会被 Akamai 机器人拦截：本次实测 `curl https://www.revolut.com/legal/rita-disclaimer/` 返回 **HTTP 403**，`curl https://help.revolut.com/sitemap.xml` 同样 **HTTP 403**。因此本表中所有 revolut 域名的页面均通过真实浏览器会话打开并提取正文；未能打开或未能读到正文的，一律记为「未核实」，不写成「已读过」（见 A.3）。

### 0.2 日期口径（重要，避免误用）

本文件的核验日期统一为 **2026-10-23**，台账「核验日期」列按此填写；「发布日期」列仍如实标注每条来源自身的日期（例：PRAGMA 生产化博客 2026-10-21、TNW 对 CEO 的访谈 2026-10-09、AIR 欧洲上线媒体报道 2026-09-01/02）。

1. 每条来源的发布日期与核验日期分开记录，不把核验日倒填成来源日期；
2. 产品现状类结论只采用核验日（2026-10-23）及之前可得的官方材料（AIR 产品页 / 新闻稿 / 博客 / 帮助中心 / AI 透明度说明）；
3. 战略表述、路线图与第三方推断单独标注性质，不用来断言「当前已具备」。

### 0.3 证据状态四档

见 C.1。全文每一条事实性结论后都带其中一档标签。

---

## A. 核验台账

### A.1 官方一手页面（浏览器实际打开并读到正文）

| # | URL | 页面标题 | 发布/更新日期 | 核验日期 | 可访问性 | 来源类型 |
|---|---|---|---|---|---|---|
| A1 | https://www.revolut.com/air-ai-by-revolut/ | AIR \| AI by Revolut \| Revolut GB | 未标注 | 2026-10-23 | 200（浏览器，全文已提取） | 官方 · 产品页 |
| A2 | https://www.revolut.com/en-NO/air-ai-by-revolut/ | AIR \| AI by Revolut \| Revolut NO | 未标注 | 2026-10-23 | 200（全文已提取） | 官方 · 产品页（挪威站，EEA 版） |
| A3 | https://www.revolut.com/news/revolut_enters_new_era_of_money_intelligence_with_launch_of_ai_assistant/ | Revolut enters new era of money intelligence with launch of AI assistant | 2026-04-09 | 2026-10-23 | 200（全文已提取） | 官方 · 新闻稿 |
| A4 | https://www.revolut.com/blog/post/air-ai-by-revolut/ | AIR - AI by Revolut \| Revolut United Kingdom | 2026-04-09（作者 Team Revolut） | 2026-10-23 | 200（全文已提取） | 官方 · 博客 |
| A5 | https://help.revolut.com/help/profile-and-plan/getting-started-with-air/ | Getting started with AIR \| Revolut United Kingdom | 未标注 | 2026-10-23 | 200（全文已提取） | 官方 · 帮助中心（**站内仅此一篇 AIR 文章**） |
| A6 | https://www.revolut.com/legal/rita-disclaimer/ | Revolut's AI Assistant \| Revolut United Kingdom | 页面未标注；其 PDF 版为 v1.0.2（文件名时间戳 ≈ 2026-06-16） | 2026-10-23 | 200（curl 403，浏览器成功，全文已提取） | 官方 · AI 透明度/法律说明 |
| A7 | https://www.revolut.com/legal/assistant-business-disclaimer/ | Revolut's AI Assistants for Revolut Business | 未标注 | 2026-10-23 | 200（全文已提取） | 官方 · AI 透明度说明（Business 版） |
| A8 | https://www.revolut.com/legal/privacy/ | Customer Privacy Notice \| Revolut United Kingdom | 生效日期 2026-03-17 | 2026-10-23 | 200（全文已提取） | 官方 · 隐私声明 |
| A9 | https://www.revolut.com/legal/tailored-ads-privacy-notice/ | Tailored Lifestyle Ads and Notifications | 未标注 | 2026-10-23 | 200（全文已提取） | 官方 · 隐私声明（营销通知，非 AIR） |
| A10 | https://www.revolut.com/legal/chat-privacy-notice/ | Revolut Messenger Privacy Notice | 生效 2023-03-01 | 2026-10-23 | 200（读到正文；**内容是 P2P/群聊，与 AI 无关**，故不作为 AIR 证据） | 官方 · 隐私声明 |
| A11 | https://www.revolut.com/legal/ | Terms & Policies \| Revolut United Kingdom（索引） | 未标注 | 2026-10-23 | 200（用于发现 A7 等入口） | 官方 · 索引页 |
| A12 | https://www.revolut.com/news/ | News and Media \| Revolut United Kingdom（索引，列最近 10 条） | 页面最新条目 2026-10-19 | 2026-10-23 | 200（用于发现 A13；**未收录 AIR 欧洲上线稿**） | 官方 · 索引页 |
| A13 | https://www.revolut.com/news/revolut_announces_launch_of_dedicated_ai_research_division_revolut_research/ | Revolut Announces Launch of Dedicated AI Research Division: Revolut Research | 2026-08-25 | 2026-10-23 | 200（全文已提取） | 官方 · 新闻稿（AI 组织与 PRAGMA，含对 AIR 的定位） |
| A14 | https://www.revolut.com/blog/post/fromresearchtorealtime/ | From Research to Real Time: Scaling a Foundation Model for Structured Data in Production | 2026-10-21（栏目 News，作者 Revolut Contributor） | 2026-10-23 | 200（全文已提取） | 官方 · 技术博客（**对象是 PRAGMA 风控栈，不是 AIR**） |
| A15 | https://www.revolut.com/blog/ | Blog \| Revolut United Kingdom（索引，列 10 篇） | 最新文章 2026-10-16 | 2026-10-23 | 200（10 篇中仅 1 篇 AI 相关，即 A14） | 官方 · 索引页 |
| A16 | https://www.revolut.com/en-IE/news/ | News and Media \| Revolut Ireland（索引） | 最新条目 2026-09-24 | 2026-10-23 | 200（最近 10 条中**无** AIR 欧洲上线稿） | 官方 · 索引页 |
| A17 | https://cdn.revolut.com/terms_and_conditions/pdf/revolut_s_ai_assistant_c305e066_1.0.2_1781775214_en.pdf | Revolut AI Assistant 条款 PDF（v1.0.2） | 文件名时间戳 ≈ 2026-06-16 | 2026-10-23 | HTTP 200、63,399 bytes，**但正文提取失败（无 pdftotext/pypdf，自写 zlib 提取器输出为空）→ 内容未核实** | 官方 · PDF（A6 的下载版） |

### A.2 第三方与厂商材料（web_search 取得；须按来源类型标注可信度）

| # | URL | 页面标题 | 发布/更新日期 | 核验日期 | 可访问性 | 来源类型与可信度 |
|---|---|---|---|---|---|---|
| B1 | https://www.linkedin.com/posts/nesterovpavel_this-is-air-activity-7448087433067397120-QtkX | This is AIR（Revolut AI 负责人 Pavel Nesterov 的公开帖） | 2026-04-10 | 2026-10-23 | **原页打开失败**（跳转 `linkedin.cn` 错误页，疑似地域/登录拦截）；内容来自搜索引擎抽取的摘要，**未经原页逐字核对** | 第一方高管公开陈述 · 可信度高但非产品文档 |
| B2 | https://allai.events/event/structured-foundational-models-ft-revolut | Structured Foundational Models (ft. Revolut)（Pavel Nesterov = Executive Director and Head of AI and Data Science at Revolut 的头衔出处） | 活动 2026-07-13 | 2026-10-23 | 搜索摘要 | 第三方会议页 · 可信（用于确认 B1 作者身份与头衔） |
| B3 | https://www.nvidia.com/es-es/case-studies/revolut/ | NVIDIA: Revolut crea un modelo básico de transacciones con la plataforma de computación acelerada de NVIDIA | 2026-06-08 | 2026-10-23 | 搜索摘要（未逐页打开） | 厂商官方案例 · 准官方（**对象是 PRAGMA 风控栈**） |
| B4 | https://irishtechnews.ie/revolut-bank-launch-of-ai-assistant | Revolut Bank enters new era of money intelligence with launch of AI assistant | 2026-09-02 | 2026-10-23 | 搜索摘要（正文措辞像官方通稿） | 行业媒体转载官方口径 · 可信 |
| B5 | https://www.europapress.es/economia/finanzas-00340/noticia-revolut-lanza-air-espana-nuevo-asistente-inteligencia-artificial-gestionar-finanzas-20260901105900.html | Revolut lanza AIR en España, su nuevo asistente de inteligencia artificial para gestionar las finanzas | 2026-09-01 | 2026-10-23 | 搜索摘要 | 官方通讯社 · 可信（AIR 西班牙/欧洲上线） |
| B6 | https://www.eubiznews.com/article/revolut-launches-air-in-spain-to-bring-banking-and-travel-services-into-one-ai-conversation | Revolut launches AIR in Spain to bring banking and travel services into one AI conversation | 2026-09-02 | 2026-10-23 | 搜索摘要（web_fetch 因 provider 冲突报错，未取全文） | 行业媒体 · 可信 |
| B7 | https://www.konsulteer.com/article/revolut-launches-ai-financial-assistant-air-in-france | Revolut Launches AI Financial Assistant AIR In France | 2026-09-02 | 2026-10-23 | 搜索摘要 | 一般媒体 · 数字需谨慎（200+ H100、1.2M 客服工单/月等均为公司自述转述） |
| B8 | https://www.fintechweekly.com/news/revolut-air-ai-assistant-uk-customers-launch-2026 | Revolut Launches AIR, An In-App AI Assistant, to 13 Million UK Customers | 2026-04-11 | 2026-10-23 | 搜索摘要 | 一般科技媒体 · 可信（**关键句："Revolut has not disclosed which AI providers power the product"**） |
| B9 | https://thenextweb.com/news/nik-storonsky-revolut-global-tech-company-europe-wave-by-vento | Nik Storonsky wants Revolut to be Europe's answer to Meta and Google | 2026-10-09 | 2026-10-23 | 搜索摘要 | 可信媒体 · CEO 访谈（**晚于核验日，属战略表述**） |
| B10 | https://fintechmagazine.com/news/a-bank-with-zero-risk-nik-storonsky-on-revoluts-expansion | A Bank With Zero Risk: Nik Storonsky on Revolut's Expansion（Google Cloud 为「foundational technology partner」，Gemini 贯穿运营与反欺诈） | 2026-09-22 | 2026-10-23 | 搜索摘要 | 行业媒体 · 可信（**晚于核验日**） |
| B11 | https://www.forestadmin.com/blog/what-revolut-air-signals-to-fintech-from-interface-to-infrastructure | What Revolut AIR signals to fintech: from interface to infrastructure | 未取得日期 | 2026-10-23 | 搜索摘要 | 第三方厂商技术博客 · 推断性质，非官方 |
| B12 | https://ai2.work/blog/inside-revolut-s-pragma-the-bank-built-ai-scoring-70m-customers | Inside Revolut's PRAGMA: The Bank-Built AI Scoring 70M Customers | 2026-07-29 | 2026-10-23 | 搜索摘要 | 第三方技术分析 · 对象是 PRAGMA，**不是 AIR** |
| B13 | https://ergomania.eu/revolut-air-ai-banking-assistant-ux | Revolut AIR：AI 银行助手 UX 分析（声称 AIR 为 **RAG 架构**、与 Google Cloud / Gemini 集成、200+ NVIDIA H100 on Nebius） | 未取得日期 | 2026-10-23 | 搜索摘要 | 个人/公司博客 · **低可信度，非官方，不得当作事实** |
| B14 | https://www.studioglobal.ai/zh-cn/discover/answers/search-6a8e51b2ce023abfc3deb2a1 | Revolut 成立 AI 研究部门，押注自研银行基础模型 PRAGMA | 2026-08-26 | 2026-10-23 | 搜索摘要 | 中文聚合媒体 · 一般 |
| B15 | https://www.fxajax.com/news/7257/Revolut-成立研究部门-携手-NVIDIA-打造金融基础模型-PRAGMA | Revolut 成立研究部门，携手 NVIDIA 打造金融基础模型 PRAGMA | 2026-08-25 | 2026-10-23 | 搜索摘要 | 中文媒体 · 一般 |

### A.3 打开失败 / 未能读取的尝试（如实登记，不得当作已读）

| # | 尝试的 URL / 动作 | 结果 | 影响 |
|---|---|---|---|
| F1 | `curl https://www.revolut.com/legal/rita-disclaimer/` | HTTP 403（Akamai 机器人拦截） | 改用浏览器后成功（A6） |
| F2 | `curl https://help.revolut.com/sitemap.xml` | HTTP 403 | 帮助中心无法做全站 URL 枚举 |
| F3 | `https://help.revolut.com/help/search/?q=AIR` | 404，页面提示 "We can’t find that one" | 官方站内检索接口不可用 |
| F4 | 在帮助中心搜索框输入 "AIR" 后回车 | 页面无跳转、无结果列表 | 「AIR 帮助文章只有一篇」这一判断**只能限定在 A5 这一已核验页面 + 索引观察**，属于推断而非穷举结论 |
| F5 | `https://cdn.revolut.com/.../revolut_s_ai_assistant_..._1.0.2_..._en.pdf` | 下载成功（63,399 bytes），**正文提取失败**（环境无 pdftotext / pypdf，自写 zlib 提取器输出为空） | PDF 正文内容**未核实**；本表 A6 的法律结论只取自同名 HTML 页面 |
| F6 | `https://www.linkedin.com/posts/nesterovpavel_this-is-air-activity-7448087433067397120-QtkX` | 跳转 `linkedin.cn` 错误页，原页未打开 | B1 的 AIR 架构描述为**搜索引擎摘要转述**，未逐字核对；见 C.3 |
| F7 | `web_fetch https://www.eubiznews.com/article/revolut-launches-air-in-spain-...` | 报错：`multiple usable web providers are registered (deepseek-fetch-api, http); configure one explicitly` | B6 仅用搜索摘要，未取全文 |

### A.4 覆盖度小结

- 官方一手材料：**17 个页面/文件实际访问**，其中 15 个读到正文、1 个仅拿到索引、1 个（PDF）下载成功但正文未提取。
- AIR 相关的官方文字总量有限：产品页 + 1 篇新闻稿 + 1 篇博客 + 1 篇帮助中心 + 1 份 AI 透明度说明 + 1 份 Business 版透明度说明 + 1 篇 AI 研究部门新闻稿。**官方没有发布 AIR 的技术架构文档、功能清单或权限分级表。**
- 唯一的架构级一手信息来自 Revolut AI 负责人 Pavel Nesterov 的 LinkedIn 帖（B1），且**原页本次未能打开**。这是本文档所有架构类结论的最大不确定性来源（见 C.3 第 1 条）。

---

## B. AIR 能力矩阵

| 研究维度 | 需要回答的问题 | 结论 | 证据状态 | 来源 URL |
|---|---|---|---|---|
| **产品定位** | AIR 要解决用户的哪些金融问题 | 把「多标签页 / 多菜单」的银行操作换成一次对话：消费洞察、预算与储蓄、订阅管理、卡片控制、投资信息查询、旅行与奖励。官方自述为 "an execution engine"（帮助中心）、"moves beyond the traditional chatbot"（新闻稿）、"a co-pilot"（高管引语）。目标用户是 Revolut App 内既有客户（英国 1300 万，后续扩至欧洲） | 官方资料明确证实 | A1 / A3 / A4 / A5 |
| **对话交互** | 如何理解用户的自然语言请求 | 入口是 App 内手势：从主屏中央下拉，或 Profile → Chats → AIR。请求以口语化单句给出，官方示例："Where's my money going?"、"How much can I save this month?"、"Freeze my card"、"Check Nvidia's stock price"、"Buy a 3GB eSIM for my trip"、"Block a merchant / future payments"。帮助中心称其可给出 "inspending patterns, trends, and activity" 的洞察，并可基于请求在 App 内执行动作 | 官方资料明确证实 | A1 / A4 / A5 |
| **个人财务洞察** | 消费、预算、储蓄信息如何展示 | 能力面：消费分析（"instant spending insights"）、分类与趋势、预算辅助（"assist with budgeting"）、储蓄测算（"How much can I save this month?"）、投资持仓与收益查询（"What are my investment earnings this year?"）。展示形态上，官方只承诺「对话式回答 + 只访问用户已可见的数据」；**消费者 AIR 的图表/可视化形态未公开**——「transaction tables and spend charts」出自 Business 版 AI 助手，不可直接套到 AIR | 能力：官方资料明确证实；消费者端可视化形态：尚未核实 | A3 / A4 / A5 / A6 / A7 |
| **银行业务操作** | 哪些操作可发起、哪些需要确认 | 可发起的写操作（官方列举）：冻结卡片（"Freeze my card"）、申请单次虚拟卡（"I need to get a single-use virtual card"）、卡片冻结与消费限额、暂停周期性支付（"Pause recurring payments"）、屏蔽商户与未来支付（"Block a merchant / future payments"）、购买 eSIM（"Buy a 3GB eSIM for my trip"）、查询订阅。需确认/强验证的：官方只给总原则——"Biometric protection: AIR cannot perform sensitive actions without your biometric approval."；**逐动作的敏感度分级表（类似赛题的红黄绿）官方从未公开** | 写操作清单与生物识别总原则：官方资料明确证实；逐动作分级：尚未核实 | A1 / A4 / A5 |
| **订阅与卡片** | 公开支持哪些查询与管理能力 | 订阅：查看订阅（"check your subscriptions"）、管理月度支出（"manage monthly spend"）、暂停经常性支付。卡片：冻结/解冻、单次虚拟卡、限额与卡控制（AIR 产品页设 "Card controls" / "Subscription management" 两个展示 Tab）。**未见任何官方材料证明支持「自动识别订阅扣费 + 一键取消订阅」**（赛题场景 5 的后半段在 AIR 侧无对标证据） | 查询与管理动作：官方资料明确证实；一键取消订阅：尚未核实 | A1 / A4 / A5 |
| **投资信息** | 如何区分投资信息查询与实际投资操作 | 分界极清晰——**可读不可写**。可读范围官方明确："your portfolio holdings, transaction history, and live market data from third party providers, as well as the terms and conditions from the Revolut company that provides your investment account, and related FAQ pages"；但写侧被法律文件堵死："You will not be able to place, modify, or cancel any order in relation to financial instruments, nor take any other action that would result in you entering into a transaction."，并且 "The AI Assistants do not provide financial, investment, or any other advice."；产品页脚注："Revolut Trading Ltd provides a non-advised reception and transmission of orders investment service"、"¹Capital at risk." | 官方资料明确证实 | A1 / A4 / A6 |
| **跨场景体验** | 是否有跨场景任务或外部服务整合 | 有，但形态是「同一对话里串联已有产品能力」：多日行程规划、挑选出行日期、回顾历史预订与旅行支出、住宿（Stays）与当地体验（Experiences）预订、RevPoints 积分兑换（换 eSIM / 机场贵宾厅）、eSIM 购买。官方新闻把 AIR 定位为 "connected engines power front-end capabilities such as AIR"；NO 站脚注 "eSIM T&Cs apply. 18+, T&Cs apply." 说明含第三方 eSIM 供应。**跨 App、跨银行的开放编排（如第三方 agent 经 MCP 接入）在核验日之前没有官方落地证据** | 产品能力：官方资料明确证实；开放编排：尚未核实 | A2 / A4 / A5 / A13 / B4 / B5 |
| **主动服务** | **是否有官方证据说明主动提醒或主动规划能力** | **未找到任何官方证据**。本次核验到的 6 个 AIR 相关官方页面中，所有场景都由用户发起；官方从未描述 AIR 会主动推送提醒、主动给理财建议或主动规划行程。可作旁证但**不可当作 AIR 主动能力证据**的两点：① Revolut 平台层有用户 opt-in 的 "Tailored Lifestyle Ads and Notifications"（基于交易与兴趣做个性化推送，与 AI、与 AIR 均无官方关联）；② 第一方高管提到 AIR 是 "next-generation text and voice AI bots" 平台、语音支持 2026-01 上线（有语音入口 ≠ 主动服务）。第三方分析也只以疑问句提及 "whether AIR graduates from reactive assistant to proactive financial advisor" | 尚未核实（倾向否定：官方无证据） | A1 / A3 / A4 / A5 / A9 / B1 |
| **安全机制** | 官方公开了哪些认证、确认、生物识别、隐私机制 | 已公开 8 项：①敏感动作需生物识别批准；②零数据留存——"personal information is never stored by third-party AI partners or used for training external models"；③最小可见性——"only accesses the data you can already see"；④银行保密豁免与 client secrets 授权（"Your use of the AI Revolut Assistant serves as an express waiver of banking secrecy"）；⑤交互记录留存并做质量评估（"Your interactions with AI Assistants will generally be recorded and assessed for quality purposes"）；⑥Business 版在展示数据或发起动作前校验账户角色权限；⑦免责与准确性声明——"Responses from the AI Revolut Assistants are provided 'as is'"、"AIR is an artificial intelligence tool and the information provided may not always be accurate"；⑧合规环境层面（UK GDPR：biometric data 为特殊类别，AI 自动化决策与 profiling 单独成章）。**未公开**：防幻觉工程手段、Prompt 注入防护、红队测试、审计日志格式、逐动作权限分级表 | 上述 8 项：官方资料明确证实；未公开项：尚未核实 | A1 / A4 / A6 / A7 / A8 |
| **技术架构** | 哪些实现细节公开，哪些未知 | **公开**：①AIR 是多 Agent 平台底座，核心团队守 "core behaviours and safety guardrails"，各产品团队自建 sub-agent；②模型策略为 "a mixture of vendor models and SOTA open-source models fine-tuned for specific tasks"，并通过 "centralized AI gateway" 实现 model-agnostic、可运行时换供应商；③自建组件清单 "orchestration, RAG pipelines, guardrails, evals, real-time monitoring, labelling, QA/QC"；④自建上下文与记忆管理系统，Agent 可查询 "a user profile stored in a secure and isolated environment for each customer"；⑤直接接入 "Revolut internal knowledge base and product ecosystem"，因此能代客执行动作；⑥官方新闻称 PRAGMA 基础模型负责风控/平台运营/产品推荐，AIR 是其上的前端能力；⑦Business 版另有 "multi-platform" 与实时可视化。**未知**：具体 LLM 供应商与型号、向量库/Embedding 选型、检索与上下文拼装细节、工具调用协议与端点定义、编排/规划算法、评测集与指标、延迟与成本指标、防幻觉工程手段。①②③④来自 B1（第一方高管 LinkedIn 帖，**原页未打开**，经搜索摘要转述） | ①-④：可信公开技术资料证实（原页未打开，降级处理）；⑤-⑦：官方资料明确证实；其余：尚未核实 | B1 / A13 / A14 / A6 / A7 |
| **产品限制** | 地区、版本、权限、功能限制、渐进开放阶段 | ①首发英国：官方新闻（2026-08-25）称 AIR "currently available in the UK only"；②渐进开放：帮助中心 "AIR is continuously improving and being rolled out to customers gradually… Your access is arriving shortly"；③欧洲扩展：媒体（2026-09-01/02）称西班牙、法国、爱尔兰等上线，但**截至核验 Revolut 官方 news 索引（GB 与 IE）均未收录欧洲上线稿**，官方只在 NO 站存在同款产品页；④版本依赖："keep your app updated"；⑤资格门槛："Opening a bank account or upgrading plans does not guarantee access to all services provided in-app… additional eligibility criteria"；⑥eSIM 等附加服务受 T&C 与 18+ 限制；⑦投资由 Revolut Trading Ltd 提供，属非投顾型 RTO 服务；⑧官方文档极薄：帮助中心仅 1 篇 AIR 文章，**没有功能清单、没有权限分级、没有 FAQ** | ①②④⑤⑥⑦：官方资料明确证实；③：可信公开技术资料证实（媒体口径，官方页面未直接证实）；⑧：推断（检索受限，见 F3/F4） | A2 / A5 / A12 / A13 / A16 / B4 / B5 / B7 |

---

## C. 逐项证据状态标注

### C.1 状态定义

| 标签 | 含义 | 使用纪律 |
|---|---|---|
| **官方资料明确证实** | 出自 revolut.com / help.revolut.com 的产品页、新闻稿、博客、帮助中心、法律与透明度文件原文，可直接引用 | 可写进技术文档与答辩 PPT |
| **可信公开技术资料证实** | 第一方高管的技术公开陈述（本例为 LinkedIn 帖，**原页未打开**）或厂商官方材料（NVIDIA 案例） | 可写进技术文档，但须写明来源类型与「原页未打开」 |
| **根据公开信息作出的合理推断** | 由已核实事实推导，推理依据写明 | 只能写成「推断 / 我们认为」，不得作为事实引用 |
| **尚未核实** | 检索后仍无可靠证据，或证据互相冲突、来源不可达 | 必须显式列出缺口，不能用常识补齐 |

### C.2 官方资料明确证实（可引用的原文）

| # | 结论 | 原文（节选） | URL |
|---|---|---|---|
| 1 | AIR 是「一次对话完成多件事」的执行型助手 | "Ask, and AIR makes it happen"；"One chat. Total control. Manage your cards and subscriptions, check your investments¹, and even plan your next trip, with just a conversation." | A1 |
| 2 | 入口是 App 内手势，非独立 App | "Just swipe down from the middle of your app screen, or go to Profile → Chats → AIR" | A1 / A5 |
| 3 | 面向英国 1300 万客户开始铺开 | "We're officially beginning the rollout of AIR (AI by Revolut) to our 13 million customers across the UK." | A4 |
| 4 | 不是传统聊天机器人 | "AIR moves beyond the traditional chatbot." | A3 |
| 5 | 官方自述为 execution engine | "It's an execution engine designed to help you manage your money, use Revolut features, and complete daily tasks instantly through a simple conversation." | A5 |
| 6 | 消费洞察 / 预算 / 订阅 / 卡片 / 投资 / 旅行六类能力 | "Gain instant spending insights, effortlessly track investments, manage subscriptions, or freeze a lost card - all through a single, intuitive conversation" | A3 |
| 7 | 写操作实例（卡片、订阅） | "Pause recurring payments, check your subscriptions, and manage monthly spend"；"Freeze my card"；"I need to get a single-use virtual card" | A4 |
| 8 | 旅行与 eSIM 属可执行场景 | "Buy a 3GB eSIM for my trip"；"Sort your travel essentials" | A4 / A1 |
| 9 | 零数据留存 + 不用于训练 | "Zero data retention: your personal data is never stored by third-party AI partners or used for training."；"personal information is never stored by third-party AI partners or used for training external models" | A4 / A3 |
| 10 | 敏感动作需生物识别批准 | "Biometric protection: AIR cannot perform sensitive actions without your biometric approval." | A4 |
| 11 | 只访问用户已可见数据 | "Total transparency: AIR only accesses the data you can already see, such as your transactions, investments, and cards." | A4 / A1 |
| 12 | 投资只读边界 | "You will not be able to place, modify, or cancel any order in relation to financial instruments, nor take any other action that would result in you entering into a transaction." | A6 |
| 13 | 不构成投资建议 | "The AI Assistants do not provide financial, investment, or any other advice."；"Information provided is not investment advice" | A6 / A4 |
| 14 | 银行保密豁免与授权 | "Your use of the AI Revolut Assistant serves as an express waiver of banking secrecy for these interactions." | A6 |
| 15 | 交互记录留存与质量评估 | "Your interactions with AI Assistants will generally be recorded and assessed for quality purposes." | A6 |
| 16 | 渐进开放 + 版本依赖 | "AIR is continuously improving and being rolled out to customers gradually… If you don't see it yet, keep your app updated. Your access is arriving shortly." | A5 |
| 17 | 官方承认输出可能不准确 | "The information provided is accurate as of 8 April 2026. AIR is an artificial intelligence tool and the information provided may not always be accurate." | A4 |
| 18 | AIR 与 PRAGMA 的关系 | "connected engines power front-end capabilities such as AIR, Revolut's in-app assistant that executes complex financial tasks in a single step (currently available in the UK only)" | A13 |
| 19 | Business 版动作前校验角色权限 | "Before showing account information or initiating an action, the AI Assistant checks the permissions attached to your role for that Business account." | A7 |
| 20 | 官方承认第三方 AI 供应商存在（但未点名） | 隐私声明中 "Suppliers who provide us with IT, artificial intelligence, payment and delivery services"；B8 明确 "Revolut has not disclosed which AI providers power the product" | A8 / B8 |

### C.3 根据公开信息作出的合理推断（附依据与"还不能升级"的原因）

| # | 推断 | 推断依据 | 为什么不能升级为「官方明确证实」 |
|---|---|---|---|
| 1 | **AIR 很可能是消费端「AI Revolut Assistant」的法律称谓/升级版** | A6 描述的 AI Revolut Assistant 能力（消费分析、预算辅助、投资事实信息、旅行信息）与 AIR 高度重合；A6 的 PDF 版本已是 2026-06 的 v1.0.2，晚于 AIR 上线（2026-04-09），却仍未出现 "AIR" 字样 | 官方从未声明两者等同，也从未在法律文件里点名 AIR；A6 全文我已读完，确认无 "AIR" 字样 |
| 2 | **AIR 内部确有 RAG 检索链路** | B1 第一方高管自述 "The architecture is built in-house: orchestration, RAG pipelines, guardrails, evals, real-time monitoring, labelling, QA/QC"；A6 也显示投资类回答会引用 "terms and conditions… and related FAQ pages" 这类文档来源 | B1 原页未打开（F6），内容为搜索引擎摘要转述，未经逐字核对；A6 只描述数据来源，未使用 "RAG" 一词 |
| 3 | **不存在向量库 / GraphRAG 的任何证据** | B1 只提 "RAG pipelines"，未提向量库、图数据库或 GraphRAG；A6 的检索来源偏结构化与文档（FAQ、条款、持仓、行情） | 属「无证据」，不能反推为「用了」也不能断言「没用」；B13 声称是 RAG 架构，但它是个人博客，且早于/弱于 B1 的第一方表述 |
| 4 | **AIR 的工具调用面 ≈ 官方列举的写操作白名单** | A4/A5/A3 列出的动作（冻结卡、限额、单次虚拟卡、暂停周期付款、屏蔽商户/未来支付、买 eSIM）与投资禁写（A6）共同界定了动作空间 | 官方未给出工具清单/端点定义，「白名单」是我对其公开描述的归纳 |
| 5 | **跨场景编排目前发生在 Revolut 自有生态内，且以「子 Agent + 工具」方式扩展** | B1 "each product team can build and operate its own sub-agent"、A13 "connected engines"、B4 的旅行/预订/RevPoints 能力 | 均为定性表述，没有官方架构图或流程说明；第三方 B11 的"统一数据层 + API 级授权端点 + human-in-the-loop"是第三方推断 |
| 6 | **「主动服务」目前应视为未实现/未公开** | 全部官方 AIR 场景均为用户发起；B12 之外的第三方也只用疑问句讨论"是否会变成 proactive advisor" | 这是「无正面证据」的否定性判断，只能写成推断；不能写成"AIR 没有主动能力"的断言 |
| 7 | **欧洲扩展在核验日之后发生，且官方页面未同步** | B4/B5/B6/B7 报道西班牙（09-01）、法国（09-02）、爱尔兰（09-02）上线；A12/A16 官方 news 索引未收录；A13（08-25）仍写 "UK only" | 官方没有对应的欧洲上线新闻稿；A2 只证明 NO 站存在产品页，不证明功能已开通 |
| 8 | **官方「零留存」承诺与「交互记录留存」并存，不矛盾但需读对** | A3/A4 说第三方 AI 伙伴不存数据；A6 说 Revolut 自己留存交互用于质量评估；B6 也点出 "External non-retention therefore does not mean that no conversation records exist" | 属对两组官方表述的合并解读；A6 未写明具体留存期限（期限在 Business Privacy Notice 中，A7 指向它，本次未逐条核） |
| 9 | **AIR 的模型层是"混合 + 可换"，与公司自研 PRAGMA 不是一回事** | B1 "mixture of vendor models and SOTA open-source models fine-tuned" + "centralized AI gateway… swap providers when needed on fly"；A13/A14 中 PRAGMA 服务于风控、平台运营与推荐 | PRAGMA 官方材料未提 AIR 用它做生成；B3（NVIDIA）也未把 PRAGMA 与 AIR 关联。两者关系只能说到"不同层" |
| 10 | **AIR 是 Revolut AI 体系的"门面"，不是唯一 AI 资产** | A13 的 Revolut Research / PRAGMA、B9 的自建模型与数据中心、B10 的 Google Cloud + Gemini 合作，均指向更广的 AI 基础设施 | 这些材料都不是 AIR 的产品文档；B9 晚于核验日且属战略表述 |

### C.4 尚未核实（明确的证据缺口）

| # | 未能核实的命题 | 已尝试的检索 | 缺口影响 |
|---|---|---|---|
| 1 | AIR 使用的 LLM 供应商与型号 | 官方 6 页 + 官方 news/blog 索引 + NVIDIA 案例 + 第三方检索 | 无法对标模型能力与成本；B10 提到 Google Cloud/Gemini 是集团级合作，不等于 AIR 的模型 |
| 2 | AIR 的 RAG 实现细节（切分、向量库、Embedding、重排） | 官方页面 + B1 + B13 | 无法复刻检索链路；**禁止在技术文档里写"AIR 用了向量库"** |
| 3 | AIR 的工具调用协议与端点清单 | A4/A5/A6/A7 + B1 | 无法对齐工具面；只能用官方公开的动作清单反推 |
| 4 | AIR 的上下文/记忆窗口与多轮策略 | B1（只提到自建 context & memory 系统） | 无法判断多轮一致性上限 |
| 5 | AIR 的评测集、指标与线上监控口径 | A6（只说交互被评估用于质量）、B1（列了 evals/monitoring 组件） | 无公开指标，无法做量化对标 |
| 6 | AIR 的防幻觉与注入防护机制 | 官方全部页面 | **只能自研**；赛题 30% 安全分不能指望对标现成方案 |
| 7 | AIR 的逐动作权限分级（哪些算 sensitive） | A4/A5/A6/A7 | 只能拿"生物识别"当唯一公开判据，无法还原分级表 |
| 8 | AIR 是否支持「一键取消订阅 / 识别订阅扣费」 | A1/A4/A5/A6 | 赛题场景 5 只能参考"暂停周期性支付"与"屏蔽商户" |
| 9 | AIR 是否有跨 App / 第三方 agent 接入（MCP 等） | B9（CEO 访谈，晚于核验日，属计划表述） | 不能宣称 AIR 已是开放 agent 平台 |
| 10 | AIR 的响应延迟、可并发量、成本 | 全部材料 | 无从对标性能 |
| 11 | 帮助中心是否另有未索引到的 AIR 文章 | F3、F4（站内搜索 404 / 无响应） | 「只有一篇」是推断 |
| 12 | 欧洲各市场上线的官方口径 | A12、A16 官方 news 索引 | 只有媒体口径，官方页面未证实 |

---

## D. 任务书 0.1 节 9 问逐条回答

### Q1｜AIR 的产品定位、目标用户与交互方式是什么？

**结论**：定位是「把 App 内多菜单操作压缩成一次对话的银行执行助手」——官方称之为 "an execution engine"（A5）、"moves beyond the traditional chatbot"（A3）、"a co-pilot"（A3 高管引语），落地方式是 "One chat. Total control."（A1）。目标用户是 Revolut 存量 App 用户（英国 1300 万，A4；后扩欧洲，见 C.3-7）。交互方式是 App 内手势唤起 + 自然语言单句请求 + 对话式回答/执行（A1/A5）。**证据状态：官方资料明确证实。**

**对项目的意义**：赛题 30% 创新与交互体验分，对标的不是「会聊天的银行」，而是「一句话办完事」。

### Q2｜AIR 能查询哪些金融信息？

**结论**：消费（金额、分类、趋势、周期对比，如 "How much did I spend on groceries last month?"）、预算与储蓄（"How much can I save this month?"、"assist with budgeting"）、订阅（"check your subscriptions"）、卡片（卡片与限额状态）、投资（持仓、个股行情如 "Check Nvidia's stock price"、"What are my investment earnings this year?"）、旅行与历史预订/旅行支出（B4/B5）、以及产品功能与规则（A6："help you understand product features"）。**证据状态：官方资料明确证实（能力清单）；具体数据维度以何种图表/卡片呈现——尚未核实。**

### Q3｜AIR 能分析哪些账单、消费、储蓄、预算信息？

**结论**：官方口径是 "insights into spending patterns, trends, and activity"、"analyse your spending, and assist with budgeting"（A5/A6），加上博客的 "Budget better, save more" 与 "Where's my money going?"、"How much can I save this month?"（A4）。**没有**官方证据显示 AIR 做「异常交易识别」「月度/年度账单报告」这类更结构化的分析（A4/A5/A6 均无）。**证据状态：消费/趋势/预算/储蓄测算——官方资料明确证实；异常交易识别与周期账单报告——尚未核实。**

### Q4｜卡片、订阅、理财、投资各自的能力是什么？

- **卡片**：冻结/解冻、单次虚拟卡、消费限额、屏蔽商户与未来支付（A4/A5）。
- **订阅**：查询订阅、管理月度支出、暂停经常性支付（A4）；**一键取消订阅/自动识别订阅扣费——尚未核实**。
- **理财/投资**：读持仓、读收益、查个股行情；**写侧（申购、赎回、改单、撤单）被法律文件明文禁止**（A6），且不构成投资建议（A6）、由 Revolut Trading Ltd 以非投顾型 RTO 方式提供（A1 脚注）。
- **证据状态**：卡片与订阅的上述能力——官方资料明确证实；投资读写边界——官方资料明确证实（边界是本节最硬的一条证据）。

### Q5｜「查询 / 建议」与「写操作」的边界在哪里？

**结论**：三句话构成完整边界：①可读范围 = 用户在 App 里已能看到的数据 + 持仓 + 第三方实时行情 + 账户条款与 FAQ（A4/A6）；②可写范围 = 卡片控制、周期付款暂停、商户/未来支付阻断、单次虚拟卡、eSIM 购买（A4/A5）；③不可写 = 任何形成交易的投资动作（A6 原文）。此外语气边界：只能给事实与信息，不能给"财务、投资或其他建议"（A6），博客也标注 "Information provided is not investment advice"（A4）。**证据状态：官方资料明确证实。**

**对项目的意义**：赛题场景 3 要求"一键申购/赎回 + 理财产品申购（红级）"，与 AIR 的只读投资边界**正面冲突**——这一条要单独立论（见 E-4 风险列）。

### Q6｜确认、认证、隐私与数据保护要求有哪些？

**结论**：①敏感动作需生物识别批准（A4）；②零留存、不用于第三方训练（A3/A4）；③最小可见性（A1/A4）；④使用即构成对技术伙伴披露相关信息的同意，含 client secrets，且构成银行保密豁免（A6/A7）；⑤交互记录由 Revolut 留存并做质量评估（A6/A7）；⑥Business 版在展示或执行动作前校验账户角色权限（A7）；⑦响应 "as is"，工具内无法直接转人工核对，可通过 AI Support 输入 "live agent" 转人工（A6）；⑧官方承认输出可能不准确（A4）；⑨合规环境：UK GDPR 下 biometric data 属特殊类别数据、自动化决策与 profiling 单独成章（A8）。**证据状态：官方资料明确证实。**

### Q7｜自然语言交互、主动服务、跨场景三块里，哪些值得借鉴？

- **自然语言交互：值得借鉴**（证据状态：官方明确证实）。手勢入口 + 一句话办一件事 + 口语示例句式（A1/A4/A5），是本项目交互层最直接的对标。
- **主动服务：目前不宜作为对标**（证据状态：尚未核实，倾向否定）。官方无任何主动提醒/主动规划的描述（A1/A3/A4/A5/A9）。赛题若要讲主动能力，应定位为"自研增量"，不要写成对标 Revolut AIR。
- **跨场景：部分值得借鉴**（证据状态：官方明确证实产品能力，架构方式为可信公开技术资料）。可借鉴的是"同一对话内跨自有产品串联 + 子 Agent 扩展"，A13 的 "connected engines" 与 B1 的 sub-agent 机制都指向这一形态；**不可**借鉴的是"开放给第三方 agent"，那是 2026-10-09 CEO 的计划表述（B9），核验日之前没有落地证据。

### Q8｜哪些能力是「已正式推出」、哪些是「逐步开放」、哪些是「能力受限」？

| 状态 | 判定 | 具体项 | 证据状态 |
|---|---|---|---|
| **已正式推出** | 官方产品页 + 新闻稿 + 博客 + 帮助中心同时覆盖，且上线日期明确 | 消费洞察、预算/储蓄测算、订阅查询与管理、卡片控制、虚拟卡、周期付款暂停、商户阻断、投资信息查询、旅行/积分/eSIM | 官方资料明确证实 |
| **逐步开放** | 官方明说灰度铺开 | 英国 1300 万客户的 rollout（"Starting to roll out to customers across the UK from today"，A3；"rolled out to customers gradually… Your access is arriving shortly"，A5）；部分市场（西班牙 09-01、法国 09-02、爱尔兰 09-02）扩展为媒体口径，官方 news 索引未收录 | 英国：官方明确证实；欧洲扩展：可信公开技术资料证实（媒体口径），官方未直接证实 |
| **能力受限** | 官方以法律/免责形式划死 | 投资：不可下单/改单/撤单、不构成建议（A6）；eSIM 等受 T&C 与 18+ 限制（A2）；"Opening a bank account or upgrading plans does not guarantee access to all services provided in-app"（A2）；响应 "as is"、工具内无法直接转人工核对（A6）；输出可能不准确（A4）；对 AI 透明度的法律文件至今未点名 AIR（C.3-1） | 官方资料明确证实 |

### Q9｜内部 Agent 架构、模型配置、工具调用链路、知识库实现：已公开证据与无法核实部分？

**已公开（可信公开技术资料证实，来源 B1 第一方高管，原页未打开）**
- 平台形态：多 Agent 系统，核心团队守 guardrails，产品团队自建 sub-agent。
- 模型策略：厂商模型 + 针对特定任务微调的 SOTA 开源模型混合；集中式 AI 网关实现 model-agnostic，运行时可切换供应商。
- 自建组件：orchestration、RAG pipelines、guardrails、evals、real-time monitoring、labelling、QA/QC。
- 上下文与记忆：自建系统，Agent 可查询"每个客户在安全隔离环境中存储的 user profile"。
- 与生态关系：直连 Revolut 内部知识库与产品生态，因而能代客执行动作。

**已公开（官方资料明确证实，A13/A14/A6/A7）**
- PRAGMA 基础模型服务风控、平台运营与产品推荐；AIR 是其上的前端能力，"executes complex financial tasks in a single step"（A13）。
- 投资类回答会用到内部知识（条款、FAQ）与第三方实时行情（A6）。
- Business 版在动作前做角色权限校验（A7）。

**无法核实（尚未核实）**：具体 LLM 供应商与型号；向量库 / Embedding / 重排实现；检索与上下文拼装策略；工具调用协议、端点与参数schema；编排与规划算法；评测集、指标与线上监控口径；延迟/并发/成本；防幻觉与注入防护；多轮记忆窗口与冲突策略。

**必须写进安全自评报告的一句话**：以上"已公开"部分中唯一直接涉及 AIR 内部实现的材料来自一位高管的个人社交平台帖，且本次未能打开原页逐字核对——**不得当作可引用的官方架构文档**。

---

## E. 对本项目（banking-agent）的可借鉴设计清单

| # | 借鉴点 | AIR 的公开依据（带证据状态） | 借鉴价值 | 实现难度 | 风险 |
|---|---|---|---|---|---|
| E1 | **手势唤起的单入口 + 对话即入口**（取代多标签页） | "Start your first conversation with AIR in seconds. Just swipe down from the middle of your app screen, or go to Profile → Chats → AIR"（A1/A5，**官方资料明确证实**） | 高：直接命中赛题 30% 交互体验分；演示时一个手势进入智能体，说服力最强 | 低 | 低 |
| E2 | **"只访问用户已可见数据"作为权限基线** | "AIR only accesses the data you can already see, such as your transactions, investments, and cards"（A4，**官方资料明确证实**） | 高：把"越权"从抽象威胁变成可测口径（工具返回值与 App 可见集一致）；可直接写进安全自评报告的检查项 | 低 | 低 |
| E3 | **敏感动作一律强验证，查询类不打扰** | "AIR cannot perform sensitive actions without your biometric approval"（A4，**官方资料明确证实**） | 高：与赛题 3.1 红黄绿分级天然对齐——官方只有"敏感/非敏感"两档，我方在此之上再细分金额维度即可 | 中 | 中：赛题要求按**日累计 1000 元**分界，单靠"生物识别"不足以表达金额维度，必须自建额度状态机 |
| E4 | **写操作白名单化，并把"投资下单"整类排除** | 动作清单见 A4/A5（**官方资料明确证实**）；"You will not be able to place, modify, or cancel any order in relation to financial instruments…"（A6，**官方资料明确证实**） | 高：工具面收敛后，注入对抗、越权、审计都好做；同时给出一个干净的"能力边界声明"模板 | 低 | **高**：赛题场景 3 明确要求"一键申购/赎回""理财产品申购（红级）"，与 AIR 边界正面冲突。必须自证取舍：建议设计为"仅生成带参数的待确认指令 + 二次强确认 + 全链路留痕"，并在文档中显式声明这是对 Revolut 边界的**有意偏离**及原因 |
| E5 | **知识库 + 产品生态直连 → 从"回答"到"代客执行"** | "Because AIR is integrated directly into Revolut internal knowledge base and product ecosystem, it can go beyond answering support questions and start executing actions on behalf of the user"（B1，**可信公开技术资料证实**，原页未打开） | 高：这是 AIR 能"单步完成复杂金融任务"的根因，对应赛题 3.3 的工具调用链路设计 | 高 | 中：知识直连一旦失控即等于越权面放大，必须配 E2 的可见性约束与 E3 的强验证 |
| E6 | **多 Agent 平台化：核心守 guardrails，产品自建 sub-agent** | "built as a multi-agent system… each product team can build and operate its own sub-agent, while the core AIR team keeps control of core behaviours and safety guardrails"（B1，**可信公开技术资料证实**） | 高：直接对应赛题场景 6「跨场景联动」——用子 Agent 承载场景，用核心层统一权限与安全 | 高 | 中：多 Agent 放大越权与注入面；需明确"所有写操作必须回到核心层校验"的硬约束 |
| E7 | **把 evals / 实时监控 / 标注 / QA-QC 当一等公民** | "orchestration, RAG pipelines, guardrails, evals, real-time monitoring, labelling, QA/QC"（B1，**可信公开技术资料证实**） | 高：赛题 40% 任务完成率需要可复现评测；监控与标注是安全自评报告的硬证据 | 中 | 低 |
| E8 | **集中式 AI 网关 + 多模型混用（可降级、可换供应商）** | "a mixture of vendor models and SOTA open-source models fine-tuned for specific tasks… model-agnostic through a centralized AI gateway, which allows us to route tasks to different LLMs and swap providers when needed on fly"（B1，**可信公开技术资料证实**） | 中：演示与压测期可切换模型/降级；也是成本可控的技术抓手 | 中 | 中：多供应商带来输出风格与安全对齐不一致，需统一护栏层 |
| E9 | **自建上下文/记忆 + 每客户隔离 profile** | "in-house context and memory management system that allows AI agents to query a user profile stored in a secure and isolated environment for each customer"（B1，**可信公开技术资料证实**） | 高：对应赛题"幻觉防护"与多轮一致性——避免串号、避免把 A 账户事实用于 B 请求 | 高 | 中：跨会话记忆是注入与越权的高危面，需读写分离 + 记忆内容同样做可见性过滤 |
| E10 | **三条隐私承诺（零留存 / 不训练 / 只可见）作为对外说明模板** | A4 "Secure by design" 三条 + A3 表述（**官方资料明确证实**） | 高：安全合规 30% 权重；可直接改写为我方系统的隐私口径，并落到日志留存与供应商协议条款上 | 低 | 低 |
| E11 | **交互记录留存 + 质量评估（可审计）** | "Your interactions with AI Assistants will generally be recorded and assessed for quality purposes"（A6，**官方资料明确证实**） | 中高：赛题要求"完整决策链路与执行日志"，这是官方现成口径，可扩展为"检索 + 工具调用 + 确认记录"三段式日志 | 低 | 低：注意留存期限与个人信息最小化（可参考 A8 的 UK GDPR 框架） |
| E12 | **动作前二次权限校验（角色/账户级）** | "Before showing account information or initiating an action, the AI Assistant checks the permissions attached to your role for that Business account"（A7，**官方资料明确证实**，对象为 Business 版） | 高：双保险模式——规划层判权限 + 执行层再判一次权限，是注入对抗的强防线 | 低 | 低 |
| E13 | **"单步完成复杂任务"作为演示验收标准** | "AIR, Revolut's in-app assistant that executes complex financial tasks in a single step"（A13，**官方资料明确证实**） | 高：给演示脚本一个可量化的指标（一句指令 → 一步到位 → 有审计记录） | 低 | 低 |
| E14 | **主动能力：自己实现，不要写成对标 AIR** | 官方全部 AIR 场景均为用户发起；平台层仅有与 AI 无关的 opt-in Lifestyle 通知（A1/A3/A4/A5/A9，**尚未核实 / 倾向否定**） | 中：主动建议是赛题创新分的差异点，值得自研；但表述必须是"我方自研" | 中 | 中：主动建议触碰投资建议合规边界；国内赛题语境下必须写明"仅基于本人账户数据的事实性提示，不构成投资建议" |

---

## F. 使用本文档的三条纪律

1. **不要把 AIR 说成"用了向量库 / GraphRAG"。** 官方层面从未出现这些词；唯一提到 "RAG pipelines" 的是一位高管的 LinkedIn 帖，且原页本次未打开（B1 / F6）。第三方个人博客声称 AIR 是 RAG 架构并集成 Gemini、200+ H100（B13），来源等级最低，不得引用。
2. **不要把 AIR 说成"主动型理财顾问"。** 核验到的全部官方 AIR 场景都是用户发起；"是否会升级为主动顾问"至今只是第三方的疑问句。
3. **不要照搬 AIR 的投资只读边界而不做取舍说明。** 赛题场景 3 明确要求"一键申购/赎回"与"理财产品申购（红级）"，与 Revolut 的法律边界直接冲突；必须在安全自评报告中写清这是有意偏离、偏离后的补偿控制是什么。

---

*文档结束。全文所有事实性陈述均可经 A 节台账的 URL 复核；标注为「推断」「尚未核实」的条目不得作为事实引用。*
