# 进度流水

> 接手的人（或模型）**第一件事就是读最后一段**，看清从哪继续。
> 格式固定：里程碑 / 日期 / 已完成 / 测试数据 / 遗留。
> 本文按时间顺序记录，**早期各段里的测试条数、规则条数、红队通过数都是当时那一刻的快照**（例如「R01—R11」「PASS 27 / FAIL 16」），不代表最终规模。终值与当前口径一律以文末 **m-final 终验** 一节为准。

## m0 骨架  2026-10-18

- [x] 仓库结构、docs/ 00—10 交接规格书
- [x] `.env` 抽取脚本（setup_env.sh）、`.env.example`、`.gitignore`
- [x] `app/config.py`（Settings / load_env / endpoint）
- [x] `app/llm.py`（LLMAdapter / MockAdapter / 重试退避 / 思维链剥离 / 流式）
- [x] `app/smoke.py` 4 项自检
- 测试：smoke 4/4 PASS（工具调用 / think 剥离 / 流式收尾 / mock 兜底）
- 模型实测：主脑 MiniMax-M3.1-Flash-Preview 1.56s 稳定 5/5；M2.1 偶发拒答需重试
- 遗留：`app/main.py`、`web/index.html` 尚未创建，`make run` 还不能起

## m1—m7 各里程碑交付（2026-10-19 按上游 commit 回填）

> 以下 7 段是 t8 集成联调时**按各成员的提交记录回填**的（原始 commit 与测试数字来自各成员交付说明），
> 不代表本轮重新实现。每一段都给出了 commit，方便追溯。

## m1 数据层  2026-10-18

- [x] `app/clock.py`（异常层级 + 唯一时间入口）、`ids.py`、`money.py`（中文口语金额）、`log.py`
- [x] `app/models.py`（全部 extra=forbid）、`db.py`（连接/PRAGMA/15 表）、`audit.py`（哈希链）
- [x] `scripts/seed.py` 幂等种子（随机种子 20261018；张伟 储蓄卡 12,860.50 元 / 信用卡额度 2 万 / 4 收款人含两条同名张三 / 320 条流水 / 5 条订阅 / 3 个植入异常）
- 测试：57 用例全绿
- commit：`a443112 feat(db): 数据层七模块与幂等种子数据`

## m2 工具层  2026-10-18

- [x] 10 个银行工具 + 注册表：`base.py` `registry.py` `queries.py` `bill.py` `cards.py` `subs.py` `transfers.py`
- [x] `bill.py` 是为守住 400 行上限从 `queries.py` 拆出（docs/02 登记表已补）
- 测试：`tests/test_tools.py` 35 例；全量 215 passed；ruff（F,E,I,B,UP,C4）全清；单文件最大 380 行
- commit：`591124c feat(tools): 十个银行工具与工具注册表`

## m3 安全层  2026-10-18

- [x] `app/policy/rules.py` + `engine.py`：R01—R11 有序短路求值 + 三行 explain
- [x] `app/security/{token,envelope,injection,breaker}.py`：七步校验 / 信封 500 字截断 / 注入三层 / 熔断 5 分钟 3 次开 60 秒半开
- [x] `app/funds.py`：hold/commit/release/expired 四路径 + 幂等键
- 测试：6 文件 120 例；全量 177 passed
- commit：`c397353 feat(policy): 三色裁决、能力令牌与两阶段资金`

## m4+m5 规划执行与 API  2026-10-18

- [x] `app/planner/{intent,dag,state,events,runner,executor}.py`：意图解析 / 五条 DAG 模板 / 确认与因子状态机
- [x] `app/api/routes.py` 十个端点 + `app/main.py` 四类异常映射；SSE 八事件
- 测试：test_planner 38 + test_api 14 + test_e2e_transfer 5；全量 272 passed；真机 `curl -N` 实测过流
- commit：`ad248d5`

## m6 前端  2026-10-18

