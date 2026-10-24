"""三个动钱的写工具：转账、预授权、商户支付。

对应文档：docs/06_工具层与场景实现规格.md 6.3、docs/05 资金两阶段协议。

三条硬规矩：
1. 每个 handler 的第一句都是 require_token，没有令牌当场 MissingCapability；
2. 余额只经 app/funds 的 hold/commit 动，工具层绝不直接 UPDATE accounts；
3. 工具不判断该不该做，额度/风控/二次因子是 policy 的活（docs/06 6.7）。
"""

from __future__ import annotations

import logging
import sqlite3
from typing import Any, Final

# 模块级导入（不用 from 包 import 模块）：tests/test_imports.py 用 ast 按模块名登记依赖，
# 包级导入会被它漏掉，允许依赖表就会与代码现状对不上。
import app.funds as funds  # noqa: PLR0402
from app.clock import (
    BankError,
    HoldExpired,
    InsufficientFunds,
    PolicyDenied,
    ValidationError,
    now_ms,
)
from app.models import ToolResult
from app.money import fen_to_yuan_str
from app.security import token as token_mod
from app.tools.base import (
    ToolContext,
    build_action,
    daily_cap_fen,
    ensure_system_payee,
    fail_result,
    ok_result,
    payee_exists,
    payee_name,
    primary_account,
    require_token,
)

log = logging.getLogger(__name__)

# 自助冲正窗口：7 天。再久就得柜面，否则账目没有终点。
REVERSAL_WINDOW_MS: Final[int] = 7 * 86_400_000

MERCHANT_WHITELIST: Final[dict[str, str]] = {
    "flower_shop": "鲜花店",
    "cake_shop": "蛋糕店",
}

HELD_TEXT: Final[str] = "钱已经锁定，还没有扣款。"
COMMITTED_TEXT: Final[str] = "钱已经转出去了。"
#: 未结算时用户看到的原话。系统里没有任何重试 worker，所以不能写"稍后补偿"：
#: hold 到期（HOLD_TTL_MS）后会自动释放，钱回到可用余额，需要用户重新发起。
UNSETTLED_TEXT: Final[str] = (
    "钱已经锁定，但扣款没成功。系统不会自动重试：这笔锁定 5 分钟后自动释放，"
    "钱回到可用余额；要转的话需要我重新发起一次。"
)
REVERSAL_SCOPE_TEXT: Final[str] = (
    "这是本地的账务冲正（反向记账），不是把已经到对方账上的钱追回来；"
    "真实跨行退汇要走争议流程，本演示环境不具备该能力。"
)
DAILY_LIMIT_TEXT: Final[str] = "今天已经花过日限额了，这笔没转出去。"
NOT_OWNED_TEXT: Final[str] = "通讯录里没有属于你的这个收款人。"


def _payee_owned_by(conn: sqlite3.Connection, payee_id: str, account: sqlite3.Row) -> bool:
    """收款人必须在本次操作账户所属用户的通讯录里。

    纵深防御：工具层是能被直接调用的公开接口，不能假设调用方一定先跑过
    resolve_payee（红队 C03 就是绕过它直调 transfer，把他名下的收款人转走了）。
    """
    row = conn.execute("SELECT user_id FROM payees WHERE id = ?", (payee_id,)).fetchone()
    return row is not None and str(row["user_id"]) == str(account["user_id"])


