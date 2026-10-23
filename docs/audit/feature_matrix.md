# 功能审计表（2026-10-23 · 以代码取证）

> 口径：只看仓库代码、测试与可复跑输出；不采信 README、演示话术、历史报告里的自我评价。
> 「一个 API 或一个按钮」不算完整业务实现，必须能追到落库/执行/拒绝的真实分支。
> 状态只取八种：已实现且有测试证明 / 已实现但测试不充分 / 只有前端展示 / 只有后端接口 / 部分实现 / 仅有文档声明 / 尚未实现 / 受环境限制未验证。
> 证据位置写到文件:行号或测试函数名；测试结果行是本机实测（2026-10-23 23:5x）。

## 一、信息架构与设计系统

| 能力 | 状态 | 证据（文件:行号 / 测试） | 缺口说明 |
|---|---|---|---|
| 六区主导航与视图切换 | 已实现但测试不充分 | web/index.html:576-581 六个 button.nav-tab；对应 #view-overview:594 / assistant:648 / bill:694 / plans:724 / manage:854 / security:873；切换 bxSetView（web/index.html:2847-2850、4032-4044） | 只有 scripts/check_frontend.py 的静态字符串门槛（66/66，本机跑过），没有 DOM 级或浏览器级导航断言；运行时正确性靠截图 |
| 设计变量与可复用组件 | 已实现但测试不充分 | web/index.html:17-70 定义 --green/--amber/--red/--brand、--bg/--panel/--line、--fs-*/--fw-*/--s-*/--r-* 三组变量；确认卡组件 .confirm-card/.confirm-head/.confirm-row/.confirm-amount/.confirm-timer 于 445-463 | 没有样式回归或对比度/可读性检查；变量是否被各视图一致使用只能人工看 |
| 财务总览读真实数据 | 已实现且有测试证明 | app/api/business.py:563-630 overview_view；tests/test_business_api.py:64 test_overview_balance_and_month_totals_come_from_the_database | 无 |
| 空列表 ≠ 暂不可用 | 已实现且有测试证明 | app/api/business.py:563-630 只把「缺账户」放 unavailable；tests/test_business_api.py:95 test_overview_never_sells_empty_lists_as_unavailable | 无 |
| 只读业务接口鉴权 | 已实现且有测试证明 | app/api/business.py:1032-1033 router 级 Depends(require_api_token)；tests/test_business_api.py:230 test_business_views_require_the_api_token | 无 |
| 执行记录与安全中心 | 已实现且有测试证明 | app/api/business.py:899-997 executions_view（会话/run/node/确认/拒绝/审计链 chain_ok）；tests/test_business_api.py:208 test_executions_expose_nine_task_states_and_audit_chain | 「九态」只覆盖标签层，见下一节状态机 |
| 规划过程默认折叠 | 已实现但测试不充分 | web/index.html:666-676 #view-assistant 内 details.panel（intent/plan/policy 等）；主聊天流只渲染结论与确认卡 | 折叠态没有自动化断言；用户是否真的「不被打扰」只能人工判断 |

## 二、交易确认、授权与状态

