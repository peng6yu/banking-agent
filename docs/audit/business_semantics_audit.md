# 业务语义审计：这个银行 Agent 到底"懂不懂业务"

- 审计对象：`banking-agent`（repo root 下的 FastAPI + 单页前端 + SQLite 原型）
- 审计日期：2026-10-23
- 审计方法：只读源码 + 只读测试，逐条 `grep`/`read` 核对；**每条判断都带 `文件:行`**
- 判定四档：✅ 已实现且语义正确 ／ ⚠️ 已实现但语义有保留（正确性/边界条件/文案）／ ❌ 尚未实现或与宣称不符
- 一句话结论：**资金安全层（令牌、幂等、日线原子扣减、审计链）是这批代码里最扎实的部分；业务语义层（账户状态、商户协议、适当性、长期计划）基本是空的，而工具层文案普遍把"本地记账"说成"真实银行业务"。**

---

## 0. 先说审计口径：这个项目里有三层互相矛盾的"事实"

同一个业务事实，代码里存在三种说法，而且**模型和用户实际看到的那一层，是最不诚实的一层**。

| 层 | 位置 | 语义诚实度 |
|---|---|---|
| 工具层（模型/LLM 可见） | `app/tools/tool_specs.py`、各 tool 的 `message` | 低：多处把模拟账务说成真实业务 |
| 只读展示层 | `app/api/business.py`（`/api/business/*`） | **高**：明写"没做就是没做" |
| 文档层 | `docs/05_安全层实现规格.md`、`docs/赛题原文要点.md` | 中：写清了赛题设定，但把赛题策略写成"银行规则" |

`app/api/business.py` 的措辞堪称坦诚，例如：

- `app/api/business.py:817`："1000 元以上需要短信+人脸双因子——这是赛题设定的分级策略，不代表所有真实银行的限额。"
- `app/api/business.py:826`："已执行的转账不能一键撤回：只能发起冲正（记一笔反向流水），且在演示环境里是模拟账务。"
- `app/api/business.py:839`："只能停止银行侧代扣，不能注销商户会员。"
- `app/api/business.py:796-801`："适当性评估没做就是没做，不能靠界面上的风险标签假装做过了。"（`suitability_assessed=False`）
- `app/api/business.py:851`："冲正是模拟账务，不代表真实银行追回；真实追回需要人工受理。"

**但这层诚实面基本没人用**：`business.router` 挂在 `app/main.py:100`，而前端 `web/index.html` 的常量表只声明了 `/api/chat`、`/api/confirm`、`/api/cancel`、`/api/audit`、`/api/audit/verify`、`/api/tools`（`web/index.html:513-518`）以及理财面板的 `/api/savings/goals`、`/api/proactive/alerts`、`/api/proactive/monitor/run`、`/api/proactive/simulate/expense`（`web/index.html:1309-1312`）——**对 `/api/business/*` 零调用**（`grep "api/business" web` 无命中；`web/` 目录只有 `index.html` 和 `.gitkeep`）。`tests/` 对 `business` 也零引用（唯一命中是 `tests/test_imports.py:530` 的层依赖断言，与本文件无关）。

结论：**"诚实的边界说明"是一个没有任何消费者、没有任何测试覆盖的只读接口层**；真正被模型读到的工具描述与一句句 `message` 才是产品对用户的真实表述。因此本审计以**工具层的实际行为与用户可见文案**为准，`business.py` 只作为"作者知道自己在做什么"的证据。

---

## 1. 转账

主链路：`app/planner/runner.py` → `app/tools/transfers.py` → `app/funds.py`（hold/commit）→ `app/audit.py`。

### 1.1 收款人唯一性 / 消歧 ✅

- `app/tools/queries.py:92-118` `resolve_payee` 分三档匹配：精确姓名 → 模糊 → 纯数字尾号，返回 `candidates`（`payee_id/name/card_tail/bank/phone`）。
- `app/planner/runner.py:202-213` `_payee_gate`：`resolve_payee` 节点返回的候选数 `!= 1` 就**停住不往下走**。0 个："我没找到叫「X」的收款人，要不要换个名字或卡号尾号？"；多个："有 N 位都叫「X」：…你要付给哪一位？"
- `app/planner/runner.py:32` `DISAMBIGUATE_LIMIT = 1`：消歧只允许一问，不无限追问。
- 用户回答后的补齐：`app/planner/clarify.py:110-130` 只接受**候选里真实存在的** 4 位尾号，或 1 起的序号，其它回答返回 `None`（不会把"A 吧"瞎映射成某个收款人）；`CLARIFY_TTL_MS=300_000`（`app/planner/clarify.py:26`），5 分钟不作答即失效。
- 纵深防御：`app/tools/transfers.py:82-85` 在真正建 hold 前再校验 `payee_exists` + `_payee_owned_by`（收款人 `user_id` 必须等于账户 `user_id`），LLM 不能凭幻觉名字转钱。
- 测试：`tests/test_tools.py:320/336/344/416`（resolve_payee 精确/模糊/尾号/无候选）；`tests/test_payee_discriminator.py:64-75`；`tests/test_phone_payee.py`；`tests/test_relation_payee.py`；`tests/test_clarify_resume.py`；`tests/test_tools_write.py:122`（跨用户收款人必须失败）。

**判定**：这是全项目最像"业务"的一处。唯一保留：候选相似度是字符串匹配，不是真实银行的户名/账号核验（无 `payee_verify` 接口）。

### 1.2 账户状态（冻结/挂失/销卡、账户类型）❌

- 数据模型里 `accounts` **没有状态列**：`app/db.py:63-71` 只有 `type IN ('debit','credit')`、`balance_fen >= 0`、`credit_limit_fen`。**账户级冻结/止付/只收不付完全未建模。**
- 卡状态只存在于 `cards` 表：`app/db.py:74-82`（`status IN ('active','frozen','lost','closed')`、`per_tx_limit_fen DEFAULT 500000`）。
- **但转账/申购/预约执行路径从不读 `cards`**：全仓 `FROM cards` 只出现在 `app/tools/cards.py:54,60,346,373`；`per_tx_limit_fen` 也只出现在 `cards.py` 与 `db.py`。
- 后果（可直接复现的语义漏洞）：卡在 `app/tools/cards.py:185-188` 被 `report_card_loss` 置 `lost` 之后，`transfers.transfer` 依然会用该账户余额完成扣款；`set_limit` 改的 `per_tx_limit_fen` 对转账**完全不生效**。卡状态机本身是完整且不可逆的（`app/tools/cards.py:262-263` 对 `lost/closed` 拒绝任何迁移），只是资金路径不消费它。
- 账户视图也不暴露状态：`app/tools/queries.py:121-131` 的 `_account_view` 只有 `id/name/tail/type/balance/credit_limit`。
- 测试：卡状态机有 `tests/test_tools_cards.py:60-204`、`tests/test_cards_close_pin.py:28-93`、`tests/test_card_intent.py:67-161`；**"冻结卡是否阻断转账"零测试、零实现**。

**判定**：❌ 尚未实现。卡片的冻结/挂失/限额是**孤立的状态机**，不影响任何出账。

### 1.3 单笔限额 / 日累计额度 ⚠️

- 常量：`app/policy/rules.py:19-20` `SINGLE_LIMIT_FEN = DAILY_LIMIT_FEN = 100_000`（1000 元）。**注意：项目把"1000 元"同时当作单笔上限和日累计上限**，见 1.11。
- 单笔：`app/policy/rules.py:343` 起 `R08_transfer_red_single`，`amount > 1000` → 红（需人脸+短信）。边界是 `>`，等于 1000 仍是黄（`rules.py:18` 注释明写"含等于仍为黄"）。
- 日累计：两层。
  1. 策略层 `R07_transfer_red_daily`（`app/policy/rules.py:323-338`）：`spent_today + amount > DAILY_LIMIT_FEN` 且 `spent_today > 0` → 红。
  2. 资金层硬线：`app/funds.py:528-575` `_bump_daily_limit` 用条件 upsert（`WHERE spent_fen + excluded.spent_fen <= limit`），`rowcount=0` 就回滚并抛 `DAILY_LIMIT_EXCEEDED`——**这是为了防两个会话并发读到旧值**，是全项目最正确的并发处理。持久化在 `daily_limits(account_id, day)` 主键（`app/db.py:208-214`）。