def transfer(
    ctx: ToolContext, params: dict[str, Any], token: str | None = None
) -> ToolResult:
    """转账：一次调用把两阶段资金走完——建 hold 之后立刻 commit（docs/06 6.3）。

    params 里可以带 payee_name（展示名，给 R06 的确认文案念），但它不进业务逻辑：
    收款人身份只认 payee_id，存在性与归属校验都看 id。
    """
    require_token("transfer", params, token)
    account = primary_account(ctx.conn, ctx.account_id)
    payee_id = str(params.get("payee_id", ""))
    if not payee_exists(ctx.conn, payee_id):
        return fail_result("PAYEE_NOT_FOUND", f"通讯录里没有这个收款人（{payee_id}）。")
    if not _payee_owned_by(ctx.conn, payee_id, account):
        return fail_result("PAYEE_NOT_OWNED", NOT_OWNED_TEXT)
    amount_fen = int(params["amount_fen"])
    action = build_action("transfer", params, str(account["id"]), amount_fen)
    try:
        hold = funds.initiate(
            ctx.conn,
            action,
            session_id=ctx.session_key(),
            node_id=ctx.node_key(),
            # 幂等键带上本轮 run_id：同一轮里点两次确认仍然幂等，
            # 但用户在同一会话里再说一次「再转 500」会被当成新的一笔，不许假成功。
            run_id=ctx.task_id or "",
            token=token,
            secret=ctx.secret,
        )
    except InsufficientFunds as exc:
        return _insufficient(exc)
    return _settle_transfer(
        ctx,
        {
            "hold_id": hold.hold_id,
            "status": hold.status,
            "amount_fen": amount_fen,
            "amount_yuan": _yuan(amount_fen),
            "payee_id": payee_id,
            "payee_name": payee_name(ctx.conn, payee_id),
            "expires_ms": hold.expires_ms,
            "idempotent": bool(hold.reused),
            "message": HELD_TEXT,
        },
        token,
    )


def _settle_transfer(
    ctx: ToolContext, data: dict[str, Any], token: str | None = None
) -> ToolResult:
    """第二阶段：立刻 commit，让 transfer 一次调用就把两阶段资金走完。

    不可逆的 commit 必须留在工具层：planner 只消费返回值，不 import app.funds。
    commit 失败不往上抛，把 settle_error 塞进 data，钱仍锁在 hold 里等释放。
    """
    hold_id = str(data["hold_id"])
    try:
        outcome = funds.commit(
            ctx.conn, hold_id, daily_limit_fen=daily_cap_fen(token, ctx.secret)
        )
    except PolicyDenied as exc:
        # 日累计硬线：这笔是**被拒**了（钱没扣、hold 还在），要说清楚为什么。
        log.warning("transfer.daily_limit_exceeded hold=%s code=%s", hold_id, exc.code)
        data["settle_error"] = exc.code
        data["message"] = DAILY_LIMIT_TEXT
        return fail_result(exc.code, DAILY_LIMIT_TEXT, data)
    except BankError as exc:
        log.warning("transfer.commit_failed hold=%s code=%s", hold_id, exc.code)
        data["settle_error"] = exc.code
        data["message"] = UNSETTLED_TEXT
        return ok_result(data)
    data["status"] = "committed"
    data["balance_after_fen"] = int(outcome.balance_after_fen)
    data["idempotent"] = bool(outcome.idempotent)
    data["message"] = COMMITTED_TEXT
    return ok_result(data)


def reserve_funds(
    ctx: ToolContext, params: dict[str, Any], token: str | None = None
) -> ToolResult:
    """预授权：只占额度不产生转账流水，收款人是系统占位 p_system。"""
    require_token("reserve_funds", params, token)
    account = primary_account(ctx.conn, ctx.account_id)
    ensure_system_payee(ctx.conn, account)
    amount_fen = int(params["amount_fen"])
    action = build_action("reserve_funds", params, str(account["id"]), amount_fen)
    try:
        hold = funds.initiate(
            ctx.conn,
            action,
            session_id=ctx.session_key(),
            node_id=ctx.node_key(),
            # 幂等键带上本轮 run_id：同一轮里点两次确认仍然幂等，
            # 但用户在同一会话里再说一次「再转 500」会被当成新的一笔，不许假成功。
            run_id=ctx.task_id or "",
            token=token,
            secret=ctx.secret,
            payee_id=funds.SYSTEM_PAYEE_ID,
        )
    except InsufficientFunds as exc:
        return _insufficient(exc)
    return ok_result(
        {
            "hold_id": hold.hold_id,
            "status": hold.status,
            "amount_fen": amount_fen,
            "amount_yuan": _yuan(amount_fen),
            "purpose": str(params.get("purpose", "")),
            "expires_ms": hold.expires_ms,
            "idempotent": bool(hold.reused),
            "message": "这笔钱先给你留着，还没有扣款。",
        }
    )