| 能力 | 状态 | 证据（文件:行号 / 测试） | 缺口说明 |
|---|---|---|---|
| 收款人唯一候选直接进确认 | 已实现且有测试证明 | app/planner/runner.py:252-263 _payee_gate（len==1 放行）；tests/test_tools.py:329 test_resolve_payee_disambiguates_duplicates_by_card_tail | 唯一候选时不再反问，但也就不给用户「换一个」的机会；改名只能重新发起 |
| 同名多候选必须让用户选 | 已实现且有测试证明 | app/planner/runner.py:252-263（>1 反问，绝不猜）；结构化候选 _payee_clarification:119-148；tests/test_payee_discriminator.py、tests/test_clarify_resume.py | 候选超过 DISAMBIGUATE_LIMIT 时只列前几条，未选到的仍需改口述 |
| 信息不足先追问 | 已实现且有测试证明 | app/planner/intent.py 低置信/缺槽位 → SESSION_ASK；tests/test_intent_question.py、tests/test_planner.py:229 test_parse_missing_slot_asks_exactly_one_question | 无 |
| 改选后收款人真的变 | 已实现但测试不充分 | app/planner/clarify.py:84-107 augment 把「尾号4417」接回上一轮原话；web/index.html:3927-3954 bxRenderPayeePicker；tests/test_clarify_resume.py 覆盖模块与 /api/chat；tests/test_e2e_transfer.py:289 test_ambiguous_payee_returns_structured_candidates_and_picks_nothing 断言候选结构化、两个尾号都在、且没有生成任何确认卡或转账 | 仍缺「在页面上点候选 → 新确认卡的 payee_id 变了」这一跳的自动化（需 DOM/浏览器）；截图 main_01→main_02 是人工证据 |
| 确认前改金额/收款人 → 旧授权失效 | 已实现且有测试证明 | app/security/token.py:331-334 params_hash 比对；tests/test_token.py:142 PARAMS_MISMATCH、:149 AMOUNT_MISMATCH | 无（改参必须重新走确认，旧令牌同时被 nonce 消耗） |
| 授权绑定工具/参数/账户/金额 | 已实现且有测试证明 | app/security/token.py:265-346 七步校验（结构→签名→过期→nonce→tool→params_hash→account/amount）；tests/test_token.py | 无 |
| 过期授权不可执行 | 已实现且有测试证明 | app/security/token.py:306-309 TOKEN_EXPIRED；TTL=120s（token.py:41）；tests/test_token.py、tests/test_confirm_timeout.py:106 test_capability_token_ttl_is_120_seconds_too | 无 |
| 确认卡 120 秒作废 | 已实现且有测试证明 | app/planner/state.py:35 CONFIRM_TIMEOUT_MS=120_000；executor.py:369-410 wait_for；tests/test_confirm_timeout.py:84 test_confirm_card_expires_on_time（墙钟 50ms 钉子） | 无 |
| 超时与用户拒绝在审计里可区分 | 部分实现 | executor.py:446-462 超时走 mark_confirmation expired + audit confirm.rejected(reason=TIMEOUT)；确认卡状态本身是 expired | 审计事件仍复用 confirm.rejected，没有 confirm.expired；事后只能靠 payload 里的 TIMEOUT 猜，旧审计 1.5 未修 |
| 确认卡字段完整（金额/收款人/账户/方式/到达/手续费/等级/验证步骤） | 已实现且有测试证明 | app/planner/events.py:192-243 describe_confirmation（黄灯也补一条「点确认」步骤，红灯是因子+确认）+ 245 prompt_payload；前端按后端字段渲染 web/index.html:3956-4025；tests/test_e2e_transfer.py:256 test_confirm_card_carries_what_a_normal_user_needs 逐字段断言 source_account.tail / payee.bank / fee.known is False / verification.level_label / steps[].{id,label,required} / scheduled_for 键存在 | 手续费只有"不知道"这一种口径（没有资费表）；卡面视觉层次仍靠截图人工看 |
| 九态任务状态机（草稿…需要人工处理） | 部分实现 | app/api/business.py:44-54 TASK_STATE_LABELS 九键；tests/test_business_api.py:208 暴露九态字典 | 仅标签字典：dag_runs.status 只有 running/waiting/done/failed/cancelled（app/db.py:152-159），dag_nodes.state 只有 pending/running/waiting_confirm/waiting_factor/done/failed/skipped（app/db.py:162-173）。draft/need_info/manual 在 app/ 里没有任何生产者（grep 仅命中 business.py 标签本身），也没有状态迁移约束 |
| 重复确认幂等 | 已实现且有测试证明 | app/planner/executor.py:153-165 resume 对非 pending 返回 idempotent；tests/test_api.py:277 test_confirm_twice_is_idempotent；tests/test_planner.py:517 | 无 |
| 前端伪造成功骗不了后端 | 已实现但测试不充分 | 资金/写操作在工具层二次 require_token（app/planner/runner.py:37-66；app/tools/transfers.py:231）；页面状态只认 SSE 与 /api/session 回包（web/index.html:3850-3909） | 没有「伪造 done/prompt 事件或直接改 DOM」的攻击用例；现有防线是令牌校验，不是端到端伪造测试 |
| 待确认卡持久化 | 已实现且有测试证明 | confirmations 表（app/db.py:177-187）；state.new_confirmation INSERT；tests/test_confirmations_persist.py（建卡 pending/批准/拒绝/超时/策略拦停都落库） | 无 |

