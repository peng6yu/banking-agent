# 浏览器旅程核对：七条旅程、同轮证据与文档标注

> 核对对象：scripts/ui_journeys.py（当前 HEAD 工作区版本）、docs/交付/browser_journeys.json、
> scripts/ui_screenshots.py、docs/交付/截图/信息架构/browser_report.json、docs/benchmark/release_manifest.json。
> 结论口径：只认当前脚本里真实存在的定义、归档 JSON 里真实存在的断言结果、以及文件系统时间戳；
> 没有重跑 Playwright，凡是没重跑的都不写「已验证通过」。

## 0. 结论

1. 七条旅程（j1、j2、j4、j5、j6、j7、j3）全部由当前 scripts/ui_journeys.py 定义并在 run() 里真实执行：六条在一个 MOCK_MODE=script 后端上顺序跑，j3 停掉该后端、重播沙箱、以 MOCK_MODE=false 重启后单独跑。
2. 归档 docs/交付/browser_journeys.json 7/7 pass，status=pass，failures 为空，demo_db_sha256 before==after。
3. j3 确实核验了服务端状态（session_view 的 node_states + audit_events + 余额），归档里 split_transfer=pending、resolve_payee=pending、无 transfer.committed、余额未变（20:38 那轮 sid=s_abf3620d）。原先「session_id 解析失败时 nodes 退化为空字典」的边界已封：`session_id_of`（scripts/ui_journeys.py:90-95）取不到会话号直接 `raise JourneyFail`，J3 在 :570-572 再断言一次；归档那次 sid 正常。
4. j4/j5 的写操作是 API 驱动（直接调 /api/plans/birthday、/api/savings/goals 等），UI 只做读断言与刷新恢复；文档把它们标为「流程级」是准确的。本轮已把「真模型」口径收窄到 j3。
5. 原有三处文档标注与代码事实不一致（真模型范围、release_manifest 的「5 条」、截图张数）：提交物对照表与比赛演示方案本轮已改准；release_manifest 已由负责人于 20:57 按其脚本重生成，旧计数（tables 27 / events 49 / pytest 1336 / 旅程 5 条）已收口（§4/§6）。
6. 同轮绑定只能做到「时间戳在窗口内 + 内容断言一致」；journey JSON 不存截图 SHA、脚本也没有 --verify 模式，因此不能声称 SHA 级绑定。

## 1. 七条旅程的定义与运行方式

| 旅程 | 函数（scripts/ui_journeys.py） | 后端 | 驱动方式 | 服务端核验 |
| --- | --- | --- | --- | --- |
| j1 小额转账 | journey_small_transfer :261 | MOCK_MODE=script | 聊天框输入 + 点确认 | /api/business/overview 余额、session_view 节点、/api/audit |
| j2 红灯 MFA | journey_red_mfa :302 | MOCK_MODE=script | 聊天框输入 + 因子挑战 | 余额、challenges、审计 factor.challenge_issued/verified |
| j4 生日计划 | journey_birthday_plan :347 | MOCK_MODE=script | 直接调 /api/plans/birthday 建计划，页面断言 + 刷新 | /api/plans/{id} status=active、余额不变 |
| j5 储蓄提醒 | journey_savings_alert :413 | MOCK_MODE=script | 直接调 /api/savings/goals、PATCH monitor_enabled、/api/proactive/monitor/run | /api/proactive/alerts 处理后不再 open、余额不变 |
| j6 点候选 | journey_candidate_click :146 | MOCK_MODE=script | 真点击候选列表里的尾号 6620 | 服务端 pending 卡的 payee.card_tail/amount、余额不变 |
| j7 点拒绝 | journey_reject :211 | MOCK_MODE=script | 真点「拒绝」 | 审计 confirm.rejected、无 transfer.initiated、节点不 done、余额不变 |
| j3 AA 歧义 | journey_aa_ambiguous :543 | MOCK_MODE=false（真模型） | 聊天框真人话术（AA_PROMPTS 两句） | session_view node_states + audit_events + 余额，要求 split_transfer 非 done 且无 transfer.committed |

run() 的调度证据：:636 def run，:659 起的 plan 列表按 j1/j2/j4/j5/j6/j7 顺序执行，:683 的分支在 j3 被选中时 stop_backend → seed_sandbox(force=True) → start_real_backend（:515，env 里 MOCK_MODE=false 并读 .env 的 LLM_* 键）→ 再跑 journey_aa_ambiguous。

## 2. 同一轮证据（时间戳与内容）

docs/交付/browser_journeys.json：