- **关键语义保留：日累计线对红牌交易不生效。** `app/tools/base.py:57-76` `daily_cap_fen`：令牌 `level == "red"` 返回 `None`（`base.py:74-75`），即 `commit(daily_limit_fen=None)` 不做日线校验。注释（`base.py:64-65`）说得坦白："红牌：策略已经让用户过了 face/sms 双因子，R07/R08 明说这时可以超线，不在这里重复拦。"
  → 所以所谓"日累计 1000 元"**不是硬上限**：任何一笔 >1000 元的转账走红牌，过了（自称的）双因子后，日线被整笔绕过。真实银行里"提额/超限"是控制点，这里的日线只是"黄牌路径的减速带"。
- 测试：`tests/test_funds_invariants.py:304/334/348`（日线、单笔、并发日线）；`tests/test_funds_invariants.py:188`（两会话并发不超支）；`tests/test_policy.py` 边界表；`tests/test_scheduled_fires.py:365`（当日超线顺延）。

**判定**：⚠️ 单笔/日线在**黄牌路径**里实现正确且有并发保证；但红牌路径整笔绕过日线，产品口径若说"日累计上限 1000 元"则与代码不符。

### 1.4 权限等级 red/yellow/green 与 required_factors ⚠️

- 分级：`app/policy/rules.py:268-366` 规则链 `R12_other_account → R01_readonly → R02_invalid_amount → R03_unknown_tool → R04_untrusted_amount → R05_confirm_only → R06_transfer_yellow → R07 → R08 → R09_sensitive_red → R10_invest_red → R11_default_deny`。
  - `R06`（`rules.py:318`）：`0 < amount <= single_limit` 且未超日线 → 黄；文案"即将向 X 转出 N 元，需要你确认"（`rules.py:189`）。
  - `R09_sensitive_red`：改密/销卡/冲正等敏感工具 → 红，`required_factors = face+sms+otp`（`rules.py:224-232`）；`R10` 申购/赎回 → 红，`face+otp`（`rules.py:235-238`，`rules.py:55` `INVEST_TOOLS`）。
  - `R11_default_deny`（`rules.py:360-366`）：`lambda: True` 兜底红，文案"这个操作我不确定是否安全，先不做"（`rules.py:250-251`）。这比"默认放行"安全得多。
- **但多因子是"客户端自称"，没有真实校验。** `app/planner/state.py:340-345` `factors_ok` 只做一件事：把 `factor` 字符串按 `+`/`,`/空格拆成集合，判断 `required_factors` 是不是它的子集。没有挑战码、没有人脸结果回执、没有短信验证码校验、没有失败计数、没有锁定。`factor` 由调用方在 `POST /api/confirm` 里自带（`app/api/routes.py:339-349`），`app/planner/executor.py:147-159` `resume` 直接把它交给 `factors_ok`。
  → 红灯三因子在语义上等价于：**用户在请求体里写了一句 `face+sms+otp`**。演示能跑通"分级"，但"多因子"不是安全控制，是协议占位。
- 等级带动的资金行为是真实的（不是装饰）：黄牌会把日线带到扣款那一刻原子核一次（`app/tools/base.py:57-76` + `transfers.py:127`）。
- 测试：`tests/test_planner.py:490/523`（红灯因子）；`tests/test_invest.py:248`（红牌 face+otp）；`tests/test_scheduled_fires.py:317`（大红需因子）；`tests/test_tools_cards.py:180-204`（写操作转红）。

**判定**：⚠️ 分级→令牌→资金三段的**传导**是真实现；因子**校验**是假的。

### 1.5 授权有效期（TOKEN_TTL_MS / CONFIRM_TIMEOUT_MS）✅

- `app/planner/state.py:41` `TOKEN_TTL_MS = 120_000`（2 分钟）；`app/security/token.py:41` `DEFAULT_TTL_MS = 120_000`；签发时黄/红才签（`app/planner/runner.py:216-228`，绿灯不签）。
- `app/planner/state.py:35` `CONFIRM_TIMEOUT_MS = 120_000`；可被环境变量 `CONFIRM_TIMEOUT_S` 覆盖，范围钳制 10–3600 秒，非法值回落默认（`state.py:36-39, 49-72`）。
- 超时行为：`app/planner/executor.py:437-453` `_timeout` → 把确认卡标 `expired`、`release_holds` 释放预授权、事件文案"等太久没等到你的确认，这笔我已经作废了，钱也退回账户了"。
- 令牌本身是**真校验**：`app/security/token.py:265-346` 七步（结构 → HMAC 签名 → 过期 → `nonce` 一次性 → 工具名 → `params_hash` → 账户/金额），nonce 消耗写 `token_nonces` 表，冲突即 `NONCE_REPLAY`。密钥绝无硬编码默认值（`token.py:107-175`，显式值 → `CAPABILITY_SECRET`（必须 64 hex）→ `data/token_secret` 自动生成 0600）。
- 测试：`tests/test_token.py`（23 例，含七步与 nonce 消耗）；`tests/test_confirm_timeout.py:81`（卡到期）、`:155`（120s）、`:161`（TTL 120s）；`tests/test_confirmations_persist.py:139` `timeout_marks_row_expired`。

**判定**：✅ 有效期与令牌校验是全项目质量最高的部分。唯一措辞瑕疵：**超时被记成 `confirm.rejected`**（`executor.py:441-446`，`reason="TIMEOUT"`），因为 `app/audit.py:24-60` 的 `EVENT_TYPES` 闭集里**没有 `confirm.expired`**。审计无法区分"用户拒绝"与"用户没看见"。

### 1.6 幂等 ⚠️（会话内 ✅，跨会话 ❌）

- 键：`app/funds.py:86-89` `idempotency_key_for = sha256(session_id|node_id|params_hash)[:32]`；`holds.idempotency_key UNIQUE`（`app/db.py:105`）。
- 发起：`app/funds.py:114-145` 命中已有 hold 则 `reused=True` 并审计 `transfer.initiated`（带 `reused`），否则 `_create_hold`。
- 确认：`app/planner/executor.py:151-152` `resume` 里 `conf.status != "pending"` 直接返回 `{"ok": True, "status": conf.status, "idempotent": True}`——重复点"确认"不产生第二次副作用。
- 提交：`app/funds.py:213-214` 已 `committed` 直接返回既有结果。
- **保留**：`session_id` 与 `node_id` 都是**内存态**（`app/planner/state.py:76` `RUNS: dict`，`open_run` 时生成）。服务重启或新会话里重放同一笔转账，`idempotency_key` 不同 → **不会命中，会重复扣款**。真正的"客户端重试幂等"需要业务侧幂等键（如订单号），本项目没有。
- 测试：`tests/test_funds_invariants.py:149`（同键复用）、`:172`（同笔两次只扣一次）；`tests/test_planner.py:696/725`（重复确认幂等）；`tests/test_funds.py`（幂等 13 处）。

**判定**：⚠️ 单会话重试安全；跨会话/跨重启不幂等，且没有任何 `client_request_id` 之类的入口。

### 1.7 预约执行 ⚠️

