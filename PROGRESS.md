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
- [x] `tests/test_imports.py` 依赖方向铁律固化：39 个 app 模块全部登记，85 项断言
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