- started_at 2026-10-24T20:38:30+08:00，finished_at 2026-10-24T20:39:21+08:00；文件 mtime 20:39:21（2026-10-24 20:38 整套复跑后刷新；20:04 那轮的归档在 20:27 复验时被 6/7 的新档覆盖，背景见 §8）。
- status=pass，failures=[]，7 条全部 pass；demo_db_sha256_before == after == eb53c908fb44bc8b9fd078c7ea65c398f0d76dbe0938597deadf49fb49f2797b，demo_db_unchanged=true（演示库没有被旅程改动；该 sha 与 20:04 轮的 2535cb… 不同，因为 20:27 的 e2e 按设计重建过演示库）。
- 每条旅程的 shot/shots 字段都指向 docs/交付/截图/信息架构/journey_*.png。

十张 journey 截图的 mtime（本机时区 +08:00）全部落在上面的窗口内：

| 文件 | mtime |
| --- | --- |
| journey_01_small_transfer.png | 20:38:35 |
| journey_02_red_mfa.png | 20:38:40 |
| journey_02_red_executed.png | 20:38:41 |
| journey_04_birthday_plan.png | 20:38:45 |
| journey_04_birthday_reloaded.png | 20:38:45 |
| journey_05_savings_alert.png | 20:38:50 |
| journey_05_alert_resolved.png | 20:38:50 |
| journey_06_candidate_click.png | 20:38:55 |
| journey_07_reject.png | 20:39:14 |
| journey_03_aa_blocked.png | 20:39:20 |

（只列到秒；原始 ls 输出里还带纳秒级小数。）

结论：journey JSON 与它点名的 10 张图来自同一次运行，时间上不可能更紧。但这是「时序 + 路径一致」的证据，不是「字节级绑定」。

## 3. 信息架构截图是另一轮（必须说清）

docs/交付/截图/信息架构/browser_report.json 由 scripts/ui_screenshots.py 产出，不是 ui_journeys.py 写的（它是另一条命令、另一个端口）。事实：

- browser_report.json 的键是 base_url/shots/checks/js_errors/payee/red_flow/daily_block/idempotent_refresh，没有 started_at/finished_at 字段，所以它的运行时间只能看 mtime：12:16:35，同批 PNG 的 mtime 在 12:16:11 到 12:16:35 之间。
- 也就是说：12 张信息架构截图 + 7 张 main_* 主线图跑在 12:16，七条旅程跑在 20:38，两者不是同一次运行（相隔约 8 小时 22 分），manifest 只是分别引用它们。
- 这不影响旅程证据的有效性，但任何「截图与旅程一次跑出」的说法都需要按上图区分：旅程图 10 张在 20:38 那轮，信息架构 12 张 + 主线 7 张在 12:16 那轮，共 29 张。

## 4. release_manifest 的引用关系

当轮（2026-10-24）的 docs/benchmark/release_manifest.json（generated_at 2026-10-24T23:34:56+08:00，git head 6124667，dirty=false）里；现行清单已于 2026-10-26T13:37:37+08:00 以 head ed6092d 重生成，journeys 块未变：

- journeys.report 指向 docs/交付/browser_journeys.json，status=pass，items 是 j1/j2/j4/j5/j6/j7/j3 全 pass，demo_db_unchanged=true —— 与归档 JSON（20:38 那轮 7/7）逐字段一致。
- 生成脚本 scripts/release_manifest.py:207-215 是读 journey JSON 现算的，不是手填；limits 里的「浏览器旅程」行已从硬编码的「5 条流程级」改为「7 条（j1/j2/j4/j5/j6/j7 + j3）」，并注明 J1/J2/J6/J7 点击级、J4/J5 API 驱动流程级，与归档一致。
- 17:35 那版清单的陈旧计数（tables 27 / events 49 / pytest 1336 / 旅程 5 条）已随本轮提交收口；现在的清单与代码事实一致（tools 35 / tables 33 / events 66 / rules 13 / paths 47 / pytest 1709 / 旅程 7）。

## 5. j3 服务端核验的具体证据与边界

归档 j3 结果（2026-10-24 20:38 那轮）：session_id=s_abf3620d，attempt=1，prompt 是「我们 AA 一下，300 元分摊给张三和李静，一人一半」，picker_cards=1，intents=["split_bill"]，node_states={resolve_payee: pending, split_transfer: pending}，audit_events 含 dag.built/handoff.created/intent.parsed/policy.decided/session.start/session.end/tool.executed 但**不含 transfer.committed**，balance_unchanged_fen=1286050。

所以 j3 不是「页面文案说拦住了就算过」：它读了 /api/session/{id} 的节点状态与 /api/audit 的事件类型，再断言 split_transfer 不是 done、没有 transfer.committed（:604-606），动钱则直接判失败（:601-603）；校验用的 sid 在 :570-572 是硬断言，拿不到就 fail，不存在「没有 sid 也通过」。

边界（已在 2026-10-24 本轮修复）：旧版 :569-572 在 session_id 解析失败时把 sid 置 None，于是 nodes/events 为空，只有页面文案也能通过。现在 `session_id_of`（scripts/ui_journeys.py:90-95）取不到会话号直接 `raise JourneyFail("[会话] 页面上没有会话号，无法核对服务端状态")`，J3 在 :568-572 再断言一次；:580 的 `if sid` 因而成为死守卫，保留无碍。归档那次 sid 正常，结论不受影响。

