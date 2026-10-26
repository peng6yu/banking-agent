"""依赖方向铁律：api → planner → policy → tools → db，不得反向依赖。

docs/00 §0 的十条铁律里有一条「policy 不许 import llm，由 tests/test_imports.py 拦」，
docs/02 §2.4 要求每个新增文件登记进本表的允许依赖表。这里把那条铁律自动化：

1. 每个 app/ 下的模块都必须在本文件登记 —— 没登记就是没走 docs/02 §2.4 的三个动作，
   测试直接失败，逼着人补文档而不是绕过文档；
2. 表里的允许依赖必须与代码现状一致（既不许偷偷加依赖，也不许留着过期条目）；
3. 分层只允许「上层引用下层」，policy 不得碰 llm/tools，security 不得碰 planner，
   tools 不得碰 planner/api —— 越界即失败。

解析用 ast 静态读 import，不 import 任何模块，所以本文件本身不会触发循环导入。
"""

from __future__ import annotations

import ast
from functools import lru_cache
from pathlib import Path

import pytest

APP_DIR = Path(__file__).resolve().parents[1] / "app"

#: 分层序号：数字越小越底层。上层可以引用下层，反过来一律失败。
LAYERS: dict[str, int] = {
    "app": 0,
    "app.audit": 0,
    "app.clock": 0,
    "app.config": 0,
    "app.db": 0,
    "app.ids": 0,
    "app.log": 0,
    "app.models": 0,
    "app.money": 0,
    # 银行领域知识层（阶段 3）：只读 knowledge/*.yaml，只依赖时钟与自身数据结构
    "app.knowledge": 0,
    "app.knowledge.loader": 0,
    "app.knowledge.models": 0,
    "app.knowledge.normalize": 0,
    "app.knowledge.parsers": 0,
    "app.knowledge.retrieve": 0,
    # 协作回放桩（MOCK_MODE=script）：模型层的离线替身，和 app.llm 一样在最底层
    "app.script_mode": 0,
    "app.security.envelope": 0,
    # 提醒时间：纯日历计算（下次到点/文案），只依赖标准库
    "app.reminder_time": 0,
    "app.timefmt": 0,
    # 边界守门（W1-A）：只有标准库依赖的纯函数与常量，被 api/planner/memory_extract 共用
    "app.scope": 0,
    "app.security.api_auth": 1,
    "app.security.breaker": 1,
    "app.security.injection": 1,
    "app.security.token": 1,
    # 多因子挑战：服务端签发 + 校验 + 一次性，只依赖数据层
    "app.security.factors": 1,
    "app.funds": 2,
    # 账户/卡通道闸门：出账前最后一道，只读 accounts/cards
    "app.channels": 2,
    # 主动财务管理（阶段 5）：确定性计算 + 目标/规则/提醒仓储，只依赖数据层
    "app.proactive": 2,
    # 投资者适当性（阶段 6 P0-6）：5 题问卷 + 画像落库 + 申购前匹配，只依赖数据层
    "app.suitability": 2,
    # 订阅推断（阶段 6 P0-7）：从流水里找周期性扣款 + 续费窗口，纯读，只依赖金额格式化
    "app.subscriptions": 2,
    # 会话记忆：把每一轮对话落库，只依赖数据层（重启后接得上话）
    "app.chat_history": 1,
    # 长期记忆：跨会话保存的偏好/事实，与会话记忆同层（仓储 + 提示词块，不碰模型）
    "app.memory": 1,
    # 记忆抽取：调模型挑事实，落库仍走 app.memory；失败只降级，不参与业务判断
    "app.memory_extract": 1,
    # 跨场景计划（P1-9）：状态全在库里，后台节拍推进；action 步只提醒、不代扣
    "app.plans": 2,
    # 生日计划：建计划（账本级预算预留）+ 到期付款走确认/令牌，依赖规划层与工具白名单
    "app.birthday": 5,
    "app.policy.engine": 2,
    "app.policy.rules": 2,
    "app.tools.base": 3,
    "app.tools.bill": 3,
    "app.tools.cards": 3,
    "app.tools.invest": 3,
    "app.tools.products": 3,
    # 阶段 5：七个主动财务工具，只做计划/提醒，没有任何动钱入口
    "app.tools.proactive": 3,
    "app.tools.tool_specs": 3,
    "app.tools.queries": 3,
    "app.tools.registry": 3,
    "app.tools.scheduled": 3,
    "app.tools.split": 3,
    "app.tools.subs": 3,
    "app.tools.transfers": 3,
    "app.planner.dag": 4,
    "app.planner.events": 4,
    "app.planner.executor": 4,
    "app.planner.intent": 4,
    "app.planner.intent_schema": 4,
    "app.planner.runner": 4,
    "app.planner.state": 4,
    "app.planner.card_actions": 4,
    # 重启恢复：从库里重建 RunState 接着跑，依赖执行器与状态
    "app.planner.recover": 4,
    # 同一会话只允许一轮没走完的写操作：只读库表 + 允许的写工具清单，不碰执行
    "app.planner.conflict": 4,
    # 知识层接线（阶段 3）：把领域知识翻译成「问不问/填什么/怎么解释」，不碰策略
    "app.planner.kb_bridge": 4,
    "app.planner.payment_guard": 4,
    # 澄清后续答（2026-10-23 限制 3）：短回答接回上一轮原话，只依赖时钟
    "app.planner.clarify": 4,
    # 确定性拒答守卫（2026-10-23 红队收口）：只依赖 re/dataclass，纯函数
    "app.planner.refusal": 4,
    "app.planner.subscription": 4,
    "app.planner.due": 4,
    "app.planner.split_plan": 4,
    "app.planner.ticker": 4,
    # 识别即执行（提醒/储蓄）：不建 DAG、不动钱，只落计划与提醒
    "app.planner.assist": 4,
    "app.api.routes": 5,
    # 只读业务视图（2026-10-23 六区信息架构）：与路由同层，可复用路由的鉴权依赖
    "app.api.business": 5,
    # 人工接管队列：搬运行级事实并驱动 recover，属于编排层之上
    "app.handoffs": 5,
    # 接管队列的 HTTP 面：复用 routes 的鉴权依赖，不自己做鉴权
    "app.api.handoffs_api": 5,
    # 长期记忆的 HTTP 面：和接管队列同层，鉴权复用 routes
    "app.api.memory_api": 5,
    "app.api.schemas": 5,
    "app.main": 5,
    # 包本身没有逻辑，聚合再导出不算越层
    "app.api": 5,
    "app.planner": 4,
    "app.policy": 2,
    "app.security": 1,
    "app.tools": 3,
}

