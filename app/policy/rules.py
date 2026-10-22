"""三色裁决的规则表：R01—R12 的数据定义与顺序。

对应文档：docs/05_安全层实现规格.md 5.1/5.2、docs/06_工具层实现规格.md 6.1。

顺序即优先级：decide() 自上而下短路求值，第一条 when 命中的规则直接产出 Decision。
本模块是纯数据加纯函数，不碰数据库、网络与时钟；时间与今日累计由 PolicyContext 传入，
这样同一条 Action 在任何时候重放都得到同一个裁决。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Final

from app.models import Action, Decision, RiskLevel
from app.money import fen_to_yuan_str

# docs/05 5.1：单笔与日累计同一条 1000 元线；> 1000 元转红，含等于仍为黄
SINGLE_LIMIT_FEN: Final[int] = 100_000
DAILY_LIMIT_FEN: Final[int] = 100_000

# 权威工具表（docs/06 6.1）：只读 9 个 + 写 17 个，共 26 个。
# 只读：基础六件套 + 理财只读两件套（与场景三、场景四的写工具分开，R01 才能一条规则覆盖全部查询）
READONLY_TOOLS: Final[frozenset[str]] = frozenset(
    {"query_balance", "query_transactions", "analyze_bill", "list_subs", "list_cards", "resolve_payee",
     "list_products", "estimate_risk", "list_scheduled"})
# 当场动钱的写工具：金额进 R06/R07/R08 的分档判定。
# split_transfer 用总额判档（AA 四个人各 250，出账仍是 1000，日累计按总额算才对）。
SPEND_TOOLS: Final[frozenset[str]] = frozenset(
    {"transfer", "reserve_funds", "pay_merchant", "split_transfer"}
)
# 只确认不验因子：赛题把「订阅取消」和「虚拟卡申请」都写在黄灯（docs/05 5.2）
CONFIRM_ONLY_TOOLS: Final[frozenset[str]] = frozenset(
    {"cancel_subscription", "apply_virtual_card", "schedule_transfer", "cancel_scheduled"}
)
# 账户安全类：改限额 / 解绑卡 / 改联系方式 / 改密码 / 卡片限额与冻结解冻挂失，三重验证
SENSITIVE_TOOLS: Final[frozenset[str]] = frozenset(
    {"change_transfer_limit", "unbind_card", "update_contact", "reset_password",
     "set_card_limit", "freeze_card", "unfreeze_card", "report_card_loss",
     "close_card", "change_card_pin", "reverse_transfer"}
)
# 理财类：人脸 + 动态口令
INVEST_TOOLS: Final[frozenset[str]] = frozenset({"buy_fund", "redeem_fund"})
# 已登记但未配规则的工具。现在这个集合是空的：销户与改卡密码也都实现了。
# R11「未知工具默认红拒」仍然是活的兜底——它的入参是「不在 KNOWN_TOOLS 里」，
# 任何没走登记就冒出来的工具名都会撞上它（tests/test_policy.py 有用例钉住）。
PENDING_TOOLS: Final[frozenset[str]] = frozenset()

# 查询类工具的「账户主体」参数名：意图层给 self / other，策略层据此判红。
# 由 app/planner/intent.py 产出、app/planner/dag.py 填进只读查询节点，是 R12 的唯一入参。
OWNER_PARAM: Final[str] = "account_owner"
SELF_OWNER: Final[str] = "self"
OTHER_OWNER: Final[str] = "other"

KNOWN_TOOLS: Final[frozenset[str]] = (
    READONLY_TOOLS | SPEND_TOOLS | CONFIRM_ONLY_TOOLS | SENSITIVE_TOOLS | INVEST_TOOLS | PENDING_TOOLS
)


@dataclass(frozen=True)
class PolicyContext:
    """裁决所需的外部事实；由调用方查好塞进来，保证 decide 可重放。"""

    spent_today_fen: int = 0
    limit_fen: int = DAILY_LIMIT_FEN
    single_limit_fen: int = SINGLE_LIMIT_FEN
    # 演示与审计用；引擎自己不取时间（docs/05 5.2 要求纯函数）
    now_ms: int = 0


@dataclass(frozen=True)
class Rule:
    """一条规则：命中条件 when + 产出裁决的 emit。"""

    rule_id: str
    level: RiskLevel
    reason_code: str
    when: Callable[[Action, PolicyContext], bool]
    emit: Callable[[Action, PolicyContext], Decision]


def _yuan(fen: int) -> str:
    """只在出口把分格式化成「1,000.00 元」，引擎内部全程 int 分。"""
    return f"{fen_to_yuan_str(fen)} 元"


def _payee_label(action: Action) -> str:
    """收款方展示名：params 里有人名用人名，只有 id 就显示 id，都没有就说「对方」。"""
    for key in ("payee_name", "merchant_name", "payee_id"):
        value = action.params.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return "对方"


def _is_spend(action: Action) -> bool:
    return action.tool in SPEND_TOOLS


def _is_foreign_owner(action: Action) -> bool:
    """查询目标是不是别人：只认意图层显式给的 account_owner=other。

    不猜、不从卡号尾号推断主体——主体判定只有一个入口，就是意图层那个槽位。
    """
    return action.params.get(OWNER_PARAM) == OTHER_OWNER


def _amount(action: Action) -> int:
    return action.amount_fen or 0


def _over_daily(action: Action, ctx: PolicyContext) -> bool:
    return ctx.spent_today_fen + _amount(action) > ctx.limit_fen


def _decision(
    level: RiskLevel,
    rule_id: str,
    reason_code: str,
    reason_text: str,
    ctx: PolicyContext,
    factors: tuple[str, ...] = (),
) -> Decision:
    return Decision(
        level=level,
        rule_id=rule_id,
        reason_code=reason_code,
        reason_text=reason_text,
        required_factors=list(factors),
        limit_fen=ctx.limit_fen,
        spent_today_fen=ctx.spent_today_fen,
    )


def _emit_readonly(action: Action, ctx: PolicyContext) -> Decision:
    return _decision("green", "R01_readonly", "READONLY", "这一步只是查询，我直接帮你看。", ctx)


def _emit_invalid_amount(action: Action, ctx: PolicyContext) -> Decision:
    return _decision("red", "R02_invalid_amount", "INVALID_AMOUNT", "金额不正确，请重新输入", ctx)


def _emit_unknown_tool(action: Action, ctx: PolicyContext) -> Decision:
    return _decision("red", "R03_unknown_tool", "UNKNOWN_TOOL", "我还不会这个操作", ctx)


def _emit_untrusted(action: Action, ctx: PolicyContext) -> Decision:
    fields = "、".join(action.untrusted_fields)
    return _decision(
        "red",
        "R04_untrusted_amount",
        "UNTRUSTED_SOURCE",
        f"这笔请求的来源需要人工核实（可疑字段：{fields}）",
        ctx,
        ("face", "sms"),
    )


def _emit_confirm_only(action: Action, ctx: PolicyContext) -> Decision:
    return _decision("yellow", "R05_confirm_only", "CONFIRM_ONLY", "即将为你执行，请确认", ctx)


def _emit_yellow_transfer(action: Action, ctx: PolicyContext) -> Decision:
    return _decision(
        "yellow",
        "R06_transfer_yellow",
        "TRANSFER_YELLOW",
        f"即将向 {_payee_label(action)} 转出 {_yuan(_amount(action))}，需要你确认",
        ctx,
    )


def _emit_red_daily(action: Action, ctx: PolicyContext) -> Decision:
    # R07 与 R08 同属「可验证的红」：因子齐了就能继续，文案不能说「我不做」。
    text = (
        f"今日已用 {_yuan(ctx.spent_today_fen)}，这笔 {_yuan(_amount(action))} "
        "会超过日累计线，需要人脸和短信验证后才能继续"
    )
    return _decision("red", "R07_transfer_red_daily", "TRANSFER_DAILY_RED", text, ctx, ("face", "sms"))


def _emit_red_single(action: Action, ctx: PolicyContext) -> Decision:
    # 单笔越线同样是可验证的红：face + sms 验过就放行（2026-10-21 起），
    # 文案要说清「验证后继续」，不能还停在「我不能直接做」。
    text = (
        f"单笔 {_yuan(_amount(action))} 超过 {_yuan(ctx.single_limit_fen)}上限，"
        "这笔需要人脸和短信验证后才能继续"
    )
    return _decision("red", "R08_transfer_red_single", "TRANSFER_SINGLE_RED", text, ctx, ("face", "sms"))


def _emit_sensitive(action: Action, ctx: PolicyContext) -> Decision:
    return _decision(
        "red",
        "R09_sensitive_red",
        "SENSITIVE_TOOL",
        "这是账户安全操作，需要三重验证",
        ctx,
        ("face", "sms", "otp"),
    )


def _emit_invest(action: Action, ctx: PolicyContext) -> Decision:
    return _decision(
        "red", "R10_invest_red", "INVEST_TOOL", "理财操作需要人脸与动态口令", ctx, ("face", "otp")
    )


def _emit_other_account(action: Action, ctx: PolicyContext) -> Decision:
    """R12：代查他人账户一律红灯，且不弹确认框——理由是主体不对，不是权限不够。"""
    text = (
        "查询类操作我只能做本人名下的账户。您说的那个账户不在我这儿能查的范围里，"
        "如果您要查的是自己的账户，我马上接着办；如果要查对方的，请让对方本人在他自己的渠道上查。"
    )
    return _decision("red", "R12_other_account", "OTHER_ACCOUNT", text, ctx)


def _emit_default_deny(action: Action, ctx: PolicyContext) -> Decision:
    return _decision("red", "R11_default_deny", "DEFAULT_DENY", "这个操作我不确定是否安全，先不做", ctx)


def _rule(
    rule_id: str,
    level: RiskLevel,
    reason_code: str,
    when: Callable[[Action, PolicyContext], bool],
    emit: Callable[[Action, PolicyContext], Decision],
) -> Rule:
    return Rule(rule_id=rule_id, level=level, reason_code=reason_code, when=when, emit=emit)


# R01—R12：元组顺序即求值顺序（docs/05 5.2 的 for 循环直接吃它）
RULES: Final[tuple[Rule, ...]] = (
    # R12 必须排在 R01 前面：只读查询本来是绿灯的活，一旦目标是别人的账户，
    # 它先被 R12 拦下；放在 R01 后面就永远走不到，变成一行死代码。
    _rule(
        "R12_other_account",
        "red",
        "OTHER_ACCOUNT",
        lambda action, ctx: action.tool in READONLY_TOOLS and _is_foreign_owner(action),
        _emit_other_account,
    ),
    _rule(
        "R01_readonly",
        "green",
        "READONLY",
        lambda action, ctx: action.tool in READONLY_TOOLS,
        _emit_readonly,
    ),
    _rule(
        "R02_invalid_amount",
        "red",
        "INVALID_AMOUNT",
        lambda action, ctx: _is_spend(action) and _amount(action) <= 0,
        _emit_invalid_amount,
    ),
    _rule(
        "R03_unknown_tool",
        "red",
        "UNKNOWN_TOOL",
        lambda action, ctx: action.tool not in KNOWN_TOOLS,
        _emit_unknown_tool,
    ),
    _rule(
        "R04_untrusted_amount",
        "red",
        "UNTRUSTED_SOURCE",
        lambda action, ctx: _is_spend(action) and bool(action.untrusted_fields),
        _emit_untrusted,
    ),
    _rule(
        "R05_confirm_only",
        "yellow",
        "CONFIRM_ONLY",
        lambda action, ctx: action.tool in CONFIRM_ONLY_TOOLS,
        _emit_confirm_only,
    ),
    _rule(
        "R06_transfer_yellow",
        "yellow",
        "TRANSFER_YELLOW",
        lambda action, ctx: _is_spend(action)
        and 0 < _amount(action) <= ctx.single_limit_fen
        and not _over_daily(action, ctx),
        _emit_yellow_transfer,
    ),
    _rule(
        "R07_transfer_red_daily",
        "red",
        "TRANSFER_DAILY_RED",
        # spent_today_fen > 0 是守卫：否则「单笔刚过线」的请求会被误判成日累计超限，
        # 与 docs/05 5.1 边界表里 1000.01 -> R08 的要求冲突
        lambda action, ctx: _is_spend(action) and ctx.spent_today_fen > 0 and _over_daily(action, ctx),
        _emit_red_daily,
    ),
    _rule(
        "R08_transfer_red_single",
        "red",
        "TRANSFER_SINGLE_RED",
        lambda action, ctx: _is_spend(action) and _amount(action) > ctx.single_limit_fen,
        _emit_red_single,
    ),
    _rule(
        "R09_sensitive_red",
        "red",
        "SENSITIVE_TOOL",
        lambda action, ctx: action.tool in SENSITIVE_TOOLS,
        _emit_sensitive,
    ),
    _rule(
        "R10_invest_red",
        "red",
        "INVEST_TOOL",
        lambda action, ctx: action.tool in INVEST_TOOLS,
        _emit_invest,
    ),
    _rule(
        "R11_default_deny",
        "red",
        "DEFAULT_DENY",
        lambda action, ctx: True,
        _emit_default_deny,
    ),
)

RULES_BY_ID: Final[dict[str, Rule]] = {rule.rule_id: rule for rule in RULES}

__all__ = [
    "CONFIRM_ONLY_TOOLS",
    "DAILY_LIMIT_FEN",
    "INVEST_TOOLS",
    "KNOWN_TOOLS",
    "OTHER_OWNER",
    "PENDING_TOOLS",
    "PolicyContext",
    "READONLY_TOOLS",
    "RULES",
    "RULES_BY_ID",
    "Rule",
    "SENSITIVE_TOOLS",
    "SINGLE_LIMIT_FEN",
    "SPEND_TOOLS",
]