- 表：`app/db.py:250-270` `scheduled_transfers(status IN ('scheduled','executed','cancelled','failed'), factor_verified_ms, defer_count, idempotency_key UNIQUE)`。
- 入口：`app/tools/scheduled.py:71-136` `schedule_transfer` —— `require_token`、时间必须在未来且 ≤366 天（`MAX_SCHEDULE_AHEAD_MS`，`scheduled.py:49`）、收款人存在、令牌**真七步校验**（`:98`）、红牌时把 `factor_verified_ms` 落库（`:101`，= 用户当时过过因子的时间戳）、写 `idempotency_key`（`:102`）、审计 `scheduled.created`。
- 节拍器：`app/planner/ticker.py` —— `SCHEDULED_TICK_MS` 默认 30 秒、`0` 关闭（`:29-30`）；`run_due_once` 开连接 + `db.tx` 调 `execute_due`（`:47-57`）；`tick_forever` 异常只记日志（`:60-67`）。文件注释（`:12-13`）自述"只调用 execute_due，不自己判时间、不自己算钱"。
- 执行：`app/tools/scheduled.py:193-212` `execute_due` 查 `status='scheduled' AND due_ms <= now`；`215-281` `_execute_one` 在**全仓唯一一处自签令牌**（`:240-242`，注释解释了为什么：执行时刻没有用户在线），然后 `funds.initiate + commit`。
- 语义保留三处：
  1. `_execute_one` 用系统时钟自签，等于**"用户授权"被无限期延期执行**；`factor_verified_ms` 只落库不校验（没有"因子必须在 N 分钟内有效"的规则），一个月后仍会执行一笔当时红牌授权的转账。
  2. `execute_due` 的 `SELECT` 之后没有 `UPDATE ... WHERE status='scheduled'` 的原子 claim（`:193-212`），靠 ticker 外面包一个 `db.tx` 串行化。单进程成立，**多实例/多进程会重复执行**（`idempotency_key` 是预约记录的键，不是执行次数的键）。
  3. `RECURRENCES = ("once","monthly")`（`:48`），月付用 `_next_month_ms` **固定 +30 天**（`:284-286`，注释承认"演示够用不做日历月"）→ 31 号会漂移。
- 测试：`tests/test_scheduled.py:35/45/54/62/76/87`；`tests/test_scheduled_fires.py:208/227/239/249/262/284/317/365/408`（端点真扣款、未到期不动、大红需因子、因子通过后执行、当日超线顺延、老库补列、需真令牌）。

**判定**：⚠️ "到点扣款"是真的（含顺延与上限），但**授权时效与并发 claim 是缺的**，且月付是 30 天近似。

### 1.8 余额不足 / 执行失败 ⚠️

- 余额不足：`app/funds.py:336-384` `_create_hold` 里校验可用余额，不足抛 `InsufficientFunds`，工具层 `app/tools/transfers.py:330-344` `_insufficient` 组文案"余额不够，还差 X 元。"——**在创建 hold 阶段就拦住，不会先扣再失败**。
- 执行失败：`app/tools/transfers.py:135-139` 其它 `BankError` → 写 `settle_error`、`message = UNSETTLED_TEXT`（`:56` "钱已经锁定，但扣款没成功，稍后我会重试。"），**但仍返回 `ok_result`**（不抛异常）。
  - 语义问题一：这句"稍后我会重试"**没有任何实现**。全仓没有任何重试 worker：`app/db.py:430-441` `sweep_expired_holds` 只会把过期 `held` 置 `expired`（钱退回可用），没有"重试扣款"的分支；`grep` `UNSETTLED`/`settle_error` 找不到消费方。用户被告知"会重试"，实际结果是 **hold 300 秒后过期、这笔转账静默消失**（`HOLD_TTL_MS`，`app/funds.py:29`）。
  - 语义问题二：`ok_result` 让上层（planner/LLM）以为这笔"成功"了，只是附带一句失败文案。
- 另一个失败口径：日线超限时 `transfers.py:129-134` 走 `fail_result` + `DAILY_LIMIT_TEXT`（`:56` "今天已经花过日限额了，这笔没转出去。"）——这一条是诚实的。
- 测试：`tests/test_tools_write.py:72/99`（settle_error 不抛、两阶段）；`tests/test_funds_invariants.py:266/286`（commit 失败钱仍在 hold）；`tests/test_funds_invariants.py:229`（300 秒过期）。

**判定**：⚠️ 余额不足处理正确；**"稍后重试"是空头承诺，且失败结果被包装成 ok**。

### 1.9 取消 vs 追回 vs 冲正 ⚠️（代码在机制上分得清，文案分不清）

代码里确实存在三条不同的路径：

1. **取消未执行的预约**：`app/tools/scheduled.py:167-190` `cancel_scheduled` —— 已是 `cancelled` 幂等返回；`status != 'scheduled'`（即已执行）→ `SCHEDULED_NOT_CANCELLABLE`；否则 `UPDATE status='cancelled'` + 审计 `scheduled.cancelled`。这是干净的"撤销授权、钱还没动"。
2. **冲正已执行的转账**：`app/tools/transfers.py:216-280` `reverse_transfer` —— 校验交易归属、原交易 `reversed_by` 已存在则幂等返回、**超过 `REVERSAL_WINDOW_MS = 7 天`（`:47`）抛 `TXN_TOO_OLD` 并提示去柜面**、金额**取自原交易**（`:234`，不允许改金额）、走 `funds.refund`（`app/funds.py:238-299`，四道拒绝 + 双写 `reversal_of/reversed_by` + 审计 `transfer.reversed`）。机制上是"反向记账"，语义正确。
3. **追回**：❌ **尚未实现，也没有入口**。没有"向收款行发起退汇/止付/司法协助"的任何工具、表或状态；`business.py:851` 自己写明了"真实追回需要人工受理"。

**问题不在机制，在文案——完成后的转账被表述成一键可逆：**

- `app/tools/transfers.py:278`：冲正成功后 `summary = "已经把 X 元退回你的账户。"` —— 用户读到的印象是"转账被撤回了"。
- `app/tools/scheduled.py:182`：取消失败时给用户指路："这笔预约已经执行过了，取消不了；需要的话我可以帮你冲正" —— 把冲正摆成取消的等价替代。
- `app/tools/tool_specs.py:539`（模型看到的工具描述）："冲正一笔已经转出去的钱，把钱退回原账户。" —— 这正是模型据以向用户承诺的那句话。
- `app/api/business.py:826` 才是正确表述："已执行的转账不能一键撤回…在演示环境里是模拟账务。"——**这句没人看得到**。

**判定**：⚠️ 三态在数据层可分（`scheduled_transfers.status` / `transactions.reversal_of|reversed_by` / 无追回）；但**用户可见文案把"冲正"说成"退回/撤回"**，且没有任何地方告诉用户：对方账户里的钱并不会因此减少（本系统只改本地两行流水）。

### 1.10 审计记录 ✅（有一处口径瑕疵）

- 表：`app/db.py:132-142` `audit_log` 带 `prev_hash/hash`；`:327-331` 对 `audit_log` 的 `UPDATE/DELETE` 建了 `ABORT` 触发器——**只增不改，哈希链可验**（`app/audit.py:104-125` `verify_chain`）。
- 事件闭集：`app/audit.py:24-60`，覆盖 `transfer.initiated/committed/released/reversed`、`scheduled.*`、`token.issued/rejected`、`confirm.requested/approved/rejected`、`policy.decided`、`session.end` 等。
- 瑕疵一：闭集里**没有 `confirm.expired`**，超时借用 `confirm.rejected`（`app/planner/executor.py:441-446`）→ 事后审计无法区分"拒绝"与"超时"。
- 瑕疵二：`app/tools/subs.py:76` 注释承认"audit event_type 是闭集，只能用 `tool.executed`"，卡片状态变更也只能落 `tool.executed`（`app/tools/cards.py` 全程无专用事件）→ **状态机变更在审计里不可按事件类型检索**。
- 瑕疵三：**预约执行失败不写审计**。`app/tools/scheduled.py:274-281` 异常分支只有 `log.warning` + `UPDATE status='failed'`，没有 `audit.append`（对比 `_defer` 给了 `scheduled.deferred` 审计）。"钱没扣成"这种最该留痕的事件，审计链上是空的。
- 测试：`tests/test_audit.py`；`tests/test_planner.py:873`（审计链）；`tests/test_invest.py:144`（份额与审计链）；`tests/test_tools_write.py:173`（cancel_subscription 落库审计）；`tests/test_proactive_api.py:415`。