#: 存量反向依赖豁免表：(上层模块, 它反向 import 的模块) -> 为什么暂时留着。
#: 分层铁律没有例外，这张表只是「债」的登记簿：每条都写清消债阶段，还清就删。
#: tests/test_imports.py::test_upward_exceptions_are_still_real 会检查过期条目。
#: TODO(W4 视图归位 + 拆包)：本表应在 W4 收口时变空。
UPWARD_EXCEPTIONS: dict[tuple[str, str], str] = {
    ("app.planner.conflict", "app.handoffs"): (
        "冲突检测要读接管队列的行级状态。TODO(W4)：handoffs 拆成 domain/use_cases 后改依赖 use_cases，本行删除。"
    ),
    ("app.planner.runner", "app.handoffs"): (
        "跑偏时把执行权交给接管队列（函数内 lazy import 避开模块级环）。"
        "TODO(W4)：handoffs 拆出 domain 层后改依赖 use_cases，本行删除。"
    ),
    ("app.planner.ticker", "app.birthday"): (
        "节拍器顺带推进生日计划到期。TODO(W4)：生日到期推进下沉到 use_cases 后由 ticker 调 use_cases，本行删除。"
    ),
    ("app.plans", "app.birthday"): (
        "计划步会引用生日计划的预算预留。TODO(W4)：birthday 拆成 domain/use_cases 后改依赖 use_cases，本行删除。"
    ),
}