## 三、转账与资金安全

| 能力 | 状态 | 证据（文件:行号 / 测试） | 缺口说明 |
|---|---|---|---|
| 收款人存在性与归属校验 | 已实现且有测试证明 | app/tools/transfers.py:70-77 _payee_owned_by；tests/test_split.py:68 PAYEE_NOT_OWNED；tests/test_acceptance.py:326-330 越权转账直调被拦 | 无 |
| 单笔限额与分级策略 | 已实现且有测试证明 | app/policy/rules.py:19-20；R08 单笔 >1000 元升红；tests/test_policy.py | 限额是赛题设定值，界面已注明「不代表所有真实银行」 |
| 日累计限额 | 部分实现 | daily_limits 表（app/db.py:208-214）+ R06/R07；tests/test_policy.py | app/tools/base.py:64-75 daily_cap_fen 对红灯返回 None → 红灯整笔绕过日累计；「一天多笔 900 元」在红侧无累计约束（旧审计 1.3 未修） |
| 预授权 hold / commit / 超时释放 | 已实现且有测试证明 | app/funds.py:193-236 commit、396-407 _expire；holds 状态机 app/db.py:98-110；tests/test_funds_invariants.py（超时后 commit 必须失败） | 无 |
| 扣款失败不承诺重试 | 已实现但测试不充分 | app/tools/transfers.py:56-61 UNSETTLED_TEXT「系统不会自动重试…5 分钟后自动释放」；理财同口径 app/tools/invest.py:41-45 | 文案没有被任何测试断言；改回去不会变红 |
| 同会话幂等防重复 | 已实现且有测试证明 | app/funds.py:86-89 idempotency_key=sha256(session|node|params)[:16]；holds.idempotency_key UNIQUE；tests/test_funds_invariants.py:149 test_same_idempotency_key_reuses_one_hold | 无 |
| 跨会话/重启幂等 | 尚未实现 | app/funds.py:86-89 键里含 session_id | 同一笔意图在新会话/重启后重放会生成新 key，可能二次扣款；旧审计 1.6 未修 |
| 卡片状态影响转账 | 尚未实现 | app/tools/cards.py 有完整状态机（freeze 170-179、close 194-242、_transition 285-319），但 app/funds.py 与 app/tools/transfers.py 全文不读 cards（grep 无命中） | 冻结/挂失/单笔限额只影响卡片类工具；「挂失的卡还能转账」是真实缺陷（旧审计 1.2 未修） |
| 账户冻结 | 尚未实现 | app/db.py:63-71 accounts 无 status 列 | 账户级冻结/止付没有建模，任何资金路径都无法表达「账户不可用」 |
| 多因子真实性 | 部分实现 | 确认请求体传 factor（app/api/routes.py:339-349）；app/planner/state.py:340-345 factors_ok 只做字符串子集校验 | 没有短信/人脸/OTP 通道，能直接调 /api/confirm 就只差两个字符串（旧审计 1.4 未修）——本轮最大未解决安全风险 |
| 冲正边界（方向/窗口/次数） | 已实现且有测试证明 | app/funds.py:261-264 只有 direction='out' 可冲正（TXN_NOT_OUT）；7 天窗口 transfers.py:47、256-258；重复冲正幂等 244-254；tests/test_reverse.py:59 test_incoming_transaction_cannot_be_reversed、:43 test_reversing_twice_does_not_pay_twice | 冲正仅在本地账本反向记账，返回 scope=local_reversal + boundary（transfers.py:62-65、287-290） |
| 真实跨行退汇/争议追回 | 尚未实现 | 边界文案 app/tools/transfers.py:62-65 明说「不是把钱从对方那里追回来」 | 没有清算/争议流程，属演示环境能力外；文案已如实，但不能算实现 |
| 越权读他人预约 | 已实现且有测试证明 | app/tools/scheduled.py list 按 account/user 过滤；tests/test_scheduled_semantics.py:test_list_scheduled_only_returns_own_rows | 本轮修掉的缺陷 |