**判定**：✅ 不可篡改审计链是真本事（有触发器兜底）；⚠️ 事件类型粒度与失败留痕有缺口。

### 1.11 关于"1000 元"这条线：它是赛题策略，不是银行规则 ⚠️

- 代码里能找到它：`app/policy/rules.py:19-20`（`SINGLE_LIMIT_FEN = DAILY_LIMIT_FEN = 100_000`），`rules.py:18` 注释直接引"docs/05 5.1：单笔与日累计同一条 1000 元线"。
- 它的出处是赛题：`docs/赛题原文要点.md:69-72` 原文"黄色…日累计 ≤ 1000 元…红色…日累计 > 1000 元""注意黄/红的分界是日累计 1000 元"。
- 因此：**"1000 元"是赛题（赛题策略）设定的分级阈值，不是任何一家真实银行的通用限额，也不是监管规定。**
- 更要紧的两个语义漂移（务必在对外材料里不要含糊）：
  1. 赛题原文只把 1000 元定义为**日累计**的分界；代码把它**同时当作单笔上限**（`rules.py:19-20`、`R08` at `:343`）——这是项目自己加的。
  2. 如 1.3 所述，红牌路径整笔绕过日线（`app/tools/base.py:74-75`），所以这条"日累计"在 >1000 元场景下**不构成上限**。若宣传语说"日累计封顶 1000 元"，与实现不符。
- 项目自己也知道：`app/api/business.py:817` 明写"这是赛题设定的分级策略，不代表所有真实银行的限额"——同样，没人看得到。

---

## 2. 订阅与代扣

涉及：`app/tools/subs.py`（读写 `subs` 表）、`app/planner/subscription.py`（意图与消歧）、`app/db.py:197-206`（表结构）。

### 2.1 能力矩阵：代码实际做了什么

| 能力 | 是否实现 | 证据 |
|---|---|---|
| 查询已有订阅 | ✅ | `app/tools/subs.py:26` `_SUBS_SQL`（`SELECT * FROM subs WHERE account_id = ? ORDER BY next_charge_ms, id`）；`subs.py:29-52` `list_subs`（默认只列 `active`，并给 `monthly_total_fen` 汇总） |
| 从流水推断潜在订阅 | ❌ 尚未实现 | `list_subs` 只读 `subs` 表，全仓无任何基于 `transactions` 的周期性识别/置信度/证据字段 |
| 取消商户服务（注销会员） | ❌ 尚未实现 | 无商户接口、无 HTTP client 调商户、`subs` 表无商户协议号/商户侧状态 |
| 停止银行侧周期性付款 | ⚠️ 只做到"改本地行状态" | `app/tools/subs.py:70-73` `UPDATE subs SET status='cancelled' WHERE id=? AND status='active'` |
| 阻止未来商户扣款 | ❌ 尚未实现 | 没有代扣白名单/止付名单/商户授权撤销表；`subs.status='cancelled'` 不被任何扣款路径读取（本系统本来也没有商户主动扣款的入口） |
| 设置续费提醒 | ❌ 尚未实现 | `subs.next_charge_ms` 只用于排序与总额，无任何提醒调度（`proactive` 里也没有订阅到期规则） |

补充：`app/db.py:197-206` 的 `subs` 只有 `merchant/amount_fen/period monthly|yearly/next_charge_ms/status active|cancelled`——**没有商户协议号、没有签约渠道、没有商户侧状态、没有最近一次扣款流水外键**。这张表本质是"用户自述的订阅清单"，不是"银行代扣授权档案"。

### 2.2 致命问题：取消本地行，却宣称商户不再扣款

`app/tools/subs.py:107-116` `_cancel_payload`，用户可见文案（`:114`）：

> **"已经取消，下次扣费不会再发生。"**

而代码实际只做了一件事：`subs.status: active → cancelled`（`subs.py:70-73`），审计落 `tool.executed`（`subs.py:76`）。

- 没有通知商户、没有撤销代扣授权、没有止付。
- 这句话是对**真实世界结果的断言**，代码完全没有能力保证。若商户侧仍在扣款，用户会认为银行已处理。
- 正确表述就在 `app/api/business.py:839`："只能停止银行侧代扣，不能注销商户会员。"——没人看得到。
- 更糟的是这句文案是**模型能直接读到的**（工具返回值里的 `message`），模型会原样转述给用户。

消歧本身做得不错（`app/planner/subscription.py:83-108` `match_one`）：空名/泛称（`GENERIC_SUB_NAMES`）→ 反问；0 条 → "我没找到叫「X」的订阅。你名下现在是：…"；多条 → "有 N 条订阅跟「X」对得上…"；**绝不替用户挑一条**。引用式参数 `@n1.subs[merchant=…].sub_id`（`subscription.py:46-59`）保证"取消的就是刚才列出来的那一条"，不是 LLM 重新编的名字。

**判定**：⚠️ 消歧与"不替用户决策"是对的；**"下次扣费不会再发生"是虚假承诺**，是本项目最危险的一句用户可见文案（与 `transfers.py:278` 同级）。

### 2.3 推断 / 潜在订阅与不确定性 ❌

完全不存在。没有"疑似订阅"的数据结构，没有 `confidence`/`evidence` 字段，`subscription.py` 的模板只有 `query` 与 `cancel` 两条路（`subscription.py:27-30`）。账单分析里有 `subscription_hike`（涨价检测），但那是**对 `category='订阅'` 流水的环比计算**（`app/tools/bill.py:308-352`），不是"发现你可能订阅了 X"。

**判定**：❌ 尚未实现。任何"我能从流水帮你找出被遗忘的订阅"的说法都无代码支撑。

### 2.4 测试覆盖

`tests/test_subscription_cancel.py:144-284`（9 例：只 cancel 保留商户名、批量反问、子节点只在 cancel 路、唯一匹配取 `sub_id`、未匹配反问、两条反问、黄灯确认+审计、未匹配与无名都不进写工具）；`tests/test_tools.py:371/402`（`list_subs`）；`tests/test_tools_write.py:155-181`（`cancel_subscription` 落库与审计）。

**没有测试**的是最关键的那句承诺——因为它是文案，不是行为。

---

## 3. 账单分析

唯一实现：`app/tools/bill.py`（`analyze_bill`，`:43-57`），只读、无 `require_token`。三条规则常量在 `:22-40`。

前置事实：`_local`（`:58-60`）用 `time.localtime`，**按服务器本地时区判"凌晨"**——生产环境跨时区会系统性误判（这是"凌晨境外消费"规则的隐性前提）。

### 3.1 规则一：`split_orders`（疑似拆单）

- 规则依据（`:207-247`）：**同一 `counterparty`**、单笔金额落在 `[SPLIT_MIN_FEN=50_000, SPLIT_MAX_FEN=100_000]`（500–1000 元）、**60 分钟内**（`SPLIT_WINDOW_MS=3_600_000`）**≥3 笔**（`SPLIT_MIN_COUNT=3`）。
- 时间窗口：滑动窗口，且 `_windows`（`:250-262`）保证窗口互不重叠（避免一笔交易被算进两次）。
- 涉及交易：同收款方的这几笔 `tx_ids`（写进 `evidence.merchant/tx_ids/occurrence_count/window_start_ms/window_end_ms`）。
- 为什么触发：金额刻意压在 500–1000 元这一带、时间密集、收款方同一 → 形似"把一笔 2000 元拆成 3 笔 800 元"以规避单笔限额。
- 误报风险（高）：分期付款、多人 AA 后各付各的、商家分单结算、同一场活动分次下单、家庭共享账户。**这套规则正是按本项目自己的 1000 元阈值反推出来的**，所以它对"规避 1000 元线"特别敏感，也对正常消费特别敏感。
- 用户可做什么：`app/tools/bill.py` 自身**不提供任何动作**——只给 `title`（"{merchant} 疑似拆单"）+ `detail`。动作只在 `app/api/business.py` 的规则元数据里（`:65-102`）。