#: 模型层是独立叶子：只有这几处允许碰它（意图解析、API 装配、冒烟脚本）。
LLM_ALLOWED: frozenset[str] = frozenset(
    {"app.planner.intent", "app.api.routes", "app.main", "app.smoke", "app.memory_extract"}
)

#: 允许依赖表：模块 → 它可以 import 的 app 内模块（由代码现状反推，改代码就要改这张表）。
ALLOWED: dict[str, frozenset[str]] = {
    "app": frozenset(),
    # 边界守门：纯函数 + 常量，只有标准库依赖（被多方共用，所以必须待在 0 层）
    "app.scope": frozenset(),
    "app.api": frozenset(),
    "app.api.routes": frozenset({
        "app.api.schemas",
        "app.audit",
        "app.birthday",
        "app.clock",
        "app.config",
        "app.db",
        "app.ids",
        "app.llm",
        "app.money",
        "app.models",
        "app.planner",
        "app.planner.assist",
        "app.planner.clarify",
        "app.planner.dag",
        "app.planner.events",
        "app.planner.executor",
        "app.planner.intent",
        "app.planner.payment_guard",
        "app.planner.recover",
        "app.planner.ticker",
        "app.plans",
        # 长期记忆已接进聊天主链路（注入提示词 + 每轮结束后抽事实），由 lead 接线
        "app.memory",
        "app.memory_extract",
        "app.proactive",
        "app.scope",
        "app.security",
        "app.security.api_auth",
        "app.security.factors",
        "app.subscriptions",
        "app.suitability",
        "app.chat_history",
        # 缺失槽位的可点选项要按主账户取候选（amount_fen / payee_name …）
        "app.tools.base",
        "app.tools.registry",
        "app.tools.scheduled",
    }),
    "app.api.business": frozenset({
        "app.api.routes",
        "app.audit",
        "app.clock",
        "app.db",
        "app.funds",
        "app.handoffs",
        "app.money",
        "app.planner",
        "app.planner.assist",
        "app.planner.ticker",
        "app.plans",
        "app.proactive",
        "app.subscriptions",
        "app.suitability",
        "app.tools",
        "app.tools.base",
        "app.tools.bill",
        "app.tools.queries",
        "app.tools.scheduled",
        "app.timefmt",
    }),
    # 入参模型里 FactorProof 复用公共契约（多因子凭证与 /api/confirm 同构）
    "app.api.schemas": frozenset({"app.models"}),
    # 接管队列的 HTTP 面只依赖数据层与路由里的鉴权依赖
    "app.api.handoffs_api": frozenset({
        "app.api.routes",
        "app.db",
        "app.handoffs",
    }),
    # 长期记忆的 HTTP 面：数据层 + 记忆仓储 + 单用户口径 + 鉴权依赖
    "app.api.memory_api": frozenset({
        "app.api.routes",
        "app.db",
        "app.memory",
        "app.proactive",
    }),
    "app.audit": frozenset({
        "app.clock",
        "app.log",
    }),
    "app.clock": frozenset(),
    "app.config": frozenset(),
    "app.db": frozenset({
        "app.audit",
        "app.clock",
    }),
    "app.funds": frozenset({
        "app.audit",
        "app.channels",
        "app.clock",
        "app.db",
        "app.ids",
        "app.log",
        "app.models",
        "app.security",
        "app.security.token",
    }),
    "app.ids": frozenset({
        "app.clock",
    }),
    "app.knowledge": frozenset({
        "app.knowledge.loader",
        "app.knowledge.models",
        "app.knowledge.normalize",
        "app.knowledge.retrieve",
    }),
    "app.knowledge.loader": frozenset({
        "app.clock",
        "app.knowledge.models",
        "app.knowledge.parsers",
    }),
    "app.knowledge.models": frozenset(),
    "app.knowledge.normalize": frozenset({
        "app.knowledge.models",
    }),
    "app.knowledge.parsers": frozenset({
        "app.clock",
        "app.knowledge.models",
    }),
    "app.knowledge.retrieve": frozenset({
        "app.knowledge.models",
    }),
    "app.llm": frozenset({
        # 降级链要记 llm.fallback.switch 审计：切目标必须留痕
        "app.log",
        "app.script_mode",
    }),
    "app.log": frozenset({
        "app.clock",
    }),
    "app.main": frozenset({
        "app.api",
        "app.api.business",
        "app.api.handoffs_api",
        "app.api.memory_api",
        "app.api.routes",
        "app.clock",
        "app.db",
        "app.llm",
        "app.planner",
        "app.planner.ticker",
        "app.security",
        "app.security.api_auth",
    }),
    "app.models": frozenset(),
    "app.money": frozenset({
        "app.clock",
    }),
    "app.planner": frozenset(),
    "app.planner.due": frozenset({"app.clock"}),
    "app.planner.split_plan": frozenset({"app.clock", "app.ids", "app.models"}),
    "app.planner.ticker": frozenset({
        "app.birthday",
        "app.db",
        "app.plans",
        "app.proactive",
        "app.tools",
        "app.tools.scheduled",
    }),
    "app.planner.dag": frozenset({
        "app.audit",
        "app.clock",
        "app.db",
        "app.ids",
        "app.models",
        "app.planner.card_actions",
        "app.planner.due",
        "app.planner.intent",
        "app.planner.refusal",
        "app.planner.split_plan",
        "app.planner.subscription",
        "app.tools.base",
        "app.tools.registry",
    }),
    "app.planner.events": frozenset({
        "app.models",
        "app.money",
        "app.planner.state",
        "app.policy.engine",
        # 订阅停止（并行分支）在确认卡上要带能力边界文案 STOPPED_BOUNDARY_TEXT
        "app.subscriptions",
    }),
    "app.planner.executor": frozenset({
        "app.audit",
        "app.clock",
        "app.db",
        "app.funds",
        "app.models",
        "app.planner",
        "app.planner.conflict",
        "app.planner.dag",
        "app.planner.events",
        "app.planner.recover",
        "app.planner.runner",
        "app.planner.state",
        "app.policy.engine",
        "app.policy.rules",
        "app.security",
        "app.security.breaker",
        "app.security.factors",
        "app.security.injection",
        "app.tools",
        "app.tools.base",
    }),
    "app.planner.intent": frozenset({
        "app.llm",
        "app.money",
        "app.planner",
        "app.planner.card_actions",
        "app.planner.intent_schema",
        "app.planner.kb_bridge",
        "app.planner.payment_guard",
        "app.planner.refusal",
        "app.security",
        "app.security.envelope",
        "app.security.injection",
    }),
    "app.planner.refusal": frozenset(),
    "app.planner.intent_schema": frozenset(),
    "app.planner.runner": frozenset({
        "app.audit",
        "app.clock",
        "app.funds",
        "app.handoffs",
        "app.models",
        "app.planner",
        "app.planner.clarify",
        "app.planner.dag",
        "app.planner.events",
        "app.planner.state",
        "app.policy.engine",
        "app.policy.rules",
        "app.security",
        "app.security.token",
        "app.tools",
        "app.tools.base",
        "app.tools.registry",
    }),
    # 识别即执行：只读账户、建计划/提醒，钱一律走原有的确认与令牌路径
    "app.planner.assist": frozenset({
        "app.audit",
        "app.clock",
        "app.money",
        "app.plans",
        "app.proactive",
        "app.reminder_time",
        "app.scope",
        # 逐笔核实订阅要读该商户的每一笔流水（只读，推断仍在 subscriptions 里）
        "app.subscriptions",
        # 提醒类话术要带上「这个人是谁」（称呼/偏好），只读记忆，不改记忆
        "app.memory",
        "app.timefmt",
    }),
    "app.planner.state": frozenset({
        "app.audit",
        "app.clock",
        "app.db",
        "app.funds",
        "app.ids",
        "app.models",
        "app.money",
        "app.security",
        "app.security.breaker",
        "app.tools",
        "app.tools.base",
    }),
    "app.planner.kb_bridge": frozenset({
        "app.knowledge",
        "app.knowledge.models",
        "app.knowledge.normalize",
        "app.knowledge.retrieve",
    }),
    "app.planner.payment_guard": frozenset(),
    # 冲突闸门：查库、写一条 run.conflict 审计、读工具清单，不驱动任何执行
    "app.planner.conflict": frozenset({
        "app.audit",
        "app.clock",
        "app.db",
        "app.handoffs",
        "app.models",
        "app.tools.registry",
        "app.timefmt",
    }),
    # 重启恢复：只重建状态、接着驱动，不重新解析意图也不重新裁决
    "app.planner.recover": frozenset({
        "app.audit",
        "app.clock",
        "app.db",
        "app.models",
        "app.planner.executor",
        "app.planner.state",
        "app.security",
        "app.security.factors",
        "app.security.token",
        # 账户要按运行时的同一条规则补回来（primary_account）：
        # 老库没有 meta_json，硬用空串会让令牌必然 ACCOUNT_MISMATCH
        "app.tools.base",
    }),
    "app.planner.card_actions": frozenset({
        "app.clock",
    }),
    "app.planner.clarify": frozenset({
        "app.clock",
    }),
    "app.planner.subscription": frozenset({
        "app.clock",
        "app.planner.intent_schema",
    }),
    "app.policy": frozenset({
        "app.policy.engine",
        "app.policy.rules",
    }),
    "app.proactive": frozenset({
        "app.audit",
        "app.clock",
        "app.db",
        "app.ids",
        "app.log",
        "app.money",
    }),
    "app.policy.engine": frozenset({
        "app.clock",
        "app.models",
        "app.money",
        "app.policy.rules",
    }),
    "app.policy.rules": frozenset({
        "app.models",
        "app.money",
    }),
    # 提醒时间：纯函数，没有 app 内依赖
    "app.reminder_time": frozenset(),
    "app.timefmt": frozenset(),  # 毫秒 -> 本地时间串：只依赖 time，不查库
    "app.script_mode": frozenset({
        "app.money",
    }),
    "app.security": frozenset(),
    "app.security.breaker": frozenset({
        "app.audit",
        "app.clock",
        "app.ids",
        "app.log",
    }),
    "app.security.envelope": frozenset({
        "app.clock",
    }),
    "app.security.api_auth": frozenset(),
    # 多因子挑战：签发/校验只碰审计、时钟、ID 生成
    "app.security.factors": frozenset({
        "app.audit",
        "app.clock",
        "app.ids",
    }),
    "app.security.injection": frozenset({
        "app.clock",
        "app.log",
        "app.models",
    }),
    "app.security.token": frozenset({
        "app.audit",
        "app.clock",
        "app.ids",
        "app.log",
        "app.models",
    }),
    # 通道闸门：账户/卡状态与单笔限额，只依赖时钟的错误类型
    "app.channels": frozenset({
        "app.clock",
    }),
    # 适当性：问卷与匹配是纯函数，落库只依赖 audit；错误类型用 app.clock
    "app.suitability": frozenset({
        "app.audit",
        "app.clock",
    }),
    # 订阅推断：只读流水 + 用户的定性结论（写这张表要审计，错误类型用 clock）
    "app.plans": frozenset({
        "app.audit",
        "app.birthday",
        "app.clock",
        "app.db",
        "app.ids",
        "app.proactive",
        "app.reminder_time",
        "app.subscriptions",
        "app.timefmt",
    }),
    # 生日计划：钱走 executor（确认/令牌），商户白名单复用工具层常量
    "app.birthday": frozenset({
        "app.audit",
        "app.clock",
        "app.db",
        "app.funds",
        "app.ids",
        "app.money",
        "app.planner",
        "app.planner.dag",
        "app.planner.executor",
        "app.plans",
        # 到期执行要按当下重新裁决、并复用工具的 action 构造
        "app.policy.engine",
        "app.policy.rules",
        "app.tools.base",
        "app.tools.transfers",
        "app.timefmt",
    }),
    # 人工接管：状态机只搬库里的事实；恢复交给 planner.recover（延迟导入）
    "app.handoffs": frozenset({
        "app.audit",
        "app.clock",
        "app.db",
        "app.funds",
        "app.ids",
        "app.planner",
        "app.planner.recover",
        "app.planner.runner",
    }),
    "app.subscriptions": frozenset({
        "app.audit",
        "app.clock",
        "app.db",
        "app.money",
        "app.timefmt",
    }),
    # 长期记忆：落库 + 审计（from app import audit）+ 时间，不碰模型、不碰业务
    "app.memory": frozenset({
        "app.audit",
        "app.clock",
        "app.db",
    }),
    # 抽取层：调模型抽事实，写入仍走 app.memory，异常只记日志
    "app.memory_extract": frozenset({
        "app.clock",
        "app.llm",
        "app.log",
        "app.memory",
        "app.scope",
    }),
    # 会话记忆：只读写 chat_turns/sessions，不碰工具、不碰权限
    "app.chat_history": frozenset({
        "app.clock",
        "app.db",
    }),
    "app.smoke": frozenset(),
    "app.tools": frozenset({
        "app.tools.base",
        "app.tools.registry",
    }),
    "app.tools.base": frozenset({
        "app.clock",
        "app.funds",
        "app.models",
        "app.policy.rules",
        "app.security",
        "app.security.token",
    }),
    "app.tools.bill": frozenset({
        "app.clock",
        "app.models",
        "app.money",
        "app.tools.base",
        "app.tools.queries",
        "app.timefmt",
    }),
    "app.tools.cards": frozenset({
        "app.audit",
        "app.db",
        "app.ids",
        "app.models",
        "app.money",
        "app.tools.base",
    }),
    "app.tools.invest": frozenset({
        "app.audit",
        "app.clock",
        "app.db",
        "app.funds",
        "app.log",
        "app.models",
        "app.money",
        "app.suitability",
        "app.tools.base",
        "app.tools.products",
    }),
    "app.tools.products": frozenset({
        "app.clock",
        "app.models",
        "app.money",
        "app.tools.base",
    }),
    "app.tools.proactive": frozenset({
        "app.models",
        "app.proactive",
        "app.tools.base",
    }),
    "app.tools.queries": frozenset({
        "app.clock",
        "app.models",
        "app.money",
        "app.tools.base",
    }),
    "app.tools.registry": frozenset({
        "app.audit",
        "app.clock",
        "app.db",
        "app.log",
        "app.models",
        "app.tools.base",
        "app.tools.tool_specs",
    }),
    "app.tools.scheduled": frozenset({
        "app.audit",
        "app.clock",
        "app.db",
        "app.funds",
        "app.ids",
        "app.models",
        "app.money",
        # 到点执行前按用户维度重新裁决：预约不许绕过当天的分级
        "app.policy",
        "app.policy.rules",
        "app.security",
        "app.security.factors",
        "app.security.token",
        "app.tools.base",
        "app.timefmt",
    }),
    "app.tools.split": frozenset({
        "app.clock",
        "app.funds",
        "app.models",
        "app.money",
        "app.tools.base",
    }),
    "app.tools.subs": frozenset({
        "app.audit",
        "app.clock",
        "app.db",
        "app.models",
        "app.money",
        "app.subscriptions",
        "app.tools.base",
    }),
    "app.tools.tool_specs": frozenset({
        "app.models",
        "app.money",
        "app.tools.base",
        "app.tools.bill",
        "app.tools.cards",
        "app.tools.invest",
        "app.tools.proactive",
        "app.tools.products",
        "app.tools.queries",
        "app.tools.scheduled",
        "app.tools.split",
        "app.tools.subs",
        "app.tools.transfers",
    }),
    "app.tools.transfers": frozenset({
        "app.clock",
        "app.funds",
        "app.models",
        "app.money",
        "app.security",
        "app.security.token",
        "app.tools.base",
    }),
}