def pay_merchant(
    ctx: ToolContext, params: dict[str, Any], token: str | None = None
) -> ToolResult:
    """商户支付：给了有效 hold 就直接 commit，没给就现建 hold 再 commit。"""
    require_token("pay_merchant", params, token)
    merchant = str(params.get("merchant", ""))
    if merchant not in MERCHANT_WHITELIST:
        return fail_result(
            "MERCHANT_NOT_ALLOWED",
            f"暂不支持商户 {merchant}。",
            {"allowed_merchants": sorted(MERCHANT_WHITELIST)},
        )
    account = primary_account(ctx.conn, ctx.account_id)
    ensure_system_payee(ctx.conn, account)
    amount_fen = int(params["amount_fen"])
    action = build_action("pay_merchant", params, str(account["id"]), amount_fen)
    hold_id = params.get("hold_id")
    if hold_id:
        return _commit_existing(ctx, str(hold_id), str(account["id"]), token, action, params)
    try:
        hold = funds.initiate(
            ctx.conn,
            action,
            session_id=ctx.session_key(),
            node_id=ctx.node_key(),
            # 幂等键带上本轮 run_id：同一轮里点两次确认仍然幂等，
            # 但用户在同一会话里再说一次「再转 500」会被当成新的一笔，不许假成功。
            run_id=ctx.task_id or "",
            token=token,
            secret=ctx.secret,
            payee_id=funds.SYSTEM_PAYEE_ID,
        )
    except InsufficientFunds as exc:
        return _insufficient(exc)
    return _commit(ctx, hold.hold_id, params, token)


def reverse_transfer(ctx: ToolContext, params: dict[str, Any], token: str | None = None) -> ToolResult:
    '''冲正一笔已出账的转账：钱加回原账户，并留下双向关联。

    为什么要有窗口：无限期可冲正等于给了一个「撤销上月生活费」的口子，账目就没有终点了。
    为什么金额取自原交易而不是参数：让模型自己报金额，就是让不可信区决定退多少钱。
    '''
    require_token('reverse_transfer', params, token)
    txn_id = str(params.get('txn_id') or '').strip()
    if not txn_id:
        return fail_result('TXN_REQUIRED', '要先知道是哪一笔交易才能冲正。')
    reason = str(params.get('reason') or '').strip() or '用户申请冲正'
    account = primary_account(ctx.conn, ctx.account_id)
    original = ctx.conn.execute(
        'SELECT * FROM transactions WHERE id = ? AND account_id = ?',
        (txn_id, str(account['id'])),
    ).fetchone()
    if original is None:
        return fail_result('TXN_NOT_FOUND', '名下没有找到这笔交易。')
    amount_fen = int(original['amount_fen'])
    if original['reversed_by']:
        return ok_result(
            {
                'original_txn_id': txn_id,
                'reversal_txn_id': str(original['reversed_by']),
                'amount_fen': amount_fen,
                'amount_yuan': fen_to_yuan_str(amount_fen),
                'idempotent': True,
                'summary': f'这笔 {fen_to_yuan_str(amount_fen)} 元已经冲正过了，没有重复入账。',
            }
        )
    age_ms = now_ms() - int(original['ts_ms'])
    if age_ms > REVERSAL_WINDOW_MS:
        days = REVERSAL_WINDOW_MS // 86_400_000
        return fail_result('TXN_TOO_OLD', f'超过 {days} 天的交易不能自助冲正，需要到柜面办理。')
    action = build_action(
        'reverse_transfer',
        {'txn_id': txn_id, 'reason': reason, 'amount_fen': amount_fen},
        str(account['id']),
        amount_fen,
    )
    try:
        outcome = funds.refund(
            ctx.conn,
            action,
            original_txn_id=txn_id,
            reason=reason,
            session_id=ctx.session_key(),
            token=token,
            secret=ctx.secret,
        )
    except ValidationError as exc:
        # ValidationError 的第一参数是中文人话，具体原因在 fields['reason'] 里，
        # 所以错误码优先取 reason，取不到才回落到基类的 VALIDATION。
        return fail_result(str(exc.fields.get('reason') or exc.code), exc.user_text)
    return ok_result(
        {
            'original_txn_id': txn_id,
            'reversal_txn_id': outcome['reversal_txn_id'],
            'amount_fen': amount_fen,
            'amount_yuan': fen_to_yuan_str(amount_fen),
            'balance_after_fen': int(outcome['balance_after_fen']),
            'idempotent': bool(outcome['idempotent']),
            'summary': f'已经做了账务冲正：{fen_to_yuan_str(amount_fen)} 元记回你的账户。',
            'scope': 'local_reversal',
            'boundary': REVERSAL_SCOPE_TEXT,
        }
    )


