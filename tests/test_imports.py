"""依赖方向铁律：api → planner → policy → tools → db，不得反向依赖。

docs/00 §0 的十二条铁律里有一条「policy 不许 import llm，由 tests/test_imports.py 拦」，
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
    # 计划生命周期钩子注册表（W4 依赖倒置）：plans/ticker 调它，birthday 向它登记；
    # 只依赖 logging 与 typing，所以必须在最底层——它不能反过来认识任何业务模块
    "app.plan_hooks": 1,
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
    # W4 把 1320 行的 routes.py 按「一次请求关心什么」拆成 9 个模块：
    # 装配（routes）、口令（auth）、状态（chat_state）、候选（chat_hints）、
    # 取证（chat_trace）、主流程（chat_flow）、聊天端点（chat_api）、
    # 基础端点（core_api）、计划端点（plans_api）、钱端点（money_api）。
    # 拆开以后每片的依赖面都变小了：哪片碰了模型/执行器一眼看得出来。
    "app.api.routes": 5,
    "app.api.auth": 5,
    "app.api.chat_state": 5,
    "app.api.chat_hints": 5,
    "app.api.chat_trace": 5,
    "app.api.chat_flow": 5,
    "app.api.chat_api": 5,
    "app.api.core_api": 5,
    "app.api.plans_api": 5,
    "app.api.money_api": 5,
    # 只读业务视图（2026-10-23 六区信息架构）：与路由同层，可复用路由的鉴权依赖
    "app.api.business": 5,
    # 只读视图装配（W4 从 business.py 拆出）：与路由同层，只查库+算+拼，不裁决、不写库；
    # 拆包以后每张卡片一个文件，改卡片不必动路由，也不许顺手在视图里加判断。
    "app.views": 5,
    "app.views.labels": 5,
    "app.views.money": 5,
    "app.views.queue": 5,
    "app.views.overview": 5,
    "app.views.bill": 5,
    "app.views.management": 5,
    "app.views.plans": 5,
    "app.views.executions": 5,
    # 人工接管单的仓储与状态机（W4 拆出）：只读写 handoffs 表与快照，不认识执行器
    "app.handoff_store": 2,
    # 人工接管队列的恢复/取消：搬运行级事实并驱动 recover，属于编排层之上
    "app.handoffs": 5,
    # 接管队列的 HTTP 面：复用 routes 的鉴权依赖，不自己做鉴权
    "app.api.handoffs_api": 5,
    # 长期记忆的 HTTP 面：和接管队列同层，鉴权复用 routes
    "app.api.memory_api": 5,
    # 助手主动提醒：探测器只读账务表 + 频控落库，不认识 planner/tools（同存储级）
    "app.messages": 2,
    # 提醒的事实层（探测器 + 洞察目录）：只读账务表，不碰频控与落库
    "app.insights": 2,
    "app.views.messages": 5,
    "app.api.messages_api": 5,
    "app.api.schemas": 5,
    "app.main": 5,
    # 包本身没有逻辑，聚合再导出不算越层
    "app.api": 5,
    "app.planner": 4,
    "app.policy": 2,
    "app.security": 1,
    "app.tools": 3,
    # 用例层（W3）：写操作的唯一入口——开事务、把领域异常翻成 Outcome，路由只管传话。
    # 与 api 同为 5 层：生日付款用例要复用执行器（app.birthday 本身就在 5 层）。
    "app.use_cases": 5,
    "app.use_cases.confirmations": 5,
    "app.use_cases.base": 5,
    "app.use_cases.demo": 5,
    "app.use_cases.handoffs": 5,
    "app.use_cases.investments": 5,
    "app.use_cases.memory": 5,
    "app.use_cases.messages": 5,
    "app.use_cases.plans": 5,
    "app.use_cases.proactive": 5,
    "app.use_cases.reminders": 5,
    "app.use_cases.savings": 5,
    "app.use_cases.scheduled": 5,
    "app.use_cases.subscriptions": 5,
    "app.use_cases.transactions": 5,
    "app.api.http": 5,
    # 路由的最后一米：Outcome -> 响应体，失败就把原始领域异常原样抛回异常映射。
    "app.api.http": 5,
}

#: 存量反向依赖豁免表：(上层模块, 它反向 import 的模块) -> 为什么暂时留着。
#: 分层铁律没有例外，这张表只是「债」的登记簿：每条都写清消债阶段，还清就删。
#: tests/test_imports.py::test_upward_exceptions_are_still_real 会检查过期条目。
#: **W4 已清零**：四条存量反向依赖全部消掉（birthday 两条改为 plan_hooks 注册表，
#: handoffs 两条改为 handoff_store 第 2 层仓储）。这张表留着是为了以后真有债务时
#: 有地方登记，但正常状态下必须是空的——test_upward_exceptions_are_still_real 会
#: 在「登记了但已经不成立」时失败，逼着还债的人删行。
UPWARD_EXCEPTIONS: dict[tuple[str, str], str] = {}

#: 模型层是独立叶子：只有这几处允许碰它（意图解析、API 装配、冒烟脚本）。
LLM_ALLOWED: frozenset[str] = frozenset(
    {
        "app.planner.intent",
        # W4：碰模型的只剩聊天主流程（解析）与状态模块（Adapter 类型），装配层不碰
        "app.api.chat_flow",
        "app.api.chat_state",
        "app.main",
        "app.smoke",
        "app.memory_extract",
    }
)

def _deps(names: str) -> frozenset[str]:
    """把「空格分隔的模块名」变成允许依赖集合；空串表示只依赖标准库。

    一格一行的写法让这张表随模块数量线性增长，而不是每加一个依赖就多三行。
    """
    return frozenset(names.split())


#: 允许依赖表：模块 → 它允许 import 的 app 内模块（空格分隔，空串=只有标准库）。
#: 每格上面的注释写「为什么允许这些依赖」；改代码就要同时改这张表。
ALLOWED: dict[str, frozenset[str]] = {
    "app": _deps(""),
    # 边界守门：纯函数 + 常量，只有标准库依赖（被多方共用，所以必须待在 0 层）
    "app.scope": _deps(""),
    "app.api": _deps(""),
    # 装配层：只认得 9 个端点模块，自己不 import 任何业务能力（这就是拆分要的结果）。
    "app.api.routes": _deps("app.api app.api.auth app.api.chat_api app.api.chat_flow app.api.chat_hints app.api.chat_state app.api.chat_trace app.api.core_api app.api.money_api app.api.plans_api"),
    # 口令校验：只认安全层的签发与比对，连库都不碰
    "app.api.auth": _deps("app.clock app.security app.security.api_auth"),
    # 进程内状态：历史环、适配器类型、SSE 帧；只有模型层是为了 Adapter 类型
    "app.api.chat_state": _deps("app.llm"),
    # 缺槽位候选：读工具元数据与提醒/计划现状，纯拼装，不写库
    "app.api.chat_hints": _deps("app.audit app.clock app.db app.planner app.planner.assist app.planner.executor app.planner.intent app.planner.payment_guard app.planner.recover app.planner.ticker app.proactive app.tools.base"),
    # 取证与记忆：审计、兜底写入、记忆回读，全部只写审计与记忆
    "app.api.chat_trace": _deps("app.api.chat_state app.api.http app.audit app.chat_history app.clock app.config app.db app.ids app.planner app.planner.assist app.planner.executor app.planner.intent app.planner.payment_guard app.planner.recover app.planner.ticker"),
    # 聊天主流程：解析→澄清→建图→执行，唯一该碰模型与执行器的端点模块
    "app.api.chat_flow": _deps("app.api.chat_hints app.api.chat_state app.api.chat_trace app.audit app.chat_history app.clock app.db app.ids app.llm app.memory app.memory_extract app.planner app.planner.assist app.planner.clarify app.planner.dag app.planner.executor app.planner.intent app.planner.payment_guard app.planner.recover app.planner.ticker app.proactive app.scope"),
    # 聊天端点：只把请求转给主流程，外加会话回看与确认卡
    "app.api.chat_api": _deps("app.api.chat_flow app.api.chat_state app.api.chat_trace app.api.http app.api.schemas app.audit app.chat_history app.clock app.db app.models app.money app.planner app.planner.assist app.planner.clarify app.planner.events app.planner.executor app.planner.intent app.planner.payment_guard app.planner.recover app.planner.ticker app.timefmt app.use_cases app.views.labels"),
    # 基础端点：健康、工具清单、审计链、根路径
    "app.api.core_api": _deps("app.api.chat_trace app.audit app.clock app.db app.planner app.planner.assist app.planner.executor app.planner.intent app.planner.payment_guard app.planner.recover app.planner.ticker app.timefmt app.tools.registry"),
    # 计划端点：计划/生日/预约/提醒，写操作只经用例层
    "app.api.plans_api": _deps("app.api.http app.api.schemas app.audit app.birthday app.clock app.db app.planner app.planner.assist app.planner.executor app.planner.intent app.planner.payment_guard app.planner.recover app.planner.ticker app.plans app.proactive app.use_cases"),
    # 钱端点：储蓄/订阅/理财/预警/手动记账/重置，写操作只经用例层
    "app.api.money_api": _deps("app.api.chat_state app.api.http app.api.schemas app.audit app.clock app.db app.proactive app.use_cases"),
    # W4 之后 business.py 只剩路由：鉴权 + 开只读连接 + 调 app.views
    "app.api.business": _deps("app.api.routes app.db app.proactive app.views app.views.labels"),
    # 视图包本身只有再导出，不认识业务
    "app.views": _deps("app.views.bill app.views.executions app.views.management app.views.overview app.views.plans"),
    # 纯常量：状态→中文说法、异常规则、快捷入口，只有标准库
    "app.views.labels": _deps(""),
    # 金额与流水积木：账户、账单、趋势。读工具层的只读查询，不执行任何动作
    "app.views.money": _deps("app.funds app.money app.proactive app.timefmt app.tools app.tools.base app.tools.bill app.tools.queries app.views.labels"),
    # 待办与队列积木：待确认卡、接管单、预约、提醒、监测。只读既有事实，不产生新裁决
    "app.views.queue": _deps("app.clock app.handoffs app.money app.planner app.planner.ticker app.proactive app.timefmt app.tools app.tools.scheduled app.views.labels"),
    "app.views.overview": _deps("app.clock app.planner app.planner.assist app.proactive app.timefmt app.tools.base app.views.labels app.views.money app.views.queue"),
    "app.views.bill": _deps("app.clock app.proactive app.timefmt app.tools app.tools.base app.tools.bill app.tools.queries app.views.labels app.views.money"),
    "app.views.management": _deps("app.clock app.money app.subscriptions app.suitability app.timefmt app.tools.base app.views.money app.views.queue"),
    "app.views.plans": _deps("app.clock app.planner app.planner.assist app.plans app.subscriptions app.timefmt app.tools.base app.views.labels app.views.queue"),
    "app.views.executions": _deps("app.audit app.clock app.timefmt app.views.labels app.views.queue"),
    # 入参模型里 FactorProof 复用公共契约（多因子凭证与 /api/confirm 同构）
    "app.api.schemas": _deps("app.models"),
    # 接管队列的 HTTP 面只依赖数据层与路由里的鉴权依赖
    "app.api.handoffs_api": _deps("app.api.http app.api.routes app.db app.handoffs app.use_cases app.use_cases.handoffs"),
    # 长期记忆的 HTTP 面：数据层 + 记忆仓储 + 单用户口径 + 鉴权依赖
    "app.api.memory_api": _deps("app.api.http app.api.routes app.db app.memory app.proactive app.use_cases"),
    "app.api.messages_api": _deps("app.api.http app.api.routes app.db app.use_cases app.use_cases.messages app.views.messages"),
    "app.messages": _deps("app.audit app.clock app.db app.ids app.insights"),
    "app.insights": _deps("app.money"),
    "app.views.messages": _deps("app.messages app.timefmt"),
    "app.audit": _deps("app.clock app.log"),
    "app.clock": _deps(""),
    "app.config": _deps(""),
    "app.db": _deps("app.audit app.clock"),
    "app.funds": _deps("app.audit app.channels app.clock app.db app.ids app.log app.models app.security app.security.token"),
    "app.ids": _deps("app.clock"),
    "app.knowledge": _deps("app.knowledge.loader app.knowledge.models app.knowledge.normalize app.knowledge.retrieve"),
    "app.knowledge.loader": _deps("app.clock app.knowledge.models app.knowledge.parsers"),
    "app.knowledge.models": _deps(""),
    "app.knowledge.normalize": _deps("app.knowledge.models"),
    "app.knowledge.parsers": _deps("app.clock app.knowledge.models"),
    "app.knowledge.retrieve": _deps("app.knowledge.models"),
    "app.llm": _deps("app.log app.script_mode"),
    "app.log": _deps("app.clock"),
    "app.main": _deps("app.api app.api.business app.api.handoffs_api app.api.memory_api app.api.messages_api app.api.routes app.clock app.db app.llm app.planner app.planner.ticker app.security app.security.api_auth"),
    "app.models": _deps(""),
    "app.money": _deps("app.clock"),
    "app.planner": _deps(""),
    "app.planner.due": _deps("app.clock"),
    "app.planner.split_plan": _deps("app.clock app.ids app.models"),
    "app.planner.ticker": _deps("app.db app.plan_hooks app.plans app.proactive app.tools app.tools.scheduled"),
    "app.planner.dag": _deps("app.audit app.clock app.db app.ids app.models app.planner.card_actions app.planner.due app.planner.intent app.planner.refusal app.planner.split_plan app.planner.subscription app.tools.base app.tools.registry"),
    "app.planner.events": _deps("app.models app.money app.planner.state app.policy.engine app.subscriptions"),
    "app.planner.executor": _deps("app.audit app.clock app.db app.funds app.models app.planner app.planner.conflict app.planner.dag app.planner.events app.planner.recover app.planner.runner app.planner.state app.policy.engine app.policy.rules app.security app.security.breaker app.security.factors app.security.injection app.tools app.tools.base"),
    "app.planner.intent": _deps("app.llm app.money app.planner app.planner.card_actions app.planner.intent_schema app.planner.kb_bridge app.planner.payment_guard app.planner.refusal app.security app.security.envelope app.security.injection"),
    "app.planner.refusal": _deps(""),
    "app.planner.intent_schema": _deps(""),
    "app.planner.runner": _deps("app.audit app.clock app.funds app.handoff_store app.models app.planner app.planner.clarify app.planner.dag app.planner.events app.planner.state app.policy.engine app.policy.rules app.security app.security.token app.tools app.tools.base app.tools.registry"),
    # 识别即执行：只读账户、建计划/提醒，钱一律走原有的确认与令牌路径
    "app.planner.assist": _deps("app.audit app.clock app.memory app.money app.plans app.proactive app.reminder_time app.scope app.subscriptions app.timefmt"),
    "app.planner.state": _deps("app.audit app.clock app.db app.funds app.ids app.models app.money app.security app.security.breaker app.tools app.tools.base"),
    "app.planner.kb_bridge": _deps("app.knowledge app.knowledge.models app.knowledge.normalize app.knowledge.retrieve"),
    "app.planner.payment_guard": _deps(""),
    # 冲突闸门：查库、写一条 run.conflict 审计、读工具清单，不驱动任何执行
    "app.planner.conflict": _deps("app.audit app.clock app.db app.handoff_store app.models app.timefmt app.tools.registry"),
    # 重启恢复：只重建状态、接着驱动，不重新解析意图也不重新裁决
    "app.planner.recover": _deps("app.audit app.clock app.db app.models app.planner.executor app.planner.state app.security app.security.factors app.security.token app.tools.base"),
    "app.planner.card_actions": _deps("app.clock"),
    "app.planner.clarify": _deps("app.clock"),
    "app.planner.subscription": _deps("app.clock app.planner.intent_schema"),
    "app.policy": _deps("app.policy.engine app.policy.rules"),
    "app.proactive": _deps("app.audit app.clock app.db app.ids app.log app.money app.timefmt"),
    "app.policy.engine": _deps("app.clock app.models app.money app.policy.rules"),
    "app.policy.rules": _deps("app.models app.money"),
    # 提醒时间：纯函数，没有 app 内依赖
    "app.reminder_time": _deps(""),
    "app.plan_hooks": _deps(""),
    "app.timefmt": _deps(""),
    "app.script_mode": _deps("app.money"),
    "app.security": _deps(""),
    "app.security.breaker": _deps("app.audit app.clock app.ids app.log"),
    "app.security.envelope": _deps("app.clock"),
    "app.security.api_auth": _deps(""),
    # 多因子挑战：签发/校验只碰审计、时钟、ID 生成
    "app.security.factors": _deps("app.audit app.clock app.ids"),
    "app.security.injection": _deps("app.clock app.log app.models"),
    "app.security.token": _deps("app.audit app.clock app.ids app.log app.models"),
    # 通道闸门：账户/卡状态与单笔限额，只依赖时钟的错误类型
    "app.channels": _deps("app.clock"),
    # 适当性：问卷与匹配是纯函数，落库只依赖 audit；错误类型用 app.clock
    "app.suitability": _deps("app.audit app.clock"),
    # 订阅推断：只读流水 + 用户的定性结论（写这张表要审计，错误类型用 clock）
    "app.plans": _deps("app.audit app.clock app.db app.ids app.plan_hooks app.proactive app.reminder_time app.subscriptions app.timefmt"),
    # 生日计划：钱走 executor（确认/令牌），商户白名单复用工具层常量
    "app.birthday": _deps("app.audit app.clock app.db app.funds app.ids app.money app.plan_hooks app.planner app.planner.dag app.planner.executor app.plans app.policy.engine app.policy.rules app.timefmt app.tools.base app.tools.transfers"),
    # 人工接管单仓储：条件 UPDATE + 审计 + 快照，只依赖数据层
    "app.handoff_store": _deps("app.audit app.clock app.db app.ids"),
    # 人工接管（恢复/取消）：状态机在 handoff_store，恢复交给 planner.recover（延迟导入）
    "app.handoffs": _deps("app.audit app.clock app.db app.funds app.handoff_store app.planner app.planner.recover app.planner.runner"),
    "app.subscriptions": _deps("app.audit app.clock app.db app.money app.timefmt"),
    # 长期记忆：落库 + 审计（from app import audit）+ 时间，不碰模型、不碰业务
    "app.memory": _deps("app.audit app.clock app.db"),
    # 抽取层：调模型抽事实，写入仍走 app.memory，异常只记日志
    "app.memory_extract": _deps("app.clock app.llm app.log app.memory app.scope"),
    # 会话记忆：只读写 chat_turns/sessions，不碰工具、不碰权限
    "app.chat_history": _deps("app.clock app.db app.timefmt"),
    "app.smoke": _deps(""),
    "app.tools": _deps("app.tools.base app.tools.registry"),
    "app.tools.base": _deps("app.clock app.funds app.models app.policy.rules app.security app.security.token"),
    "app.tools.bill": _deps("app.clock app.models app.money app.timefmt app.tools.base app.tools.queries"),
    "app.tools.cards": _deps("app.audit app.db app.ids app.models app.money app.tools.base"),
    "app.tools.invest": _deps("app.audit app.clock app.db app.funds app.log app.models app.money app.suitability app.tools.base app.tools.products"),
    "app.tools.products": _deps("app.clock app.models app.money app.tools.base"),
    "app.tools.proactive": _deps("app.models app.proactive app.tools.base"),
    "app.tools.queries": _deps("app.clock app.models app.money app.tools.base"),
    "app.tools.registry": _deps("app.audit app.clock app.db app.log app.models app.tools.base app.tools.tool_specs"),
    "app.tools.scheduled": _deps("app.audit app.clock app.db app.funds app.ids app.models app.money app.policy app.policy.rules app.security app.security.factors app.security.token app.timefmt app.tools.base"),
    "app.tools.split": _deps("app.clock app.funds app.models app.money app.tools.base"),
    "app.tools.subs": _deps("app.audit app.clock app.db app.models app.money app.subscriptions app.tools.base"),
    "app.tools.tool_specs": _deps("app.models app.money app.tools.base app.tools.bill app.tools.cards app.tools.invest app.tools.proactive app.tools.products app.tools.queries app.tools.scheduled app.tools.split app.tools.subs app.tools.transfers"),
    "app.tools.transfers": _deps("app.clock app.funds app.models app.money app.security app.security.token app.tools.base"),
    # W3 用例层：写操作的唯一入口。用例只收基本类型、不 import app.api.schemas，
    # 所以依赖永远单向（api -> use_cases -> 领域），领域异常统一收成 Outcome。
    "app.use_cases": _deps("app.use_cases app.use_cases.base app.use_cases.confirmations app.use_cases.demo app.use_cases.handoffs app.use_cases.investments app.use_cases.memory app.use_cases.plans app.use_cases.proactive app.use_cases.reminders app.use_cases.savings app.use_cases.scheduled app.use_cases.subscriptions app.use_cases.transactions"),
    "app.use_cases.base": _deps("app.clock app.db"),
    # 多因子挑战的签发（写：库里签发 challenge 行）；放行依据始终在服务端
    "app.use_cases.confirmations": _deps("app.clock app.security.factors app.use_cases.base"),
    "app.use_cases.demo": _deps("app.clock app.db app.planner app.planner.executor app.use_cases.base"),
    "app.use_cases.handoffs": _deps("app.clock app.db app.handoffs app.use_cases.base"),
    "app.use_cases.investments": _deps("app.proactive app.suitability app.use_cases.base"),
    "app.use_cases.memory": _deps("app.memory app.proactive app.use_cases.base"),
    "app.use_cases.messages": _deps("app.db app.messages app.use_cases.base app.views.messages"),
        # 手动推进到点计划：run_due 自己开连接（生日付款经线程池另开连接，外层事务会锁库）
    "app.use_cases.plans": _deps("app.birthday app.clock app.db app.plans app.proactive app.use_cases.base"),
    "app.use_cases.proactive": _deps("app.clock app.proactive app.use_cases.base"),
    "app.use_cases.reminders": _deps("app.planner app.planner.assist app.proactive app.use_cases.base"),
    "app.use_cases.savings": _deps("app.clock app.proactive app.use_cases.base"),
    "app.use_cases.scheduled": _deps("app.tools.scheduled app.use_cases.base"),
    "app.use_cases.subscriptions": _deps("app.proactive app.subscriptions app.use_cases.base"),
    "app.use_cases.transactions": _deps("app.audit app.clock app.ids app.money app.proactive app.use_cases.base"),
    "app.api.http": _deps("app.use_cases.base"),
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