def _module_name(path: Path) -> str:
    parts = path.relative_to(APP_DIR).with_suffix("").parts
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(("app", *parts)) if parts else "app"


def _exists(dotted: str) -> bool:
    """dotted 是 app 内的完整模块路径（如 app.audit），判断它在 app/ 下真的存在。

    注意：APP_DIR 已经指向仓库里的 app/，所以拼路径前必须先剥掉前缀 "app"，
    否则 app.audit 会被拼成 app/app/audit、永远不存在 —— 那会让 "from app import audit"
    这类写法永远解析不出子模块，整张分层表形同虚设（2026-10-26 修）。
    """
    parts = dotted.split(".")
    if parts and parts[0] == "app":
        parts = parts[1:]
    if not parts:  # 裸包名（app 本身），没有子模块可判
        return True
    rel = Path(*parts)
    return (APP_DIR / rel).with_suffix(".py").exists() or (APP_DIR / rel / "__init__.py").exists()


def _imports_of(path: Path) -> frozenset[str]:
    """静态读出该文件 import 的全部 app 内模块；不执行 import，所以不会触发循环导入。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    deps: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("app"):
                    deps.add(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("app"):
            base = node.module
            deps.add(base)
            for alias in node.names:
                child = f"{base}.{alias.name}"
                if _exists(child):
                    deps.add(child)
    deps.discard("app")  # 裸包名没有约束力
    return frozenset(deps)


@lru_cache(maxsize=1)
def _actual() -> dict[str, frozenset[str]]:
    """全量扫一遍 app/（结果缓存：本文件有 ~170 个参数化用例，别重复解析 84 个模块）。"""
    return {
        _module_name(path): _imports_of(path)
        for path in sorted(APP_DIR.rglob("*.py"))
    }


def _rank(module: str) -> int:
    """取最长匹配前缀的分层序号；没登记的模块按根包算 0（会被登记测试挡下）。"""
    best, best_len = 0, -1
    for known, level in LAYERS.items():
        if (module == known or module.startswith(known + ".")) and len(known) > best_len:
            best, best_len = level, len(known)
    return best


def test_every_app_module_is_registered() -> None:
    """新增文件必须同时登记进 LAYERS 与 ALLOWED（docs/02 §2.4 的三个动作之一）。"""
    actual = set(_actual())
    missing = sorted(actual - set(ALLOWED))
    assert not missing, f"这些模块没登记进 tests/test_imports.py 的允许依赖表：{missing}"
    stale = sorted(set(ALLOWED) - actual)
    assert not stale, f"允许依赖表里有文件已经不存在的条目：{stale}"


@pytest.mark.parametrize("module", sorted(ALLOWED))
def test_imports_stay_inside_the_allowed_table(module: str) -> None:
    """表内允许的照旧，超出表的一律失败（加依赖要先改表、改文档）。"""
    actual = _actual()[module]
    extra = sorted(actual - ALLOWED[module])
    assert not extra, f"{module} 新增了未登记的依赖：{extra}"
    missing = sorted(ALLOWED[module] - actual)
    assert not missing, f"允许依赖表里列了但代码里没有：{module} → {missing}（清掉过期条目）"


@pytest.mark.parametrize("module", sorted(ALLOWED))
def test_layers_never_depend_upward(module: str) -> None:
    """分层只允许上层引用下层：底层模块不许 import 更高层的模块。

    存量债（4 条 lazy import）逐条登记在 UPWARD_EXCEPTIONS 并写明消债阶段；
    新增一条反向依赖就会失败 —— 别往豁免表里加，先想能不能下沉。
    """
    own = _rank(module)
    upward = sorted(dep for dep in _actual()[module] if _rank(dep) > own)
    new = [dep for dep in upward if (module, dep) not in UPWARD_EXCEPTIONS]
    assert not new, (
        f"{module}（第 {own} 层）反向依赖了更高层：{new}。"
        "分层铁律只允许上层引用下层（docs/00 §0）。"
        "修法：把这段逻辑下沉成纯函数/参数注入，或改成事件/回调；"
        "确属 W4 才拆的存量债才进 UPWARD_EXCEPTIONS，并写清消债阶段。"
    )


def test_upward_exceptions_are_still_real() -> None:
    """豁免表是债的登记簿：债还清了就要删，过期条目会掩盖新的越层。"""
    actual = _actual()
    stale = sorted(
        f"{module} → {dep}"
        for (module, dep) in UPWARD_EXCEPTIONS
        if dep not in actual.get(module, frozenset()) or _rank(dep) <= _rank(module)
    )
    assert not stale, (
        f"这些豁免已经没有对应的反向依赖（或层级已变）：{stale}。"
        "请从 tests/test_imports.py 的 UPWARD_EXCEPTIONS 删掉，让表继续变空。"
    )


def test_policy_layer_never_imports_llm_or_tools() -> None:
    """docs/00 铁律：策略层是纯函数层，不许碰模型与工具。"""
    for module, deps in _actual().items():
        if not module.startswith(("app.policy", "app.policy.")):
            continue
        assert not deps & {"app.llm", "app.tools", "app.tools.registry"}, f"{module} 越界"


def test_security_layer_never_depends_on_planner() -> None:
    """安全层不许知道规划层存在，否则权限判定会被业务编排反向污染。"""
    for module, deps in _actual().items():
        if not module.startswith(("app.security", "app.security.")):
            continue
        assert not deps & {"app.planner", "app.planner.executor", "app.planner.runner"}, module
        assert not deps & {"app.api", "app.api.routes", "app.tools", "app.llm"}, module


def test_tools_never_depend_on_planner_or_api() -> None:
    """工具层只认令牌与参数，不认意图与 HTTP。"""
    for module, deps in _actual().items():
        if not module.startswith(("app.tools", "app.tools.")):
            continue
        banned = {d for d in deps if d.startswith(("app.planner", "app.api"))}
        assert not banned, f"{module} 反向依赖 {sorted(banned)}"
        assert "app.llm" not in deps, f"{module} 不该碰模型层"


def test_planner_never_depends_on_api() -> None:
    """规划层是接口层的下游，反过来 import 会形成环。"""
    for module, deps in _actual().items():
        if not module.startswith(("app.planner", "app.planner.")):
            continue
        assert not deps & {"app.api", "app.api.routes", "app.main"}, module


def test_data_layer_never_depends_on_business_layers() -> None:
    """数据层（clock/db/ids/money/models/audit/log/config）不认识业务。"""
    business = ("app.policy", "app.tools", "app.planner", "app.api", "app.security", "app.funds")
    for module, deps in _actual().items():
        if module not in {"app", "app.audit", "app.clock", "app.config", "app.db",
                          "app.ids", "app.log", "app.models", "app.money"}:
            continue
        bad = sorted(d for d in deps if d.startswith(business))
        assert not bad, f"{module} 引入了业务层依赖 {bad}"


def test_llm_layer_is_a_leaf() -> None:
    """模型层只在意图解析、API 装配与冒烟脚本里出现。"""
    users = sorted(m for m, deps in _actual().items() if "app.llm" in deps)
    assert set(users) <= set(LLM_ALLOWED), f"这些模块不该直接调模型：{users}"
