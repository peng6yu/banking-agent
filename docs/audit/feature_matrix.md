# 功能审计表（2026-10-24 复核 · 以代码取证）

> 口径：只看仓库代码、测试与可复跑输出；不采信 README、演示话术、历史报告里的自我评价。
> 「一个 API 或一个按钮」不算完整业务实现，必须能追到落库/执行/拒绝的真实分支。
> 状态只取八种：已实现且有测试证明 / 已实现但测试不充分 / 只有前端展示 / 只有后端接口 / 部分实现 / 仅有文档声明 / 尚未实现 / 受环境限制未验证。
> 证据位置写到文件:行号或测试函数名；测试结果行是本机实测（2026-10-24 10:5x）。
> 2026-10-24 这轮把 1.2/1.3/1.4/1.5/5.2/5.3/5.9/5.13 与「适当性」「订阅推断」逐条补上了实现与测试，
> 下面凡是改动过的行都以「本轮补」开头；第十一节的状态变化表同步更新，没改的行仍按 10-23 的取证口径。

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
| 超时与用户拒绝在审计里可区分 | 已实现且有测试证明 | 本轮补：app/planner/executor.py `_timeout` 改记 `confirm.expired`（payload reason=TIMEOUT），用户拒绝才是 `confirm.rejected`；事件类型在 app/audit.py EVENT_TYPES 里分开登记；tests/test_confirm_timeout.py 断言「有 expired、且没有 rejected」；恢复通道同口径 app/planner/recover.py | 无 |
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
| 日累计限额 | 已实现且有测试证明 | 本轮补：app/policy/rules.py `_emit_red_daily`（R07）改成**不给因子的硬线红灯**，文案直说「超额度的额度是监管口径，多因子不能创造额度」；红灯路径不再走 daily_cap_fen 的豁免；tests/test_policy.py 三条 R07 用例（含「不给 required_factors」）；app/tools/base.py daily_cap_fen 的豁免只对 R08 生效；界面侧被 R07 拦下时多因子面板显示「这一步不能靠验证码放行 / 直接拦截」，不下发挑战、不给提交按钮（截图 main_06_daily_limit.png） | 无 |
| 预授权 hold / commit / 超时释放 | 已实现且有测试证明 | app/funds.py:193-236 commit、396-407 _expire；holds 状态机 app/db.py:98-110；tests/test_funds_invariants.py（超时后 commit 必须失败） | 无 |
| 扣款失败不承诺重试 | 已实现但测试不充分 | app/tools/transfers.py:56-61 UNSETTLED_TEXT「系统不会自动重试…5 分钟后自动释放」；理财同口径 app/tools/invest.py:41-45 | 文案没有被任何测试断言；改回去不会变红 |
| 同会话幂等防重复 | 已实现且有测试证明 | app/funds.py:86-89 idempotency_key=sha256(session|node|params)[:16]；holds.idempotency_key UNIQUE；tests/test_funds_invariants.py:149 test_same_idempotency_key_reuses_one_hold | 无 |
| 跨会话/重启幂等 | 部分实现 | 本轮补：同一会话内的重启恢复不会重跑终态节点（app/planner/recover.py `_drive` 跳过 done/failed/skipped）+ tests/test_recover.py::test_resume_never_reruns_finished_nodes（断言只有一条 transfer.initiated）；跨会话（换 session_id 重新发同一句）仍会生成新幂等键 | 幂等键仍含 session_id：同一意图在新会话重放仍可能二次扣款。这是有意的取舍（用户重新授权就是新的一笔），但没有「同一用户 + 同一参数 + 短时间内」的去重层 |
| 卡片状态影响转账 | 已实现且有测试证明 | 本轮补：新增 app/channels.py `assert_spendable`（账户状态 → 卡状态 → 单笔限额），接进 app/funds.py 的 `_create_hold` 与 `_settle`（建 hold 与扣款两端都过闸）；tests/test_channels.py 14 条，含「卡冻结后 initiate 拒绝」「hold 建立后冻结卡 commit 拒绝、余额不动」与正常路径对照 | 账户一张卡都没有时跳过卡校验（演示库每个活账户都有卡，边界写在 channels 模块注释里） |
| 账户冻结 | 已实现且有测试证明 | 本轮补：accounts 增加 status（app/db.py `_ADDED_COLUMNS`，DEFAULT 'active'）；app/channels.py `account_status` 拒绝非 active，文案「这个账户现在是已冻结/已销户状态，不能出账。」；tests/test_channels.py::test_frozen_account_is_refused / test_funds_initiate_refuses_a_closed_account | 没有「解冻/止付」业务入口（只做了状态闸门，没做操作卡） |
| 多因子真实性 | 已实现且有测试证明 | 本轮补：app/security/factors.py 服务端签发一次性挑战（只存 sha256(secret|challenge_id|code)，TTL 300s，5 次锁定），POST /api/factor/challenge 下发、POST /api/confirm {proofs:[…]} 校验；签发/通过/拒绝分别写 factor.challenge_issued / verified / rejected；tests/test_factor_challenges.py 12 条（明文不落库、只能用一次、绑定会话+确认、错码烧次数、过期、缺因子不静默通过、红灯无证明不执行且记 confirm.expired、HTTP 端点）；前端已改成走挑战接口（web/index.html renderFactor 调 POST /api/factor/challenge、按行收集 {factor,challenge_id,code} 作为 proofs 提交），截图 main_04_red_mfa.png / main_05_red_executed.png 为浏览器实测 | 仍是桩渠道：没有短信网关、没有人脸比对，验证码走 FACTOR_DEMO_ECHO 回显，真实部署要换渠道适配器；`factor` 字段保留只为兼容旧客户端，不再被信任 |
| 冲正边界（方向/窗口/次数） | 已实现且有测试证明 | app/funds.py:261-264 只有 direction='out' 可冲正（TXN_NOT_OUT）；7 天窗口 transfers.py:47、256-258；重复冲正幂等 244-254；tests/test_reverse.py:59 test_incoming_transaction_cannot_be_reversed、:43 test_reversing_twice_does_not_pay_twice | 冲正仅在本地账本反向记账，返回 scope=local_reversal + boundary（transfers.py:62-65、287-290） |
| 真实跨行退汇/争议追回 | 尚未实现 | 边界文案 app/tools/transfers.py:62-65 明说「不是把钱从对方那里追回来」 | 没有清算/争议流程，属演示环境能力外；文案已如实，但不能算实现 |
| 越权读他人预约 | 已实现且有测试证明 | app/tools/scheduled.py list 按 account/user 过滤；tests/test_scheduled_semantics.py:test_list_scheduled_only_returns_own_rows | 本轮修掉的缺陷 |