### 3.2 规则二：`odd_hour_overseas`（凌晨境外消费）

- 规则依据（`:265-305`）：逐笔判断，要求**出账**、金额 `>= OVERSEAS_MIN_FEN=50_000`（500 元）、本地小时 ∈ `[0, 5)`（`OVERSEAS_NIGHT_START_HOUR=0` / `END=5`），且 `_is_overseas` 为真——即 `channel == 'overseas'` **或** `counterparty + note` 命中 `OVERSEAS_MARKERS`（`:36-40`，30 个硬编码地名/关键词：境外、overseas、香港、澳门、台湾、日本、东京…）。
- 时间窗口：单笔，不看上下文。
- 涉及交易：当笔 `tx_id`。
- 为什么触发：盗刷的典型画像（深夜 + 境外 + 大额）。
- 误报风险（很高）：时差（用户在欧洲刷国内卡）、夜间国际航班消费、境外订阅自动续费、跨境网购、在港澳台正常消费（`OVERSEAS_MARKERS` 把"香港/澳门/台湾"直接当境外）。而且关键词表是硬编码字符串匹配——收款方名字里出现"日本"两个字就会命中，与真实地域无关。
- 用户可做什么：规则本身只输出标题与明细；`business.py:65-102` 给的动作提示里含 `freeze_card`。

### 3.3 规则三：`subscription_hike`（订阅涨价）

- 规则依据（`:308-352`）：筛选 `category == '订阅'` 且出账，按 `(counterparty, 月)` 汇总，与上月比，涨幅 `>= SUBSCRIPTION_HIKE_RATIO=0.30`（30%）。
- 时间窗口：自然月；`_hike_baseline`（`:97-106`）在按月分析时额外取上一个自然月的订阅流水——**按年分析时没有基线，这条规则不会触发**（`:window_start_ms=null`）。
- 涉及交易：本月该商户的订阅流水（`tx_ids`）+ `comparison`（上月金额/本月金额/涨幅）。
- 为什么触发：订阅提价、套餐变更、汇率变动导致的等值人民币上涨。
- 误报风险（中）：套餐升级（用户自己升的）、一次性加购被归到"订阅"分类、按用量计费（云服务、话费）月度波动大、上月有促销价而本月恢复原价。
- 用户可做什么：只能自己看；没有"取消这条订阅"的联动（`subscription` 意图要用户另外说一句）。

### 3.4 输出是否区分「发现异常 / 建议核实 / 已确认问题」？

- **`app/tools/bill.py` 层：完全没有分级。** 输出只有 `kind/title/detail/amount_fen`（`_detect_anomalies`，`:192-204` 固定顺序 + 金额降序），没有 `severity`、没有 `confidence`、没有 `status`、没有 `possible_false_positive`、没有 `actions`。
- **分级只存在于 `app/api/business.py:65-102`**（`severity`/`confidence`/`possible_false_positive`/`actions`）与 `:184-224` `_anomaly_view`：`status` 固定为 `"发现异常"`（`:195`），`status_note` = "规则命中只表示值得核实，需要你自己或客服确认。"（`:196`）。`:194` 的注释把原则写死了："状态只有「发现异常」：规则命中不是欺诈认定，不能写成「已确认问题」。"
- 也就是说：**"发现异常 / 建议核实"这个区分在诚实层是显式的**（status + status_note + possible_false_positive 三件套），但这一层无人调用（见第 0 节）；**模型实际看到的是无分级的 `bill.py` 输出**，标题里只有"疑似"两个字（"疑似拆单"）。

### 3.5 文案是否把规则命中等同于欺诈？

- `bill.py` 的标题用"疑似拆单"、"凌晨境外消费"、"订阅涨价 X%"——**没有出现"盗刷/欺诈/诈骗"字样**，也没有"你的卡被盗了"这类断言。标题是描述性的，`detail` 是陈述句（时间区间、笔数、金额、涨幅）。
- 但风险在于**没有兜底免责**：`bill.py` 输出里没有任何"这只是规则命中、请自行核实"的字段。当模型转述时，很容易把"凌晨境外消费"讲成"你的卡可能被盗刷了"。免责措辞只写在无人调用的 `business.py:196`。
- 测试：`tests/test_tools.py:233`（固定形状）、`:254`（三异常）、`:270`（近似不误报）、`:284`（空月）、`:296`（并列稳定）、`:310`（`year` 期）。三条规则各有 2 处断言，**没有任何测试断言"误报场景不触发"**（`:270` 只覆盖金额近似阈值）。

**判定**：⚠️ 规则可解释、窗口与阈值明确、标题克制；但**分级与免责只在死层**，`bill.py` 自身缺少"这是规则命中，不是结论"的字段，且时区与关键词表是硬编码脆弱点。

---

## 4. 理财

涉及：`app/tools/invest.py`（申赎）、`app/tools/products.py`（产品与风险提示）、`app/funds.py`（资金）、`app/db.py:227-247`（表）。

### 4.1 产品数据源 / 风险等级 / 期限 / 流动性 / 费用

| 项 | 实现情况 | 证据 |
|---|---|---|
| 数据源 | **本地 `fund_products` 表**，非行情源、非基金公司接口 | `app/tools/products.py:60-62`（`SELECT * FROM fund_products WHERE code=?`）；`app/db.py:227-238` 字段仅 `code/name/category/risk_level/min_buy_fen/nav_micro/nav_updated_ms/description` |
| 风险等级 | 有 1–5 级 + 中文标签 | `app/db.py:227-238`（`risk_level` 1–5）；`app/tools/products.py:36-42`（`RISK_LABELS`：R1 谨慎…R5 激进） |
| 期限 | ❌ 无字段 | `fund_products` 表中没有 `term`/`maturity`；产品均为开放式基金口径 |
| 流动性 | ❌ 无字段 | 无"T+1/T+2 到账""赎回限额""封闭期"任何建模 |
| 费用 | ❌ 无字段 | 无申购费/赎回费/管理费；申赎金额与份额按 `nav_micro` 直接折算（`invest.py` 的 `shares_for`） |
| 净值 | ⚠️ 静态字段，不接行情 | `nav_micro` + `nav_updated_ms`；`business.py:795` 自述"净值固定不接行情" |

### 4.2 适当性（suitability）：**没有真实评估** ❌

这是本项目最明确的"没做"之一，且作者自己承认：

- 全仓 `grep` `"风险测评|适当性|suitability|risk_tolerance|risk_assess"` 在 `app/` 下 **0 命中**（唯一出现是 `app/api/business.py:796-801` 的说明文字）。
- 没有客户风险承受力问卷、没有 `risk_tolerance` 字段（`users` 表无此列）、没有"产品风险等级 vs 客户等级"的匹配判断、**没有"不适当"拦截**。
- `app/tools/products.py:115-159` `estimate_risk` 只做四件事：算市值、算份额、按 `CONCENTRATION_FEN`（`:43-45`）给集中度软提示、`risk_level >= 4` 时给"波动大"提示（`:162-172` 四条 warning）+ 一句按等级生成的建议（`:175-182`）。**这是"提示"，不是"评估"，更不是"拦截"。**
- 更关键：**`buy_fund` 根本不调用 `estimate_risk`**。`app/tools/invest.py:44-80` 的流程是 `require_token` → `fetch_product` → 起购校验 → 折份额 → `funds.initiate` → `_commit`。风险等级只作为返回值里的一个标签（`invest.py:217` `risk_label`）。
- 所以答案是：**既没有真实适当性评估，也不是"只有前端文案"——连前端文案都没有**（`web/index.html` 无任何风险提示卡）。唯一写正确的地方是 `app/api/business.py:796-801` 的 JSON 说明：`suitability_assessed=False`、`suitability_note="没有做投资者适当性问卷与匹配判定"`。

### 4.3 购买前确认 ⚠️

