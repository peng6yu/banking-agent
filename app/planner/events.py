"""SSE 事件与文案：把内部状态翻译成前端能直接渲染的字段。

事件名与字段名和 docs/07 7.6 一一对应，和已实现的前端 web/index.html 对齐：
start / intent / trace / confirm / factor / tool / done / error，一个不多一个不少。
"""

from __future__ import annotations

import inspect
import time
from typing import Any

from app.models import Action, Decision
from app.money import fen_to_yuan_str
from app.subscriptions import STOPPED_BOUNDARY_TEXT
from app.planner.state import (
    CONFIRM_TIMEOUT_MS,
    confirm_timeout_ms,
    Confirmation,
    Emit,
    action_text,
    tool_name,
)
from app.policy.engine import explain


def summarize(tool: str, data: dict[str, Any]) -> str:
    """一句话摘要：给前端 tool 事件和最终 done 都用。"""
    amount = fen_to_yuan_str(int(data.get("amount_fen") or 0))
    if tool == "transfer":
        return f"已转出 {amount} 元"
    if tool == "reserve_funds":
        return f"已预留 {amount} 元"
    if tool == "pay_merchant":
        return f"已在 {data.get('merchant', '商户')} 支付 {amount} 元"
    if tool == "cancel_subscription":
        # 能力边界必须出现在摘要里：本地只停银行侧代扣，碰不到商户系统，
        # 所以不能写成"已取消订阅"（那是在替商户做承诺）。
        who = data.get("merchant") or data.get("name") or data.get("sub_id")
        return f"已停掉银行侧代扣「{who}」（商户会员本身没有取消）"
    if tool == "reverse_transfer":
        return f"已做账务冲正（本地反向记账），{amount} 元记回你的账户"
    if tool == "cancel_scheduled":
        return str(data.get("summary") or "这条预约已经取消了")
    if tool == "split_transfer":
        # AA 的钱分散在多个收款人身上，只说总额会让人以为只转了一笔；
        # 工具已经算好"谁多少"，直接用它那句（缺了就退回总额）。
        return str(data.get("summary") or f"已按 AA 拆分转出 {amount} 元")
    if tool == "query_balance":
        return f"余额 {_first_account(data).get('balance_yuan', '0.00')} 元"
    if tool == "query_transactions":
        return f"共查到 {data.get('count', 0)} 笔交易"
    if tool == "analyze_bill":
        return f"账单支出合计 {data.get('total_out_yuan', '0.00')} 元（{_bill_range_label(data)}）"
    if tool == "list_subs":
        return f"在订订阅 {data.get('count', 0)} 项，每月 {data.get('monthly_total_yuan', '0.00')} 元"
    if tool == "list_cards":
        return f"名下卡片 {data.get('count', 0)} 张"
    if tool == "resolve_payee":
        return f"找到收款人 {data.get('query', '')}"
    if tool in CARD_WRITE_TOOLS:
        # 卡片五件套各自带一句人话（含幂等与挂失终态的差别），直接用工具的话
        return str(data.get("summary") or f"{tool} 执行完成")
    return f"{tool} 执行完成"


CARD_WRITE_TOOLS = frozenset({
    "apply_virtual_card",
    "set_card_limit",
    "freeze_card",
    "unfreeze_card",
    "report_card_loss",
})


def _first_account(data: dict[str, Any]) -> dict[str, Any]:
    accounts = [item for item in (data.get("accounts") or []) if isinstance(item, dict)]
    return accounts[0] if accounts else {}


def _bill_range_label(data: dict[str, Any]) -> str:
    """账单摘要里的统计区间标签：优先用账单结果自带的 range.label。

    标签必须来自工具算出来的区间（app/tools/bill.py 的 _range_label），
    不在这里另算月份——另算就是「账单差一个月、金额照样像模像样」的来源。
    """
    span = data.get("range")
    if isinstance(span, dict):
        label = str(span.get("label") or "").strip()
        if label:
            return label
    year = data.get("year")
    month = data.get("month")
    if year and month:
        return f"{year} 年 {month} 月"
    if month:
        return f"{month} 月"
    return "统计区间未知"