## 四、预约转账与调度器

| 能力 | 状态 | 证据（文件:行号 / 测试） | 缺口说明 |
|---|---|---|---|
| 预约创建/列表/取消 | 已实现且有测试证明 | app/tools/scheduled.py:71-194；tests/test_scheduled.py:35/45/62 | 只有 once/monthly 两种周期，没有每周/每年 |
| 到点执行与失败留痕 | 已实现且有测试证明 | app/tools/scheduled.py:202-299 execute_due/_execute_one；失败写 scheduled.failed（283-299）；tests/test_scheduled_fires.py:208/284、tests/test_scheduled_semantics.py | 无 |
| 进程内调度器 | 部分实现 | app/planner/ticker.py:29-44（SCHEDULED_TICK_MS 默认 30s，0 可关）、47-57 run_due_once、76-88 start/is_started；app/main.py:83-89 启动 | 无独立 claim/lease 列，串行性靠 run_due_once 外包 db.tx（BEGIN IMMEDIATE）；进程退出期间不执行，重启不补跑，无重试队列 |
| 预约授权有效期 | 部分实现 | app/tools/scheduled.py:101 factor_verified_ms 落库；240 只判 >0 | 授权永不过期：三天前红牌验证过的预约到点仍会自签令牌执行（旧审计 5.10 未修） |
| 月付周期语义 | 部分实现 | app/tools/scheduled.py:302-304 due_ms=_next_month_ms，固定 +30 天并注释「不做日历月推算」 | 1 月 31 日的月付会漂移；跨月/闰年无日历月推算 |
| 调度器真实状态上报 | 已实现且有测试证明 | app/api/business.py:535-557 _scheduler_block（kind/interval/running + 手动端点）；tests/test_business_api.py:197 test_plans_reports_the_real_scheduler_not_a_promise | 无 |

## 五、订阅与代扣

| 能力 | 状态 | 证据（文件:行号 / 测试） | 缺口说明 |
|---|---|---|---|
| 订阅列表/查询 | 已实现且有测试证明 | app/tools/subs.py:26 查询；tests/test_tools.py:380 test_list_subs_only_active_by_default | 无 |
| 银行侧取消代扣 | 已实现且有测试证明 | app/tools/subs.py:107-128 CANCEL_SCOPE=bank_side_only + 真改 subs.status；tests/test_subscription_cancel.py 四道防线 | 无 |
| 取消边界写死三处 | 已实现且有测试证明 | 工具返回 boundary（app/tools/subs.py:109-124）、SSE 摘要（app/planner/events.py summarize）、界面（app/api/business.py:163）；tests/test_business_api.py:163 test_management_subscription_boundary_says_merchant_untouched | 无 |
| 从流水推断潜在订阅 | 尚未实现 | app/tools/subs.py 无推断逻辑 | 没有「这条流水像订阅，要不要记下来」的链路；只有订阅涨价异常会侧面暴露（旧审计 2.1 未修） |
| 商户协议号/续费提醒 | 尚未实现 | app/db.py:197-206 subs 无商户协议号、无提醒字段 | 续费前不会提醒；商户协议无法核验（旧审计 2.3 未修） |
| 阻止未来商户扣款 | 尚未实现 | 取消只改本地 subs 行（app/tools/subs.py:107-128） | 商户侧照扣，只能事后发现；文案已明说，但能力不存在 |

## 六、账单分析