- 有"确认"这条流程：申购/赎回属 `INVEST_TOOLS`（`app/policy/rules.py:55`），`R10_invest_red` 判红、`required_factors = face+otp`（`rules.py:235-238`）。`tests/test_invest.py:248` 覆盖。
- 但这是**权限确认（谁在操作）**，不是**产品确认（买的是什么、风险匹配不匹配、费多少）**。没有产品说明页、没有风险揭示书、没有"我已阅读"记录、没有冷静期。
- 红灯还需注意的是：因子是自证（见 1.4），所以"买基金要人脸+OTP"在实现上等价于前端多传两个字符串。

### 4.4 赎回条件 / 执行结果

- `app/tools/invest.py:83-127` `redeem_fund`：`require_token`（`:89`）；份额不足 `shares_milli > held_shares` → `REDEEM_EXCEEDS_HOLDING`（`:93-96`）；`funds.initiate(check_balance=False)`（`:115-118`，入账方向不校验余额）；`_redeem`（`:145-156`）走 `funds.redeem`；`_sell_shares`（`:174-191`）按剩余份额比例留成本，赎完 `DELETE` 行。
- **没有赎回条件**：无持有期限制、无赎回费、无到账时间（`BOUGHT_TEXT`/`REDEEMED_TEXT` 出现在 `invest.py:39-40`："申购成功，钱已经变成基金份额了。"/"赎回成功，钱已经回到你的卡里。"——后者暗示即时到账，真实基金是 T+1 起）。
- 执行结果的语义问题与转账同源：`buy_fund` 在 `_commit` 失败时只写 `settle_error` + `UNSETTLED_TEXT`"钱已经锁定，但结算没成功，稍后我会重试。"（`invest.py:41`，`_commit` 在 `:130-142`），**仍返回 `ok_result`**，同样**没有重试实现**；只有成功才 `_add_shares`（`:77-79`）。
- 起购：`invest.py:52-59` `BELOW_MIN_BUY`"这只基金起购 X，还差一点。"；份额折算 `<=0` → `AMOUNT_TOO_SMALL`（`:60-63`）。
- 测试：`tests/test_invest.py:82/98/109/117/133/144/160/180/197/219/234/248`（列表/风险提示/起购拒绝/份额与审计链/超额赎回/赎回到卡/无令牌拒/红牌 face+otp）；`tests/test_invest_intent_dag.py:241/261/276/289/304`。

**判定**：⚠️ 申赎资金流转正确、份额记账与审计链可验；❌ 产品要素（期限/流动性/费用/净值来源）缺失，❌ 适当性未实现，⚠️ 结果文案"稍后重试"同样是空头承诺。

---

## 5. 跨场景计划

涉及：`app/planner/dag.py`（模板与依赖）、`app/planner/executor.py`（驱动）、`app/proactive.py`（储蓄目标与提醒）、`app/tools/proactive.py`（工具面）、`app/planner/ticker.py`（唯一的定时器）、以及 `savings_goals/monitor_rules/proactive_alerts/monitor_runs` 四张表（`app/db.py:277-324`）。

### 5.1 任务依赖 ✅（但只有链，没有图）

- `app/planner/dag.py:43-71` `INTENT_TEMPLATES`：`transfer=(resolve_payee, query_balance, transfer)`、`birthday_plan=(resolve_payee, query_balance, reserve_funds, pay_merchant)`、`schedule_transfer=(resolve_payee, schedule_transfer)`、`reversal=(query_transactions, reverse_transfer)` 等。
- `dag.py:77-85` `CROSS_TEMPLATES`：**全仓唯一真正的跨场景计划** `redeem_then_transfer = (resolve_payee, redeem_fund, query_balance, transfer)`——先赎回基金、再转账。
- `dag.py:228-242` `_nodes`：每个节点 `depends_on=[前一个节点]`。**纯串行链，没有扇出、没有并行分支、没有条件分支、没有"若 A 则 B"**。
- 校验是认真的：`dag.py:170-183` `validate` 四道（ID 不重复、依赖不悬空、工具在册、`_topo` 无环）；`dag.py:129-167` `build_or_single` 在跨场景建图失败时返回 `unsupported_request` 哨兵（`:93`、`:115-126`），**不静默降级成普通转账**。

### 5.2 计划持久化 ⚠️

- 表在：`app/db.py:145-175` `dag_runs`/`dag_nodes`；`app/planner/state.py:364-366` `persist_node` 注释写"节点状态落库；DAG 面板和中断续跑都靠这张表"。
- **但"中断续跑"是假的**：`app/planner/state.py:326-330` `require_run` 只查内存 `RUNS`（`:76`），查不到就抛 `ValidationError`"会话已结束或不存在"；`state.py:333-337` `require_confirmation` 只查内存。`app/api/routes.py:358-397` `/api/session` 只是**只读展示**（能从 DB 看回节点与确认卡），**没有任何"从这里继续跑"的入口**。注释与实现自相矛盾。
- 结果：计划的生命周期 = 一次 HTTP 请求的生命周期（`app/planner/executor.py:92-145` `run` 里 `open_run` → `_drive` → `finally finish_run`，`finish_run` 会 `RUNS.pop`，`state.py:192-214`）。**进程重启 / 请求结束 = 计划不可续。**

### 5.3 延迟执行 ⚠️（只支持"转账"这一种延迟动作）

- 唯一的延迟执行是预约转账：`app/planner/ticker.py`（30 秒节拍）+ `app/tools/scheduled.py:193-212` `execute_due`。`scheduled_transfers` 表（`app/db.py:250-270`）是**跨会话持久**的——这一条是真的能扛重启的（只要进程重新起来）。
- 但：**`ticker` 只服务定时转账**。它不调用 `proactive.run_monitor`（`ticker.py` 全文件只 import `scheduled`）。所以"延迟执行"在业务上仅覆盖"某天转一笔钱"，不覆盖"某天检查一下目标进度"。

### 5.4 到点前复检条件 ⚠️（只复检日限额，不复检别的一切）

- 有个别复检：`scheduled.py:246-255` 执行时 `funds.commit` 若抛 `DAILY_LIMIT_EXCEEDED` → `release` + `_defer`（顺延到下一个自然日同一墙上时间）。这是"到点发现超线就顺延"。
- 不复检的：余额（不足就 `failed`，不会等）、收款人是否仍存在/是否被删除、卡是否被冻结（资金路径根本不看卡）、`factor_verified_ms` 是否已过期（没有过期规则）、商户是否仍在。
- `_defer` 还带 `MAX_DEFERRALS=7`（`scheduled.py:52`）上限，超了置 `failed` 并写审计 `scheduled.deferred`（`gave_up`）（`:299-344`）——这个设计是对的。

### 5.5 余额不足 ⚠️

- 预约执行：余额不足 → `InsufficientFunds` → 落 `status='failed'`（`scheduled.py:274-281`），**不顺延、不降额、不通知**（通知见 5.11）。
- 储蓄目标（proactive）：`app/proactive.py:332-436` `compute_savings_plan` 会算 `feasibility(feasible|infeasible|achieved|expired|unknown)` + `period_gap/period_surplus/total_gap` + warnings + assumptions。**只算，不动钱**（`app/proactive.py` 模块头 `:11-13` 明写不转账/不申购/不改余额）。

### 5.6 用户取消 ✅（但仅限"这一轮还没跑完"）

- `app/planner/executor.py:162-175` `cancel`：释放本会话全部 `held`、忙节点标 `skipped`、`session.end` 审计带 `released_holds`。
- 预约的取消另有路径（`app/tools/scheduled.py:167-190`），语义正确（见 1.9）。
- 局限：取消只对**正在进行的那一轮**（内存 `RUNS`）生效；请求结束后没有"撤销"对象。

### 5.7 重复执行 ⚠️

- 同一轮内：确认幂等（`executor.py:151-152`）、hold 幂等（`funds.py:137-143`）、commit 幂等（`funds.py:213-214`）、`_bump_daily_limit` 原子（`funds.py:528-575`）——这些都有测试（`tests/test_funds_invariants.py:149/172/188`）。
- 跨轮/跨进程：没有业务幂等键，`execute_due` 没有原子 claim（见 1.7）→ **多实例下同一笔预约可能被执行两次**。