def _commit_existing(
    ctx: ToolContext,
    hold_id: str,
    account_id: str,
    token: str | None,
    action: Any,
    params: dict[str, Any],
) -> ToolResult:
    """复用已有 hold：先按第 6 步重新校一次令牌，再落第二阶段。"""
    hold = funds.fetch_hold(ctx.conn, hold_id)
    if hold is None or str(hold.account_id) != account_id:
        return fail_result("HOLD_NOT_FOUND", f"找不到这笔预授权（{hold_id}）。")
    token_mod.verify(ctx.conn, str(token), action, secret=ctx.secret)
    return _commit(ctx, hold_id, params, token)


def _commit(
    ctx: ToolContext,
    hold_id: str,
    params: dict[str, Any],
    token: str | None = None,
) -> ToolResult:
    try:
        outcome = funds.commit(
            ctx.conn, hold_id, daily_limit_fen=daily_cap_fen(token, ctx.secret)
        )
    except HoldExpired as exc:
        return fail_result("HOLD_EXPIRED", exc.user_text, {"hold_id": hold_id})
    except BankError as exc:
        # 别的拒绝（例如日累计硬线）：钱没扣、hold 还在，如实报错，别让异常逃出去。
        return fail_result(exc.code, exc.user_text, {"hold_id": hold_id})
    amount_fen = int(outcome.amount_fen)
    return ok_result(
        {
            "hold_id": hold_id,
            "status": "committed",
            "amount_fen": amount_fen,
            "amount_yuan": _yuan(amount_fen),
            "merchant": str(params.get("merchant", "")),
            "item": str(params.get("item", "")),
            "balance_after_fen": int(outcome.balance_after_fen),
            "idempotent": bool(outcome.idempotent),
            "message": f"已经付了 {params.get('item', '')} {fen_to_yuan_str(amount_fen)} 元。",
        }
    )


def _insufficient(exc: InsufficientFunds) -> ToolResult:
    """余额不够：把还差多少一并说清楚，让人知道差在哪。"""
    balance_fen = int(exc.fields.get("balance_fen", 0))
    amount_fen = int(exc.fields.get("amount_fen", 0))
    short_fen = max(amount_fen - balance_fen, 0)
    return fail_result(
        "INSUFFICIENT_FUNDS",
        f"余额不够，还差 {short_fen / 100:.2f} 元。",
        {
            "balance_fen": balance_fen,
            "amount_fen": amount_fen,
            "short_fen": short_fen,
            "account_id": str(exc.fields.get("account_id", "")),
        },
    )


def _yuan(amount_fen: int) -> str:
    """分转成带千分位的中文金额串。"""
    return fen_to_yuan_str(amount_fen)


__all__ = ["MERCHANT_WHITELIST", "pay_merchant", "reserve_funds", "transfer"]