| 能力 | 状态 | 证据（文件:行号 / 测试） | 缺口说明 |
|---|---|---|---|
| 分类/商户/月年统计 | 已实现且有测试证明 | app/tools/bill.py 主流程；tests/test_tools.py:233 test_analyze_bill_returns_fixed_shape_and_sums、:319 年账单 | 无 |
| 三类异常识别 | 已实现且有测试证明 | split_orders / odd_hour_overseas / subscription_hike：app/tools/bill.py:198、229、267-307、326；tests/test_tools.py:254 test_analyze_bill_detects_all_three_anomalies | 规则硬编码阈值（500 元、0-5 点、涨幅 30%、OVERSEAS_MARKERS：bill.py:29-36），不能配置 |
| 异常证据链 | 已实现且有测试证明 | 本轮补 tx_ids/occurrence_count/window_start_ms/window_end_ms/comparison：app/tools/bill.py；tests/test_tools.py:266-271 断言计数、窗口、涨幅 | 无 |
| 规则命中 ≠ 欺诈认定 | 已实现且有测试证明 | app/api/business.py:65-102 ANOMALY_RULES（severity/confidence/possible_false_positive/actions）+ :675-689 文案；tests/test_business_api.py:121/135 | 工具层 bill.py 不输出 severity/confidence，分级只存在于 business 层映射；换一个消费者就没有分级 |
| 凌晨判定时区正确性 | 已实现但测试不充分 | app/tools/bill.py 用 time.localtime 判 0-5 点 | 生产随机器时区漂移；没有跨时区用例，只有固定种子的单测 |
| 异常 → 后续动作 | 部分实现 | business.ANOMALY_RULES actions + 前端按钮转 AI 助手 prompt | 「看订阅管理」等跳转没有断言；只能在助手侧把问题再问一遍 |

## 七、理财

| 能力 | 状态 | 证据（文件:行号 / 测试） | 缺口说明 |
|---|---|---|---|
| 产品查询/风险等级/申赎 | 已实现且有测试证明 | app/tools/invest.py 四个工具；tests/test_invest.py:68/82/160/197 | 无 |
| 申购/赎回强验证 | 已实现且有测试证明 | R10 face+otp 红牌；tests/test_invest.py:234 test_policy_keeps_fund_operations_red_with_face_and_otp | 无 |
| 适当性评估（问卷/风险匹配） | 尚未实现 | app/tools/invest.py 全文无适当性调用；app/db.py:227-238 fund_products 无期限/流动性;business CAPABILITIES 自认 suitability_assessed=false | buy_fund 从不调用 estimate_risk；旧审计第 4 节未修 |
| 期限/流动性/费率 | 尚未实现 | app/db.py:227-238 无 term/liquidity/fee 列 | 用户无法在申购前看到锁定期与费用 |
| 净值真实性 | 部分实现 | fund_products.nav_micro 为本地种子静态值（app/db.py:227-238、app/tools/invest.py:222） | 不是行情源；收益/净值曲线不随时间变化 |
| 赎回口径不暗示即时到账 | 已实现但测试不充分 | app/tools/invest.py:41 REDEEMED_TEXT「按本地账务记回账户」；:43-45 失败明说不重试 | 文案无测试断言，改回旧话术不会变红 |

## 八、储蓄目标与主动提醒

| 能力 | 状态 | 证据（文件:行号 / 测试） | 缺口说明 |
|---|---|---|---|
| 储蓄计划计算 | 已实现且有测试证明 | app/proactive.py:332-437 compute_savings_plan（periods/gap/surplus 互斥）；tests/test_proactive_calc.py | 现金流是历史均值外推，不是真实收入日历 |
| 目标保存/修改 | 已实现且有测试证明 | savings_goals（app/db.py:277-287）+ PATCH/GET；tests/test_proactive_api.py（真实 ASGI HTTP） | 无 |
| 手动监测（点一次跑一次） | 已实现且有测试证明 | app/tools/proactive.py:225-249 run_proactive_monitor；app/proactive.py:1223-1353 run_monitor；tests/test_proactive_api.py | 设计约束就是手动，见下一行 |
| 持续后台监测 | 尚未实现 | app/tools/proactive.py:9-10 注释「手动触发是设计约束」；app/planner/ticker.py 只调 scheduled.execute_due | ticker 从不跑 run_monitor；进程退出期间无人监测（旧审计 5.3/5.8 未修）；界面 monitoring_scope 已如实写「需要手动点一次」 |
| 提醒去重与处理 | 已实现且有测试证明 | _alert_exists 按 (goal,rule,reason,period_start) 去重；proactive_alerts/resolve；tests/test_proactive_api.py、tests/test_proactive_safety.py | 无 |