## 四、预约转账与调度器

| 能力 | 状态 | 证据（文件:行号 / 测试） | 缺口说明 |
|---|---|---|---|
| 预约创建/列表/取消 | 已实现且有测试证明 | app/tools/scheduled.py:71-194；tests/test_scheduled.py:35/45/62 | 只有 once/monthly 两种周期，没有每周/每年 |
| 到点执行与失败留痕 | 已实现且有测试证明 | app/tools/scheduled.py:202-299 execute_due/_execute_one；失败写 scheduled.failed（283-299）；tests/test_scheduled_fires.py:208/284、tests/test_scheduled_semantics.py | 无 |
| 进程内调度器 | 部分实现 | 本轮补：ticker 接三段 —— run_due_once（预约转账）、run_monitor_once（储蓄目标监测，只对开启监测的目标）、run_plans_once（持久计划到点步骤），错误分别记 ticker.due_failed / monitor_failed / plans_failed；is_started() 供界面如实回答；tests/test_plans.py::test_ticker_runs_due_plans | 无独立 claim/lease 列，串行性靠外包 db.tx（BEGIN IMMEDIATE）；进程退出期间不执行、重启不补跑（重启后靠 recover 手动续跑），无重试队列 |
| 预约授权有效期 | 已实现且有测试证明 | app/tools/scheduled.py `_execute_one` 判 `now - factor_verified_ms <= AUTH_TTL_MS`（30 天），过期退回 yellow 顺延并写 `scheduled.auth_expired`；tests/test_scheduled_lease.py 两条时效用例 | 30 天是产品取值，不是监管口径；过期只顺延、不判失败 |
| 月付周期语义 | 部分实现 | app/tools/scheduled.py:302-304 due_ms=_next_month_ms，固定 +30 天并注释「不做日历月推算」 | 1 月 31 日的月付会漂移；跨月/闰年无日历月推算 |
| 调度器真实状态上报 | 已实现且有测试证明 | app/api/business.py:535-557 _scheduler_block（kind/interval/running + 手动端点）；tests/test_business_api.py:197 test_plans_reports_the_real_scheduler_not_a_promise | 无 |

## 五、订阅与代扣

