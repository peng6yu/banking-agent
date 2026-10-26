"""工具权威表：35 个工具的名字、参数模式、处理器与写标记。

对应文档：docs/06_工具层与场景实现规格.md 6.1/6.2。

docs/06 6.1 的 10 个基础工具按原顺序在前，场景三新增的四个理财工具、场景四新增的
五个卡片管理工具依次追加在后。名字是权威表，TOOL_NAMES 会拿去做回归测试；
不在表里的工具名就是 ToolNotFound。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Final

# 模块级导入（不用 from 包 import 模块）：tests/test_imports.py 用 ast 按模块名登记依赖，
# 包级导入会被它漏掉，允许依赖表就会与代码现状对不上。
import app.tools.bill as bill  # noqa: PLR0402
import app.tools.cards as cards  # noqa: PLR0402
import app.tools.invest as invest  # noqa: PLR0402
import app.tools.proactive as proactive_tools  # noqa: PLR0402
import app.tools.products as products  # noqa: PLR0402
import app.tools.queries as queries  # noqa: PLR0402
import app.tools.scheduled as scheduled  # noqa: PLR0402
import app.tools.split as split_tools  # noqa: PLR0402
import app.tools.subs as subs  # noqa: PLR0402
import app.tools.transfers as transfers  # noqa: PLR0402
from app.models import ToolResult
from app.money import MAX_SINGLE_FEN
from app.tools.base import ToolContext

_AMOUNT_FEN: Final[dict[str, Any]] = {
    "type": "integer",
    "minimum": 1,
    "maximum": MAX_SINGLE_FEN,
    "description": "金额，单位分（1 元 = 100 分），必须是最小的整数分",
}
_PRODUCT_CODE: Final[dict[str, Any]] = {
    "type": "string",
    "minLength": 2,
    "maxLength": 32,
    "description": "基金产品代码，用 list_products 查到的 code 原样传回来",
}
_TAIL: Final[dict[str, Any]] = {
    "type": "string",
    "minLength": 4,
    "maxLength": 4,
    "description": "卡号后四位；不填就是默认账户",
}
_CARD_TAIL: Final[dict[str, Any]] = {
    "type": "string",
    "minLength": 4,
    "maxLength": 4,
    "description": "要操作的那张卡的卡号后四位。先用 list_cards 查出来，不要猜。",
}
_CARD_LIMIT_FEN: Final[dict[str, Any]] = {
    "type": "integer",
    "minimum": 1,
    "maximum": 5_000_000,
    "description": "单笔限额，单位分（1 元 = 100 分），上限 500 万分即 5 万元",
}

# 阶段 5：储蓄目标的金额与日期。日期是日历日 YYYY-MM-DD，不用时间戳——
# 目标「到 2026-12-31」是账面上的日期，用 UTC 小时推日会在跨时区时差一天。
MAX_TARGET_FEN: Final[int] = 100_000_000_00
_TARGET_FEN: Final[dict[str, Any]] = {
    "type": "integer",
    "minimum": 1,
    "maximum": MAX_TARGET_FEN,
    "description": "目标金额，单位分（1 元 = 100 分），上限 1 亿元",
}
_TARGET_DATE: Final[dict[str, Any]] = {
    "type": "string",
    "minLength": 10,
    "maxLength": 10,
    "pattern": r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$",
    "description": "目标日期，格式 YYYY-MM-DD，例如 2026-12-31",
}


@dataclass(frozen=True)
class ToolSpec:
    """一个工具的注册项：模型看得到的部分 + 内部处理器。"""

    name: str
    description: str
    params: dict[str, Any]
    handler: Callable[[ToolContext, dict[str, Any], str | None], ToolResult]
    writes: bool

    def schema(self) -> dict[str, Any]:
        """喂给模型的 JSON Schema，不带 handler，避免把内部实现暴露出去。"""
        return {"name": self.name, "description": self.description, "params": self.params}


TOOL_SPECS: Final[tuple[ToolSpec, ...]] = (
    ToolSpec(
        name="query_balance",
        description="查账户余额，金额单位是分。不传尾号就返回名下全部账户，主账户排第一个。",
        params={
            "type": "object",
            "properties": {"account_tail": _TAIL},
            "required": [],
            "additionalProperties": False,
        },
        handler=queries.query_balance,
        writes=False,
    ),
    ToolSpec(
        name="query_transactions",
        description="查最近的收支流水，支持时间天数、分类、金额区间和条数。查不到就返回空集合。",
        params={
            "type": "object",
            "properties": {
                "days": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 400,
                    "description": "回看天数，默认 30；超过 365 按 365 截断",
                },
                "category": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 12,
                    # 不写 enum：分类非法时工具要回 BAD_CATEGORY 并列出合法分类，
                    # 这是 docs/06 6.3 的分支，不该在校验层就变成 ValidationError
                    "description": "只看某一个分类，合法值：" + "、".join(queries.CATEGORIES),
                },
                "min_fen": {"type": "integer", "minimum": 0, "description": "金额下限（分）"},
                "max_fen": {"type": "integer", "minimum": 0, "description": "金额上限（分）"},
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": queries.MAX_LIMIT,
                    "description": "最多回几条，默认 20，最多 100",
                },
            },
            "required": [],
            "additionalProperties": False,
        },
        handler=queries.query_transactions,
        writes=False,
    ),
    ToolSpec(
        name="analyze_bill",
        description=(
            "按月或按年分析账单，汇总分类、商户 Top，"
            "并检出拆单、凌晨境外、订阅涨价三类异常。"
        ),
        params={
            "type": "object",
            "properties": {
                "period": {"type": "string", "enum": ["month", "year"], "description": "分析粒度"},
                "year": {"type": "integer", "minimum": 1970, "maximum": 2999},
                "month": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 12,
                    "description": "period=month 时必填",
                },
            },
            "required": ["period", "year"],
            "additionalProperties": False,
        },
        handler=bill.analyze_bill,
        writes=False,
    ),
    ToolSpec(
        name="list_subs",
        description="列出生效中的订阅扣款，含金额、周期和下次扣费日期。",
        params={
            "type": "object",
            "properties": {
                "include_cancelled": {
                    "type": "boolean",
                    "description": "是否把已取消的也列出来，默认只列生效中的",
                }
            },
            "required": [],
            "additionalProperties": False,
        },
        handler=subs.list_subs,
        writes=False,
    ),
    ToolSpec(
        name="list_cards",
        description="列出名下银行卡，含实体卡/虚拟卡、状态与单笔限额。",
        params={
            "type": "object",
            "properties": {"account_tail": _TAIL},
            "required": [],
            "additionalProperties": False,
        },
        handler=cards.list_cards,
        writes=False,
    ),
    ToolSpec(
        name="resolve_payee",
        description="按姓名、备注名或手机号找收款人；同名多个时带上卡尾号可以区分。",
        params={
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 50,
                    "description": "姓名、备注名或手机号，例如 李静 / 老妈 / 13800138000",
                },
                "phone": {
                    "type": "string",
                    "minLength": 4,
                    "maxLength": 20,
                    "description": "用户单独说了手机号就带上，可以是完整号码或后四位",
                },
                "card_tail": {
                    "type": "string",
                    "pattern": "^[0-9]{4}$",
                    "description": "用户说了卡尾号就带上，用来区分同名收款人",
                },
            },
            "required": ["name"],
            "additionalProperties": False,
        },
        handler=queries.resolve_payee,
        writes=False,
    ),
    ToolSpec(
        name="transfer",
        description="向通讯录收款人转账。第一阶段只建预授权，钱未扣款，等确认后才真正转出。",
        params={
            "type": "object",
            "properties": {
                "payee_id": {"type": "string", "minLength": 1, "maxLength": 64},
                "payee_name": {
                    "type": "string",
                    "maxLength": 64,
                    "description": "收款人展示名，仅用于给用户看的文案",
                },
                "amount_fen": _AMOUNT_FEN,
                "note": {"type": "string", "maxLength": 100, "description": "转账备注"},
            },
            "required": ["payee_id", "amount_fen"],
            "additionalProperties": False,
        },
        handler=transfers.transfer,
        writes=True,
    ),
    ToolSpec(
        name="cancel_subscription",
        description="取消一条订阅扣款。已经取消过的再取消一次算幂等成功。",
        params={
            "type": "object",
            "properties": {"sub_id": {"type": "string", "minLength": 1, "maxLength": 64}},
            "required": ["sub_id"],
            "additionalProperties": False,
        },
        handler=subs.cancel_subscription,
        writes=True,
    ),
    ToolSpec(
        name="reserve_funds",
        description="预授权一笔钱：只占住额度，不产生转账流水，不扣款。",
        params={
            "type": "object",
            "properties": {
                "amount_fen": _AMOUNT_FEN,
                "purpose": {
                    "type": "string",
                    "maxLength": 50,
                    "description": "预授权用途，例如 酒店押金",
                },
            },
            "required": ["amount_fen"],
            "additionalProperties": False,
        },
        handler=transfers.reserve_funds,
        writes=True,
    ),
    ToolSpec(
        name="pay_merchant",
        description="在支持的商户付款：没有预授权就现建一笔再扣，有有效预授权就直接扣。",
        params={
            "type": "object",
            "properties": {
                "merchant": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 32,
                    "description": "商户代码，目前只有 flower_shop 与 cake_shop",
                },
                "item": {
                    "type": "string", "minLength": 1, "maxLength": 50, "description": "买了什么",
                },
                "amount_fen": _AMOUNT_FEN,
                "hold_id": {
                    "type": "string",
                    "maxLength": 64,
                    "description": "已有的预授权 ID；有就复用这笔额度付款",
                },
            },
            "required": ["merchant", "item", "amount_fen"],
            "additionalProperties": False,
        },
        handler=transfers.pay_merchant,
        writes=True,
    ),
    ToolSpec(
        name="list_products",
        description=(
            "列出现在在售的基金，带风险等级、起购金额、最新净值和我持有的份额；"
            "可以按风险等级或类型筛选。"
        ),
        params={
            "type": "object",
            "properties": {
                "risk_level": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 5,
                    "description": "只看这个风险等级（R1 最稳、R5 最激进）",
                },
                "category": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 12,
                    "description": "只看某一类，合法值：" + "、".join(products.CATEGORY_LABELS),
                },
            },
            "required": [],
            "additionalProperties": False,
        },
        handler=products.list_products,
        writes=False,
    ),
    ToolSpec(
        name="estimate_risk",
        description="买之前算一笔：这只基金多险、起购多少、这笔钱能买到多少份、有什么提示。",
        params={
            "type": "object",
            "properties": {
                "product_code": _PRODUCT_CODE,
                "amount_fen": _AMOUNT_FEN,
            },
            "required": ["product_code", "amount_fen"],
            "additionalProperties": False,
        },
        handler=products.estimate_risk,
        writes=False,
    ),
    ToolSpec(
        name="buy_fund",
        description="申购基金：钱从卡里扣，换成基金份额。金额不能低于该产品的起购金额。",
        params={
            "type": "object",
            "properties": {
                "product_code": _PRODUCT_CODE,
                "amount_fen": _AMOUNT_FEN,
                "note": {"type": "string", "maxLength": 50, "description": "备注，可不填"},
            },
            "required": ["product_code", "amount_fen"],
            "additionalProperties": False,
        },
        handler=invest.buy_fund,
        writes=True,
    ),
    ToolSpec(
        name="redeem_fund",
        description=(
            "赎回基金：把持有的份额卖掉换回现金，钱回到卡里。"
            "金额可以是一次赎完，也可以只赎一部分。"
        ),
        params={
            "type": "object",
            "properties": {
                "product_code": _PRODUCT_CODE,
                "amount_fen": _AMOUNT_FEN,
                "note": {"type": "string", "maxLength": 50, "description": "备注，可不填"},
            },
            "required": ["product_code", "amount_fen"],
            "additionalProperties": False,
        },
        handler=invest.redeem_fund,
        writes=True,
    ),
    ToolSpec(
        name="apply_virtual_card",
        description="申请一张虚拟卡，立刻可用。默认单笔限额 5000 元，之后可以用 set_card_limit 调整。",
        params={
            "type": "object",
            "properties": {"account_tail": _TAIL},
            "required": [],
            "additionalProperties": False,
        },
        handler=cards.apply_virtual_card,
        writes=True,
    ),
    ToolSpec(
        name="set_card_limit",
        description="调整一张卡的单笔限额。要改的是哪张卡，用卡号后四位指定。",
        params={
            "type": "object",
            "properties": {"card_tail": _CARD_TAIL, "per_tx_limit_fen": _CARD_LIMIT_FEN},
            "required": ["card_tail", "per_tx_limit_fen"],
            "additionalProperties": False,
        },
        handler=cards.set_card_limit,
        writes=True,
    ),
    ToolSpec(
        name="freeze_card",
        description="冻结一张卡，冻结后这张卡不能再用。只影响这一张卡，不影响账户里的其他卡。",
        params={
            "type": "object",
            "properties": {"card_tail": _CARD_TAIL},
            "required": ["card_tail"],
            "additionalProperties": False,
        },
        handler=cards.freeze_card,
        writes=True,
    ),
    ToolSpec(
        name="unfreeze_card",
        description="把冻结的卡恢复正常。已挂失的卡不能用这个方法解挂，必须到柜面办理。",
        params={
            "type": "object",
            "properties": {"card_tail": _CARD_TAIL},
            "required": ["card_tail"],
            "additionalProperties": False,
        },
        handler=cards.unfreeze_card,
        writes=True,
    ),
    ToolSpec(
        name="report_card_loss",
        description="挂失一张卡。挂失后这张卡立刻停用，且不能自助撤销，需要到柜面补办。",
        params={
            "type": "object",
            "properties": {"card_tail": _CARD_TAIL},
            "required": ["card_tail"],
            "additionalProperties": False,
        },
        handler=cards.report_card_loss,
        writes=True,
    ),
    ToolSpec(
        name='schedule_transfer',
        description='预约一笔转账到未来的某个时间。用户说了「下个月」「周三」「明天」这类时间就要用这个，而不是立刻转。',
        params={
            "type": "object",
            "properties": {
                "payee_id": {"type": "string", "minLength": 1, "description": "收款人 id，先用 resolve_payee 拿到"},
                "amount_fen": _AMOUNT_FEN,
                "due_ms": {"type": "integer", "minimum": 1, "description": "预约执行的毫秒时间戳"},
                "recurrence": {"type": "string", "enum": list(scheduled.RECURRENCES), "description": "once 一次性；monthly 每月重复"},
                "note": {"type": "string", "minLength": 1, "maxLength": 60, "description": "转账备注"},
                "payee_name": {"type": "string", "minLength": 1, "description": "收款人显示名，只用于文案"}
            },
            "required": ['payee_id', 'amount_fen', 'due_ms'],
            "additionalProperties": False,
        },
        handler=scheduled.schedule_transfer,
        writes=True,
    ),
    ToolSpec(
        name='list_scheduled',
        description='列出还没执行的预约转账，含金额、收款人和执行时间。',
        params={
            "type": "object",
            "properties": {
                "include_done": {"type": "boolean", "description": "是否把已执行/已取消的也列出来"}
            },
            "required": [],
            "additionalProperties": False,
        },
        handler=scheduled.list_scheduled,
        writes=False,
    ),
    ToolSpec(
        name='cancel_scheduled',
        description='取消一笔还没执行的预约转账。已经执行过的取消不了。',
        params={
            "type": "object",
            "properties": {
                "scheduled_id": {"type": "string", "minLength": 1, "description": "预约 id，先用 list_scheduled 拿到"}
            },
            "required": ['scheduled_id'],
            "additionalProperties": False,
        },
        handler=scheduled.cancel_scheduled,
        writes=True,
    ),
    ToolSpec(
        name='split_transfer',
        description='AA 收款：把一笔钱按人头平摊给多个收款人。一次授权、每个收款人各记一笔账。',
        params={
            "type": "object",
            "properties": {
                "payee_ids": {"type": "array", "items": {"type": "string"}, "minItems": 2, "maxItems": 10, "description": "分摊对象的收款人 id 列表（必须已经由 resolve_payee 唯一确定，不能有重复）"},
                "amount_fen": _AMOUNT_FEN,
                "per_person_amount_fen": {"type": "integer", "minimum": 1, "description": "可选的校验字段：每人应付。除不尽时不要传；传了就必须与总额整除结果一致，否则整笔拒绝"},
                "note": {"type": "string", "minLength": 1, "maxLength": 60, "description": "备注"}
            },
            "required": ['payee_ids', 'amount_fen'],
            "additionalProperties": False,
        },
        handler=split_tools.split_transfer,
        writes=True,
    ),
    ToolSpec(
        name='close_card',
        description='销户一张卡，销了不能恢复。卡里还有钱或者还有没结清的资金占用时要先处理这些。',
        params={
            "type": "object",
            "properties": {
                "card_tail": _CARD_TAIL
            },
            "required": ['card_tail'],
            "additionalProperties": False,
        },
        handler=cards.close_card,
        writes=True,
    ),
    ToolSpec(
        name='change_card_pin',
        description='修改一张卡的密码，必须是 6 位数字。已经设过密码的卡要带上原密码，否则改不了。密码本身不会被回显。',
        params={
            "type": "object",
            "properties": {
                "card_tail": _CARD_TAIL,
                "pin": {"type": "string", "minLength": 6, "maxLength": 6, "pattern": "^[0-9]{6}$", "description": "6 位数字新密码"},
                "old_pin": {"type": "string", "minLength": 6, "maxLength": 6, "pattern": "^[0-9]{6}$", "description": "原 6 位密码；这张卡已经设过密码时必填"}
            },
            "required": ['card_tail', 'pin'], "additionalProperties": False,
        },
        handler=cards.change_card_pin,
        writes=True,
    ),
    ToolSpec(
        name='reverse_transfer',
        description='冲正一笔已经转出去的钱：在自己的账本上做反向记账，把这笔钱记回原账户。只能冲正 7 天内、自己账户的出账，且一笔只能冲一次。这是本地账务处理，不代表能从对方追回资金。',
        params={
            "type": "object",
            "properties": {
                "txn_id": {"type": "string", "minLength": 1, "description": "要冲正的交易 id，先用 query_transactions 拿到"},
                "reason": {"type": "string", "minLength": 1, "maxLength": 60, "description": "冲正原因"}
            },
            "required": ['txn_id'],
            "additionalProperties": False,
        },
        handler=transfers.reverse_transfer,
        writes=True,
    ),

    # ---------------------------------------------------------------- 阶段 5 主动财务管理
    # 只读三件套：看目标、算计划、看提醒。计划里的钱全部来自 app.proactive 的 int 分计算。
    ToolSpec(
        name='list_savings_goals',
        description='列出储蓄目标与当前进度：还差多少、每期要存多少、可行性如何。默认只看进行中的目标。',
        params={
            "type": "object",
            "properties": {
                "status": {"type": "string", "enum": ["active", "achieved", "cancelled", "all"], "description": "按状态筛选；all 表示不过滤"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 200, "description": "最多返回多少条，默认 20"}
            },
            "required": [],
            "additionalProperties": False,
        },
        handler=proactive_tools.list_savings_goals,
        writes=False,
    ),
    ToolSpec(
        name='compute_savings_plan',
        description='确定性计算储蓄计划：剩余目标、剩余期数、每期应存、历史平均收入与必要支出、预计结余、可行性。给 goal_id 按已有目标算，否则按临时参数算，不写库。',
        params={
            "type": "object",
            "properties": {
                "goal_id": {"type": "string", "minLength": 1, "description": "已有目标 id，先用 list_savings_goals 拿到；给了它就不用再传目标金额与日期"},
                "target_fen": _TARGET_FEN,
                "target_date": _TARGET_DATE,
                "saved_fen": {"type": "integer", "minimum": 0, "maximum": MAX_TARGET_FEN, "description": "已存金额，单位分，默认 0"},
                "lookback_months": {"type": "integer", "minimum": 1, "maximum": 24, "description": "回看几个月的历史流水来估收入与必要支出，默认 6"}
            },
            "required": [],
            "additionalProperties": False,
        },
        handler=proactive_tools.compute_savings_plan,
        writes=False,
    ),
    ToolSpec(
        name='list_proactive_alerts',
        description='列出主动提醒：进度落后、预算超支、现金流不足，每条都带证据。默认只看待处理的。',
        params={
            "type": "object",
            "properties": {
                "status": {"type": "string", "enum": ["open", "ignored", "resolved", "all"], "description": "按状态筛选；all 表示不过滤"},
                "goal_id": {"type": "string", "minLength": 1, "description": "只看某个目标的提醒"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 200, "description": "最多返回多少条，默认 20"}
            },
            "required": [],
            "additionalProperties": False,
        },
        handler=proactive_tools.list_proactive_alerts,
        writes=False,
    ),
    # 黄灯四件套：建目标/调目标/处理提醒/手动监测。只写目标、规则与提醒，钱一分不动。
    ToolSpec(
        name='create_savings_goal',
        description='创建一个储蓄目标（例如「2026-12-31 前存够 3 万元」），同时建一条按月盯进度的监测规则；给了月度预算再加一条预算规则。',
        params={
            "type": "object",
            "properties": {
                "name": {"type": "string", "minLength": 1, "maxLength": 40, "description": "目标名称，例如「年终奖存钱」"},
                "target_fen": _TARGET_FEN,
                "target_date": _TARGET_DATE,
                "saved_fen": {"type": "integer", "minimum": 0, "maximum": MAX_TARGET_FEN, "description": "已经存了多少，单位分，默认 0"},
                "budget_fen": {"type": "integer", "minimum": 1, "maximum": MAX_TARGET_FEN, "description": "可选的月度支出预算，单位分；给了就额外建预算提醒"},
                "progress_tolerance": {"type": "integer", "minimum": 1, "maximum": 100, "description": "进度落后多少个百分点才提醒，默认 10"},
                "period": {"type": "string", "enum": ["monthly", "weekly"], "description": "监测周期，默认 monthly"}
            },
            "required": ['name', 'target_fen', 'target_date'],
            "additionalProperties": False,
        },
        handler=proactive_tools.create_savings_goal,
        writes=True,
    ),
    ToolSpec(
        name='update_savings_goal',
        description='调整储蓄目标：改目标金额、日期、已存金额、预算或开关监测。达成与回退会自动切换状态，并按新参数重算计划。',
        params={
            "type": "object",
            "properties": {
                "goal_id": {"type": "string", "minLength": 1, "description": "要调整的目标 id"},
                "name": {"type": "string", "minLength": 1, "maxLength": 40, "description": "新名称"},
                "target_fen": _TARGET_FEN,
                "target_date": _TARGET_DATE,
                "saved_fen": {"type": "integer", "minimum": 0, "maximum": MAX_TARGET_FEN, "description": "新的已存金额，单位分"},
                "status": {"type": "string", "enum": ["active", "achieved", "cancelled"], "description": "手动改状态；一般不用传，系统会自动收口"},
                "budget_fen": {"type": "integer", "minimum": 1, "maximum": MAX_TARGET_FEN, "description": "新的月度支出预算，单位分"},
                "progress_tolerance": {"type": "integer", "minimum": 1, "maximum": 100, "description": "新的进度落后容忍百分点"},
                "monitor_enabled": {"type": "boolean", "description": "是否开启监测；关掉后手动监测也不会为它生成提醒"}
            },
            "required": ['goal_id'],
            "additionalProperties": False,
        },
        handler=proactive_tools.update_savings_goal,
        writes=True,
    ),
    ToolSpec(
        name='resolve_proactive_alert',
        description='处理一条主动提醒：ignore 表示先放着不再弹，resolve 表示已经处理完。重复处理同一条不会出错。',
        params={
            "type": "object",
            "properties": {
                "alert_id": {"type": "string", "minLength": 1, "description": "提醒 id，先用 list_proactive_alerts 拿到"},
                "action": {"type": "string", "enum": ["ignore", "resolve"], "description": "ignore 忽略，resolve 标记已处理"}
            },
            "required": ['alert_id', 'action'],
            "additionalProperties": False,
        },
        handler=proactive_tools.resolve_proactive_alert,
        writes=True,
    ),
    ToolSpec(
        name='run_proactive_monitor',
        description='手动触发一次主动监测：读目标、交易与储蓄，按规则生成有证据的提醒。只写提醒与运行记录，不转账、不申购、不改余额；可以重复触发。',
        params={
            "type": "object",
            "properties": {
                "goal_id": {"type": "string", "minLength": 1, "description": "只监测某个目标；不传则跑该用户全部进行中的目标"}
            },
            "required": [],
            "additionalProperties": False,
        },
        handler=proactive_tools.run_proactive_monitor,
        writes=True,
    ),
    # ---------------------------------------------------------------- 阶段 6 投资者适当性
    # 买基金前的硬前置：没测评或等级不够，buy_fund 直接拒（PRODUCT_RISK_TOO_HIGH）。
    # 问卷是简化版，不是监管口径的正式评估，回执里要照实说。
    ToolSpec(
        name='get_suitability',
        description='查当前的风险测评画像（C1—C5、最高可买 R 几），以及还没做时的 5 道题。买基金前先看这里。',
        params={
            "type": "object",
            "properties": {},
            "required": [],
            "additionalProperties": False,
        },
        handler=invest.get_suitability,
        writes=False,
    ),
    ToolSpec(
        name='assess_suitability',
        description='提交风险测评答案，保存/更新画像（C1—C5）。5 道题都要答：q_horizon、q_experience、q_loss、q_goal、q_share，选项值先用 get_suitability 查。这是简化演示问卷，不是监管口径的正式风险评估。',
        params={
            "type": "object",
            "properties": {
                "answers": {
                    "type": "object",
                    "description": "题号 -> 选项值，5 道题都要有",
                    "properties": {
                        "q_horizon": {"type": "string", "enum": ["lt_6m", "6m_1y", "1y_3y", "gt_3y"]},
                        "q_experience": {"type": "string", "enum": ["deposit_only", "money_bond", "mixed", "equity"]},
                        "q_loss": {"type": "string", "enum": ["sell_all", "sell_part", "hold", "buy_more"]},
                        "q_goal": {"type": "string", "enum": ["preserve", "steady", "growth", "aggressive"]},
                        "q_share": {"type": "string", "enum": ["gt_50", "30_50", "10_30", "lt_10"]}
                    },
                    "required": ['q_horizon', 'q_experience', 'q_loss', 'q_goal', 'q_share'],
                    "additionalProperties": False
                }
            },
            "required": ['answers'],
            "additionalProperties": False,
        },
        handler=invest.assess_suitability,
        writes=True,
    ),
)
