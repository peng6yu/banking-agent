"""银行卡工具：查询、申请虚拟卡、调限额、冻结/解冻、挂失。

对应文档：docs/06_工具层与场景实现规格.md 6.3 场景 4 卡片管理。

list_cards 只读；其余五个都是写工具，第一句就是 require_token（docs/05 5.3）。
状态机由库里的 CHECK 约束兜底（active / frozen / lost），这里再显式挡一次非法迁移，
并把每次真实状态变更写进 tool.executed 审计（event_type 是闭集，只有这一种写法）。
幂等：目标状态已达成时返回 idempotent=True 的成功，不重复写审计。
"""

from __future__ import annotations

from typing import Any

# 模块级导入（不用 from 包 import 模块）：tests/test_imports.py 用 ast 按模块名登记依赖，
# 包级导入会被它漏掉，允许依赖表就会与代码现状对不上。
import app.audit as audit  # noqa: PLR0402
import app.db as db  # noqa: PLR0402
from app.ids import new_id
from app.models import ToolResult
from app.money import fen_to_yuan_str
from app.tools.base import (
    ToolContext,
    fail_result,
    find_account_by_tail,
    ok_result,
    primary_account,
    require_token,
)

# 虚拟卡开卡默认单笔限额：5000 元（与实际发卡口径一致，避免开卡即超限）
DEFAULT_PER_TX_LIMIT_FEN = 500_000
# 单笔限额的业务上界：5 万元。超过这个数不该由用户自助设，需要柜面。
MAX_PER_TX_LIMIT_FEN = 5_000_000

_STATUS_ZH = {"active": "正常", "frozen": "已冻结", "lost": "已挂失"}


def list_cards(ctx: ToolContext, params: dict[str, Any], token: str | None = None) -> ToolResult:
    """列卡：传了尾号只回那一张对应的卡，查不到就是 ACCOUNT_NOT_FOUND。"""
    tail = params.get("account_tail")
    account = primary_account(ctx.conn, ctx.account_id)
    if tail is not None:
        account = find_account_by_tail(ctx.conn, str(tail), ctx.account_id)
        if account is None:
            return fail_result("ACCOUNT_NOT_FOUND", f"没找到尾号 {tail} 的账户。")
    rows = ctx.conn.execute(
        "SELECT c.*, a.tail AS account_tail FROM cards c"
        " JOIN accounts a ON a.id = c.account_id WHERE c.account_id = ? ORDER BY c.id",
        (account["id"],),
    ).fetchall()
    cards = [_card_view(row) for row in rows]
    return ok_result(
        {
            "cards": cards,
            "count": len(cards),
            "account_tail": str(account["tail"]),
            "summary": f"共 {len(cards)} 张卡，账户尾号 {account['tail']}。",
        }
    )


def apply_virtual_card(
    ctx: ToolContext, params: dict[str, Any], token: str | None = None
) -> ToolResult:
    """申请一张虚拟卡：立刻可用，初始状态 active，默认单笔限额 5000 元。"""
    require_token("apply_virtual_card", params, token)
    account, error = _resolve_account(ctx, params)
    if account is None:
        return fail_result("ACCOUNT_NOT_FOUND", f"没找到尾号 {params.get('account_tail')} 的账户。")
    card_tail = _next_virtual_tail(ctx.conn, str(account["id"]))
    if card_tail is None:
        return fail_result("CARD_TAIL_EXHAUSTED", "这个账户下的卡号尾号已经用完了，请到柜面办理。")
    card_id = new_id("c")
    with db.tx(ctx.conn):
        ctx.conn.execute(
            "INSERT INTO cards (id, account_id, tail, kind, status, per_tx_limit_fen)"
            " VALUES (?, ?, ?, 'virtual', 'active', ?)",
            (card_id, str(account["id"]), card_tail, DEFAULT_PER_TX_LIMIT_FEN),
        )
    audit.append(
        ctx.conn,
        "tool.executed",
        {
            "tool": "apply_virtual_card",
            "card_id": card_id,
            "card_tail": card_tail,
            "account_tail": str(account["tail"]),
            "state_change": "none->active",
        },
    )
    row = ctx.conn.execute("SELECT * FROM cards WHERE id = ?", (card_id,)).fetchone()
    return ok_result(
        _card_payload(
            row,
            status="active",
            idempotent=False,
            summary=(
                f"已为你开出一张虚拟卡，尾号 {card_tail}，"
                f"单笔限额 {fen_to_yuan_str(DEFAULT_PER_TX_LIMIT_FEN)} 元。"
            ),
        )
    )