| 能力 | 状态 | 证据（文件:行号 / 测试） | 缺口说明 |
|---|---|---|---|
| 订阅列表/查询 | 已实现且有测试证明 | app/tools/subs.py:26 查询；tests/test_tools.py:380 test_list_subs_only_active_by_default | 无 |
| 银行侧取消代扣 | 已实现且有测试证明 | app/tools/subs.py:107-128 CANCEL_SCOPE=bank_side_only + 真改 subs.status；tests/test_subscription_cancel.py 四道防线 | 无 |
| 取消边界写死三处 | 已实现且有测试证明 | 工具返回 boundary（app/tools/subs.py:109-124）、SSE 摘要（app/planner/events.py summarize）、界面（app/api/business.py:163）；tests/test_business_api.py:163 test_management_subscription_boundary_says_merchant_untouched | 无 |
| 从流水推断潜在订阅 | 已实现且有测试证明 | 本轮补：app/subscriptions.py `detect_recurring`（6 个月窗口、≥3 次、金额离散 ≤1.25、间隔落在月/年档），输出把握度、间隔、依据流水与不确定性；list_subs 与 /api/business/management 都带 inferred；tests/test_subscriptions.py（识别、拒绝一次性、把握度、证据、upcoming） | 只是启发式推断：按对手方分组，改名/合并扣款会漏；推断项没有取消按钮（它不是已登记的代扣） |
| 商户协议号/续费提醒 | 部分实现 | 本轮补：`subscriptions.upcoming` 按 next_charge_ms 列出 7 天内可能扣费，进 /api/business/plans 与「计划与提醒」页（tests/test_subscriptions.py、tests/test_business_api.py）；协议号仍没有 | 没有商户协议号；提醒只在页面/接口层展示，没有推送渠道（短信/邮件/站内信都没接） |
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
| 适当性评估（问卷/风险匹配） | 已实现且有测试证明 | 本轮补：app/suitability.py（5 题 15 分 → C1–C5，能力 1–5，investor_profiles 落库，suitability.assessed 审计），buy_fund 在起购校验之后、动钱之前调 `check`，未测评或超等级一律拒买；GET/POST /api/investment/suitability；tests/test_suitability.py 17 条 + tests/test_invest_intent_dag.py 覆盖拒买路径 | 演示级简化问卷：不是监管口径，无双录、冷静期、风险揭示书确认；产品没有期限/流动性/费率字段 |
| 期限/流动性/费率 | 尚未实现 | app/db.py:227-238 无 term/liquidity/fee 列 | 用户无法在申购前看到锁定期与费用 |
| 净值真实性 | 部分实现 | fund_products.nav_micro 为本地种子静态值（app/db.py:227-238、app/tools/invest.py:222） | 不是行情源；收益/净值曲线不随时间变化 |
| 赎回口径不暗示即时到账 | 已实现但测试不充分 | app/tools/invest.py:41 REDEEMED_TEXT「按本地账务记回账户」；:43-45 失败明说不重试 | 文案无测试断言，改回旧话术不会变红 |

## 八、储蓄目标与主动提醒

| 能力 | 状态 | 证据（文件:行号 / 测试） | 缺口说明 |
|---|---|---|---|
| 储蓄计划计算 | 已实现且有测试证明 | app/proactive.py:332-437 compute_savings_plan（periods/gap/surplus 互斥）；tests/test_proactive_calc.py | 现金流是历史均值外推，不是真实收入日历 |
| 目标保存/修改 | 已实现且有测试证明 | savings_goals（app/db.py:277-287）+ PATCH/GET；tests/test_proactive_api.py（真实 ASGI HTTP） | 无 |
| 手动监测（点一次跑一次） | 已实现且有测试证明 | app/tools/proactive.py:225-249 run_proactive_monitor；app/proactive.py:1223-1353 run_monitor；tests/test_proactive_api.py | 设计约束就是手动，见下一行 |
| 持续后台监测 | 已实现且有测试证明 | 本轮补：proactive.run_due_monitors 只挑 monitor_enabled=1 的目标，由 ticker.run_monitor_once 按 SCHEDULED_TICK_MS 推进（默认 30s，间隔由 monitor_rules 判定），proactive_alerts 落库、trigger 区分 manual/scheduler；目标上的开关走 PATCH /api/savings/goals/{id}；tests/test_proactive_api.py、tests/test_plans.py | 进程退出期间不监测；只有提醒，不动钱（这是有意设计，见 README 已知限制 8） |
| 提醒去重与处理 | 已实现且有测试证明 | _alert_exists 按 (goal,rule,reason,period_start) 去重；proactive_alerts/resolve；tests/test_proactive_api.py、tests/test_proactive_safety.py | 无 |

## 九、计划执行与恢复