## 6. 文档标注与代码事实不一致（2026-10-24 20:00 复核：1/2/5 已改；3/4 不在本组写权限）

| 位置 | 现文案 | 事实 | 建议 |
| --- | --- | --- | --- |
| docs/提交物对照表.md:293 | 「scripts/ui_journeys.py（真模型 + playwright）」 | 六条走 MOCK_MODE=script，只有 j3 真模型 | **已改**（本轮）：j3 真模型，其余六条确定性桩 + 真浏览器；J1/J2/J6/J7 点击级，J4/J5 API 驱动流程级 |
| docs/交付/比赛演示方案.md:279 | 「在真模型 + 真浏览器下跑出」 | 同上 | **已改**（本轮）：改为「Playwright 真浏览器 7/7；j3 单独 MOCK_MODE=false，其余 MOCK_MODE=script」 |
| docs/benchmark/final_acceptance_report.md:24 | 「7 条旅程…用 DeepSeek 复跑 PASS」 | 六条不经过模型；DeepSeek 只影响 j3 | 缩窄到 j3，或注明桩模式 |
| scripts/release_manifest.py / release_manifest.json | 「5 条流程级」（17:35 版） | 实际 7 条，同一 JSON items 也是 7 条 | **已修**：脚本改 7 条并注明点击级/流程级；清单于 20:57 重生成（tables 33 / events 66 / pytest 1709 / 旅程 7） |
| docs/交付/比赛演示方案.md:360 | 「27 张截图（12 视图 + 7 主线 + 8 旅程）」 | 实际 29 张：12 信息架构 + 7 主线 + 10 旅程 | **已改**（本轮，29 张） |

本轮同时修正的邻近说法：docs/提交物对照表.md 原 :302「7 条里只有 J6/J7 是点击级」已过时——j1/j2 是聊天输入 + 点按钮，j3 是聊天输入 + 服务端断言，j4/j5 才是纯 API 驱动；现统一为「J1/J2/J6/J7 点击级，J3 聊天输入 + 服务端状态硬断言，J4/J5 API 驱动流程级」。

## 7. 本次改动

只改了 scripts/ui_journeys.py 的说明文字，不动任何旅程逻辑：

- 文件头 docstring 第 2 行由「五条高风险浏览器旅程」改为「七条…」，并补一段说明七条分两批、j3 单独真模型后端。
- main() 的 argparse description 由「五条高风险浏览器旅程（DOM + 服务端状态双证）」改为「七条…（j3 单独走真模型后端）」。
- 全文件已无「五条」字样（grep 为空）。

没有新增旅程、没有给低风险按钮加 E2E、没有重建 j3：已有的七条就是能跑的那七条，缺的从来不是旅程数量，而是文档口径与脚本事实对齐。

同一轮还更新了四份交付文档的数字与口径（不改代码）：docs/提交物对照表.md（1709 条测试、工具 35/规则 13/表 30/审计 61、R07 分级线语义、旅程口径）、docs/答辩PPT大纲.md（1415、R07、人工接管、沙箱说明、A/B 限制）、docs/交付/比赛演示方案.md（1415、29 张截图、33 张表、R07 演示话术、r3 数据）、本文件（20:38 归档证据与已修边界）。

## 8. 未重跑声明与重跑条件

2026-10-24 20:04–20:06 的刷新由 Lead 实跑（本文件作者没有重跑，也没有伪造结果）。20:27 的整套复验中 J2 偶发失败（6/7，报「挑战不足或验证码为空」），该轮覆盖了归档；随后 20:38 整套复跑一次（7/7 pass，demo_db_unchanged=true），本文件 §2/§5 已按 20:38 这轮更新。20:27 的失败在 J2 单跑与 20:38 整套复跑中均未复现，判定为加载下的等待竞态（脚本只等 400ms 就取验证码行），不是产品缺陷；如需根除，可把 scripts/ui_journeys.py:314-319 的等待条件改为等满 2 行。

如需重跑（例如答辩前复核）：

    cd /home/peng/peng_workspace/deepseek harness/银行agent/banking-agent
    /home/peng/.local/bin/python3.12 scripts/ui_journeys.py

前置条件：playwright + chromium 已安装；跑 j3 需要 .env 里有可用的 LLM key（否则真模型后端 40 秒内起不来会 fail，而不是悄悄降级）。重跑会重写 docs/交付/browser_journeys.json 与 10 张 journey 图；建议先备份归档，跑完对比 started_at/finished_at、7 条 status、demo_db_sha256。

## 9. 一句话给评委

七条旅程都有代码定义、都有归档的 DOM + 服务端双证；其中六条是确定性桩上的流程/点击证据（证明前后端真的连得上、状态真的落库），j3 是真模型上的歧义拦截证据（证明模型想动钱时被拦在动钱之前）。两类证据不要混着说。