### 5.8 重试 ❌

- `app/planner/ticker.py:60-67` `tick_forever`：异常只 `log`，下一拍继续——这是"循环不退出"，不是"业务重试"。
- 转账失败 → 静默过期（1.8）；预约失败 → `failed` 终态，无重试；申购 `settle_error` → 无重试。**全仓没有任何重试队列/退避/死信。**

### 5.9 服务重启恢复 ❌

- 内存态三件套（`RUNS`、`clarify._PENDING`、`Confirmation.future`）全部不落盘/不可重建：`app/planner/state.py:76`、`app/planner/clarify.py:48`、`app/planner/state.py:78-93/234`。
- 重启后：进行中的多步计划消失（`/api/session` 只读看得见，不能续跑）；待确认的卡永远 `pending`（`confirmations` 表有行，但没有 worker 去 `expire` 它——`_timeout` 只在请求内 `asyncio.wait_for` 超时时执行）。
- 能扛重启的只有：`scheduled_transfers`（`scheduled` 状态 + ticker 重新扫描）与 `savings_goals/proactive_alerts`（纯数据）。

### 5.10 授权过期 ⚠️

- 请求内：`TOKEN_TTL_MS=120_000` + `CONFIRM_TIMEOUT_MS=120_000`（`state.py:41/35`），超时释放 hold——正确。
- **跨时间：预约转账的授权永不过期**。`schedule_transfer` 接受最长 366 天后的预约（`scheduled.py:49`），执行时自签令牌（`scheduled.py:240-242`），`factor_verified_ms` 只记录不校验。**一个月前做过的（自称的）人脸+短信，会在一个月后再次生效**，且用户不会收到任何再确认请求。
  > 2026-10-24 更新：本条已修。`AUTH_TTL_MS = 30 天`，过期不再自签红灯令牌，改为退回 yellow 走当日额度并顺延（`defer_count+1`），同时写 `scheduled.auth_expired`；同日补上 `claim_token/claimed_ms` 领取租约（`CLAIM_LEASE_MS = 10 分钟`）。证据：`tests/test_scheduled_lease.py` 7 条。「多因子仍是自称」这一半另见 `app/security/factors.py`（服务端下发/校验，但渠道仍是桩）。

### 5.11 结果通知 ⚠️

- 请求内：SSE 事件流（`prompt_event`/`done`/`error` 等，`executor.py` 各处 `emit_event`）。
- 请求外：**预约执行完成/失败、顺延、放弃，没有任何通知通道**（无邮件/短信/推送/站内信）。`scheduled.py:274-281` 只 `log.warning`。
- proactive 有一层"通知"：`proactive_alerts`（`app/db.py:302-311`，`status open|ignored|resolved`、`evidence_json`），由前端面板拉取（`web/index.html:1310`）。但它只在**用户手动触发监测**时才产生（见 5.13）。

### 5.12 审计 ✅

- 计划层：`dag.built`（`open_run`，`state.py:154-189`）、`policy.decided`、`token.issued`、`transfer.*`、`session.end` 全进哈希链（`app/audit.py:24-60`）。
- proactive 层：`savings_goal.created/updated`、`proactive.monitor_run`、`proactive.alert_created/resolved`（`app/proactive.py:733-994, 1319-1392`），且失败分支先落 `monitor_runs(failed)` 再独立落审计。
- 缺口：**`run_monitor` 的审计 `trigger` 字段硬编码为 `"manual"`**（`app/proactive.py:1341`、`:1386` 两处）。将来真接了定时器，审计也无法区分"用户手动跑"与"系统自动跑"——这会直接毁掉"自动监测是否失效"的追责能力。

### 5.13 关键判定：**这是"单会话内串行 DAG + 手动触发的监测"，不是"长期财务计划"**

证据链（四条，互相独立）：

1. **计划活不过一次请求**：`RUNS` 内存字典（`state.py:76`）+ `finish_run` 里 `RUNS.pop`（`state.py:192-214`）+ `require_run` 只查内存（`state.py:326-330`）。
2. **图只有链**：`dag.py:228-242` 每个节点只依赖前一个；`INTENT_TEMPLATES` 全是线性 2–4 步（`dag.py:43-71`）。
3. **唯一的定时器不服务业务计划**：`ticker.py` 只调 `scheduled.execute_due`（`ticker.py:47-57`），注释自述"不自己判时间、不自己算钱"（`:12-13`）。
4. **监测明确是手动的**：`app/tools/proactive.py:9-10` 原文——"「手动触发」是设计约束：`run_proactive_monitor` 由用户或前端显式调用，没有任何定时器或后台循环挂在它后面（定时接入不是本阶段的范围）。" 代码印证：`app/proactive.py:1223-1403` `run_monitor` 无调用方，只有 `app/api/routes.py:543-551` `/api/proactive/monitor/run` 与工具 `run_proactive_monitor`（`tools/proactive.py:229`）两个手动入口。

所以准确的表述是：**"一次性、串行、请求内"的任务编排 + "用户点一下才算一次"的财务体检**。它没有"计划"（plan）作为一等对象：没有"计划表"、没有"计划状态机"、没有"计划的下一步何时该发生"、没有跨天推进。`savings_goals` 是**目标**不是**计划**——它存 `target_fen/deadline/rules`，由 `compute_savings_plan` 在**被调用时**现场算，没有任何东西负责"到期时算一次并提醒"。

### 5.14 最高价值剩余缺口（按性价比排序）

1. **把"计划"变成持久对象**（优先级最高）：新建 `plans(plan_id, status, next_step_at)`，让 `RUNS` 可从 DB 重建，`require_run` 支持 `resume_from_db`。否则 5.2/5.9 永远是假的。
2. **把 proactive 接到 ticker 上**（一天一次即可）：ticker 增加 `run_monitor(trigger="schedule")` 分支，同时把 `app/proactive.py:1341/1386` 的硬编码 `"manual"` 改成真实 `trigger`。这一步就能把"主动财务"从 demo 变成产品。
3. **预约授权时效**：给 `scheduled_transfers` 加 `auth_expires_ms`，执行前校验；过期则转"待用户再确认"而不是自签执行（5.10）。
4. **失败可见性**：预约失败必须写审计 + 生成 `proactive_alert`（现成的表），否则用户永远不知道钱没转出去（1.8/5.11）。
5. **原子 claim**：`execute_due` 的 `SELECT` 改为 `UPDATE ... WHERE status='scheduled' RETURNING`（或 `UPDATE` 后按 `rowcount` 取），解决多实例重复执行（5.7）。
6. **活跃计划的"下一步"语义**：现在没有"等 3 天后再检查"这种状态，一切延迟只能表达为"某天转一笔钱"。

---

## 6. 汇总表

判定说明：**"已实现且有测试证明"** 指有专门测试文件/用例断言其行为；**"已实现但测试不足"** 指有代码但只被间接覆盖或只测了 happy path；**"只有前端/接口"** 指后端只提供只读展示或接口，无执行语义；**"尚未实现"** 指无代码；**"受环境限制未验证"** 指代码在，但依赖本环境不存在的外部系统。