- [x] `web/index.html`：1148 行零依赖单文件，双栏决策剧场，7 个面板 id，手工切包 SSE
- [x] `scripts/check_frontend.py` 62 项静态校验；另用 Node 假 DOM 跑通 17 项运行时冒烟
- commit：`7373b12`

## m7 红队用例集与 runner  2026-10-18

- [x] `tests/redteam/cases.yaml` 43 条（A 10 / B 8 / C 10 / D 10 / E 5）+ 判定口径 README
- [x] `scripts/redteam.py` + model/setup/judge 三份拆分，全离线，约 20 秒跑完
- 首次交付时 PASS 16 / FAIL 27，并如实上报 9 个真实缺陷；t15/t16 已修掉注入回调崩溃、transfer 归属校验、注入审计、多语种关键词、用户原话扫描
- commit：`bd9de35`（用例集）、`c94a8cd`（runner）

## m8 集成联调与验收  2026-10-19

- [x] `scripts/e2e.py`：13 步端到端演示（余额 → 月账单 → 年度账单异常 → 订阅查询 → 订阅取消 → 转 500 黄灯 → 转 2000 红灯双因子 → 提示注入 → 场景 6 生日四步 DAG → 收尾问句 → 审计链与篡改 → 前端 7 面板 → 恶意模型），每步打印「预期 vs 实际」
- [x] `--offline` 断网兜底（真实 uvicorn + 固定脚本模型），与在线模式结果一致：13/13 PASS
- [x] `tests/test_acceptance.py`：赛题四项硬指标自动化断言（任务完成率 ≥ 3/6、四类攻击各有被拦、交互体验四项、MOCK_MODE 与密钥 fail closed）
- [x] `tests/test_imports.py` 依赖方向铁律固化：40 个 app 模块全部登记，85 项断言
- [x] `docs/安全自评报告.md`：11 条规则逐条对照表 + 已知风险清单 + 红队实测拦截率（真实数字）
- [x] `docs/02` 登记补齐：改名/新增文件逐个登记 + 已知遗留 L-01—L-06
- 红队实测（10:36，commit 5183684 + t15/t16 工作树）：43 条 PASS 27 / FAIL 16；攻击类 38 条 **拦住 38/38（100%）**、级别达标 25/38、审计齐 36/38；16 条未达标全部是「级别判低」，无一条资金被动
- 遗留：见 `docs/02` 已知遗留 L-01—L-06 与 `docs/安全自评报告.md` §2

## 下一步

已无待办。上面三条终验全部跑过，结果见下一节。
## m-final 终验  2026-10-21

- [x] 终验三条命令全部跑过：`pytest -q` = **522 passed**（9.9s，2026-10-22 复跑）；`scripts/e2e` 与 `--offline` 13/13；`scripts.redteam` = **PASS 29 / FAIL 14**（约 12 秒，全离线 ScriptedAdapter）
- [x] 理财场景落地：`fund_products` / `fund_holdings` 两张表、`buy_fund` / `redeem_fund` / `list_products` / `estimate_risk` 四个工具、invest 意图 + DAG 三分支（R10 红灯 + 双因子）；工具 10→14、表 15→17、意图模板 5→6、槽位 7→9
- [x] 文档一致性终审：105 个文件的 `path:NN` 引用脚本核对 0 条越界；工具/表/规则/用例/测试五类数字全仓统一；`docs/架构图.svg` 与 `.mmd` 同步
- [x] 修掉两个真缺陷：① `emit_intent_schema` 漏声明 merchant / card_tail（`additionalProperties:false` → 真实模型永远填不出来，t15 的商户名扫描是死代码，`tests/test_planner.py` 新增断言钉住 schema 与 SLOT_KEYS 一致）；② `list_products` / `estimate_risk` 已实现却不在 `KNOWN_TOOLS`，R03 会把只读查询误判成红灯
- 红队终值：43 条 **PASS 29 / FAIL 14**；分组 A 10/10、B 4/8、C 6/10、D 7/10、E 2/5；攻击类 38 条拦住 38/38、级别达标 27/38、审计齐 36/38。14 条 FAIL 全部是「级别判低」（12 条停 `level=none`、2 条判 green），**must_not 探针零命中，无一条攻击得手**
- 遗留：`docs/安全自评报告.md` §2.1 R-05/R-07/R-08/R-09/R-12 与 `docs/技术文档.md` §7 的 R14（反问轮不做注入扫描，登记为已知限制）


