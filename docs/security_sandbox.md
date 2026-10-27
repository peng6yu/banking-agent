# 安全沙箱说明：为什么本系统不执行模型生成的代码

> 对应赛题条款：§3.2「生成的代码在受限、可监控的逻辑隔离环境中运行」。
> 本文是架构性论证 + 可复核证据，所有数字都来自同仓库脚本，可当场重跑。
> 证据脚本：scripts/prove_no_exec_surface.py；证据测试：tests/test_no_exec_surface.py。

## 0. 结论先行

1. 主 Agent 不执行模型生成的代码，也不接受任何代码字符串作为输入。
2. 模型输出只落在三个结构化位置：意图、槽位、工具名 + 参数；三者都要过白名单（工具表 + validate_params），然后由服务端自己调用固定函数。
3. 因此 app/ 下不存在代码执行面：AST 扫描 75 个 .py / 25383 行，执行面命中 0。
4. 对 §3.2 的满足方式是「消除执行面」而不是「关进笼子」：没有 eval/exec/子进程/动态加载，就没有可被绕过的沙箱边界。这条选择同时把「沙箱是否可靠」变成一个可机器复核的事实，而不是一段需要被信任的实现。
5. 边界必须说清：这不是「沙箱做得很好」，是「不需要沙箱的架构 + 证明它确实不需要」。将来若要执行用户代码，必须引入独立沙箱（见 §7），不能把本文当成永久豁免。

## 1. 赛题条款与两种满足路径

§3.2 的字面要求是「生成的代码」「受限、可监控」「逻辑隔离环境」，它默认了一个前提：系统会执行某段代码。常规实现是沙箱：独立进程或容器 + 只读文件系统 + 断网 + rlimit + 超时 + 审计，再配 AST 白名单或 seccomp。

本仓库选择另一条路径：把「可执行的东西」从系统里拿掉。

| 路径 | 做法 | 攻击面 | 验收方式 |
| --- | --- | --- | --- |
| 沙箱 | 允许执行，靠隔离与限额兜底 | 隔离逃逸、资源耗尽、出网、策略绕过 | 跑攻击样例看是否逃逸 |
| 无执行面（本仓库） | 不允许执行，模型只提结构化申请 | 参数滥用、业务逻辑滥用（不同层） | 静态证明执行面为 0 + 白名单审计 |

选择理由（为什么不是偷懒）：

- 本任务的模型输出是「下一步做什么」，不是「一段程序」。把意图映射成工具调用本来就不需要图灵完备的执行环境；引入 eval 只会把可靠性和安全性同时变差。
- 沙箱的正确性很难被外部复核：评委只能相信你的隔离实现。而「app/ 下没有 eval/exec/子进程」是一条任何人跑一条命令就能验证的事实。
- 少一层执行面，就少一类「模型被注入后能做什么」的答案：注入最多让模型提出一个越权工具调用，而越权调用会被 R04/R11 与能力令牌拒掉，而不是被沙箱的资源限额挡住。

## 2. 在线调用链证据（模型输出到底去了哪里）

一条用户消息的完整路径，逐跳给出文件:行号：

1. 前端把文本发给 POST /api/chat —— app/api/routes.py:333（async def chat，:334）。请求体是 ChatRequest，字段只有 session/text 等，没有 code/script 字段（见 §4.3）。
2. 意图与槽位：app/planner/intent.py 把文本解析成 intent + slots（结构化数据）。
3. 建图：app/planner/dag.py 按 intent 生成 DAG，节点里的 tool 只能来自工具表。
4. 执行：app/planner/runner.py:57 调用 tools.execute(node.tool, params, token=token, ctx=ctx)。令牌只在黄/红裁决下签发（app/planner/runner.py:329 issue_token，注释写明「只有黄/红才签令牌；绿灯工具自己会 require_token 拒绝」）。
5. 调度：app/tools/registry.py:67 def execute(...) —— 顺序是 get_tool → validate_params → spec.handler(context, checked, token) → 审计 tool.executed。未知工具直接抛 ToolNotFound(reason=UNKNOWN_TOOL)，不做模糊匹配、不做动态导入。
6. handler：app/tools/tool_specs.py 里 35 个 ToolSpec 的 handler 全是静态点分引用（如 transfers.transfer、cards.report_card_loss），没有 lambda、没有字符串反射。
7. 能力令牌七步校验：app/security/token.py:265 verify → :281 _verify_or_reject，依次为两段结构（:253 _split）、HMAC 签名（:239 decode）、过期（:306）、nonce 一次性（:312）、工具一致、参数哈希一致、主体一致（账户/金额上限）。任何一步失败写 token.rejected 审计并抛 TokenMismatch / TokenExpired。
8. 审计：每一步都落 audit_events（tool.executed、confirm.approved、token.issued 等），这是「可监控」的落点：不是监控一段代码的运行，而是监控每一次工具调用的四要素（谁、哪个工具、什么参数、哪张令牌）。