# —— 确认卡片的"人话"字典 ——
# 确认卡要回答的是"我到底在同意什么"，不是"引擎判了几级"。
LEVEL_LABELS: dict[str, str] = {"green": "低", "yellow": "中", "red": "高"}
LEVEL_NOTES: dict[str, str] = {
    "green": "这一步不需要你额外授权。",
    "yellow": "需要你点确认才会执行。",
    "red": "需要先通过下面的验证步骤，验证结果只对这一次操作有效。",
}
FACTOR_LABELS: dict[str, str] = {
    "sms": "短信验证码",
    "face": "人脸识别",
    "otp": "动态口令",
}
# 每种操作在确认卡上叫什么、怎么到账：这些是产品事实，不是渲染细节。
TOOL_DISPLAY: dict[str, dict[str, str]] = {
    "transfer": {"name": "转账", "method": "银行卡转账", "arrival": "实时到账"},
    "split_transfer": {"name": "多人分账", "method": "按份额拆分的多笔转账", "arrival": "实时到账"},
    "reserve_funds": {"name": "资金预留", "method": "冻结一笔可用余额", "arrival": "预留即生效"},
    "pay_merchant": {"name": "商户支付", "method": "向商户付款", "arrival": "实时到账"},
    "scheduled_transfer": {"name": "预约转账", "method": "到期自动执行的转账", "arrival": "到期日执行"},
    "cancel_scheduled": {"name": "撤销预约", "method": "取消一笔尚未执行的预约", "arrival": "立即生效"},
    "buy_fund": {"name": "申购理财", "method": "按当日净值申购", "arrival": "以份额确认结果为准"},
    "redeem_fund": {"name": "赎回理财", "method": "按当日净值赎回", "arrival": "以到账结果为准"},
    "cancel_subscription": {"name": "停止代扣", "method": "停止银行侧周期性付款", "arrival": "下次扣费起生效"},
    "set_card_limit": {"name": "调整卡片限额", "method": "修改单笔消费上限", "arrival": "立即生效"},
    "apply_virtual_card": {"name": "申请虚拟卡", "method": "新开一张虚拟卡", "arrival": "即时开户"},
    "freeze_card": {"name": "冻结卡片", "method": "暂停卡片支付能力", "arrival": "立即生效"},
    "reverse_transfer": {"name": "申请冲正", "method": "对已记账交易发起冲正申请", "arrival": "以处理结果为准"},
}

# 演示环境没有计费引擎：宁可在卡上写"不知道"，也不编一个手续费。
FEE_UNKNOWN_TEXT = "本演示环境不计算手续费；真实资费以银行公示为准。"
SCHEDULE_FIELDS = ("due_ms", "due_date", "scheduled_at", "execute_at")


def _fmt_ms(value: Any) -> str | None:
    """毫秒时间戳 -> 「2026-10-23 15:04」；拿不到就返回 None，不猜。"""
    try:
        stamp = int(value)
    except (TypeError, ValueError):
        return None
    if stamp <= 0:
        return None
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(stamp / 1000))


def _mask_phone(phone: Any) -> str | None:
    """手机号只留首尾：确认卡要能区分人，不需要把完整号码摊在屏幕上。"""
    text = str(phone or "").strip()
    if len(text) < 7:
        return None
    return f"{text[:3]}****{text[-4:]}"


def _account_row(conn: Any, account_id: Any) -> dict[str, Any] | None:
    if not account_id:
        return None
    row = conn.execute(
        "SELECT id, name, tail, type, balance_fen FROM accounts WHERE id = ?",
        (str(account_id),),
    ).fetchone()
    if row is None:
        return None
    return {
        "account_id": str(row["id"]),
        "name": str(row["name"]),
        "tail": str(row["tail"]),
        "type": str(row["type"]),
        "balance_fen": int(row["balance_fen"]),
        "balance_yuan": fen_to_yuan_str(int(row["balance_fen"])),
        # 明确写清楚这是"当前余额"，不是"扣款后余额"：后者要按 hold 口径算，
        # 演示层不推算，免得确认卡上出现一个和账面不一致的数字。
        "note": "当前余额，不是扣款后的余额。",
    }


def _payee_row(conn: Any, payee_id: Any) -> dict[str, Any] | None:
    if not payee_id:
        return None
    row = conn.execute(
        "SELECT id, name, bank, card_tail, alias, phone FROM payees WHERE id = ?",
        (str(payee_id),),
    ).fetchone()
    if row is None:
        return None
    return {
        "payee_id": str(row["id"]),
        "name": str(row["name"]),
        "bank": row["bank"],
        "card_tail": row["card_tail"],
        "alias": row["alias"],
        "phone_masked": _mask_phone(row["phone"]),
    }


