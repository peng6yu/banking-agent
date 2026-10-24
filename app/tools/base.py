"""工具层公共壳：执行上下文、令牌闸门、结果构造与账户解析。

对应文档：docs/06_工具层与场景实现规格.md 6.2、docs/02_目录结构与文件职责.md 2.2。

这一层只做三件事：把执行上下文传下去、把「没有令牌」变成硬失败、把工具返回
统一成 ToolResult。该不该做是 policy 的活，工具层不做任何裁决（docs/06 6.7）。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any

from app.clock import BankError, MissingCapability, ValidationError
from app.funds import SYSTEM_PAYEE_ID
from app.models import Action, ToolResult
from app.policy.rules import ABSOLUTE_DAILY_CAP_FEN, DAILY_RISK_THRESHOLD_FEN
from app.security import token as token_mod


@dataclass(frozen=True)
class ToolContext:
    """一次工具执行的外部事实。

    conn 由调用方（执行器）持有：工具不开连接、不关连接，测试可以注入临时库。
    account_id 是本次会话默认操作的账户，没给就按主账户解析（先储蓄卡后信用卡）。
    secret 是能力令牌的签名密钥，只在写工具里透传给 security.token 用。
    """

    conn: sqlite3.Connection
    session_id: str | None = None
    node_id: str | None = "n0"
    task_id: str | None = None
    account_id: str | None = None
    secret: str | None = None

    def session_key(self) -> str:
        """幂等键要用 session_id，匿名调用也得有稳定的值。"""
        return self.session_id or "s_anonymous"

    def node_key(self) -> str:
        return self.node_id or "n0"


def require_token(tool: str, params: dict[str, Any], token: str | None) -> str:
    """写工具的第一句：没有令牌就是没有授权，当场抛 MissingCapability。

    这里只判「有没有」，不判「对不对」；签名、过期、nonce、参数指纹这四步由
    app.security.token.verify 在真正动钱之前完成（docs/05 5.3）。
    """
    if token:
        return str(token)
    raise MissingCapability(reason="TOKEN_MISSING", tool=tool, params_keys=sorted(params))


def commit_limits(
    token: str | None, secret: str | None
) -> tuple[int | None, int | None]:
    """这次出账要带下去的两条日累计线：(风险阈值, 绝对上限)。

    工具层不自己判断「能不能过线」——那是 policy 的活（docs/06 6.7），
    policy 的结论已经写在令牌的等级里（token.level 就是当次决策的等级）。
    这里只负责把两条线原样交给资金层，让钱在扣款那一刻、同一个事务里再核一次：
    - 黄牌：带上风险阈值。资金层若发现这笔会让当日累计越线（说明这期间别的
      交易已经把它推过线了），就整笔拒绝并抛 DAILY_RISK_UPGRADE_REQUIRED，
      上层重新走强验证——旧黄卡不能替红操作签字。
    - 红牌：这笔本就已经在红灯下验过人脸+短信，风险阈值不再拦它（传 None）；
      但绝对上限（若部署配置了）依然生效，因子不能给监管额度加码。
    - 令牌解不开：按最严处理（守住风险阈值），宁可多验一次也不越级。
    """
    if not token or token_mod is None:
        return (DAILY_RISK_THRESHOLD_FEN, ABSOLUTE_DAILY_CAP_FEN)
    try:
        payload = token_mod.decode(token, secret=secret)
    except (BankError, ValueError, KeyError, TypeError):
        return (DAILY_RISK_THRESHOLD_FEN, ABSOLUTE_DAILY_CAP_FEN)
    if payload.level == "red":
        return (None, ABSOLUTE_DAILY_CAP_FEN)
    return (DAILY_RISK_THRESHOLD_FEN, ABSOLUTE_DAILY_CAP_FEN)


def ok_result(data: dict[str, Any] | None = None) -> ToolResult:
    """成功的工具结果。"""
    return ToolResult(ok=True, data=data or {})


def fail_result(
    error_code: str,
    error_text: str,
    data: dict[str, Any] | None = None,
) -> ToolResult:
    """业务分支上的失败：ok=False + 稳定错误码 + 能直接念给人听的中文。"""
    return ToolResult(
        ok=False, data=data or {}, error_code=error_code, error_text=error_text
    )


def build_action(
    tool: str,
    params: dict[str, Any],
    account_id: str,
    amount_fen: int | None = None,
) -> Action:
    """按工具名与原始参数拼 Action。

    params 必须原样传：令牌里的 params_hash 就是对这同一份参数算的，
    工具层任何「顺手补默认值」都会让第 6 步校验失败（docs/05 5.3）。
    """
    return Action(tool=tool, params=params, account_id=account_id, amount_fen=amount_fen)


def account_rows(
    conn: sqlite3.Connection, account_id: str | None = None
) -> list[sqlite3.Row]:
    """列出账户；给了 account_id 就只回同一个人的账户，单用户演示下即全部。"""
    if account_id:
        rows = conn.execute(
            "SELECT * FROM accounts"
            " WHERE user_id = (SELECT user_id FROM accounts WHERE id = ?)"
            " ORDER BY id",
            (account_id,),
        ).fetchall()
        return list(rows)
    return list(conn.execute("SELECT * FROM accounts ORDER BY id").fetchall())


def find_account_by_tail(
    conn: sqlite3.Connection,
    tail: str,
    account_id: str | None = None,
) -> sqlite3.Row | None:
    """按卡号后四位找账户，找不到返回 None（由调用方决定算业务失败还是报错）。"""
    for row in account_rows(conn, account_id):
        if str(row["tail"]) == str(tail):
            return row
    return None


def primary_account(
    conn: sqlite3.Connection, account_id: str | None = None
) -> sqlite3.Row:
    """本次操作的账户：显式指定 > 储蓄卡 > 任意一张，全都不存在才算错。"""
    if account_id:
        row = conn.execute("SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone()
        if row is None:
            raise ValidationError("账户不存在", account_id=account_id)
        return row
    row = conn.execute(
        "SELECT * FROM accounts"
        " ORDER BY CASE type WHEN 'debit' THEN 0 ELSE 1 END, id LIMIT 1"
    ).fetchone()
    if row is None:
        raise ValidationError("账户不存在", reason="NO_ACCOUNTS")
    return row


def payee_exists(conn: sqlite3.Connection, payee_id: str) -> bool:
    """通讯录里有没有这个收款人。"""
    row = conn.execute("SELECT 1 FROM payees WHERE id = ?", (payee_id,)).fetchone()
    return row is not None


def payee_name(conn: sqlite3.Connection, payee_id: str) -> str:
    """收款人显示名；查不到就回显 id，不抛错。"""
    row = conn.execute("SELECT name FROM payees WHERE id = ?", (payee_id,)).fetchone()
    return str(row["name"]) if row else str(payee_id)


def ensure_system_payee(conn: sqlite3.Connection, account: sqlite3.Row) -> str:
    """建出系统占位收款人 p_system 并返回它的 id。

    holds.payee_id 有外键指向 payees，reserve_funds / pay_merchant 没有真实收款人，
    必须先把这个占位行插出来，否则建 hold 会被外键拦下。幂等，重放不插第二行。
    """
    conn.execute(
        "INSERT OR IGNORE INTO payees (id, user_id, name, bank, card_tail, alias)"
        " VALUES (?, ?, ?, ?, ?, NULL)",
        (SYSTEM_PAYEE_ID, account["user_id"], "系统预授权", "本行", "0000"),
    )
    return SYSTEM_PAYEE_ID


def tool_result(
    ok: bool,
    data: dict[str, Any] | None = None,
    error_code: str | None = None,
    error_text: str | None = None,
) -> ToolResult:
    """统一的 ToolResult 出口：成功只给 data，失败必须带错误码和中文说明。"""
    return ToolResult(ok=ok, data=data or {}, error_code=error_code, error_text=error_text)


__all__ = [
    "ToolContext",
    "account_rows",
    "build_action",
    "commit_limits",
    "ensure_system_payee",
    "fail_result",
    "find_account_by_tail",
    "ok_result",
    "payee_exists",
    "payee_name",
    "primary_account",
    "require_token",
    "tool_result",
]