| 业务域 | 已实现且有测试证明 | 已实现但测试不足 | 只有前端/接口 | 尚未实现 | 受环境限制未验证 |
|---|---|---|---|---|---|
| **1 转账** | 收款人消歧（`tests/test_payee_discriminator.py:64-75`、`tests/test_clarify_resume.py`、`tests/test_phone_payee.py`、`tests/test_relation_payee.py`）；令牌七步与 TTL（`tests/test_token.py` 23 例、`tests/test_confirm_timeout.py:161`）；确认幂等（`tests/test_planner.py:696/725`）；hold/commit/并发日线（`tests/test_funds_invariants.py:149/172/188/229/266/286/304/334/348`）；端到端两阶段（`tests/test_e2e_transfer.py:145-243`、`tests/test_tools_write.py:39-135`）；冲正（`tests/test_reverse.py:36-72`）；预约（`tests/test_scheduled.py:35-87`、`tests/test_scheduled_fires.py:208-408`）；审计链（`tests/test_audit.py`、`tests/test_planner.py:873`） | 单笔 1000 元边界（规则在 `rules.py:343`，只有 `tests/test_policy.py` 的边界表，无独立断言文件）；红牌绕过日线（无测试断言"红牌不受日线"）；预约月付 +30 天漂移（无测试） | — | 账户/卡状态参与出账（`accounts` 无状态列，`db.py:63-71`；`FROM cards` 不在资金路径）；`per_tx_limit_fen` 对转账生效；真实多因子校验（`state.py:340-345` 只做字符串包含）；追回/退汇；失败重试 worker | 真实人脸/短信/OTP 通道；真实银行核心（本系统只改本地 `accounts.balance_fen`） |
| **2 订阅与代扣** | 查询与总额（`tests/test_tools.py:371/402`）；取消落库与审计（`tests/test_tools_write.py:155-181`）；消歧不替用户选（`tests/test_subscription_cancel.py:144-284` 9 例） | 取消后"下次扣费不会再发生"这句承诺无测试（它是文案） | — | 从流水推断潜在订阅；取消商户会员；阻止未来商户扣款；续费提醒；商户协议号/代扣授权档案 | 真实商户系统（无接口可调，`subs.py:114` 的承诺无法验证也无法兑现） |
| **3 账单分析** | 三条规则的形状与阈值（`tests/test_tools.py:233/254/270/284/296/310`，每条规则 2 处断言） | 误报场景无断言（"分期/AA/时差/港澳台正常消费"不触发）；时区依赖（`bill.py:58-60` `time.localtime`）无测试 | 规则元数据（`severity/confidence/possible_false_positive/actions`）只在 `app/api/business.py:65-102`，前端零调用、测试零引用 | 分级输出（发现异常/建议核实/已确认）在 `bill.py` 层；规则的自助处置动作（冻结卡、取消订阅）联动 | 真实境外/凌晨的地域判定（`OVERSEAS_MARKERS` 是硬编码字符串表） |
| **4 理财** | 申赎资金与份额/审计链（`tests/test_invest.py:82-248` 12 例、`tests/test_invest_intent_dag.py:241-304`）；起购与超额赎回拒绝；红牌 face+otp（`tests/test_invest.py:248`） | 风险提示四条 warning（`products.py:162-172`）只测了部分（`tests/test_invest.py:98`） | 风险等级/建议是原型标签（`products.py:36-42/175-182`），前端无任何风险提示卡 | **适当性评估**（全仓 0 命中）；期限/流动性/费用字段；净值行情源；冷静期/风险揭示书；赎回 T+N | 实时净值（`business.py:795` 自述不接行情） |
| **5 跨场景计划** | 串行 DAG 的构建/校验/回退（`tests/test_planner.py:426-585`）；`redeem_then_transfer` 打通（`tests/test_invest_intent_dag.py:241-304`）；取消释放（`tests/test_planner.py:752`）；proactive 计算（`tests/test_proactive_calc.py` 26 例）；proactive 闭环与不动钱（`tests/test_proactive_api.py:199-415`、`tests/test_proactive_safety.py:199-441`、`tests/test_security_proactive.py:241-730`） | 预约执行的并发 claim（无测试，单进程外不安全）；`trigger="manual"` 硬编码（无测试发现它）；中途重启/续跑（无测试、无实现） | `/api/business/plans`、`/api/business/executions`（`business.py:1032-1072`）只读展示计划与执行，前端不调、测试不测；`/api/session`（`routes.py:358-397`）只读回看，不能续跑 | **持久化计划对象**（无 `plans` 表）；跨天/延迟推进；条件复检；重试；结果通知；定时 proactive 监测（`tools/proactive.py:9-10` 明写手动）；授权跨期失效 | 多实例部署下的定时器一致性 |

补充两条跨域结论（不在表内，但决定对外表述的边界）：

- **凡是"状态机"（卡、预约、确认、hold）的实现质量都高于"业务语义"**：状态迁移有守卫、有幂等、有测试；而"这笔业务在真实世界意味着什么"几乎没建模。
- **诚实文案存在但不可达**：`app/api/business.py` 是全项目语义最准的文件，却没被前端调用、没被测试引用。**建议：要么让前端消费它，要么把它的关键措辞搬进工具层 `message`**——否则"我们知道边界在哪"这件事对用户和评委都是隐形的。

---

## 7. 三个最严重的语义问题（按危害排序）

### 问题一：工具层文案与真实能力相反，而模型只读工具层

模型根据 `tool_specs.py` 与工具返回值里的 `message` 组织回答，这两处正好是全项目最不诚实的文本：

- `app/tools/subs.py:114`："已经取消，下次扣费不会再发生。"（实际只把本地 `subs.status` 改成 `cancelled`，`subs.py:70-73`）
- `app/tools/tool_specs.py:539`："冲正一笔已经转出去的钱，把钱退回原账户。"（实际是本地反向记账，`transfers.py:216-280`）
- `app/tools/transfers.py:278` / `invest.py:39-40`："已经把 X 元退回你的账户。"/"赎回成功，钱已经回到你的卡里。"（后者暗示即时到账）
- `app/tools/transfers.py:56` / `invest.py:41`："稍后我会重试。"（**没有任何重试实现**，hold 300 秒后静默过期）

同一件事在 `app/api/business.py:826/839/851` 里写的是正确的，但那层无人调用（`web/index.html:513-518, 1309-1312` 零调用；`tests/` 零引用）。

**后果**：用户被告知"商户不会再扣款""钱退回来了""会重试"，而系统没有一项能力兑现。这不是措辞问题，是**承诺与执行能力脱钩**，也是这类演示最容易在答辩现场被一句话问穿的地方。

### 问题二：红灯多因子是"客户端自称"，安全分级的安全性是装饰

`app/planner/state.py:340-345` `factors_ok` 只把 `factor` 字符串拆集合求子集；`factor` 由 `POST /api/confirm` 的调用方自己传（`app/api/routes.py:339-349`），`app/planner/executor.py:156-157` 直接采信。没有人脸回执、没有验证码校验、没有失败计数与锁定。

**后果**：`R09` 的 `face+sms+otp`、`R10` 的 `face+otp` 在语义上等价于"请求体里多写两个字段"。分级 → 令牌 → 资金这条传导链是真的（有测试），但"高权限需要更强认证"这个业务命题**没有被实现**，只有它的形状。

### 问题三：冲正被表述成"撤回/取消的替代方案"，且"已执行"与"已追回"混为一谈

机制上，本项目**正确地**区分了三态（`scheduled_transfers.status` 的取消、`transactions.reversal_of/reversed_by` 的冲正、追回则完全不存在），但呈现层把它们糊成一件事：

- `app/tools/scheduled.py:182`：取消已执行的预约时指向冲正——"需要的话我可以帮你冲正"，暗示结果等价于取消。
- `app/tools/transfers.py:278`："已经把 X 元退回你的账户。"
- 而没有一处告诉用户：**对方账户里的钱不会因此减少**，本系统只在自己库里记一笔反向流水（`app/funds.py:238-299`），且 7 天后连冲正都不许（`transfers.py:47`，`TXN_TOO_OLD` 提示去柜面）。

**后果**：这是三条里唯一可能造成真实资金误解的一条——用户会以为"转错了也没关系，点一下冲正就回来了"。真实银行里，转账一旦落地，撤回取决于收款行配合，本原型没有这条路径，也没有任何地方声明这一点（除了没人看的 `business.py:851`）。

> 附带（不算"最严重"，但必须在评审前补）：**预约执行失败不写审计**（`scheduled.py:274-281`），而 `scheduled.deferred` 却写了——最需要留痕的"钱没扣成"是审计盲区；以及 **`run_monitor` 的 `trigger` 硬编码 `"manual"`**（`proactive.py:1341/1386`），一旦接入定时器，审计将无法区分自动与手动，等于自毁追溯能力。