关键点：整条链上没有任何一处把 str 交给解释器。模型能影响的最强对象是「工具名 + 参数字典」，而这两者都在白名单与类型/区间校验之内。

## 3. 审查方法：为什么是 AST 而不是 grep

grep 会同时误报和漏报：

- 误报：conn.execute 的 SQL 字符串、re.compile 的正则、tools.execute 都不是执行面；
- 漏报：getattr(builtins, 字符串) 与 from builtins import eval as e 不会出现 eval( 字样。

scripts/prove_no_exec_surface.py 因此对 app/ 下每个 .py 做 AST 遍历，按精确点分名判定，并自带 15 段植入样本做自检（漏报即退出码非 0）。三类命中分开报：

- 代码执行面：eval / exec / compile / __import__；os.system、os.popen、os.exec*、os.spawn*；subprocess.*；importlib.import_module/reload；pickle、marshal、ctypes、runpy、multiprocessing、pty、code、codeop、imp、shelve、commands 的导入或调用；asyncio.create_subprocess_*；getattr(builtins, 执行名)。
- 网络出网面：urllib.request、socket、ssl、http.client、requests、httpx、aiohttp 等。
- 动态 SQL：conn.execute 的首参是 f-string 或 + 拼接（见 §4.2）。

## 4. 审查结果（2026-10-24 扫描）

### 4.1 app/：执行面 0，出网面只有模型 API

扫描范围：75 个 .py / 25383 行（app/ 下全部 .py，测试断言扫描集合等于磁盘集合）。

- 代码执行面：0。
- 网络出网面：3 处，全在 app/llm.py —— app/llm.py:7 import urllib.request；app/llm.py:297 urllib.request.Request；app/llm.py:303 urllib.request.urlopen。这是赛题要求的模型调用，endpoint 来自环境变量，不是用户代码执行。测试把出网面钉死在这三个符号上，别处新增会红。
- getattr 动态属性 5 处，第二参全是字面量、只取数据字段，不取可调用对象：app/llm.py:155 fallback_targets、app/llm.py:220 retryable、app/planner/executor.py:411 patterns、app/security/injection.py:243 content、app/security/breaker.py:76 hasattr(model_dump)。
- 已知误报（保留说明，避免后人重蹈）：app/planner/kb_bridge.py:472 的 code.group(0) 里 code 是局部 re.Match 变量，不是内置 compile。前缀匹配会把这类名字误判，精确点分名才是对的。

### 4.2 动态 SQL：16 处，全部是内部常量或占位符生成，无用户文本进语法位

扫描口径：conn.execute / executemany / executescript 的首参是 f-string 或 + 拼接。数量与行号会随代码演进漂移（测试只冻结「必须被报告 + 必须被文档复核」，不冻结数字）。

| # | 位置 | 拼进去的是什么 | 复核结论 |
| --- | --- | --- | --- |
| 1 | app/views/money.py:87 | marks = len(clean) 个占位符 | 占位符个数，值走参数绑定（W4 从 app/api/business.py 搬来，逻辑未变） |
| 2 | app/db.py:596 | SAVEPOINT + savepoint | savepoint 名由内部计数器生成 |
| 3 | app/db.py:600 | ROLLBACK TO + savepoint | 同上，同一内部变量 |
| 4 | app/db.py:601 | RELEASE + savepoint | 同上 |
| 5 | app/db.py:603 | RELEASE + savepoint | 同上 |
| 6 | app/db.py:651 | PRAGMA table_info + table | table 来自模块级 _ADDED_COLUMNS 常量 |
| 7 | app/db.py:655 | ALTER TABLE + table + column + ddl | 三个值全部来自 _ADDED_COLUMNS 常量表 |
| 8 | app/planner/recover.py:52 | 逗号连 N 个占位符 | 占位符个数，值走参数绑定 |
| 9 | app/plans.py:302 | fields 全字面量拼接 | 无变量，等价于常量串 |
| 10 | app/proactive.py:1106 | 二选一常量片段（rule_id IS NULL / rule_id = 占位符） | 常量片段 |
| 11 | app/tools/products.py:97 | 列名常量（risk_level = ? / category = ?） | 列名是常量；category 值先过 CATEGORY_LABELS 白名单 |
| 12 | app/birthday.py:535 | 逗号连 N 个占位符（keys） | 只拼占位符个数，plan_id/keys 走参数绑定 |
| 13 | app/handoff_store.py:418 | 逗号连 N 个占位符（expect）+ 条件 UPDATE 的 sets 常量片段 | 同上；sets 只追加常量片段，值全部占位符（W4 从 app/handoffs.py 搬来，逻辑未变） |
| 14 | app/insights.py:_spend_since | 逗号连 N 个占位符（INTERNAL_CATEGORIES） | 只拼占位符个数，分类常量来自 app/money.py，金额与时间走参数绑定（原 app/messages.py 的同一段搬来） |
| 15 | app/insights.py:_payday_candidate | 逗号连 N 个占位符（INTERNAL_CATEGORIES） | 同上；只有一个 IN 子句，其它条件全是字面量与占位符 |
| 16 | app/views/transactions.py:128（另有 :71 _totals、:132 取页） | where 由 _where 拼出：账户/方向/分类三个等值占位符 + 时间区间两个占位符 + LIKE 两个占位符 + 需要时 1 个 IN 子句（INTERNAL_CATEGORIES 逗号占位符） | 只拼常量条件名与占位符个数；账户/方向/分类/时间/关键词全部走参数绑定，q 的通配符已在 _like 里转义并配 ESCAPE，用户文本永远不碰语法位 |

规则：新增动态 SQL 会让 tests/test_no_exec_surface.py 的 test_dynamic_sql_is_reported_and_documented 变红，直到本文补上对应文件的复核结论。这是有意为之——拼接是「未来可能把用户文本送进语法位」的唯一入口。

### 4.3 API 层不接受代码字符串

app/api/schemas.py 的 58 个字段里没有任何 code/script/command/shell/sql/exec 字段（AST 检查 + 正则断言）；app/api/routes.py 的 30 个路由全部落在 /api/* 或 /，没有接收代码文本的端点。前端 web/index.html 的渲染：唯一 innerHTML 在 :1306（审计行），所有后端值都过 escapeHtml（:1014-1022，转义 & < > 双引号 单引号）；其余走 textContent；没有 eval/new Function/srcdoc/javascript:/document.write。

## 5. 离线/测试脚本里的系统命令（照实列出，不在在线路径）

这些脚本是开发与验收脚手架，不是在线 Agent 的调用链：

- scripts/ui_journeys.py:528 subprocess.Popen 起 uvicorn（env 里 MOCK_MODE=false）：只有 j3 走真模型后端；:30 import subprocess。
- scripts/browser_smoke.py:248 subprocess.run 执行 python -m scripts.seed 重建演示库；:286 subprocess.Popen 起 uvicorn（MOCK_MODE=script）；:31 import subprocess。
- scripts/release_manifest.py:41 subprocess.run(args, cwd=ROOT, ...) 采集 git head 与测试计数等事实；:21 import subprocess。
- scripts/ui_screenshots.py:128 __import__("time") 取毫秒时间戳的动态导入写法；建议改为顶部 import time（不在本任务 scope，已报 Lead）。
- scripts/e2e.py 不用 subprocess。

边界：这些命令的入参全部是脚本内常量或 sys.executable，没有一处接受用户/模型输入；它们不 import app/ 的在线模块路径，也不被 app/ 反向调用。

## 6. 结构化测试：比「搜字符串」更强的那部分

tests/test_no_exec_surface.py 现有 14 个用例，分三层：

- 事实层：扫描范围等于磁盘上全部 app 下的 .py；执行面为 0；出网面只允许 app/llm.py 三个符号。
- 反证层：15 段植入样本必须被逐条报出（防「扫描器坏了也报 0」）；5 段良性同名调用（conn.execute、re.compile、tools.execute、asyncio.run(executor.run)、compile_report）必须不报（防误报逼着人加豁免）。
- 结构层（本次补强）：(1) tool_specs.py 的每个 handler= 必须是 Attribute 节点（模块.函数），不得是 Call/Lambda；运行时再查每个 handler 是 app.tools 下的普通函数、qualname 不含尖括号。(2) app/api/schemas.py 的字段名不得匹配 code/script/command/shell/sql/exec。(3) 动态 SQL 必须被报告，且每个出现文件都在本文有复核结论。(4) prove 脚本输出必须含「离线/测试脚本系统命令」清单与 scripts/ui_journeys.py。(5) 本文必须存在且含 3.2、零代码执行面、subprocess、两个证据文件名、不执行模型生成的代码。

跑法（真实命令与输出摘要见 §8）：

    cd /home/peng/peng_workspace/deepseek harness/银行agent/banking-agent
    .venv/bin/python -m pytest tests/test_no_exec_surface.py -q

## 7. 与 §3.2 三要素的逐句对应

| 条款要素 | 本仓库如何满足 | 证据 |
| --- | --- | --- |
| 「生成的代码」 | 系统不生成也不执行代码；模型只产出结构化意图、槽位、工具调用 | §2 调用链；AST 执行面 0 |
| 「受限」 | 工具白名单 35 个 + 参数类型/枚举/区间校验 + 能力令牌七步 + 金额/账户主体绑定 | registry.py:67、token.py:265 |
| 「可监控」 | 每次工具调用、裁决、令牌签发与拒绝、确认、因子校验都写 audit_events；前端可回放 | §2 第 8 步；J1/J2/J4/J5/J7 旅程断言审计 |
| 「逻辑隔离」 | 不需要「执行环境隔离」——没有执行环境；模型调用与业务进程通过 HTTP API 分离，业务进程不暴露任何执行入口 | §4.1 出网面 3 处全在 llm.py |

## 8. 复现与本次真实输出摘要

    cd /home/peng/peng_workspace/deepseek harness/银行agent/banking-agent
    .venv/bin/python scripts/prove_no_exec_surface.py

本次输出（摘要，2026-10-24 复核）：扫描范围 75 个 .py / 25383 行；命中（代码执行面）0；命中（网络出网面）3；命中（动态 SQL，需人工确认参数化）16；离线/测试脚本系统命令 9；扫描器自检 15/15；退出码 0。行号随代码演进而变，复核请以脚本当场输出为准。

tests/test_no_exec_surface.py 的真实 pytest 输出见给 Lead 的交付消息（14 passed）。

## 9. 剩余验收风险（不掩饰）

1. 「没有执行面」不等于「没有风险」。模型仍可通过参数与工具顺序造成业务影响（转错人、超额、重复支付）。那一层由 policy 裁决、确认卡片、能力令牌、幂等键兜底，属于业务风控，不是代码执行安全；两条线不能互相替代。
2. 评委可能期待「演示沙箱」。如果答辩要求现场跑一段模型生成的代码，本仓库没有这个能力，也不应临时加 eval 去迎合。正确回应是解释架构选择并出示本证明；把「没有执行面」当成缺陷去补齐，反而会引入真实风险。
3. 未来接入代码执行时的必做项（不在当前范围）：独立进程或容器 + 非 root + 只读根文件系统 + 默认断网 + CPU/内存/文件大小 rlimit + 墙钟超时 + 每次执行落审计 + 结果只回传 stdout 文本；绝不能在 app/ 主进程里 exec，也不能把沙箱放在与业务库同权限的账户下。
4. 动态 SQL 的长期维护：目前 16 处都是内部常量或占位符，但拼接写法本身是风险面。测试已把「新增未复核的拼接」变成红灯，但复核结论仍需人工更新，不能自动推断。
5. 前端转义依赖手写 escapeHtml：单点 innerHTML 已转义，但没有 CSP。若后续加 innerHTML，需同时加 CSP 或改用模板 + textContent。
6. 离线脚本用 subprocess 起服务、写演示库，只适合开发机；交付环境不应暴露这些脚本的 HTTP 端口（ui_journeys 起的 uvicorn 监听 127.0.0.1）。