| 能力 | 状态 | 证据（文件:行号 / 测试） | 缺口说明 |
|---|---|---|---|
| DAG 模板与结构校验 | 已实现且有测试证明 | app/planner/dag.py 模板 + validate；tests/test_planner.py:278/296/360 | 只有串行链，无并发分支/失败重试策略 |
| 多步执行（意图→DAG→确认→扣款→审计） | 已实现且有测试证明 | app/planner/executor.py；tests/test_e2e_transfer.py 全程驱动 | 无 |
| 取消执行并释放资金 | 已实现且有测试证明 | app/planner/executor.py:168-181 cancel；tests/test_api.py:256 test_cancel_endpoint_stops_the_run_and_frees_the_hold | 无 |
| 刷新/SSE 断开后恢复 | 已实现且有测试证明 | 本轮补：`/api/session` 每条确认带服务端重建的 `card`（`events.confirmation_frame` + `routes._card_view`）；`bxRestoreSession` 发现待确认就打回 AI 助手视图并滚到卡片；`bxRenderPendingCard` 画真卡、过期卡不画按钮 | `tests/test_session_cards.py` 3 条（字段齐全 / 老行缺理由仍可重建 / action_json 坏掉时 card=None）+ 浏览器 `main_07_idempotent_refresh.png`（连点只扣一次、F5 后卡片带 200.00 与收款人回来）。缺口：没有点击级 DOM E2E |
| 进程重启后续跑 | 已实现且有测试证明 | 本轮补：app/planner/recover.py 从 dag_runs/dag_nodes/confirmations 重建 RunState（账户从 dag_runs.meta_json 还原），POST /api/session/{session_id}/recover 与 POST /api/confirm 在内存缺失时自动走恢复；三条边界写死在代码里：不重新解析/不重新裁决、绝不重跑终态节点、遇到新确认卡就停；tests/test_recover.py 8 条（断流后交回卡、确认后真出账、二次点击幂等、过期记 confirm.expired、拒绝记 rejected、恢复不重跑、rehydrate 还原、未知会话报错） | 恢复只认「最近一次 dag_runs」；两轮并行会话不隔离（同一 session 串行执行，本身不允许并行） |
| 跨场景延迟执行（生日前 2 天订购） | 已实现且有测试证明 | 本轮补：app/plans.py + plans/plan_steps 表（跨天、跨会话、可暂停/取消、重启继续），POST/GET /api/plans、PATCH /api/plans/{id}、POST /api/plans/run；步骤类型 check/action/reminder，检查类到期重试、超过次数标 failed；tests/test_plans.py 12 条 | 计划的资金动作**永不后台执行**：到期只生成待确认提醒、计划转 paused，真正扣款要回助手重新裁决（ACTION_BOUNDARY 常量与测试都钉住这条） |

## 十、安全、审计与实验