def _subscription_row(conn: Any, sub_id: Any, account_id: Any = None) -> dict[str, Any] | None:
    """按订阅号查 subs 行，翻成确认卡上的订阅详情；查不到就 None，不编造。

    确认卡只写「取消订阅」等于让用户在不知道对象的情况下点头——所以要把商户、
    月费、下次扣费时间和能力边界一起给出来。这里只读，不改订阅状态。
    """
    key = str(sub_id or "").strip()
    if not key:
        return None
    if account_id:
        row = conn.execute(
            "SELECT * FROM subs WHERE id = ? AND account_id = ?", (key, str(account_id))
        ).fetchone()
    else:
        row = conn.execute("SELECT * FROM subs WHERE id = ?", (key,)).fetchone()
    if row is None:
        return None
    amount_fen = int(row["amount_fen"])
    period = str(row["period"])
    status = str(row["status"])
    return {
        "merchant": str(row["merchant"]),
        "amount_fen": amount_fen,
        "amount_yuan": fen_to_yuan_str(amount_fen),
        "period": period,
        "period_label": "每月" if period == "monthly" else "每年",
        "next_charge_text": _fmt_ms(int(row["next_charge_ms"])) or "",
        "boundary_text": STOPPED_BOUNDARY_TEXT,
        "status": status,
        "status_label": "生效中" if status == "active" else "已停止（银行侧）",
    }


def describe_confirmation(
    conn: Any, action: Action, decision: Decision, *, tool: str = ""
) -> dict[str, Any]:
    """把一次待确认的操作，翻译成确认卡需要的全部字段。

    这里的每一个字段都必须有出处：来源账户查 accounts 表，收款人查 payees 表，
    手续费没有计费引擎就明写"不知道"。前端不再各自拼文案。
    """
    name = tool or action.tool
    display = TOOL_DISPLAY.get(name, {})
    params = action.params if isinstance(action.params, dict) else {}
    scheduled_for = None
    for field in SCHEDULE_FIELDS:
        text = _fmt_ms(params.get(field))
        if text:
            scheduled_for = text
            break
    if scheduled_for is None and isinstance(params.get("due_date"), str):
        scheduled_for = params["due_date"]
    steps = [
        {"id": factor, "label": FACTOR_LABELS.get(factor, factor), "required": True}
        for factor in decision.required_factors
    ]
    # 黄灯也要有「你要做什么」：否则卡片上「验证步骤」是空的，用户不知道还要干嘛。
    if decision.level == "yellow":
        steps = [{"id": "confirm", "label": "核对收款人与金额后点「确认」授权这笔操作", "required": True}]
    elif decision.level == "red":
        steps = steps + [{"id": "confirm", "label": "验证通过后点右侧「提交验证并执行」", "required": True}]
    return {
        "tool_name": display.get("name", tool_name(name)),
        "method": display.get("method", "这项操作"),
        "arrival": display.get("arrival"),
        "source_account": _account_row(conn, action.account_id),
        "payee": _payee_row(conn, params.get("payee_id")) or (
            {"name": str(params["payee_name"])} if params.get("payee_name") else None
        ),
        "scheduled_for": scheduled_for,
        "fee": {"known": False, "amount_fen": None, "text": FEE_UNKNOWN_TEXT},
        # 停止代扣要把对象摊开：哪家商户、多少钱、多久一次、下次什么时候、边界是什么。
        # 别的工具没有订阅详情，这里就是 None——不硬塞一个空对象冒充。
        "subscription": (
            _subscription_row(conn, params.get("sub_id"), action.account_id)
            if name == "cancel_subscription"
            else None
        ),
        "verification": {
            "level": decision.level,
            "level_label": LEVEL_LABELS.get(decision.level, decision.level),
            "note": LEVEL_NOTES.get(decision.level, ""),
            "steps": steps,
            "total_steps": len(steps),
        },
    }


def prompt_event(decision: Decision) -> str:
    """红绿黄三色对应三种事件：factor 要双因子，confirm 只要点头。"""
    return "factor" if decision.level == "red" else "confirm"


def _frame_core(
    *,
    confirmation_id: str,
    node_id: str,
    tool: str,
    level: str,
    rule_id: str,
    reason_text: str,
    text: str,
    amount_fen: int | None,
    payee_name: str | None,
    required: list[str],
    expires_in_s: int,
) -> dict[str, Any]:
    """确认卡里「跟这次操作绑定」的那几个字段；SSE 与刷新恢复共用同一份定义。"""
    amount = fen_to_yuan_str(amount_fen) if amount_fen else None
    return {
        "confirmation_id": confirmation_id,
        "node_id": node_id,
        "tool": tool,
        "level": level,
        "rule_id": rule_id,
        "action_text": text,
        "amount_fen": amount_fen,
        "amount": amount,
        "amount_yuan": amount,
        "payee_name": payee_name,
        "slots": {"payee_name": payee_name} if payee_name else {},
        "required": list(required),
        "expires_in_s": int(expires_in_s),
        "reason_text": reason_text,
    }