def set_card_limit(
    ctx: ToolContext, params: dict[str, Any], token: str | None = None
) -> ToolResult:
    """调一张卡的单笔限额。新限额与现值相同算幂等成功。"""
    require_token("set_card_limit", params, token)
    card, error = _locate_card(ctx, params)
    if card is None:
        return _lookup_failure(error, params)
    current = int(card["per_tx_limit_fen"])
    target = int(params.get("per_tx_limit_fen", 0))
    if target <= 0 or target > MAX_PER_TX_LIMIT_FEN:
        return fail_result(
            "LIMIT_OUT_OF_RANGE",
            f"单笔限额只能设在 0.01 元到 {fen_to_yuan_str(MAX_PER_TX_LIMIT_FEN)} 元之间。",
        )
    if target == current:
        return ok_result(
            _card_payload(card, status=str(card["status"]), idempotent=True, summary=_limit_summary(card, target))
        )
    with db.tx(ctx.conn):
        changed = ctx.conn.execute(
            "UPDATE cards SET per_tx_limit_fen = ? WHERE id = ? AND per_tx_limit_fen = ?",
            (target, str(card["id"]), current),
        ).rowcount
    if changed != 1:
        return fail_result("CARD_STATE_INVALID", "这张卡的限额刚刚被改过，请重新确认当前限额。")
    audit.append(
        ctx.conn,
        "tool.executed",
        {
            "tool": "set_card_limit",
            "card_id": str(card["id"]),
            "card_tail": str(card["tail"]),
            "state_change": f"limit:{current}->{target}",
        },
    )
    payload = _card_payload(
        card, status=str(card["status"]), idempotent=False, summary=_limit_summary(card, target)
    )
    payload.update(
        {"per_tx_limit_fen": target, "per_tx_limit_yuan": fen_to_yuan_str(target), "previous_limit_fen": current}
    )
    return ok_result(payload)


def freeze_card(ctx: ToolContext, params: dict[str, Any], token: str | None = None) -> ToolResult:
    """冻结一张卡：active -> frozen。已冻结算幂等成功。"""
    require_token("freeze_card", params, token)
    return _transition(ctx, params, tool="freeze_card", to_status="frozen", allowed=("active",))


def unfreeze_card(ctx: ToolContext, params: dict[str, Any], token: str | None = None) -> ToolResult:
    """解冻一张卡：frozen -> active。已挂失的卡不能自助解冻，必须柜面。"""
    require_token("unfreeze_card", params, token)
    return _transition(ctx, params, tool="unfreeze_card", to_status="active", allowed=("frozen",))


def report_card_loss(
    ctx: ToolContext, params: dict[str, Any], token: str | None = None
) -> ToolResult:
    """挂失一张卡：非挂失状态 -> lost，且不可自助撤销。"""
    require_token("report_card_loss", params, token)
    return _transition(
        ctx, params, tool="report_card_loss", to_status="lost", allowed=("active", "frozen")
    )


def _transition(
    ctx: ToolContext,
    params: dict[str, Any],
    *,
    tool: str,
    to_status: str,
    allowed: tuple[str, ...],
) -> ToolResult:
    """一张卡的通用状态迁移：幂等、非法迁移拒绝、成功写审计。"""
    card, error = _locate_card(ctx, params)
    if card is None:
        return _lookup_failure(error, params)
    current = str(card["status"])
    if current == to_status:
        return ok_result(
            _card_payload(card, status=to_status, idempotent=True, summary=_state_summary(card, to_status, True))
        )
    if current not in allowed:
        code = "CARD_LOST" if current == "lost" else "CARD_STATE_INVALID"
        hint = "已挂失的卡只能到柜面办理解挂。" if current == "lost" else "这张卡当前状态不支持该操作。"
        return fail_result(code, f"尾号 {card['tail']} 的卡{_STATUS_ZH.get(current, current)}，{hint}")
    with db.tx(ctx.conn):
        changed = ctx.conn.execute(
            "UPDATE cards SET status = ? WHERE id = ? AND status = ?",
            (to_status, str(card["id"]), current),
        ).rowcount
    if changed != 1:
        return fail_result("CARD_STATE_INVALID", "这张卡的状态刚刚变了，请重新确认后再操作。")
    audit.append(
        ctx.conn,
        "tool.executed",
        {
            "tool": tool,
            "card_id": str(card["id"]),
            "card_tail": str(card["tail"]),
            "state_change": f"{current}->{to_status}",
        },
    )
    return ok_result(
        _card_payload(card, status=to_status, idempotent=False, summary=_state_summary(card, to_status, False))
    )