## m-final+ 场景4 补齐  2026-10-22

- [x] 场景 4 卡片管理从「仅查询」补到完整：`apply_virtual_card` / `set_card_limit` / `freeze_card` / `unfreeze_card` / `report_card_loss` 五件套在册（工具 14→19，m-final+ 再扩到 26），申请走黄灯 `R05`、其余四个走红灯 `R09`（face+sms+otp）；`PENDING_TOOLS` 收窄（随后清空，销户与改密码也已实现）；`R11` 的可达性改由 monkeypatch 单测证明
- [x] 新增 `card_manage` 意图与 `app/planner/card_actions.py`：动作只从用户原话的动词读，**模型填的 `card_action` 一律覆盖**；要动某张卡却没说卡号时反问，不按主账户猜
- [x] 修掉两个真缺陷：① `list_cards` 不传尾号只列主账户（名下两张卡只报一张）；② 卡片写工具的 SSE 回执念内部工具名（`freeze_card 执行完成`）
- [x] 真模型端到端实测（`tmp/card_probe2.log`）：「我有几张卡」→ 名下卡片 2 张；「申请一张虚拟卡」→ 黄灯确认 → 尾号 9000、限额 5,000.00 元；「把我尾号1234的卡冻结了」→ 红灯三重验证 → 冻结落库；审计 `confirm.approved 2 / token.issued 2`
- [x] 终验复跑：`pytest -q` = **662 passed**；`scripts.redteam` = PASS 29 / FAIL 14（不变）；`scripts.e2e --offline` 13/13；`check_frontend` 62/62；`seed --force` 17 表全绿
- [x] 全仓数字同步：548/522 → **656**；工具 14 → **19**；场景表扩到六个场景并逐项标完成度；答辩 PPT 重出（第 10/11/12 页更正工具数与挂失规则、删掉已修复的「订阅取消无意图模板」）
- 遗留不变：红队 14 条仍为「拦得住但级别判低/裁决痕迹不全」，非攻击得手；场景 1 仍缺定时与 AA 拆分；演示视频（可选）未录制

**发布点：`bc11bd3`（main），tag `v1.3.0`。**

## v1.4.0 五项缺口全部补齐  2026-10-22

