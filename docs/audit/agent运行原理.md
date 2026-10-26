# agent 运行原理（一次输入 → 裁决 → 执行 → 审计）

> 版本 v1.0 · 2026-10-26 · 只读代码核对，行号为当前工作树（HEAD 0aa9689 之后）。
> 一句话：**这不是一个自主 agent 循环，而是「一次模型调用 + 一张确定性 DAG + 每个节点一次三色裁决 + 每个写操作一张能力令牌」。**
> 模型的职责到「意图 + 槽位」为止；颜色、金额、放行、落账全部由代码与规则决定。

## 1. 五层与依赖方向

| 层 | 模块 | 职责 | 禁止 |
|---|---|---|---|
| L5 接口/视图 | app/api/*（8 个 router）、app/views/*、app/planner/* | 解析请求、编排一轮对话、装配只读视图 | 路由里写 SQL/开事务；视图写库 |
| L4 用例 | app/use_cases/* | 一个写操作 = 一个事务 + 一个 Outcome | 收 API schema、发 HTTP 话术 |
| L3 领域 | app/funds.py、plans.py、proactive.py、subscriptions.py 等 | 业务规则、表读写、审计 | import policy/tools/planner |
| L2 仓储/策略 | app/handoff_store.py、app/policy/* | 状态迁移与快照；纯函数裁决与签发 | policy 不许碰网络/模型 |
| L1 基础设施 | app/db.py、audit.py、clock.py、ids.py、tools/* | 连接/事务、审计链、时间、工具执行 | db 不许 import 上层；tools 不许 import planner |

依赖方向写死在 tests/test_imports.py 的 LAYERS：数字大的可 import 小的，反向依赖豁免表必须为空。

## 2. 一次输入的全链路（12 个阶段）

| # | 阶段 | 触发点 | 模型/代码 | 产出 |
|---|---|---|---|---|
| 1 | 接会话 | app/api/chat_api.py:72 | 代码 | 没带 session_id 就接最近一条会话（chat_flow.py:49-63） |
| 2 | 澄清补全 | app/api/chat_api.py:76；planner/clarify.py:117-142 | 代码（正则） | 「尾号 4417」「第一个」拼回上一轮问句 |
| 3 | 快路 | app/api/chat_flow.py:66-77 | 代码（词表） | 储蓄目标/提醒类请求跳过模型，source="assist" |
| 4 | 确定性拒答 | app/planner/intent.py:315-327；planner/refusal.py | 代码（正则） | 念验证码/要密钥/冒充内部身份 → unsupported 哨兵 |
| 5 | **意图解析** | app/planner/intent.py:298-343 | **模型（全链路唯一一次）** | {intent, slots, missing, confidence}；没调工具重试 1 次，仍没有就词表兜底 |
| 6 | 上下文补全 | app/api/chat_flow.py:142-161 | 代码 | unknown 或置信度<0.5 时把上一句带进反问 |
| 7 | 记忆注入 | chat_flow.py:183-193 → intent.py:278-288 | 代码 | 长期记忆单独一条 system，截 2000 字，只当默认值 |
| 8 | 三道闸 | chat_flow.py:259-275；planner/payment_guard.py；app/scope.py | 代码（零 IO） | 付款歧义 / 越界请求 → 只反问，不建图 |
| 9 | 发 SSE | chat_flow.py:276-277 | 代码 | start + intent |
| 10 | 建图 | chat_flow.py:319-322；planner/dag.py:129-167 | 代码（模板） | 意图→固定节点模板；失败落哨兵图，绝不悄悄改成查余额 |
| 11 | 开轮次 | planner/executor.py:151-152；state.py:157-192 | 代码（事务） | 清过期 hold → 熔断检查 → insert_run → audit dag.built |
| 12 | 逐节点执行 | planner/executor.py:272-341 | 代码 | 拓扑序：解析 @ref 参数 → 三色裁决 → 确认/因子 → 令牌 → 工具 |

收尾：done{summary, balance_after_fen} + audit session.end；异常 → trace_id + run.failed + SSE error。

## 3. 模型到底做了什么

- 调用：intent.parse → client.chat(tools=[emit_intent], temperature=0.0, max_tokens=512)（intent.py:331-355）。
- 输出被强制成 schema（intent_schema.py:192-235，additionalProperties=false），且模型**不能执行任何工具**。
- 之后的槽位白名单、缺槽收口、金额正则、关系词表（「我妈」）、他人账户判定、一次只问一个问题、置信度门限，全是代码（intent.py:725-764、815-851、682-702）。
- 因此「AI 理解」的边界就到槽位；越界由付款闸、边界闸、R12、R03 四道代码闸接管。

## 4. DAG 是模板，不是模型现编

| 意图 | 节点序列 | 位置 |
|---|---|---|
| query_balance / bill_analysis / subscription | 单节点 | dag.py:43-46 |
| transfer | resolve_payee → query_balance → transfer | dag.py:47-51 |
| birthday_plan | resolve_payee → query_balance → reserve_funds → pay_merchant | dag.py:52-57 |
| schedule_transfer | resolve_payee → schedule_transfer | dag.py:60-63 |
| reversal | query_transactions → reverse_transfer | dag.py:65-68 |
| 赎回后转账 | resolve_payee → redeem_fund → query_balance → transfer | dag.py:78-85 |

建图四道校验：ID 不重复、依赖不悬空、工具在册、无环（dag.py:170-183）。节点间只通过 @node.field 传值；resolve_payee 候选必须恰好 1 个，否则停轮并挂人工单。

## 5. 三色裁决（R01–R13，顺序即优先级）

| 序 | 规则 | 条件 | 色 | 因子 |
|---|---|---|---|---|
| 1 | R12_other_account | 只读工具但目标是别人账户 | red | — |
| 2 | R01_readonly | 只读工具 | green | — |
| 3 | R02_invalid_amount | 花钱且金额<=0 | red | — |
| 4 | R03_unknown_tool | 工具不在册（含哨兵） | red | — |
| 5 | R04_untrusted_amount | 花钱且参数/原话命中注入 | red | face+sms |
| 6 | R05_confirm_only | 8 个「只确认」工具 | yellow | — |
| 7 | R13_absolute_cap | 配置了绝对日限额且越线 | red | 无（硬拒） |
| 8 | R06_transfer_yellow | 0<金额<=1000 且未越日线 | yellow | — |
| 9 | R07_transfer_red_daily | 日累计（含本笔）>1000 | red | face+sms |
| 10 | R08_transfer_red_single | 单笔>1000 | red | face+sms |
| 11 | R09_sensitive_red | 11 个敏感工具 | red | face+sms+otp |
| 12 | R10_invest_red | 申购/赎回 | red | face+otp |
| 13 | R11_default_deny | 兜底 | red | 无（硬拒） |

- 引擎按顺序取第一条命中；走完没人命中直接抛 RULES_BROKEN（engine.py:34-41）——宁可炸，不默认放行。
- 红黄绿的真实差异：green 直接执行；yellow 弹确认卡；red 弹卡且必须有因子；**无因子的红 = POLICY_BLOCKED 硬拒**（executor.py:526-552、608-646）。
- 判色只读 PolicyContext（spent_today_fen + now_ms），执行器自己不判色（executor.py:502-523）。

## 6. 确认卡与多因子

- 卡片 TTL 默认 120 秒（state.py:35），可用 CONFIRM_TIMEOUT_S（10-3600）覆盖；过期 → expired + 释放 hold。
- 卡片字段在发事件这一刻从库里查（收款人、账户、手续费口径），不在前端拼（events.py:230-282）。
- 因子挑战：6 位码，库里只存 sha256(secret|challenge_id|code)（factors.py:82-89）；有效期 5 分钟。
- 次数限制：同一挑战错 5 次作废；同一确认同一因子最多签发 3 张挑战；两次签发间隔 ≥10 秒（factors.py:44-52；A1 修复后）。
- 校验用 hmac.compare_digest；声明自己做了哪个因子不算数，必须有服务端签发的一次性 proof（executor.py:184-235）。
- 确认/拒绝/过期/硬拒四种结果分开审计，能区分「人不要」「没人点」「规则不让」（executor.py:608-646）。

## 7. 执行：令牌七步 + 资金两阶段

- 写工具 handler 第一行必须 require_token（tools/base.py:46-54）。
- 令牌校验顺序固定：结构→签名→过期→nonce 一次性→tool→params_hash→account/amount（security/token.py:281-303）。改金额或收款人，令牌立即失效。
- 幂等键 = sha256(session|node|params_hash|run_id) 前 32 位：同轮重放幂等，同会话再说一次算新单（funds.py:100-107）。
- hold→commit 两阶段；commit 在 BEGIN IMMEDIATE 事务里复核日累计、通道、余额，任一不符整笔回滚（funds.py:603-645）。
- 提交时如果发现日累计已越线（判档后被别的交易推过线），旧黄卡作废、按红灯重新授权，绝不拿黄卡放行（executor.py:329-336、344-401）。

## 8. 审计哈希链

- 材料 = prev_hash | ts_ms | event_type | payload_json；sha256 链式；GENESIS 起始（audit.py:6-8、134-141）。
- payload_json 固定 sort_keys + 紧凑分隔符，保证同一 payload 只有一个哈希。
- event_type 是闭集，写错直接 ValidationError——审计不能被顺手加事件名污染。
- verify_chain 逐行重算，任一不符写 audit.chain_break 并返回 False；页面「审计链 校验通过」即来自这里。

## 9. 恢复（进程重启/刷新之后）

- 三条不变量：不重解析意图、不重问策略、绝不重跑终态节点（recover.py:8-12）。
- 选轮次：先挑未完成（running/waiting），再按 created_ms 倒序，避免已答完的新轮盖住挂卡的旧轮。
- 点确认时内存没有这一轮 → 走 recover.confirm；/api/session/{id}/recover 走 recover.resume；恢复中冒出新的确认卡就停住等用户点，不把 HTTP 挂 120 秒。

## 10. 降级与防护

- 模型降级：401/403/404/408/429 与 5xx 切下一个目标；400/422 直接抛不降级；每目标重试 3 次；全失败 → HTTP 503（main.py:127-130）。注意 503 发生在流开始前，不是 SSE error。
- 注入防护：NFKC + 折叠 + 抹零宽后按七类模式打分；工具参数与用户原话双面扫描；命中只抬等级（R04 → face+sms），不直接拒绝。
- 越权：R12 拦「查别人账户」；R03 拦模型编造的工具；两者都是代码，不依赖模型自觉。

## 11. 已知边界（不是 bug，是设计取舍）

1. 没有 agent 循环，所以没有「步数爆炸」；代价是模板覆盖不到的请求统一落 unsupported 红卡，而不是由模型现编计划。
2. 澄清状态 _PENDING 与运行态 RUNS 都在进程内；重启/多 worker 会丢澄清，只能靠 recover 重建卡与节点。
3. 限额口径有两处：策略层先读后判、资金层事务内复核；两处 spent_today_fen 必须同源，否则会出现「验完人脸仍被拒」。
4. 模型不可用走 HTTP 503；将来若改成流式解析，这条分支要重做。

## 12. 读者怎么验证

- 单测：tests/test_policy.py（R01-R13）、tests/test_planner.py（DAG）、tests/test_security.py（令牌/因子）、tests/test_audit.py（哈希链）。
- 手测：启动后发「帮我转 500 元给张三」→ 看 SSE 依次出现 intent/trace/confirm；点确认 → tool/done；到「执行记录与安全」看同一 run 的节点与审计。