def _resolve_account(ctx: ToolContext, params: dict[str, Any]):
    """解析本次操作的账户：没给尾号就用主账户。"""
    tail = params.get("account_tail")
    if tail is None:
        return primary_account(ctx.conn, ctx.account_id), None
    account = find_account_by_tail(ctx.conn, str(tail), ctx.account_id)
    return account, (None if account is not None else "ACCOUNT_NOT_FOUND")


def _locate_card(ctx: ToolContext, params: dict[str, Any]):
    """按卡尾号定位唯一一张卡。返回 (row, error_code)。

    0 张 → CARD_NOT_FOUND；同账户下尾号重号多张 → CARD_AMBIGUOUS。
    宁可反问也不猜，因为猜错的下一句可能就是冻结/挂失错了卡。
    """
    account, account_error = _resolve_account(ctx, params)
    if account is None:
        return None, account_error
    tail = str(params.get("card_tail", "")).strip()
    if not tail:
        return None, "CARD_TAIL_REQUIRED"
    rows = ctx.conn.execute(
        "SELECT * FROM cards WHERE account_id = ? AND tail = ?",
        (str(account["id"]), tail),
    ).fetchall()
    if not rows:
        return None, "CARD_NOT_FOUND"
    if len(rows) > 1:
        return None, "CARD_AMBIGUOUS"
    return rows[0], None


def _lookup_failure(error: str | None, params: dict[str, Any]) -> ToolResult:
    """定位失败的统一文案；每种失败都给一个能照着做的下一步。"""
    tail = params.get("card_tail")
    if error == "CARD_TAIL_REQUIRED":
        return fail_result("CARD_TAIL_REQUIRED", "要操作哪张卡？请告诉我卡号后四位。")
    if error == "CARD_AMBIGUOUS":
        return fail_result("CARD_AMBIGUOUS", f"尾号 {tail} 对应不止一张卡，请到卡列表里确认后再操作。")
    if error == "ACCOUNT_NOT_FOUND":
        return fail_result("ACCOUNT_NOT_FOUND", f"没找到尾号 {params.get('account_tail')} 的账户。")
    return fail_result("CARD_NOT_FOUND", f"没找到尾号 {tail} 的卡。")


def _next_virtual_tail(conn, account_id: str) -> str | None:
    """挑一个本账户下还没用过的四位尾号，虚拟卡固定从 9xxx 段开始发。"""
    used = {
        str(row["tail"])
        for row in conn.execute("SELECT tail FROM cards WHERE account_id = ?", (account_id,))
    }
    for candidate in list(range(9000, 10000)) + list(range(1000, 9000)):
        text = str(candidate)
        if text not in used:
            return text
    return None


def _limit_summary(card, target_fen: int) -> str:
    return f"尾号 {card['tail']} 的单笔限额已设为 {fen_to_yuan_str(target_fen)} 元。"


def _state_summary(card, to_status: str, idempotent: bool) -> str:
    tail = str(card["tail"])
    if idempotent:
        return f"尾号 {tail} 的卡已经是{_STATUS_ZH.get(to_status, to_status)}状态，无需重复操作。"
    if to_status == "frozen":
        return f"尾号 {tail} 的卡已冻结，现在不能再用它付款。"
    if to_status == "lost":
        return f"尾号 {tail} 的卡已挂失，这张卡已经停用，请尽快到柜面补办。"
    return f"尾号 {tail} 的卡已恢复正常使用。"


def _card_payload(row, *, status: str, idempotent: bool, summary: str) -> dict[str, Any]:
    """写工具的统一返回体：卡的基本信息 + 是否幂等 + 一句给用户看的话。"""
    limit_fen = int(row["per_tx_limit_fen"])
    return {
        "card_id": str(row["id"]),
        "card_tail": str(row["tail"]),
        "kind": str(row["kind"]),
        "status": status,
        "status_zh": _STATUS_ZH.get(status, status),
        "per_tx_limit_fen": limit_fen,
        "per_tx_limit_yuan": fen_to_yuan_str(limit_fen),
        "idempotent": idempotent,
        "summary": summary,
    }


def _card_view(row) -> dict[str, Any]:
    limit_fen = int(row["per_tx_limit_fen"])
    return {
        "card_id": str(row["id"]),
        "account_tail": str(row["account_tail"]),
        "tail": str(row["tail"]),
        "kind": str(row["kind"]),
        "status": str(row["status"]),
        "per_tx_limit_fen": limit_fen,
        "per_tx_limit_yuan": fen_to_yuan_str(limit_fen),
    }