- [x] **按手机号转账**：`resolve_payee` 支持 `phone` 参数与后四位匹配，候选带 `phone` 字段；查不到人返回空列表（`ok=True`），由上层反问，不猜
- [x] **定时转账**：`app/planner/due.py:parse_due_ms` 认「今天/明天/后天/大后天/月底/下个月/N 天后/下周X/本周X」并折算到当天 09:00，认不出抛 `DUE_UNPARSED`；`schedule_transfer` / `list_scheduled` / `cancel_scheduled` 三工具在册，排队只排期不动钱，到点由 `execute_due` 重新签令牌再执行
- [x] **拆分 AA**：`app/planner/split_plan.py` 自造 DAG（一人一个 `resolve_payee` 节点 + 末尾 `split_transfer`），`split_shares` 均分到分、余数给第一人；限额按总额算日累计
- [x] **销户与改卡密码**：`close_card`（余额须为零、无未结 hold，lost 卡只能柜面）与 `change_card_pin`（只存 `salt$sha256`，返回不回显）
- [x] **回退（冲正）**：`reverse_transfer`，7 天窗口、金额取自原交易而非模型报数、幂等、非出账与冲正再冲正均拒
- 测试：`pytest -q tests/` = **662 passed**（9.8s，37 条新增分在 5 个新文件）；`scripts.redteam` = PASS 29 / FAIL 14（未退化）；`scripts.e2e --offline` 13/13；`check_frontend` 62/62；`seed --force` 18 张表全绿
- 新测试逼出两个真 bug 并修掉：① `close_card` 读 `cards.balance_fen`（该列在 `accounts`）→ 销户必然 `IndexError`，改为 JOIN `accounts`；② `execute_due` 不透传 `secret` → 整批预约 `CAPABILITY_SECRET_MISSING` 全部置 failed
- 顺带修正：`app/funds.py` 写流水 `category="转账"`（原为英文 `transfer`，导致按分类查询与账单分析漏掉 App 发起的转账）
- 工具 19→26、意图模板 8→11、测试 597→656、表 17→18；`PENDING_TOOLS` 已清空，R11 可达性改由 `tests/test_policy.py::test_r11_default_deny_is_the_backstop` 的 monkeypatch 证明
- 文档口径同步：`docs/05` 工具集合表按代码重写、`docs/06` 补 §6.11（定时/AA/冲正）与 26 行工具表、`docs/07` §7.3 补四行模板、`docs/安全自评报告` R09 敏感集合补全 11 个、`docs/技术文档` §6/§7 与 `提交物对照表` 六场景全部 ✅、`答辩PPT.pptx` 第 10—12 页重生成
- 遗留：定时转账依赖 `execute_due` 手动/定时触发，无常驻调度器；AA 遇同名收款人取第一候选（已在模块 docstring 注明）

## v1.4.1 定时转账补上生产触发点  2026-10-22

- [x] **修掉一类「实现了但生产走不到」的缺口**：`execute_due` 此前只有单测在调，全仓零生产调用点，用户预约成功之后那笔钱永远不会动。新增 `app/planner/ticker.py`：应用启动时起 asyncio 任务，每 `SCHEDULED_TICK_MS`（默认 30000，设 0 关闭）调一次 `execute_due`；另加 `POST /api/scheduled/run` 供现场立刻触发。
- [x] `app/main.py` 改用 lifespan 起停节拍器；`app/api/routes.py` 新增端点。
- [x] 新增 `tests/test_scheduled_fires.py`：断言的是**真实入口打进来之后余额确实变了**（1286050→1236050）、二次触发不重复扣钱、未到点的行一个字不动、只有 0 表示关闭后台节拍。
- [x] `tests/test_imports.py` 登记 `app.planner.ticker`（模块行数表、允许依赖表、`app.main` 依赖）。
- 同步：docs/02 目录表、docs/06 定时六步第 5 行、docs/07 端点表、docs/09 环境变量表、docs/技术文档与提交物对照表里「无常驻调度器」的旧说法。
- 复验：pytest **662 passed**、redteam PASS 29 / FAIL 14（未退化）、e2e 13/13、check_frontend 62/62、seed 18 张表全绿、PPT 已重生成（13 页）。
- 遗留：没有跨进程分布式锁，多实例部署需外部串行化；`execute_due` 失败的行由它自己置 failed 并写审计。
## v1.5.0 模型配置去厂商化 + 换厂商配置文档  2026-10-23