def confirmation_frame(
    conn: Any,
    *,
    confirmation_id: str,
    node_id: str,
    tool: str,
    action: Action,
    decision: Decision,
    required: list[str] | None = None,
    expires_in_s: int | None = None,
    status: str = "pending",
) -> dict[str, Any]:
    """从库里重建一张确认卡：刷新、SSE 断流、重连都该看到同一张卡。

    只回一个确认号是不负责任的：用户看不见金额和收款人，却要按「再提交一次」。
    所以卡片的每个字段都从 confirmations.action_json 与 dag_nodes.decision_json 重建，
    来源账户和收款人也重新查库，跟第一次推送时用的是同一套 describe_confirmation。
    """
    details = describe_confirmation(conn, action, decision, tool=tool)
    payee = details.get("payee") or {}
    payload = _frame_core(
        confirmation_id=confirmation_id,
        node_id=node_id,
        tool=tool,
        level=decision.level,
        rule_id=decision.rule_id,
        reason_text=decision.reason_text,
        text=action_text(tool, action, payee=payee.get("name")),
        amount_fen=action.amount_fen,
        payee_name=payee.get("name"),
        required=list(decision.required_factors if required is None else required),
        expires_in_s=(
            confirm_timeout_ms() // 1000 if expires_in_s is None else int(expires_in_s)
        ),
    )
    for key in (
        "tool_name",
        "method",
        "arrival",
        "source_account",
        "payee",
        "scheduled_for",
        "fee",
        "subscription",
        "verification",
    ):
        payload[key] = details.get(key)
    payload["status"] = status
    return payload


def prompt_payload(
    node: Any,
    conf: Confirmation,
    decision: Decision,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """确认卡片载荷。

    details 由 describe_confirmation 从库里查出来（来源账户、收款人、手续费口径）；
    老调用方只传三个参数也能用，缺的字段是 None 而不是编出来的值。
    """
    payload = _frame_core(
        confirmation_id=conf.id,
        node_id=node.id,
        tool=node.tool,
        level=conf.level,
        rule_id=decision.rule_id,
        reason_text=decision.reason_text,
        text=conf.action_text,
        amount_fen=conf.action.amount_fen,
        payee_name=conf.payee_name,
        required=list(conf.required_factors),
        # 前端倒计时用的是实际生效的那一档（CONFIRM_TIMEOUT_S 可调），不是写死的 120。
        expires_in_s=confirm_timeout_ms() // 1000,
    )
    if details:
        payload.update(
            {
                "tool_name": details.get("tool_name"),
                "method": details.get("method"),
                "arrival": details.get("arrival"),
                "source_account": details.get("source_account"),
                "payee": details.get("payee"),
                "scheduled_for": details.get("scheduled_for"),
                "fee": details.get("fee"),
                # 停代扣的详情：前端用它把「取消订阅」铺成商户 / 月费 / 下次扣费 / 边界。
                # 别的工具这里是 None。
                "subscription": details.get("subscription"),
                "verification": details.get("verification"),
            }
        )
    else:
        # 老调用方（只传三个参数）也要拿到同一个键，缺的是 None 而不是 KeyError。
        payload["subscription"] = None
    return payload


def trace_event(node: Any, action: Action, decision: Decision) -> dict[str, Any]:
    """trace 事件：前端策略卡片直接吃这个。"""
    return {
        "node_id": node.id,
        "tool": node.tool,
        "tool_name": tool_name(node.tool),
        "level": decision.level,
        "rule_id": decision.rule_id,
        "reason_text": decision.reason_text,
        "explain": explain(decision, action),
        "amount_fen": action.amount_fen,
    }


async def emit_event(emit: Emit, name: str, data: dict[str, Any]) -> None:
    """发一个 SSE 事件；emit 可以是同步函数也可以是协程。"""
    result = emit(name, data)
    if inspect.isawaitable(result):
        await result


__all__ = [
    "FACTOR_LABELS",
    "LEVEL_LABELS",
    "TOOL_DISPLAY",
    "action_text",
    "confirmation_frame",
    "describe_confirmation",
    "emit_event",
    "prompt_event",
    "prompt_payload",
    "summarize",
    "tool_name",
    "trace_event",
]