| 能力 | 状态 | 证据（文件:行号 / 测试） | 缺口说明 |
|---|---|---|---|
| 能力令牌七步校验 | 已实现且有测试证明 | app/security/token.py:265-346；tests/test_token.py（23 项） | 无 |
| 审计哈希链 append-only | 已实现且有测试证明 | app/audit.py；app/db.py:326-331 UPDATE/DELETE 触发器；tests/test_audit.py、tests/test_api.py:304 | 无 |
| 审计事件完整性 | 已实现且有测试证明 | 本轮补：EVENT_TYPES 30 → 42（confirm.expired、policy.blocked、factor.* 三条、suitability.assessed、subscription.inferred、plan.* 四条等）；监控 trigger 区分 manual / scheduler 并落 payload；tests/test_confirm_timeout.py、tests/test_factor_challenges.py、tests/test_plans.py、tests/test_proactive_api.py | 事件类型是封闭集合，新增要同步 app/audit.py 与界面标签；历史事件的 trigger 无法追溯 |
| 提示注入防御 | 已实现且有测试证明 | app/security/injection 词表+信封+R04；tests/test_injection*.py、tests/test_security_knowledge.py | L2 模型分类器未在生产启用 |
| 熔断 | 已实现且有测试证明 | app/security/breaker.py；tests/test_breaker.py、test_breaker_production.py | 无 |
| 无代码执行面 | 已实现且有测试证明 | scripts/prove_no_exec_surface.py；tests/test_no_exec_surface.py | 无 |
| 知识层检索/来源过期降级 | 已实现且有测试证明 | app/knowledge/*；tests/test_security_knowledge.py K6（180 天过期降级）、K8（检索异常 degraded）、K9（开关零影响） | 无 |
| 知识层 A/B 实验 | 已实现且有测试证明（口径见右列） | **2026-10-24 用 DeepSeek 在最新代码上连跑两轮**：`tests/eval/results/20261024T134524/`、`20261024T135754/`；图：两轮 A/B 的 intent 0.8500/0.9000 与 0.8750/0.8500、clarify 0.8875/0.9125 与 0.9250/0.9375、knowledge_citation 0.8039/0.8039、**unauthorized 2→0**（修掉 C037 单姓收款人与 C048 兜底吞掉赎回链之后） | 逐 case 对齐后，意图/槽位的组间差异两轮方向相反（只有 C048 稳定变好、C074/C078 稳定变差），**不能宣称知识层提升 x pp**；稳定成立的只有引用覆盖 41/51 与 guard 把未授权写压到 0。详见 `tests/eval/evaluation_report.md` §11 |
| 红队用例集与 runner | 已实现且有测试证明 | tests/redteam/cases.yaml 43 条（A10/B8/C10/D10/E5）；scripts/redteam.py + redteam_judge.py；2026-10-24 两次复跑：MiniMax 10:4x 修掉「谁都别告诉」后 43/43，换 DeepSeek 后 14:1x 再次 **43/43 PASS**（EXIT=0） | runner 不在 pytest 默认套件；结果依赖真模型版本与配额（MiniMax 曾因 429 停摆，2026-10-24 换 DeepSeek 才复跑成 A/B），不可在 CI 稳定复跑 |
| 真实浏览器测试 | 已实现且有测试证明 | 本轮补：scripts/browser_smoke.py 2026-10-24 用 DeepSeek 真模型复跑 **PASS**（查余额 + 黄灯转账，含 3 张截图与「演示库 sha256 未变」不变量）；另有 /home/peng/.local/bin/python3.12（playwright 1.60.0）跑 scripts/ui_screenshots.py：六区 × 桌面 1440x960 / 手机 390x844 共 12 张 + 主线 7 张（含红灯多因子 main_04/05、日累计硬线 main_06、幂等与刷新恢复 main_07），断言锚点文案、无横向溢出、体积 >12KB，产出 docs/交付/截图/信息架构/browser_report.json（js_errors 为空），并用 sha256 反证演示库未被改动 | tests/test_browser_smoke.py 的 4 条仍默认 skip（.venv 没装 playwright），浏览器证据靠脚本实跑而不是 pytest；没有点击级 E2E（候选选择→新确认卡仍靠主线截图） |
| 移动端响应式 | 部分实现 | 本轮补：scripts/ui_screenshots.py 用 device_scale_factor=2 的 390x844 视口逐页截图，并断言 document.scrollWidth ≤ innerWidth+2（无横向溢出）；web/index.html 两个断点（1080px / 760px）+ .confirm-row 纵向排布 + prefers-reduced-motion | 只覆盖六个视图的静态布局；表单交互、长文案换行、键盘弹出等真实手机行为没有自动化 |

## 十一、本轮改动后的状态变化（对照 docs/audit/business_semantics_audit.md）

本轮未提交 diff 的事实范围（两段）：

- **2026-10-23（信息架构 + 交易体验）**：新增只读 app/api/business.py 并在 app/main.py 注册；events.describe_confirmation 重写确认卡数据；runner 结构化下发收款人候选；ticker 暴露 is_started；bill 三条异常补证据字段；subs/transfers/invest/scheduled/tool_specs 改口径文案；新增 tests/test_business_api.py、test_scheduled_semantics.py 等。
- **2026-10-24（补真实缺陷）**：新增 app/security/factors.py、app/channels.py、app/planner/recover.py、app/suitability.py、app/subscriptions.py、app/plans.py；改 app/policy/rules.py（R07 硬线）、app/funds.py（两端过闸）、app/planner/{state,executor}.py（meta_json、waiting 状态、ttl_ms、confirm.expired）、app/api/{routes,business}.py（因子挑战、恢复、适当性、计划、订阅推断）、app/planner/ticker.py（三段）；新增 tests/test_factor_challenges.py、test_channels.py、test_recover.py、test_suitability.py、test_subscriptions.py、test_plans.py。

| 旧审计缺陷 | 现在的状态 | 说明 |
|---|---|---|
| 1.2 账户无状态、卡片状态与资金路径解耦 | 已修 | 2026-10-24：accounts 增 status；新增 app/channels.py 闸门接进 app/funds.py 的 _create_hold 与 _settle（两端都查账户状态 / 卡状态 / 单笔限额）；tests/test_channels.py 14 条，含「hold 建立后冻结卡，commit 拒绝且余额不动」 |
| 1.3 单笔=日累计、红灯绕过日线 | 已修 | 2026-10-24：R07 改为不给因子的硬线红灯（app/policy/rules.py _emit_red_daily），红灯不再绕过日累计；单笔限额与日累计拆成两条规则；tests/test_policy.py 覆盖 |
| 1.4 多因子是自证 | 已修（渠道仍是桩） | 2026-10-24：app/security/factors.py 服务端签发/校验一次性挑战（sha256 落库、TTL 300s、5 次锁定），POST /api/factor/challenge 下发，/api/confirm 收 proofs；tests/test_factor_challenges.py 12 条。剩余：没有真短信/人脸渠道，验证码走 FACTOR_DEMO_ECHO 回显 |
| 1.5 TTL/超时正确，但超时记成用户拒绝 | 已修 | 2026-10-24：新增 confirm.expired 事件，_timeout 记 expired、用户拒绝才记 rejected；tests/test_confirm_timeout.py 断言「有 expired 且无 rejected」 |
| 1.6 幂等键含 session_id，跨会话不幂等 | 部分修 | 同一会话内重启/断流不再重跑终态节点（app/planner/recover.py + tests/test_recover.py）；换 session_id 重放仍是新的一笔，属有意取舍，但缺「同用户+同参数+短时窗」去重层 |
| 1.7 预约无原子 claim、授权不过期、月付 +30 天 | 已修 | 月付改成日历月 + 月末钳制（`_next_month_ms`，1/31→2/28、闰年 2/29、3/31→4/30，tests/test_scheduled_calendar.py 6 条）；`execute_due` 每行先抢 `UPDATE … SET claim_token, claimed_ms WHERE status='scheduled' AND (claim_token='' OR claimed_ms <= now-lease)`，抢不到记 `CLAIMED_BY_ANOTHER_WORKER` 直接放行，执行完在 finally 里只放自己那把（CLAIM_LEASE_MS=10 分钟）；授权加保质期 AUTH_TTL_MS=30 天，过期退回 yellow 顺延并写 `scheduled.auth_expired`。tests/test_scheduled_lease.py 7 条钉住：被持有→skipped 且余额不动、租约过期可接管、失败也放租约、31 天前授权→顺延。剩余：进程全停就没有节拍（ticker 与 uvicorn 同生共死） |
| 1.8 扣款失败却承诺重试 | 已修 | transfers.py:56-61 与 invest.py:43-45 明说「不会自动重试、5 分钟自动释放」 |
| 1.9 冲正说成「退回/撤回」 | 已修 | transfers.py:62-65 boundary + 287-290 scope=local_reversal；scheduled 取消同口径 |
| 1.10 审计链本身 OK，但预约失败不留痕、无 expired | 已修 | scheduled.failed 已补（scheduled.py:283-299）+ 测试；confirm.expired 已补（executor._timeout，tests/test_confirm_timeout.py 断言「有 expired 且无 rejected」）；`policy.blocked` 也已从 confirm.rejected 里拆出来（executor._blocked / runner 的收款人前闸门）；proactive trigger 区分 manual / scheduler（app/proactive.py） |
| 2.2 订阅说「下次扣费不会再发生」 | 已修 | subs.py:109-128 scope=bank_side_only + 「商户会员本身没有取消」；三处一致 |
| 2.1/2.3 无潜在订阅推断、无商户协议/续费提醒 | 部分修 | 2026-10-24：新增 app/subscriptions.py detect_recurring（≥3 次、金额离散 ≤1.25、间隔落在月/年档）+ upcoming（7 天内可能扣费），进 list_subs 与 /api/business/management、/api/business/plans；tests/test_subscriptions.py。仍缺商户协议号与推送渠道 |
| 3 账单误报无分级、时区依赖、硬编码商户表 | 部分修 | business.ANOMALY_RULES 已给出 severity/confidence/possible_false_positive 且被界面消费；bill.py 仍不输出分级，time.localtime 与 OVERSEAS_MARKERS 未动 |
| 4 理财无适当性、无期限/流动性/费用、赎回暗示即时 | 部分修 | 赎回文案已改（invest.py:41）；适当性与产品字段完全未做，CAPABILITIES 自认未做 |
| 5.2/5.9 计划不可续跑、/api/session 只读 | 已修 | app/planner/recover.py 从库里重建 RunState（账户从 dag_runs.meta_json 还原），POST /api/session/{id}/recover，/api/confirm 内存缺失时自动走恢复（tests/test_recover.py 9 条，含「优先挑未完成的那一轮」）；`/api/session` 现在用 `planner.events.confirmation_frame` 把还挂着的确认卡整张重建出来（金额/收款人/转出账户/权限等级/验证步骤），前端刷新后画的是同一张卡而不是一句「确认号 cf_xxx」（tests/test_session_cards.py 3 条） |
| 5.3/5.8 ticker 不跑主动监测、无重试队列 | 已修（监测）/未修（重试队列） | ticker 三段：预约转账、开启监测的储蓄目标、持久计划到点步骤（run_due_once / run_monitor_once / run_plans_once）；重试只在计划步骤层（max_attempts，默认 3），预约转账仍无重试队列 |
| 5.10 预约授权跨期永不过期 | 已修 | `_execute_one` 判 `now - factor_verified_ms <= AUTH_TTL_MS`（30 天），过期不再按已授权直接扣款：退回 yellow → 当日额度生效 → 顺延（`defer_count+1`），并写 `scheduled.auth_expired`；tests/test_scheduled_lease.py |
| 5.12 审计 trigger 硬编码 manual | 已修 | trigger 区分 manual / scheduler 并落 payload（app/proactive.py），测试覆盖；历史事件无法追溯 |
| 5.13 单会话串行 DAG ≠ 长期计划 | 已修（有边界） | 2026-10-24：app/plans.py + plans/plan_steps（跨天、跨会话、可暂停取消、重启继续，POST /api/plans 等）；tests/test_plans.py 12 条。边界：计划的资金动作永不后台执行，到期只生成待确认提醒并把计划转 paused |

## 十一·二、第四轮（任务书 8 项整改）状态变化，2026-10-24

| 能力 | 之前 → 现在 | 证据 |
|---|---|---|
| AA 拆分收款人消歧 | 只有后端接口（默认取第一候选）→ **已实现且有测试证明** | `tests/test_split_safety.py` 14 条 + `tests/test_split.py` 8 条：唯一候选才解析、多候选必须消歧、任一人有歧义整笔不进资金写、工具层再校验 |
| 生日跨场景长期计划 | 尚未实现 → **已实现且有测试证明（有边界）** | `app/birthday.py` + `plan_budgets`（账本预留，不是 5 分钟 hold）；`tests/test_birthday_plan.py` 11 条；边界：到点只提醒，付款必须重新确认 |
| 调度器一致性 | 部分实现（只有一台节拍器）→ **已实现且有测试证明（单进程）** | `tests/test_ticker_consistency.py` 7 条：三类任务、异常隔离、重复 start 幂等、stop 冻结；多进程未验证，模块说明里写明 |
| 同一会话多轮未完成资金写 | 尚未实现（后一轮静默覆盖 RUNS）→ **已实现且有测试证明** | `app/planner/conflict.py` + `run.conflict` 事件；`tests/test_run_conflict.py` 8 条（冲突带 run_id、只读不拦、重启仍检测、取消后解锁） |
| 真实短信/人脸渠道 | 仅有文档声明 → **部分实现**：没配 `FACTOR_PROVIDER` 且关掉回显时 `SystemBusy` 失败关闭（不落挑战、不写审计） | `tests/test_real_channels.py` 6 条（含 HTTP 503 与 0 挑战断言）；渠道本身仍是桩 |
| 理财产品期限/流动性/费率 | 仅有文档声明 → **已实现（诚实标注）** | 三项显式 `null` + `fee_text` + `data_source: contest_simulation` + `comparison.supported: false`；`tests/test_real_channels.py` |
| 浏览器高风险旅程 | 部分实现（只有六区截图）→ **已实现且有测试证明** | `scripts/ui_journeys.py` 5/5（小额确认 / 高额双因子 / AA 同名被阻止 / 生日计划建+刷新恢复 / 储蓄提醒处理），`docs/交付/browser_journeys.json` 的 `demo_db_unchanged: true` |
| 版本数字口径 | 多份文档各写一套 → **已实现** | `scripts/release_manifest.py` → `docs/benchmark/release_manifest.json`；README / 验收报告 / 提交物对照表 / 比赛演示方案 / PPT 全部引用 |
| A/B 与最终代码一致 | 受环境限制未复跑 → **已实现（三轮）** | r1 `20261024T134524` / r2 `20261024T135754` / r3 `20261024T160805`；仍单次采样，不宣称显著（final_acceptance_report §6） |

**这一轮新增的明确未验证**：多进程并发下 ticker 的重复执行（租约只做了单机测试）、真实短信/人脸链路、点击级 DOM 断言（旅程是流程级）、生日计划长期运行一个真实生日周期。

## 十二、证据强度自评

> 本节及全表涉及的计数（工具 / 表 / 审计事件 / API 路径 / 测试通过数 / A/B 轮次）以
> `docs/benchmark/release_manifest.json` 为准；该文件由 `scripts/release_manifest.py` 从代码与运行日志生成。

**本机实际跑过、可复现的强证据**
- 全量 pytest：**1330 passed / 4 skipped / 1 warning（46.61s，.venv/bin/python -m pytest -q，2026-10-24 终测）**。4 skipped 仍是浏览器冒烟（.venv 无 playwright）。
- 与上一版差异：1131（10-23 基线）→ 1152（信息架构轮）→ 1258（安全补强）→ 1264（策略拦截事件 + 月末钳制）→ 1275（租约/授权保质期/恢复多轮/刷新重建，+11）→ 1279（单姓收款人 + 兜底赎回链，+4）→ **1330（本轮任务书整改：AA 拆分、生日计划、ticker 一致性、真实渠道闸门等，+51）**。中间新增批次：test_factor_challenges 12、test_suitability 17、test_channels 14、test_recover 8、test_plans 12、test_session_cards 3、test_scheduled_calendar 6、test_scheduled_lease 7、test_business_api 14。
- 本轮新增的运行时证据：多因子服务端挑战（签发/通过/拒绝三条审计 + 无证明不执行）、卡与账户闸门（含 hold 后冻结卡）、R07 硬线、confirm.expired 与 rejected 可区分、断流恢复后真出账、适当性拒买、订阅推断、持久计划跨重启、34 条原话防护、浏览器六区 + 主线 7 张（含刷新后重建的卡片）。
- 预约转账的并发与时效证据：租约（`claim_token`/`claimed_ms`）让第二个 worker 抢不到就跳过，`scheduled.auth_expired` 让 31 天前的人脸验证不再被当成永久通行证——两条都在 tests/test_scheduled_lease.py 里用「余额不动 / 只少一次」反证。
- 刷新恢复证据：`/api/session` 重建整张卡（tests/test_session_cards.py 3 条）+ 浏览器 main_07（连点两次只扣一次、F5 后卡片带着 200.00 与收款人回来）。
- 定向复跑：test_token / test_reverse / test_confirmations_persist / test_clarify_resume / test_business_api / test_scheduled_semantics 共 61 passed；test_tools / test_funds_invariants / test_token 共 58 passed。
- scripts/check_frontend.py：66/66 静态门槛通过（但它是正则扫源码，不执行 JS）。
- 浏览器环境探测：scripts/browser_smoke.py --probe-only 显示 playwright 模块不可用、chromium 缓存存在 → 真实浏览器链路只到「脚本存在」。
- 红队最近一次报告（tmp/redteam_failures.txt，23:17）：43/43 PASS；属真模型运行，报告未入库、不可稳定复跑。
- A/B 最新两轮（2026-10-24 13:45 / 13:57，DeepSeek）：见 tests/eval/evaluation_report.md §11；两轮原始 jsonl 入库，可重新聚合。
- A/B 历史轮（2026-10-23 21:15，MiniMax）：A 0.8375 / B 0.8875（intent），保留在原报告 §4，不再作为最新结论。

**只有代码阅读、没有运行时证据的结论**
- 九态中 draft/need_info/manual 无可达路径：靠 grep 全仓库 + db.py CHECK 约束推断。
- （已作废）卡片状态与账户冻结不影响转账 —— 2026-10-24 已由 tests/test_channels.py 动态验证修复。
- 进程内调度器无 claim/lease、进程退出不补跑：靠 ticker.py 结构推断，没有「kill 进程后到点不执行」的用例。
- ~~预约授权永不过期、月付 +30 天~~：2026-10-24 已补用例（tests/test_scheduled_lease.py 的 31 天前授权→顺延、tests/test_scheduled_calendar.py 的月末钳制），不再是「只靠读代码」。
- ~~前端刷新恢复逻辑：只读了源码~~：2026-10-24 已有浏览器级验证（main_07 + browser_report.json 的 idempotent_refresh 键）与后端 tests/test_session_cards.py；仍未做点击级 DOM 断言。
- 时区依赖、硬编码商户表、审计 trigger=manual：静态阅读（其中 trigger 行号已复核）。
- 「多因子自证」已由 tests/test_factor_challenges.py 反证修复；「跨会话不幂等」仍只有代码路径，没有攻击用例打出二次扣款。

**明确未验证/不可宣称**
- 真实短信/人脸通道、真实跨行清算/争议退汇、实时行情源、多进程并发压测：代码里就没有，不能靠文案补。
- 「知识层让意图/槽位更准」这句话：两轮方向相反（§11.4），只能引用「引用覆盖稳定 41/51」与「guard 让未授权写 2→0」，不能引用单轮 pp。
- 移动端可用性、确认卡「普通用户是否看得懂」：无自动化，只有截图与人工。