## 九、计划执行与恢复

| 能力 | 状态 | 证据（文件:行号 / 测试） | 缺口说明 |
|---|---|---|---|
| DAG 模板与结构校验 | 已实现且有测试证明 | app/planner/dag.py 模板 + validate；tests/test_planner.py:278/296/360 | 只有串行链，无并发分支/失败重试策略 |
| 多步执行（意图→DAG→确认→扣款→审计） | 已实现且有测试证明 | app/planner/executor.py；tests/test_e2e_transfer.py 全程驱动 | 无 |
| 取消执行并释放资金 | 已实现且有测试证明 | app/planner/executor.py:168-181 cancel；tests/test_api.py:256 test_cancel_endpoint_stops_the_run_and_frees_the_hold | 无 |
| 刷新/SSE 断开后恢复 | 已实现但测试不充分 | web/index.html:3845-3873 bxRestoreSession → /api/session；3875-3909 bxPollSession；1598-1663 confirmAction 断流分支 | 前端恢复逻辑没有自动化断言，浏览器冒烟默认跳过；只有后端 /api/session 有测试 |
| 进程重启后续跑 | 部分实现 | dag_runs/dag_nodes/confirmations 已落库（app/db.py:152-187），/api/session 可读历史（app/api/routes.py:358-397） | app/planner/state.py:76 RUNS 是内存字典，require_run 只查内存（326-330）；重启后未完成 run 无人恢复，pending 确认卡也无人 expire（旧审计 5.2/5.9 未修） |
| 跨场景延迟执行（生日前 2 天订购） | 尚未实现 | app/planner/dag.py 生日模板当场预留+支付；无延迟队列 | 不能宣称「提前订购」；旧审计 5.13 的长期计划缺口仍在 |

## 十、安全、审计与实验