- [x] **先说事实：仓库公开但不含任何密钥。** 审计确认 `.env` 未入库（`.gitignore` 第 2 行）、跟踪文件里查不到任何 key 形态字符串（`git grep -nE 'sk-[A-Za-z0-9_-]{8,}'` 无命中）；key 缺失时按设计 fail closed，不降级放行。所以问题不在「漏传 key」，而在「没写清楚怎么配」。
- [x] **配置变量去厂商化**（`app/config.py`）：通用名 `LLM_API_KEY` / `LLM_BASE_URL` / `LLM_ENDPOINT` / `LLM_MODEL` / `LLM_MODEL_FAST` / `LLM_MODEL_BACKUP`；旧的 `MINIMAX_API_KEY` / `MINIMAX_API_HOST` / `MODEL_BRAIN` / `MODEL_FAST` / `MODEL_BACKUP` **仍然生效**，新名优先，老 `.env` 不用重写。
- [x] **endpoint 拼接规则**（`build_endpoint`）：`LLM_ENDPOINT` 优先 > base 已含 `/chat/completions` > base 以 `/v1`、`/v4` 结尾只补 `/chat/completions` > 其余补 `/v1/chat/completions`。覆盖 OpenAI / DeepSeek / 智谱 / DashScope / Moonshot / Ollama / vLLM。
- [x] `Settings` 字段改用 `default_factory`：每次实例化读环境变量，改 `.env` 或 monkeypatch 立即生效，不用重启进程；新增 `Settings.provider` 粗判厂商（只用于自检输出与文档提示，不参与任何路由决策）；保留 `settings.host` 旧字段别名。
- [x] `setup_env.sh` 重写：从 `~/.dsh/.env` 按厂商常见变量名自动抽取（`LLM_API_KEY`/`MINIMAX_CN_API_KEY`/`DEEPSEEK_API_KEY`/`OPENAI_API_KEY`/`DASHSCOPE_API_KEY`/`MOONSHOT_API_KEY`/`ZHIPU_API_KEY`），按 key 名套用对应厂商的 base url 与默认模型；`--prompt` 交互粘贴任意厂商的 key 并顺带问 base url 与模型名；生成的 `.env` 改用通用键名。
- [x] `.env.example` 重写：逐项注释 + 旧名兼容说明 + `MOCK_MODE` 三态说明（false 真模型 / true 对抗恶意桩 / script 离线协作桩）。
- [x] **新增 `docs/模型配置说明.md`**：三十秒版、为什么仓库里没有 key、配置读取优先级、环境变量总表、endpoint 拼接规则、各家填法表、换厂商三条硬要求（支持 `tool_calls` / 接受 `tool_choice: "auto"` / 支持 SSE 流式）、配好后怎么验（smoke 输出解读 + 三套回归）、报错速查表、换模型后必跑的回归清单、密钥安全铁律、常见问答。
- [x] README 新增「模型配置（仓库不含密钥，换厂商只改三行）」一节（环境变量表 + 各家 base url/模型名表 + 三条硬要求 + 指向配置文档）；「已知限制 7」与「技术选型」同步注明**只实测过 MiniMax 一家**，本仓库不替其他厂商背书。
- [x] 同步：`docs/09` §9.9 环境变量表（新键名 + 旧名兼容关系）、`docs/技术文档.md`、`docs/提交物对照表.md`、`docs/交付/交付说明.md`、`docs/答辩PPT大纲.md` 的实测条数 **731 passed / 4 skipped**；答辩 PPT 重生成（13 页）。
- [x] 新增 `tests/test_config_model_provider.py`（30 条）：endpoint 拼接 10 组、通用名/旧名/优先级、旧 `host` 别名、`LLM_TIMEOUT_S` 兜底、默认值、厂商粗判 8 组、缺 key fail closed 且报错可操作、Bearer 头、两种 mock 模式。
- 复验：`pytest -q tests/` = **731 passed / 4 skipped**（11.3s）；`scripts.redteam` = PASS 29 / FAIL 14（未退化）；`check_frontend` 62/62；`app.smoke` 真模型 4/4（旧名路径与通用名路径各跑一遍，均通，MiniMax 端点与模型名解析正确）。
- 遗留：**只实测过 MiniMax 一家**，其余厂商的兼容性由使用者按 `docs/模型配置说明.md` 自验；未做多厂商自动降级（`LLM_MODEL_BACKUP` 只给 smoke 自检用，生产链路不读）。