| 能力 | 状态 | 证据（文件:行号 / 测试） | 缺口说明 |
|---|---|---|---|
| 能力令牌七步校验 | 已实现且有测试证明 | app/security/token.py:265-346；tests/test_token.py（23 项） | 无 |
| 审计哈希链 append-only | 已实现且有测试证明 | app/audit.py；app/db.py:326-331 UPDATE/DELETE 触发器；tests/test_audit.py、tests/test_api.py:304 | 无 |
| 审计事件完整性 | 部分实现 | 预约失败已补 scheduled.failed（app/tools/scheduled.py:283-299）；tests/test_scheduled_semantics.py | 缺 confirm.expired；监控审计 trigger 硬编码 "manual"（app/proactive.py:1341、1386），无法区分手动/自动触发（旧审计 5.12 未修） |
| 提示注入防御 | 已实现且有测试证明 | app/security/injection 词表+信封+R04；tests/test_injection*.py、tests/test_security_knowledge.py | L2 模型分类器未在生产启用 |
| 熔断 | 已实现且有测试证明 | app/security/breaker.py；tests/test_breaker.py、test_breaker_production.py | 无 |
| 无代码执行面 | 已实现且有测试证明 | scripts/prove_no_exec_surface.py；tests/test_no_exec_surface.py | 无 |
| 知识层检索/来源过期降级 | 已实现且有测试证明 | app/knowledge/*；tests/test_security_knowledge.py K6（180 天过期降级）、K8（检索异常 degraded）、K9（开关零影响） | 无 |
| 知识层 A/B 实验 | 部分实现 | tests/eval/run_eval.py（A 组 KNOWLEDGE_ENABLED=0 / B 组 =1）、80 例 intent_cases.yaml；tests/eval/results/latest_summary.json：intent 83.75%→88.75%、slot 82.19%→87.67%、clarify 88.75%→86.25% | 该 summary 完成于 2026-10-23 21:15，晚于它的提交是 22:32（b769da1），本轮 planner/events 改动之后**没有重跑**；配额 429，属受环境限制 |
| 红队用例集与 runner | 已实现且有测试证明 | tests/redteam/cases.yaml 43 条（A10/B8/C10/D10/E5）；scripts/redteam.py + redteam_judge.py；tmp/redteam_failures.txt 报告 2026-10-23 23:17 总数 43 / PASS 43 / FAIL 0 | runner 不在 pytest 默认套件；结果依赖真模型版本，报告写在 tmp 未入库，不可回归复跑 |
| 真实浏览器测试 | 受环境限制未验证 | scripts/browser_smoke.py（Playwright + Chromium，沙箱库 + sha256 反证）、scripts/ui_screenshots.py；tests/test_browser_smoke.py:27-30 skipif RUN_BROWSER_SMOKE!=1 | 本机 .venv 无 playwright（实测 ModuleNotFoundError），4 条冒烟默认 skip；只能算「代码与脚本在」 |
| 移动端响应式 | 已实现但测试不充分 | web/index.html:539 @media(max-width:1080px)、:545 @media(max-width:760px)（.confirm-row 纵向 554-555）、:560 prefers-reduced-motion | 没有任何设备/视口自动化；移动端体验只有截图与人工目测 |

## 十一、本轮改动后的状态变化（对照 docs/audit/business_semantics_audit.md）

本轮未提交 diff 的事实范围：新增只读 app/api/business.py 并在 app/main.py 注册；events.describe_confirmation 重写确认卡数据；runner 结构化下发收款人候选；ticker 暴露 is_started；bill 三条异常补证据字段；subs/transfers/invest/scheduled/tool_specs 改口径文案；新增 tests/test_business_api.py、test_scheduled_semantics.py 等。

| 旧审计缺陷 | 现在的状态 | 说明 |
|---|---|---|
| 1.2 账户无状态、卡片状态与资金路径解耦 | 未修 | accounts 仍无 status（app/db.py:63-71）；funds/transfers 全文不读 cards；冻结卡照转 |
| 1.3 单笔=日累计、红灯绕过日线 | 未修 | app/policy/rules.py:19-20 常量未动；base.py daily_cap_fen 对 red 返回 None |
| 1.4 多因子是自证 | 未修 | factor 仍由 POST /api/confirm 请求体传入；factors_ok 只做字符串子集（state.py:340-345） |
| 1.5 TTL/超时正确，但超时记成用户拒绝 | 部分修 | 120s 超时有墙钟测试（test_confirm_timeout.py）；审计仍无 confirm.expired，超时复用 confirm.rejected(reason=TIMEOUT)（executor.py:446-462） |
| 1.6 幂等键含 session_id，跨会话不幂等 | 未修 | app/funds.py:86-89 未改 |
| 1.7 预约无原子 claim、授权不过期、月付 +30 天 | 未修 | ticker 仍靠 db.tx 串行（app/planner/ticker.py:47-57）；scheduled.py:240 只判 factor_verified_ms>0；302-304 固定 +30 天 |
| 1.8 扣款失败却承诺重试 | 已修 | transfers.py:56-61 与 invest.py:43-45 明说「不会自动重试、5 分钟自动释放」 |
| 1.9 冲正说成「退回/撤回」 | 已修 | transfers.py:62-65 boundary + 287-290 scope=local_reversal；scheduled 取消同口径 |
| 1.10 审计链本身 OK，但预约失败不留痕、无 expired | 部分修 | scheduled.failed 已补（scheduled.py:283-299）+ 测试；confirm.expired 仍未补；proactive trigger 仍硬编码 manual（1341/1386） |
| 2.2 订阅说「下次扣费不会再发生」 | 已修 | subs.py:109-128 scope=bank_side_only + 「商户会员本身没有取消」；三处一致 |
| 2.1/2.3 无潜在订阅推断、无商户协议/续费提醒 | 未修 | subs 表仍无协议号/提醒字段 |
| 3 账单误报无分级、时区依赖、硬编码商户表 | 部分修 | business.ANOMALY_RULES 已给出 severity/confidence/possible_false_positive 且被界面消费；bill.py 仍不输出分级，time.localtime 与 OVERSEAS_MARKERS 未动 |
| 4 理财无适当性、无期限/流动性/费用、赎回暗示即时 | 部分修 | 赎回文案已改（invest.py:41）；适当性与产品字段完全未做，CAPABILITIES 自认未做 |
| 5.2/5.9 计划不可续跑、/api/session 只读 | 未修 | state.py:76 RUNS 仍是唯一真相源，require_run 只查内存（326-330） |
| 5.3/5.8 ticker 不跑主动监测、无重试队列 | 未修 | ticker 只调 scheduled.execute_due；监测仍靠手动端点 |
| 5.10 预约授权跨期永不过期 | 未修 | scheduled.py:240 无年龄判断 |
| 5.12 审计 trigger 硬编码 manual | 未修 | app/proactive.py:1341、1386 |
| 5.13 单会话串行 DAG ≠ 长期计划 | 未改 | 仍无跨会话计划实体 |

## 十二、证据强度自评

**本机实际跑过、可复现的强证据**
- 全量 pytest：1152 passed / 4 skipped（20.1s，.venv/bin/python -m pytest -q）。4 skipped 即浏览器冒烟。
- 与上一版差异：本轮末补了两条端到端断言（确认卡九字段、歧义收款人结构化候选），故 1150 → 1152。
- 定向复跑：test_token / test_reverse / test_confirmations_persist / test_clarify_resume / test_business_api / test_scheduled_semantics 共 61 passed；test_tools / test_funds_invariants / test_token 共 58 passed。
- scripts/check_frontend.py：66/66 静态门槛通过（但它是正则扫源码，不执行 JS）。
- 浏览器环境探测：scripts/browser_smoke.py --probe-only 显示 playwright 模块不可用、chromium 缓存存在 → 真实浏览器链路只到「脚本存在」。
- 红队最近一次报告（tmp/redteam_failures.txt，23:17）：43/43 PASS；属真模型运行，报告未入库、不可稳定复跑。
- A/B 最新 summary（21:15）：A 组 0.8375 / B 组 0.8875（intent）；生成时间早于最后一次提交（22:32）。

**只有代码阅读、没有运行时证据的结论**
- 九态中 draft/need_info/manual 无可达路径：靠 grep 全仓库 + db.py CHECK 约束推断。
- 卡片状态与账户冻结不影响转账：靠 app/funds.py、app/tools/transfers.py 全文无 cards/status 读取推断（不是动态注入验证）。
- 进程内调度器无 claim/lease、进程退出不补跑：靠 ticker.py 结构推断，没有「kill 进程后到点不执行」的用例。
- 预约授权永不过期、月付 +30 天：靠 scheduled.py 分支阅读，无用例钉住「三天前的授权仍执行」。
- 前端刷新恢复逻辑：只读了 bxRestoreSession/bxPollSession 源码，没有浏览器级验证。
- 时区依赖、硬编码商户表、审计 trigger=manual：静态阅读（其中 trigger 行号已复核）。
- 「多因子自证」「跨会话不幂等」：有代码路径与旧审计互相印证，但没有攻击用例把二次扣款/伪造因子打出来。

**明确未验证/不可宣称**
- 真实短信/人脸通道、真实跨行清算/争议退汇、实时行情源、多进程并发压测：代码里就没有，不能靠文案补。
- 最新代码的知识层 A/B 增益：不能引用 21:15 的数字当作本轮结论，只能标注「针对上一版 planner」。
- 移动端可用性、确认卡「普通用户是否看得懂」：无自动化，只有截图与人工。
