"""生日跨场景计划：一笔预算，拆成鲜花与蛋糕两项，生日前两天触发。

四条设计边界（写在代码里，而不是只写进文档）：

1. **预算预留真实占用可支配金额。** 预留不扣余额、不建 5 分钟临时 hold，但会从
   「可支配金额」（余额 - 有效预留）里扣掉，所以同一笔钱不能再去转账、预约出账、
   AA 或申购——资金层的余额校验读的就是 funds.available_fen 这一层。它仍然不是银行
   侧冻结：真实预授权要商户参与，这里做的是本系统自建的可用额度约束（plan_budgets），
   取消/拒绝/失败之后必须还回去。
2. **到点后台只检查、不代扣。** 默认路径：生日前两天核对余额是否够预算 → 把两个
   待确认项目推给用户。真正的付款走助手的确认卡片：策略裁决 → 用户确认/多因子 →
   能力令牌 → 工具执行，和手动转账同一条路径，后台调度绕不过去。
   **唯一例外**：用户建计划时显式勾选保存一次性授权（plan_authorizations），
   冻结当时的清单/金额/账户/窗口；到点 run_due_authorized 先用最新日累计重新裁决，
   绿灯或黄灯且参数完全没变才自动付，红灯/参数漂移/额度不够一律暂停计划待确认。
   授权用一次即 used，用户拒绝/取消/修改计划即作废，后台不得再启动付款。
3. **重复触发不重复下单。** 预算行有状态机（reserved/paying/committed/declined/
   failed/released），领取是一次带条件的 UPDATE，抢不到就跳过；已 committed 的
   项目再被触发只返回 already_paid，不会再建一张执行图。
4. **预留必须可回收。** 取消/失败/完成/用户拒绝之后，仍在 reserved 的行要么继续
   占款（reserved 仍在 = 仍占款），要么明确释放；reconcile() 负责把「进程崩在
   支付中间」的行对回真实 hold 与流水，避免额度被永久漏占却查不出来。
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import datetime as _dt
import json
import logging
from typing import Any, Final

from app import audit, clock, db, funds
from app.clock import BankError, InsufficientFunds, ValidationError
from app import ids
from app.ids import new_id
from app.money import fen_to_yuan_str
from app.planner import executor
from app.planner.dag import DAGNode, Dag
from app.policy.engine import decide
from app.policy.rules import PolicyContext
from app.tools.base import build_action
from app.tools.transfers import MERCHANT_WHITELIST

log = logging.getLogger(__name__)

DEFAULT_BUDGET_FEN: Final[int] = 100_000
TRIGGER_DAYS_BEFORE: Final[int] = 2
TRIGGER_HOUR: Final[int] = 9
MAX_ITEMS: Final[int] = 4
MAX_TITLE: Final[int] = 60
BUDGET_STATUSES: Final[tuple[str, ...]] = (
    "reserved",
    "paying",
    "committed",
    "declined",
    "failed",
    "released",
)
DEFAULT_ITEMS: Final[tuple[dict[str, Any], ...]] = (
    {"key": "flowers", "label": "鲜花", "merchant": "flower_shop", "amount_fen": 40_000},
    {"key": "cake", "label": "蛋糕", "merchant": "cake_shop", "amount_fen": 60_000},
)
BUDGET_BOUNDARY: Final[str] = (
    "这里的「预算预留」真实占用可支配金额：余额不动，但可用额度会把它算进去，同一笔钱"
    "不能再去转账、预约出账或理财申购。它不是银行侧冻结，也不需要商户参与——是本系统"
    "自建的可用额度约束（plan_budgets）；取消、拒绝或失败之后会释放回可支配金额。"
)
# 审计里的 scope 从 bookkeeping_reservation 改过来：老数据保留旧值，新数据用这个，
# 对账时能一眼分出「当年只是记账」和「现在真实占额度」两种口径。
RESERVATION_SCOPE: Final[str] = "available_ledger_reservation"
TERMINAL_BUDGET_STATUSES: Final[tuple[str, ...]] = (
    "committed",
    "declined",
    "failed",
    "released",
)


class BudgetNotCovered(BankError):
    """建计划时可用额度不够：明确拒绝，不建半个计划、不动一分钱。

    单独给码而不是复用 VALIDATION：前端/调用方要能区分「参数写错了」和
    「额度不够」——前者让用户改参数，后者要先去腾额度或等余额到账。
    """

    code = "BUDGET_NOT_COVERED"
    http_status = 409
    user_text = "可支配金额不够这笔预算，先把预算调小一点，或者等余额到账再建。"
PAY_BOUNDARY: Final[str] = (
    "后台到点只提醒，不代扣；真正付款要你在助手里确认一次，"
    "同样过策略裁决、能力令牌（必要时多因子）。"
)
TIME_TEXT_FMT: Final[str] = "%Y-%m-%d %H:%M"


def _now_ms() -> int:
    return clock.now_ms()


def _time_text(ms: int) -> str:
    return _dt.datetime.fromtimestamp(ms / 1000).strftime(TIME_TEXT_FMT)


def _load(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    try:
        loaded = json.loads(str(raw))
    except (TypeError, ValueError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _resolve_account(conn, user_id: str, account_id: str | None) -> Any:
    """预算记在哪个账户上：显式指定 > 这个用户名下的储蓄卡 > 任意一张。

    显式指定时必须属于本人：对账是按 account_id 查预留的，记到别人账户上
    就会出现「我的额度被占了、他的额度没少」这种永远对不平的账。
    """
    if account_id:
        row = conn.execute("SELECT * FROM accounts WHERE id = ?", (str(account_id),)).fetchone()
        if row is None:
            raise ValidationError("账户不存在", account_id=account_id)
        if str(row["user_id"]) != str(user_id):
            raise ValidationError("这个账户不属于当前用户", account_id=account_id)
        return row
    row = conn.execute(
        "SELECT * FROM accounts WHERE user_id = ?"
        " ORDER BY CASE type WHEN 'debit' THEN 0 ELSE 1 END, id LIMIT 1",
        (str(user_id),),
    ).fetchone()
    if row is None:
        raise ValidationError("名下没有可用账户", reason="NO_ACCOUNTS", user_id=user_id)
    return row


def account_summary(conn, account_id: str) -> dict[str, Any]:
    """账户额度视图：余额、有效预留、可支配金额三个数必须一起给。

    只回余额的话，用户看到 12860 元却不知道为什么转 1000 元被拒——另外 1000 元
    正被生日预算占着。预留不是冻结：它不进流水、不影响余额，取消/拒绝/失败后
    下一次查询就该还回可支配金额里。
    """
    row = conn.execute("SELECT * FROM accounts WHERE id = ?", (str(account_id),)).fetchone()
    if row is None:
        raise ValidationError("账户不存在", account_id=account_id)
    balance = int(row["balance_fen"])
    reserved = funds.reserved_budget_fen(conn, str(account_id))
    available = balance - reserved
    return {
        "account_id": str(account_id),
        "balance_fen": balance,
        "balance_yuan": fen_to_yuan_str(balance),
        "reserved_budget_fen": reserved,
        "reserved_budget_yuan": fen_to_yuan_str(reserved),
        "available_fen": available,
        "available_yuan": fen_to_yuan_str(available),
        "note": "可支配金额 = 余额 - 生日/计划预算的有效预留；预留不是冻结，取消或失败会释放。",
        "boundary": BUDGET_BOUNDARY,
    }


def _birthday_end_ms(day: _dt.date) -> int:
    """执行窗口的右端：生日当天 23:59:59（本地时区）。

    一次性授权的有效期就截止到这儿：生日都过完了还允许后台自动扣款，
    等于给了一张永不过期的代扣券。
    """
    return _ms(_dt.datetime(day.year, day.month, day.day, 23, 59, 59))


def _latest_hold_id(conn, *, session_id: str, account_id: str, amount_fen: int) -> str:
    """本轮执行真正用掉的那条 hold：按 会话+账户+金额 取最近一条。

    为什么要反查：hold 是 pay_merchant 在工具层建的，执行器只回 ok/summary，
    不把 hold_id 带进事件流。按会话反查是能对上账的最短链路，也不必为了拿一个
    id 去改执行器或工具层（那两块不归预算模块管）。查不到返回空串，不抛错：
    拿不到 hold_id 只影响追溯，不能因此把一笔已经成功的付款判成失败。
    """
    row = conn.execute(
        "SELECT id FROM holds WHERE session_id = ? AND account_id = ? AND amount_fen = ?"
        " ORDER BY created_ms DESC, rowid DESC LIMIT 1",
        (session_id, account_id, amount_fen),
    ).fetchone()
    return str(row["id"]) if row else ""


def _assert_available_for_item(conn, account_id: str, amount_fen: int) -> None:
    """付款前的可支配校验：领取（reserved→paying）之后这笔预算不再算预留。

    所以真正要覆盖的条件是「余额 >= 其它有效预留 + 本笔金额」；等价于
    balance < reserved_budget_fen（reserved 里还含本笔）就付不出。提前判一次，
    用户拿到的是清楚的「可支配不够」，而不是执行到一半工具层抛 INSUFFICIENT_FUNDS。
    """
    row = conn.execute("SELECT balance_fen FROM accounts WHERE id = ?", (account_id,)).fetchone()
    if row is None:
        raise ValidationError("账户不存在", account_id=account_id)
    balance = int(row["balance_fen"])
    reserved = funds.reserved_budget_fen(conn, account_id)
    if balance < reserved:
        raise InsufficientFunds(
            "可支配金额不够这一项，先把别的安排收一收。",
            balance_fen=balance,
            reserved_budget_fen=reserved,
            available_fen=balance - reserved,
            amount_fen=amount_fen,
            account_id=account_id,
        )


# --------------------------------------------------------------- 一次性授权
#
# 任务四：建生日计划时可以顺手保存一份一次性授权——用户预先同意「到点后按这份
# 清单自动付款」。它是这份**特定版本**计划的一次性通行证，不是免确认黑箱：
#   1. 版本必须对得上：用户改标题/步骤 → meta.version +1，旧授权当场作废；
#   2. 账户、预算、每一项的商户/金额必须与当前预算行逐项一致；
#   3. 窗口之前不执行；到点后按**最新**日累计重新裁决，红灯或任何参数变化就
#      退回待确认（needs_verification），一分钱都不自动扣；
#   4. 一次性的意思是：整份清单付完即 used；用户拒绝、取消、改计划、窗口过期
#      即 revoked/expired，后台再也不能拿它启动付款。

AUTH_STATUSES: Final[tuple[str, ...]] = ("active", "used", "revoked", "expired")
AUTH_SCOPE_SCHEMA: Final[int] = 1
AUTH_BOUNDARY: Final[str] = (
    "一次性授权只覆盖保存时那份计划：清单/商户/金额/账户/执行窗口任何一项变化都失效；"
    "到点先用最新日累计重新裁决，红灯不自动付款。它不是银行代扣，付完即 used，"
    "拒绝、取消、修改或过期即作废。"
)

_AUTH_STATUS_LABELS: Final[dict[str, str]] = {
    "active": "授权有效",
    "used": "已使用",
    "revoked": "已作废",
    "expired": "已过期",
}


def _plan_version(meta: dict[str, Any]) -> str:
    """计划版本号：只有用户改内容才 +1（见 on_plan_updated），不是 updated_ms。

    拿 updated_ms 当版本会误伤：后台到点提醒、把计划暂停成待确认都会更新它，
    用户什么都没改，授权也会被判成「计划已变」。
    """
    try:
        return str(int(meta.get("version") or 1))
    except (TypeError, ValueError):
        return "1"


def _authorization_row(conn, plan_id: str, *, status: str | None = None):
    """取计划最近一条授权；status 给了就只要那个状态。"""
    sql = "SELECT * FROM plan_authorizations WHERE plan_id = ?"
    args: list[Any] = [plan_id]
    if status:
        sql += " AND status = ?"
        args.append(status)
    sql += " ORDER BY created_ms DESC, rowid DESC LIMIT 1"
    return conn.execute(sql, args).fetchone()


def _authorization_view(row) -> dict[str, Any]:
    scope = _load(row["scope_json"])
    decision = _load(row["decision_json"])
    status = str(row["status"])
    return {
        "id": str(row["id"]),
        "plan_id": str(row["plan_id"]),
        "plan_version": str(row["plan_version"]),
        "account_id": str(row["account_id"] or ""),
        "status": status,
        "status_label": _AUTH_STATUS_LABELS.get(status, status),
        "scope": scope,
        "decision": decision,
        "items": list(scope.get("items") or []),
        "created_ms": int(row["created_ms"]),
        "created_text": _time_text(int(row["created_ms"])),
        "used_ms": int(row["used_ms"]) if row["used_ms"] is not None else None,
        "revoked_ms": int(row["revoked_ms"]) if row["revoked_ms"] is not None else None,
        "expires_ms": int(row["expires_ms"]) if row["expires_ms"] is not None else None,
        "expires_text": _time_text(int(row["expires_ms"])) if row["expires_ms"] is not None else "",
        "active": status == "active",
        "boundary": AUTH_BOUNDARY,
    }


def authorization(conn, plan_id: str) -> dict[str, Any] | None:
    """给界面看的授权视图（最近一条，任何状态都返回）。"""
    row = _authorization_row(conn, plan_id)
    return _authorization_view(row) if row is not None else None


def _policy_context(conn, account_id: str, now: int) -> PolicyContext:
    """裁决需要的外部事实：用**当下**的日累计，不缓存保存授权时的旧值。"""
    return PolicyContext(
        spent_today_fen=funds.spent_today_fen(conn, account_id, at_ms=now),
        now_ms=now,
    )


def _decide_item(conn, account_id: str, item: dict[str, Any], now: int):
    """对单个预算项目跑一次三色裁决（参数和 pay_merchant 工具层一致）。"""
    amount = int(item.get("amount_fen") or 0)
    action = build_action(
        "pay_merchant",
        {
            "merchant": str(item.get("merchant") or ""),
            "item": str(item.get("label") or item.get("item_key") or ""),
            "amount_fen": amount,
        },
        account_id,
        amount,
    )
    return decide(action, _policy_context(conn, account_id, now))


def _decision_record(decision) -> dict[str, Any]:
    """把裁决压成可审计的 JSON；保存授权和到点复核都写这一份。"""
    return decision.model_dump(mode="json")


def _scope_items(conn, plan_id: str) -> list[dict[str, Any]]:
    """授权范围里的项目清单：直接读当前 plan_budgets，而不是读 meta 里的副本。"""
    rows = conn.execute(
        "SELECT item_key, label, merchant, amount_fen FROM plan_budgets"
        " WHERE plan_id = ? ORDER BY created_ms, rowid",
        (plan_id,),
    ).fetchall()
    return [
        {
            "item_key": str(row["item_key"]),
            "label": str(row["label"]),
            "merchant": str(row["merchant"]),
            "amount_fen": int(row["amount_fen"]),
        }
        for row in rows
    ]


def _scope_for_plan(conn, plan_id: str, meta: dict[str, Any], account_id: str) -> dict[str, Any]:
    """保存授权时冻结的范围：版本、账户、预算、逐项清单、执行窗口。"""
    day = parse_birthday(str(meta.get("birthday") or ""))
    trigger_ms = int(meta.get("trigger_ms") or _ms(trigger_at(day)))
    end_ms = _birthday_end_ms(day)
    return {
        "schema": AUTH_SCOPE_SCHEMA,
        "plan_version": _plan_version(meta),
        "account_id": account_id,
        "birthday": day.isoformat(),
        "budget_fen": int(meta.get("budget_fen") or 0),
        "items": _scope_items(conn, plan_id),
        "window": {"start_ms": trigger_ms, "trigger_ms": trigger_ms, "end_ms": end_ms},
        "expires_ms": end_ms,
    }


def authorize_plan(
    conn,
    plan_id: str,
    *,
    now_ms: int | None = None,
    session_id: str = "",
) -> dict[str, Any]:
    """保存一份一次性授权：冻结范围 + 当时的裁决，替换计划上旧的 active 授权。

    边界：
    - 只有 active 的生日计划能保存/重存（取消、完成的计划没有「到点」可言）；
    - 同一计划保留一份有效授权即可，旧的直接 revoked，避免两份范围不同的授权并存；
    - 保存时不扣钱、不建 hold，只写 plan_authorizations；真正付款在窗口内由
      run_due_authorized 按最新事实复核后走 pay_item 的同一条执行路径。
    """
    now = int(now_ms) if now_ms is not None else _now_ms()
    plan_row = conn.execute("SELECT * FROM plans WHERE id = ?", (plan_id,)).fetchone()
    if plan_row is None:
        raise ValidationError("计划不存在", plan_id=plan_id)
    meta = _load(plan_row["meta_json"])
    if meta.get("kind") != "birthday":
        raise ValidationError("这个计划不是生日计划", plan_id=plan_id, kind=meta.get("kind"))
    if str(plan_row["status"]) != "active":
        raise ValidationError(
            "这个计划现在不是进行中，不能保存自动付款授权",
            plan_id=plan_id,
            status=str(plan_row["status"]),
        )
    account_id = str(meta.get("account_id") or "")
    if not account_id:
        account_id = str(_resolve_account(conn, str(plan_row["user_id"]), None)["id"])
    scope = _scope_for_plan(conn, plan_id, meta, account_id)
    items = list(scope["items"])
    if not items:
        raise ValidationError("计划里还没有可授权的付款项目", plan_id=plan_id)
    decisions = {
        item["item_key"]: _decision_record(_decide_item(conn, account_id, item, now))
        for item in items
    }
    expires_ms = int(scope["window"]["end_ms"])
    decision_json = {
        "schema": AUTH_SCOPE_SCHEMA,
        "decided_ms": now,
        "spent_today_fen": funds.spent_today_fen(conn, account_id, at_ms=now),
        "items": decisions,
        "note": "保存授权时的裁决；到点执行前会用最新日累计和最新预算再裁决一次。",
    }
    auth_id = new_id("pa")
    with db.tx(conn):
        conn.execute(
            "UPDATE plan_authorizations SET status = 'revoked', revoked_ms = ?"
            " WHERE plan_id = ? AND status = 'active'",
            (now, plan_id),
        )
        conn.execute(
            "INSERT INTO plan_authorizations"
            " (id, plan_id, plan_version, account_id, scope_json, decision_json,"
            "  status, session_id, created_ms, expires_ms)"
            " VALUES (?, ?, ?, ?, ?, ?, 'active', ?, ?, ?)",
            (
                auth_id,
                plan_id,
                str(scope["plan_version"]),
                account_id,
                _dump(scope),
                _dump(decision_json),
                session_id or f"plan:{plan_id}",
                now,
                expires_ms,
            ),
        )
        audit.append(
            conn,
            "plan.updated",
            {
                "plan_id": plan_id,
                "changes": ["authorization_saved"],
                "authorization_id": auth_id,
                "plan_version": str(scope["plan_version"]),
                "account_id": account_id,
                "items": items,
                "expires_ms": expires_ms,
                "decision": decisions,
            },
            session_id=session_id or f"plan:{plan_id}",
        )
    view = authorization(conn, plan_id)
    assert view is not None
    return view


def revoke_authorization(
    conn,
    plan_id: str,
    *,
    reason: str,
    status: str = "revoked",
    now_ms: int | None = None,
) -> int:
    """作废计划上所有 active 授权，返回作废条数（0 表示本来就没有）。"""
    if status not in ("revoked", "expired"):
        raise ValidationError("授权只能作废或过期", status=status)
    now = int(now_ms) if now_ms is not None else _now_ms()
    rows = conn.execute(
        "SELECT id FROM plan_authorizations WHERE plan_id = ? AND status = 'active'",
        (plan_id,),
    ).fetchall()
    if not rows:
        return 0
    ids = [str(row["id"]) for row in rows]
    with db.tx(conn):
        conn.execute(
            "UPDATE plan_authorizations SET status = ?, revoked_ms = ?"
            " WHERE plan_id = ? AND status = 'active'",
            (status, now, plan_id),
        )
        audit.append(
            conn,
            "plan.updated",
            {
                "plan_id": plan_id,
                "changes": ["authorization_revoked"],
                "authorization_status": status,
                "authorization_ids": ids,
                "reason": reason,
            },
            session_id=f"plan:{plan_id}",
        )
    return len(ids)


def expire_authorizations(conn, *, now_ms: int | None = None) -> int:
    """把过了执行窗口的 active 授权置为 expired（后台不得再启动付款）。"""
    now = int(now_ms) if now_ms is not None else _now_ms()
    rows = conn.execute(
        "SELECT id, plan_id FROM plan_authorizations"
        " WHERE status = 'active' AND expires_ms IS NOT NULL AND expires_ms < ?",
        (now,),
    ).fetchall()
    if not rows:
        return 0
    by_plan: dict[str, list[str]] = {}
    for row in rows:
        by_plan.setdefault(str(row["plan_id"]), []).append(str(row["id"]))
    with db.tx(conn):
        for plan_id, ids in by_plan.items():
            conn.execute(
                "UPDATE plan_authorizations SET status = 'expired', revoked_ms = ?"
                " WHERE plan_id = ? AND status = 'active'",
                (now, plan_id),
            )
            audit.append(
                conn,
                "plan.updated",
                {
                    "plan_id": plan_id,
                    "changes": ["authorization_expired"],
                    "authorization_ids": ids,
                    "reason": "执行窗口已过",
                },
                session_id=f"plan:{plan_id}",
            )
    return len(rows)


def _maybe_use_authorization(conn, plan_id: str, *, now_ms: int | None = None) -> bool:
    """授权范围里的项目全部到终态时把授权标成 used（一次性用掉）。"""
    auth_row = _authorization_row(conn, plan_id, status="active")
    if auth_row is None:
        return False
    scope = _load(auth_row["scope_json"])
    keys = [str(item.get("item_key") or "") for item in scope.get("items") or []]
    keys = [key for key in keys if key]
    if not keys:
        return False
    placeholders = ", ".join("?" for _ in keys)
    rows = conn.execute(
        f"SELECT status FROM plan_budgets WHERE plan_id = ? AND item_key IN ({placeholders})",
        (plan_id, *keys),
    ).fetchall()
    if not rows or any(str(row["status"]) not in TERMINAL_BUDGET_STATUSES for row in rows):
        return False
    now = int(now_ms) if now_ms is not None else _now_ms()
    with db.tx(conn):
        changed = conn.execute(
            "UPDATE plan_authorizations SET status = 'used', used_ms = ?"
            " WHERE id = ? AND status = 'active'",
            (now, str(auth_row["id"])),
        ).rowcount
        if changed:
            audit.append(
                conn,
                "plan.updated",
                {
                    "plan_id": plan_id,
                    "changes": ["authorization_used"],
                    "authorization_id": str(auth_row["id"]),
                    "items": keys,
                },
                session_id=f"plan:{plan_id}",
            )
    return changed > 0


def _needs_verification(
    conn,
    plan_id: str,
    *,
    reason: str,
    detail: Any = None,
    now_ms: int | None = None,
) -> bool:
    """把计划暂停成待确认并记下原因（同一原因不重复写审计）。"""
    row = conn.execute("SELECT * FROM plans WHERE id = ?", (plan_id,)).fetchone()
    if row is None:
        return False
    meta = _load(row["meta_json"])
    existing = meta.get("needs_verification") or {}
    if str(existing.get("reason") or "") == reason:
        return False
    now = int(now_ms) if now_ms is not None else _now_ms()
    meta["needs_verification"] = {"reason": reason, "detail": detail or {}, "at_ms": now}
    with db.tx(conn):
        if str(row["status"]) == "active":
            conn.execute(
                "UPDATE plans SET status = 'paused', updated_ms = ? WHERE id = ?", (now, plan_id)
            )
        conn.execute(
            "UPDATE plans SET meta_json = ?, updated_ms = ? WHERE id = ?",
            (_dump(meta), now, plan_id),
        )
        audit.append(
            conn,
            "plan.updated",
            {
                "plan_id": plan_id,
                "changes": ["needs_verification"],
                "reason": reason,
                "detail": detail or {},
            },
            session_id=f"plan:{plan_id}",
        )
    return True


def _scope_mismatch(
    meta: dict[str, Any],
    scope: dict[str, Any],
    budget_items: list[dict[str, Any]],
    account_id: str,
) -> list[str]:
    """授权范围与当前计划逐项比对，返回不一致的字段名列表（空 = 完全匹配）。"""
    reasons: list[str] = []
    if str(scope.get("plan_version") or "") != _plan_version(meta):
        reasons.append("plan_version")
    if str(scope.get("account_id") or "") != account_id:
        reasons.append("account_id")
    if int(scope.get("budget_fen") or 0) != int(meta.get("budget_fen") or 0):
        reasons.append("budget_fen")
    expected = {
        str(item.get("item_key")): (str(item.get("merchant")), int(item.get("amount_fen") or 0))
        for item in scope.get("items") or []
    }
    actual = {
        str(item["item_key"]): (str(item["merchant"]), int(item["amount_fen"]))
        for item in budget_items
    }
    if expected != actual:
        reasons.append("items")
    return reasons


def _run_awaitable(coro):
    """在同步函数里跑协程：节拍器在工作线程里调，普通脚本/测试也能直接调。"""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    # 已有事件循环（例如 async 端点直接调用）时另开线程跑，避免 coroutine never awaited
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


def parse_birthday(text: str) -> _dt.date:
    """生日必须写成 YYYY-MM-DD；解析失败直接拒绝，不猜。"""
    raw = str(text or "").strip()
    if not raw:
        raise ValidationError("要告诉我生日是哪一天", field="birthday")
    try:
        return _dt.date.fromisoformat(raw)
    except ValueError as exc:
        raise ValidationError("生日日期要写成 YYYY-MM-DD", field="birthday", value=raw) from exc


def trigger_at(birthday: _dt.date) -> _dt.datetime:
    """触发时间：生日前两天的上午 9 点（本地时区）。"""
    day = birthday - _dt.timedelta(days=TRIGGER_DAYS_BEFORE)
    return _dt.datetime(day.year, day.month, day.day, TRIGGER_HOUR, 0, 0)


def _ms(moment: _dt.datetime) -> int:
    return int(moment.timestamp() * 1000)


def _normalize_items(items: list[dict[str, Any]] | None, budget_fen: int) -> list[dict[str, Any]]:
    """校验项目：键唯一、商户在白名单、金额为正，且总额不能超过预算。"""
    raw_items = list(items or DEFAULT_ITEMS)
    if not raw_items:
        raise ValidationError("至少要有一样生日项目", field="items")
    if len(raw_items) > MAX_ITEMS:
        raise ValidationError("生日项目太多", field="items", max=MAX_ITEMS)
    clean: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in raw_items:
        key = str(raw.get("key") or "").strip() or f"item{len(clean) + 1}"
        if key in seen:
            raise ValidationError("同一个项目不能出现两次", field="items", key=key)
        seen.add(key)
        label = str(raw.get("label") or "").strip() or key
        merchant = str(raw.get("merchant") or "").strip()
        if merchant not in MERCHANT_WHITELIST:
            raise ValidationError(
                f"暂不支持商户 {merchant or '（空）'}",
                field="items",
                key=key,
                allowed_merchants=sorted(MERCHANT_WHITELIST),
            )
        try:
            amount_fen = int(raw.get("amount_fen"))
        except (TypeError, ValueError) as exc:
            raise ValidationError("项目金额要写成分", field="items", key=key) from exc
        if amount_fen <= 0:
            raise ValidationError("项目金额必须大于 0", field="items", key=key)
        clean.append(
            {
                "key": key,
                "label": label,
                "merchant": merchant,
                "merchant_name": MERCHANT_WHITELIST[merchant],
                "amount_fen": amount_fen,
                "amount_yuan": fen_to_yuan_str(amount_fen),
            }
        )
    total_fen = sum(item["amount_fen"] for item in clean)
    if total_fen > budget_fen:
        raise ValidationError(
            f"这几项加起来 {fen_to_yuan_str(total_fen)} 元，超过预算 {fen_to_yuan_str(budget_fen)} 元",
            field="items",
            total_fen=total_fen,
            budget_fen=budget_fen,
        )
    return clean


def _budget_view(row: Any, step: Any | None) -> dict[str, Any]:
    payload = _load(row["extra_json"])
    return {
        "item_key": str(row["item_key"]),
        "label": str(row["label"]),
        "merchant": str(row["merchant"]),
        "merchant_name": MERCHANT_WHITELIST.get(str(row["merchant"]), str(row["merchant"])),
        "amount_fen": int(row["amount_fen"]),
        "amount_yuan": fen_to_yuan_str(int(row["amount_fen"])),
        "status": str(row["status"]),
        "status_label": {
            "reserved": "预算已预留（未付款）",
            "paying": "付款执行中",
            "committed": "已付款",
            "declined": "用户拒绝付款",
            "failed": "付款失败",
            "released": "预算已释放",
        }.get(str(row["status"]), str(row["status"])),
        "reason": str(row["reason"]),
        # 老库没有这两列时保证视图不缺字段：前端不用为迁移前后写两套渲染。
        "account_id": str(row["account_id"] or "") if "account_id" in row.keys() else "",
        "hold_id": str(row["hold_id"] or "") if "hold_id" in row.keys() else "",
        "step_state": str(step["state"]) if step is not None else "",
        "step_due_ms": int(step["due_ms"]) if step is not None else None,
        "step_due_text": _time_text(int(step["due_ms"])) if step is not None else "",
        "detail": payload,
        "needs_user": str(row["status"]) == "reserved",
        "boundary": PAY_BOUNDARY,
    }


def budgets(conn, plan_id: str) -> list[Any]:
    return list(
        conn.execute(
            "SELECT * FROM plan_budgets WHERE plan_id = ? ORDER BY created_ms ASC, rowid ASC",
            (plan_id,),
        ).fetchall()
    )


def _step_map(conn, plan_id: str) -> dict[str, Any]:
    """把 action 步骤按 payload.item_key 归位，方便预算行带上执行状态。"""
    found: dict[str, Any] = {}
    rows = conn.execute(
        "SELECT * FROM plan_steps WHERE plan_id = ? AND kind = 'action' ORDER BY seq ASC",
        (plan_id,),
    ).fetchall()
    for row in rows:
        payload = _load(row["payload_json"])
        key = str(payload.get("item_key") or "")
        if key and key not in found:
            found[key] = row
    return found


def plan_with_budgets(conn, plan_id: str) -> dict[str, Any] | None:
    from app import plans as plans_mod

    view = plans_mod.view_plan(conn, plan_id)
    if view is None:
        return None
    meta = view.get("meta") or {}
    steps = _step_map(conn, plan_id)
    rows = budgets(conn, plan_id)
    items = [_budget_view(row, steps.get(str(row["item_key"]))) for row in rows]
    total_fen = sum(item["amount_fen"] for item in items)
    committed_fen = sum(item["amount_fen"] for item in items if item["status"] == "committed")
    budget_fen = int(meta.get("budget_fen") or 0)
    account_id = str(meta.get("account_id") or "")
    if not account_id and rows:
        account_id = str(rows[0]["account_id"] or "")
    # 账户额度视图和预算视图放一起：只给预算数字、不给「余额-预留=可支配」，
    # 用户看到「预算 1000」也不知道自己还能不能转 500。
    account = account_summary(conn, account_id) if account_id else {}
    view["meta"] = meta
    view["budget"] = {
        "budget_fen": budget_fen,
        "budget_yuan": fen_to_yuan_str(budget_fen) if budget_fen else "",
        "planned_fen": total_fen,
        "planned_yuan": fen_to_yuan_str(total_fen),
        "committed_fen": committed_fen,
        "committed_yuan": fen_to_yuan_str(committed_fen),
        "remaining_fen": budget_fen - total_fen,
        "remaining_yuan": fen_to_yuan_str(budget_fen - total_fen) if budget_fen else "",
        "trigger_ms": int(meta.get("trigger_ms") or 0),
        "trigger_text": str(meta.get("trigger_text") or ""),
        "birthday": str(meta.get("birthday") or ""),
        "account_id": account_id,
        "balance_fen": account.get("balance_fen"),
        "balance_yuan": account.get("balance_yuan"),
        "reserved_budget_fen": account.get("reserved_budget_fen"),
        "reserved_budget_yuan": account.get("reserved_budget_yuan"),
        "available_fen": account.get("available_fen"),
        "available_yuan": account.get("available_yuan"),
        "boundary": BUDGET_BOUNDARY,
    }
    view["items"] = items
    view["item_count"] = len(items)
    view["needs_user"] = [item["item_key"] for item in items if item["needs_user"]]
    # 自动付款授权放进来：界面上「这一单会不会到点自己付」必须一眼能看见
    view["authorization"] = authorization(conn, plan_id)
    return view


def create_plan(
    conn,
    *,
    user_id: str,
    birthday: str,
    budget_fen: int = DEFAULT_BUDGET_FEN,
    items: list[dict[str, Any]] | None = None,
    title: str | None = None,
    account_id: str | None = None,
    save_authorization: bool = False,
) -> dict[str, Any]:
    """建生日计划：预算 + 两个项目 + 生日前两天的检查/提醒步骤，全部落库。

    save_authorization=True 时在同一事务里再写一份一次性授权（plan_authorizations）：
    到点后按冻结的清单自动付款，但仍然先按最新日累计重新裁决，红灯不付、参数变了不付。

    预算预留真实占用可支配金额，所以建计划前必须在同一事务里确认覆盖得掉：
    显式指定 account_id 时用那个账户，否则用用户名下第一张储蓄卡。额度不够直接
    失败（BUDGET_NOT_COVERED）且不留半个计划——否则用户会看到「计划建好了但付不出」，
    比一开始就拒绝更糟。
    """
    from app import plans as plans_mod

    day = parse_birthday(birthday)
    today = _dt.date.fromtimestamp(_now_ms() / 1000)
    if day <= today:
        raise ValidationError(
            "这个生日已经过去了，我只能为将来的日期做计划", field="birthday", value=day.isoformat()
        )
    try:
        budget = int(budget_fen)
    except (TypeError, ValueError) as exc:
        raise ValidationError("预算要写成分", field="budget_fen") from exc
    if budget <= 0:
        raise ValidationError("预算必须大于 0", field="budget_fen")
    clean = _normalize_items(items, budget)
    account = _resolve_account(conn, user_id, account_id)
    trigger = trigger_at(day)
    trigger_ms = _ms(trigger)
    steps: list[dict[str, Any]] = [
        {
            "kind": "check",
            "title": f"确认余额够生日预算（{fen_to_yuan_str(budget)} 元）",
            "due_ms": trigger_ms,
            "payload": {
                "check": "balance_at_least",
                "amount_fen": budget,
                "note": "生日计划：预算核查",
            },
        }
    ]
    for index, item in enumerate(clean):
        steps.append(
            {
                "kind": "action",
                "title": f"付款：{item['label']}（预算 {item['amount_yuan']} 元）",
                # 两个项目错开一分钟，避免预览里两个"到点"看着像同一件事
                "due_ms": trigger_ms + index * 60_000,
                "payload": {
                    "tool": "pay_merchant",
                    "params": {
                        "merchant": item["merchant"],
                        "item": item["label"],
                        "amount_fen": item["amount_fen"],
                        "note": f"生日计划：{item['label']}",
                    },
                    "item_key": item["key"],
                    "label": item["label"],
                    "prompt": f"付 {item['label']} {item['amount_yuan']} 元（{item['merchant_name']}）",
                },
            }
        )
    stored_items = [
        {k: item[k] for k in ("key", "label", "merchant", "amount_fen")} for item in clean
    ]
    meta = {
        "kind": "birthday",
        "birthday": day.isoformat(),
        "budget_fen": budget,
        "trigger_ms": trigger_ms,
        "trigger_text": _time_text(trigger_ms),
        "items": stored_items,
        "account_id": str(account["id"]),
        # 授权（一次性代扣授权）绑定的计划版本号：只有用户改计划才 +1，
        # 后台的暂停/定时推进不动它，否则一生成待确认项目授权就自动失效了。
        "version": 1,
        "summary": f"生日 {day.isoformat()}，预算 {fen_to_yuan_str(budget)} 元，"
        f"生日前 {TRIGGER_DAYS_BEFORE} 天（{_time_text(trigger_ms)}）提醒，付款要你确认。",
    }
    plan_title = str(title or "").strip() or f"{day.month} 月 {day.day} 日生日计划"
    reserved_fen = sum(item["amount_fen"] for item in clean)
    need_fen = max(budget, reserved_fen)
    now = _now_ms()
    with db.tx(conn):
        # 校验和写入在同一把写锁里（顶层 BEGIN IMMEDIATE）：两个并发建计划的请求
        # 各自读到旧可支配金额、都通过校验，加起来就超卖了额度。
        available = funds.available_fen(conn, str(account["id"]))
        if available < need_fen:
            raise BudgetNotCovered(
                f"可支配金额 {fen_to_yuan_str(available)} 元，不够这笔预算"
                f" {fen_to_yuan_str(need_fen)} 元",
                account_id=str(account["id"]),
                available_fen=available,
                need_fen=need_fen,
                budget_fen=budget,
                reserved_fen=reserved_fen,
            )
        created = plans_mod.create(
            conn, user_id=user_id, title=plan_title[:MAX_TITLE], steps=steps, meta=meta
        )
        plan_id = str(created["plan_id"])
        for item in clean:
            conn.execute(
                "INSERT INTO plan_budgets (id, plan_id, item_key, label, merchant, amount_fen,"
                " account_id, hold_id, extra_json, status, reason, created_ms, updated_ms)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?, 'reserved', '', ?, ?)",
                (
                    new_id("pb"),
                    plan_id,
                    item["key"],
                    item["label"],
                    item["merchant"],
                    item["amount_fen"],
                    str(account["id"]),
                    _dump({"merchant_name": item["merchant_name"]}),
                    now,
                    now,
                ),
            )
        seq = audit.append(
            conn,
            "plan.budget_reserved",
            {
                "plan_id": plan_id,
                "account_id": str(account["id"]),
                "budget_fen": budget,
                "reserved_fen": reserved_fen,
                "items": [item["key"] for item in clean],
                "trigger_ms": trigger_ms,
                # 语义变了：从「记账占用」升级为「真实占用可支配金额」。
                # 老数据 scope=bookkeeping_reservation，新数据用新值，对账能分开看。
                "scope": RESERVATION_SCOPE,
            },
            session_id=f"plan:{plan_id}",
        )
        if save_authorization:
            # 授权和计划同事务：不能出现「计划建好了、授权没落库」或反过来
            authorize_plan(conn, plan_id, now_ms=now, session_id=f"plan:{plan_id}")
    view = plan_with_budgets(conn, plan_id) or {}
    view["audit_seq"] = seq
    return view


def _claim(conn, plan_id: str, item_key: str) -> bool:
    """领走一项预算：带条件 UPDATE，抢不到就是别人已经在付了。

    顺手清空 hold_id：上一轮如果退回过 reserved，旧 hold 不能留在行上冒充本轮付款。
    """
    cursor = conn.execute(
        "UPDATE plan_budgets SET status = 'paying', hold_id = NULL, updated_ms = ?"
        " WHERE plan_id = ? AND item_key = ? AND status = 'reserved'",
        (_now_ms(), plan_id, item_key),
    )
    return cursor.rowcount == 1


def _finish(
    conn,
    plan_id: str,
    item_key: str,
    status: str,
    reason: str = "",
    *,
    hold_id: str | None = None,
) -> None:
    """预算行收尾：状态 + 原因 + 本轮真实 hold（付成功/失败都记，便于对账）。"""
    if status not in BUDGET_STATUSES:
        raise ValidationError("未知的预算状态", status=status)
    if hold_id is None:
        conn.execute(
            "UPDATE plan_budgets SET status = ?, reason = ?, updated_ms = ?"
            " WHERE plan_id = ? AND item_key = ?",
            (status, reason, _now_ms(), plan_id, item_key),
        )
        return
    conn.execute(
        "UPDATE plan_budgets SET status = ?, reason = ?, hold_id = ?, updated_ms = ?"
        " WHERE plan_id = ? AND item_key = ?",
        (status, reason, hold_id, _now_ms(), plan_id, item_key),
    )

def _mark_step(conn, plan_id: str, item_key: str, state: str, error: str = "") -> None:
    row = _step_map(conn, plan_id).get(item_key)
    if row is None:
        return
    conn.execute(
        "UPDATE plan_steps SET state = ?, last_error = ?, updated_ms = ? WHERE id = ?",
        (state, error, _now_ms(), str(row["id"])),
    )


async def pay_item(
    *,
    plan_id: str,
    item_key: str,
    session_id: str,
    approve: bool = True,
    proofs: list[dict[str, Any]] | None = None,
    confirm_timeout_ms: int = 15_000,
) -> dict[str, Any]:
    """付生日计划里的某一项：确认 → 策略 → 令牌 → 执行，和手动付款同一条路径。"""
    conn = db.connect()
    try:
        plan_row = conn.execute("SELECT * FROM plans WHERE id = ?", (plan_id,)).fetchone()
        if plan_row is None:
            raise ValidationError("计划不存在", plan_id=plan_id)
        meta = _load(plan_row["meta_json"])
        if meta.get("kind") != "birthday":
            raise ValidationError("这个计划不是生日计划", plan_id=plan_id, kind=meta.get("kind"))
        row = conn.execute(
            "SELECT * FROM plan_budgets WHERE plan_id = ? AND item_key = ?", (plan_id, item_key)
        ).fetchone()
        if row is None:
            raise ValidationError("计划里没有这个项目", item_key=item_key)
        status = str(row["status"])
        if status == "committed":
            # 重复触发不重复下单：这里直接返回，不再建执行图
            return {
                "ok": True,
                "status": "already_paid",
                "idempotent": True,
                "item": _budget_view(row, _step_map(conn, plan_id).get(item_key)),
                "message": "这一项已经付过了，没有重复下单。",
            }
        if status != "reserved":
            raise ValidationError(
                f"这一项现在的状态是 {status}，不能付款", item_key=item_key, status=status
            )
        if str(plan_row["status"]) in ("cancelled", "failed", "completed"):
            raise ValidationError(
                "这个计划已经结束或取消了，不能再付款",
                plan_id=plan_id,
                status=str(plan_row["status"]),
            )
        account_id = str(row["account_id"] or "") if "account_id" in row.keys() else ""
        if not account_id:
            # 老库的预算行没有 account_id：按计划归属补一张卡，并把同计划的行一起回填。
            # 不回填的话预留永远算不到任何账户头上——available 不减，等于没占额度。
            account_id = str(_resolve_account(conn, str(plan_row["user_id"]), None)["id"])
            with db.tx(conn):
                conn.execute(
                    "UPDATE plan_budgets SET account_id = ?, updated_ms = ?"
                    " WHERE plan_id = ? AND (account_id IS NULL OR account_id = '')",
                    (account_id, _now_ms(), plan_id),
                )
            row = conn.execute(
                "SELECT * FROM plan_budgets WHERE plan_id = ? AND item_key = ?", (plan_id, item_key)
            ).fetchone()
        # 领取之前先确认余额覆盖得住「其它预留 + 本笔」：领取后本笔不再计入预留，
        # 所以此刻 balance < reserved_budget_fen 就是真的付不出，提前给明确错误，
        # 而不是让工具层在建 hold 时抛一个难懂的执行错误。
        _assert_available_for_item(conn, account_id, int(row["amount_fen"]))
        view_item = _budget_view(row, _step_map(conn, plan_id).get(item_key))
        if not approve:
            with db.tx(conn):
                _finish(conn, plan_id, item_key, "declined", "用户拒绝付款")
                _mark_step(conn, plan_id, item_key, "skipped", "用户拒绝付款")
                audit.append(
                    conn,
                    "plan.item_declined",
                    {"plan_id": plan_id, "item_key": item_key, "amount_fen": row["amount_fen"]},
                    session_id=f"plan:{plan_id}",
                )
                # 用户明确拒绝 = 改主意：这张一次性授权不能再拿来替别的项目自动付款
                revoke_authorization(conn, plan_id, reason="用户拒绝付款")
            return {
                "ok": True,
                "status": "declined",
                "item": view_item,
                "message": "这一项按你说的没有付款，预算留着。",
            }
        if not _claim(conn, plan_id, item_key):
            raise ValidationError("这一项已经被另一次请求领走了，请刷新看最新状态", item_key=item_key)

        params = {
            "merchant": str(row["merchant"]),
            "item": str(row["label"]),
            "amount_fen": int(row["amount_fen"]),
        }
        dag = Dag(
            id=ids.new_id("r"), nodes=[DAGNode(id="n1", tool="pay_merchant", params=params)]
        )
        # 执行完 executor 会把 RunState 从 RUNS 里摘掉（finish_run），所以结果只能
        # 从事件流里接：tool 帧带 ok / error_code / summary，和界面看到的一致。
        seen: dict[str, Any] = {"confirmation_id": None, "tool": None}

        async def emit(name: str, data: dict[str, Any]) -> None:
            if name in ("confirm", "factor") and not seen["confirmation_id"]:
                seen["confirmation_id"] = data.get("confirmation_id")
            if name == "tool" and str(data.get("node_id") or "n1") == "n1":
                seen["tool"] = dict(data)

        task = asyncio.create_task(
            executor.run(
                session_id,
                dag,
                emit,
                # 明确指定账户：执行器默认会挑用户名下的卡，但预算记在哪张卡上，
                # 就必须从哪张卡付，否则 hold 和预留挂在两个账户上，对账必错。
                account_id=account_id,
                confirm_timeout_ms=confirm_timeout_ms,
                user_text=f"生日计划：{row['label']}",
            )
        )
        cid = None
        for _ in range(500):
            if seen["confirmation_id"]:
                cid = str(seen["confirmation_id"])
                break
            if task.done():
                break
            await asyncio.sleep(0.01)
        try:
            if cid is None:
                result: dict[str, Any] = {"ok": False, "status": "no_confirmation"}
            else:
                result = await executor.resume(session_id, cid, True, proofs=proofs or [])
                if str(result.get("status")) in ("approved", "rejected", "expired"):
                    # 答复之后这一轮还要继续往下跑（令牌 → 工具）。resume 只负责
                    # 解掉确认的等待，执行是原来那个 task 在做——不等它收尾就取消，
                    # 会拿到一张「已批准但钱没动」的假结果。
                    try:
                        await asyncio.wait_for(task, timeout=30)
                    except asyncio.TimeoutError:
                        task.cancel()
                        await asyncio.gather(task, return_exceptions=True)
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        if str(result.get("status")) == "waiting_factor":
            # 缺因子：一分钱没动，把预算退回 reserved，等用户补验证
            with db.tx(conn):
                _finish(conn, plan_id, item_key, "reserved", "等待多因子验证")
            return {
                "ok": False,
                "status": "waiting_factor",
                "required": list(result.get("required") or []),
                "confirmation_id": cid,
                "item": view_item,
                "message": "还差验证：这一步要动钱，缺口令不会执行，补上验证再提交。",
            }
        tool_event = seen.get("tool") or {}
        if bool(tool_event.get("ok")):
            summary = str(tool_event.get("summary") or "")
            hold_id = _latest_hold_id(
                conn,
                session_id=session_id,
                account_id=account_id,
                amount_fen=int(row["amount_fen"]),
            )
            with db.tx(conn):
                _finish(conn, plan_id, item_key, "committed", summary, hold_id=hold_id or None)
                _mark_step(conn, plan_id, item_key, "done")
                audit.append(
                    conn,
                    "plan.item_paid",
                    {
                        "plan_id": plan_id,
                        "item_key": item_key,
                        "account_id": account_id,
                        "amount_fen": int(row["amount_fen"]),
                        "hold_id": hold_id,
                        "confirmation_id": cid,
                        "summary": summary,
                    },
                    session_id=f"plan:{plan_id}",
                )
            # 清单全部付完时把授权标 used（一次性）；还有项目没付就留着给到点用
            _maybe_use_authorization(conn, plan_id)
            return {
                "ok": True,
                "status": "paid",
                "item": view_item,
                "summary": summary,
                "confirmation_id": cid,
                "message": f"已经付了{row['label']} {fen_to_yuan_str(int(row['amount_fen']))} 元。",
            }
        error_code = str(tool_event.get("error_code") or "PAYMENT_FAILED")
        last_error = str(tool_event.get("summary") or "付款没有成功")
        # 失败也可能已经建过 hold（建了又释放）：能查到就记上，出问题能顺着 hold 看流水。
        failed_hold = _latest_hold_id(
            conn,
            session_id=session_id,
            account_id=account_id,
            amount_fen=int(row["amount_fen"]),
        )
        with db.tx(conn):
            _finish(
                conn, plan_id, item_key, "failed", f"{error_code}:{last_error}",
                hold_id=failed_hold or None,
            )
            _mark_step(conn, plan_id, item_key, "failed", last_error)
            audit.append(
                conn,
                "plan.item_failed",
                {
                    "plan_id": plan_id,
                    "item_key": item_key,
                    "account_id": account_id,
                    "amount_fen": int(row["amount_fen"]),
                    "hold_id": failed_hold,
                    "error_code": error_code,
                    "error_text": last_error,
                    "confirmation_id": cid,
                },
                session_id=f"plan:{plan_id}",
            )
        return {
            "ok": False,
            "status": "failed",
            "error_code": error_code,
            "user_text": last_error,
            "item": view_item,
            "confirmation_id": cid,
        }
    finally:
        conn.close()


def release_reserved(conn, plan_id: str, *, reason: str = "计划结束，预算释放") -> int:
    """释放一个计划里所有还处于 reserved 的预算行，返回释放金额（分）。

    只放 reserved：paying 表示正在执行，钱由那条 hold 占着，这时候改状态会把
    「正在付」的账改坏——paying 的收尾由 pay_item 自己做。committed/declined/
    failed/released 都是终态，重复调用不会多释放一分钱。
    """
    rows = conn.execute(
        "SELECT id, amount_fen FROM plan_budgets WHERE plan_id = ? AND status = 'reserved'",
        (plan_id,),
    ).fetchall()
    if not rows:
        return 0
    freed = sum(int(row["amount_fen"]) for row in rows)
    conn.execute(
        "UPDATE plan_budgets SET status = 'released', reason = ?, updated_ms = ?"
        " WHERE plan_id = ? AND status = 'reserved'",
        (reason, _now_ms(), plan_id),
    )
    # 审计事件类型是 audit.py 里的封闭集合（不属于本模块），释放明细挂在
    # plan.updated 上：changes 标 budget_released、带上金额与原因，按事件查
    # 或按 plan_id 查都能看到「哪个计划、为什么、释放了多少」。
    audit.append(
        conn,
        "plan.updated",
        {
            "plan_id": plan_id,
            "changes": ["budget_released"],
            "released_fen": freed,
            "items": [str(row["id"]) for row in rows],
            "reason": reason,
        },
        session_id=f"plan:{plan_id}",
    )
    return freed


def on_plan_updated(
    conn,
    plan_id: str,
    *,
    status: str | None = None,
    changes: tuple[str, ...] | list[str] = (),
) -> None:
    """计划生命周期钩子：plans.update 改状态/改参数后、_settle_plan 收尾后调用。

    预算侧规则：
    - 取消/失败/完成 → 仍在 reserved 的行全部释放，不能让额度跟着一个死计划占着；
    - 用户改了标题或步骤 → 计划内容变了，先不释放（可能只是改日期），但把 meta
      里的版本号 +1，让之前保存的一次性授权自动对不上版本（任务四的授权判定靠它）；
    - 暂停/恢复不动：暂停不是放弃，钱还得继续给这个计划留着。

    为什么把版本号放在这里而不是用 plans.updated_ms 当版本：后台到点提醒、把计划
    暂停成待确认，同样会更新 updated_ms——拿它当版本号，用户什么都没改，授权也会
    被判成「计划已变」，自动执行永远走不到。
    """
    row = conn.execute("SELECT * FROM plans WHERE id = ?", (plan_id,)).fetchone()
    if row is None:
        return
    meta = _load(row["meta_json"])
    if meta.get("kind") != "birthday":
        return
    changes = tuple(str(change) for change in changes)
    current = str(row["status"])
    with db.tx(conn):
        if current in ("cancelled", "failed", "completed"):
            release_reserved(conn, plan_id, reason=f"计划{current}，预算释放")
            # 计划都结束了，一次性授权必须一起死：留着它，后台到点还会去「执行」一个死计划
            revoke_authorization(conn, plan_id, reason=f"计划{current}，授权作废")
        if status == "paused" and "status" in changes:
            # 用户主动暂停 = 明确要求别自动付（到点提醒导致的暂停不经过这里，
            # 那条路直接写 SQL，授权保持 active 等用户确认后继续用）
            revoke_authorization(conn, plan_id, reason="用户暂停计划，授权作废")
        if any(change in ("title", "steps") for change in changes):
            meta["version"] = int(meta.get("version") or 1) + 1
            conn.execute(
                "UPDATE plans SET meta_json = ?, updated_ms = ? WHERE id = ?",
                (_dump(meta), _now_ms(), plan_id),
            )
            # 版本先 +1 再作废：即便作废写失败，旧授权也会因为版本对不上而被拒
            revoke_authorization(conn, plan_id, reason="计划内容已修改，授权作废")


def reconcile(
    conn,
    *,
    plan_id: str | None = None,
    stale_ms: int = 600_000,
) -> dict[str, Any]:
    """把「进程崩在付款中间」的预算行对回真实 hold 与流水。

    判定顺序（证据由强到弱）：
    1. paying + hold 已 committed → 钱其实付掉了，补记 committed；
    2. paying + hold 已 released/expired → 没付成，标 failed（不再占款）；
    3. paying + 没有 hold、且超过 stale_ms 没动过 → 领取后没走到工具就崩了，退回
       reserved（仍然占款，用户可以直接重试）;
    4. committed 但查不到 hold 或出账流水 → 只报进 issues、绝不自动改：可能是历史
       数据，也可能是真问题，自动「修」会把一笔真付款抹掉。

    另外兜底一类漂移：计划已经 cancelled/failed/completed，预算却还是 reserved——
    正常路径里钩子和状态更新在同一事务，但老数据/异常退出可能留下这种行，释放它。

    返回 {checked, repaired, released_fen, revoked, expired, issues}，节拍器和测试脚本都能直接用。
    """
    sql = "SELECT * FROM plan_budgets WHERE status IN ('paying', 'committed')"
    params: tuple[Any, ...] = ()
    if plan_id:
        sql += " AND plan_id = ?"
        params = (plan_id,)
    rows = conn.execute(sql, params).fetchall()
    now = _now_ms()
    repaired: list[dict[str, str]] = []
    issues: list[dict[str, Any]] = []
    released_fen = 0
    with db.tx(conn):
        for row in rows:
            row_plan = str(row["plan_id"])
            item = str(row["item_key"])
            status = str(row["status"])
            hold_id = str(row["hold_id"] or "") if "hold_id" in row.keys() else ""
            hold = None
            if hold_id:
                hold = conn.execute("SELECT * FROM holds WHERE id = ?", (hold_id,)).fetchone()
            if status == "paying":
                if hold is not None and str(hold["status"]) == "committed":
                    _finish(conn, row_plan, item, "committed", "对账补记：hold 已入账")
                    repaired.append({"plan_id": row_plan, "item_key": item, "to": "committed"})
                elif hold is not None and str(hold["status"]) in ("released", "expired"):
                    _finish(
                        conn, row_plan, item, "failed", f"对账：hold 已 {hold['status']}"
                    )
                    repaired.append({"plan_id": row_plan, "item_key": item, "to": "failed"})
                elif hold is None and now - int(row["updated_ms"]) >= stale_ms:
                    _finish(conn, row_plan, item, "reserved", "对账：执行中断且没有 hold")
                    repaired.append({"plan_id": row_plan, "item_key": item, "to": "reserved"})
                else:
                    issues.append(
                        {
                            "plan_id": row_plan,
                            "item_key": item,
                            "kind": "paying_in_flight",
                            "detail": hold_id,
                        }
                    )
                continue
            paid_tx = 0
            if hold_id:
                paid_tx = int(
                    conn.execute(
                        "SELECT COUNT(*) AS n FROM transactions WHERE hold_id = ?", (hold_id,)
                    ).fetchone()["n"]
                )
            if hold is None or paid_tx == 0:
                issues.append(
                    {
                        "plan_id": row_plan,
                        "item_key": item,
                        "kind": "committed_without_evidence",
                        "hold_id": hold_id,
                        "transactions": paid_tx,
                    }
                )
        drift = conn.execute(
            "SELECT DISTINCT b.plan_id FROM plan_budgets b JOIN plans p ON p.id = b.plan_id"
            " WHERE b.status = 'reserved' AND p.status IN ('cancelled', 'failed', 'completed')"
        ).fetchall()
        for drift_row in drift:
            released_fen += release_reserved(
                conn, str(drift_row["plan_id"]), reason="对账：计划已结束，预算释放"
            )
    # 授权侧对账：窗口过了补 expired；计划早就结束却还 active 的补 revoked。
    # 两个操作都是「只改 active 行」的幂等 UPDATE，重复跑不会重复记账。
    expired_auth = expire_authorizations(conn, now_ms=now)
    ended = conn.execute(
        "SELECT DISTINCT a.plan_id FROM plan_authorizations a JOIN plans p ON p.id = a.plan_id"
        " WHERE a.status = 'active' AND p.status IN ('cancelled', 'failed', 'completed')"
    ).fetchall()
    revoked_auth = 0
    for ended_row in ended:
        revoked_auth += revoke_authorization(
            conn, str(ended_row["plan_id"]), reason="对账：计划已结束，授权作废", now_ms=now
        )
    return {
        "checked": len(rows),
        "repaired": repaired,
        "released_fen": released_fen,
        "revoked": revoked_auth,
        "expired": expired_auth,
        "issues": issues,
    }


def run_due_authorized(
    conn, *, now_ms: int | None = None, plan_id: str | None = None
) -> dict[str, Any]:
    """推进到点的**已授权**生日付款；这是任务四唯一的自动扣款入口。

    plan_id 只影响扫描范围（演示现场手动推进某一份计划时用），不改动任何安全校验。

    每一步都先复核再动钱（顺序即优先级）：
    1. 计划仍是 active 且是生日计划；cancel/failed/completed → 授权作废；
    2. 授权 active、计划版本与冻结范围完全一致（版本/账户/预算/逐项商户金额）；
    3. 当前时间在窗口内，且该项目的步骤已到点；
    4. 余额覆盖得住「其它预留 + 本笔」；
    5. 用**最新**日累计重新裁决：red → 暂停计划待确认，一分钱不扣；green/yellow
       才走 pay_item（和手动付款同一条执行路径）。

    幂等靠状态机而不是内存标记：付过的项目是 committed，领取是带条件的 UPDATE，
    重复触发只会返回 already_paid 或跳过；服务重启后按库里的授权与预算状态重判。
    返回 {checked, paid, skipped, needs_verification, revoked, expired, items}。
    """
    now = int(now_ms) if now_ms is not None else _now_ms()
    summary: dict[str, Any] = {
        "checked": 0,
        "paid": 0,
        "skipped": 0,
        "needs_verification": 0,
        "revoked": 0,
        "expired": expire_authorizations(conn, now_ms=now),
        "items": [],
    }
    sql = (
        "SELECT a.id AS auth_id, a.plan_id, a.plan_version, a.scope_json, a.expires_ms,"
        " a.account_id AS auth_account_id, p.status AS plan_status, p.meta_json, p.user_id"
        " FROM plan_authorizations a JOIN plans p ON p.id = a.plan_id"
        " WHERE a.status = 'active'"
    )
    params: list[Any] = []
    if plan_id:
        sql += " AND a.plan_id = ?"
        params.append(str(plan_id))
    sql += " ORDER BY a.created_ms, a.rowid"
    rows = conn.execute(sql, params).fetchall()
    for row in rows:
        summary["checked"] += 1
        plan_id = str(row["plan_id"])
        meta = _load(row["meta_json"])
        if meta.get("kind") != "birthday":
            summary["revoked"] += revoke_authorization(
                conn, plan_id, reason="不是生日计划", now_ms=now
            )
            continue
        plan_status = str(row["plan_status"])
        if plan_status in ("cancelled", "failed", "completed"):
            summary["revoked"] += revoke_authorization(
                conn, plan_id, reason=f"计划{plan_status}，授权作废", now_ms=now
            )
            continue
        if plan_status != "active":
            # 用户暂停或待确认：后台不得自作主张继续扣款
            summary["skipped"] += 1
            continue
        scope = _load(row["scope_json"])
        budget_items = _scope_items(conn, plan_id)
        account_id = str(row["auth_account_id"] or meta.get("account_id") or "")
        if not account_id:
            account_id = str(_resolve_account(conn, str(row["user_id"]), None)["id"])
        mismatch = _scope_mismatch(meta, scope, budget_items, account_id)
        if mismatch:
            if _needs_verification(
                conn, plan_id, reason="AUTH_SCOPE_CHANGED", detail=mismatch, now_ms=now
            ):
                summary["needs_verification"] += 1
            summary["revoked"] += revoke_authorization(
                conn, plan_id, reason="计划参数与授权不一致", now_ms=now
            )
            summary["items"].append({"plan_id": plan_id, "result": "scope_changed", "mismatch": mismatch})
            continue
        window = scope.get("window") or {}
        start_ms = int(window.get("start_ms") or window.get("trigger_ms") or 0)
        end_ms = int(window.get("end_ms") or row["expires_ms"] or 0)
        if end_ms and now > end_ms:
            summary["revoked"] += revoke_authorization(
                conn, plan_id, reason="执行窗口已过", status="expired", now_ms=now
            )
            continue
        if now < start_ms:
            summary["skipped"] += 1
            continue
        step_map = _step_map(conn, plan_id)
        for item in budget_items:
            item_key = str(item["item_key"])
            budget_row = conn.execute(
                "SELECT status FROM plan_budgets WHERE plan_id = ? AND item_key = ?",
                (plan_id, item_key),
            ).fetchone()
            status = str(budget_row["status"]) if budget_row is not None else ""
            if status in TERMINAL_BUDGET_STATUSES:
                continue
            if status != "reserved":
                summary["skipped"] += 1
                continue
            step = step_map.get(item_key)
            due_ms = int(step["due_ms"]) if step is not None and step["due_ms"] is not None else 0
            if due_ms > now:
                summary["skipped"] += 1
                continue
            try:
                _assert_available_for_item(conn, account_id, int(item["amount_fen"]))
            except InsufficientFunds as exc:
                if _needs_verification(
                    conn,
                    plan_id,
                    reason="INSUFFICIENT_FUNDS",
                    detail={"item_key": item_key, "fields": dict(exc.fields)},
                    now_ms=now,
                ):
                    summary["needs_verification"] += 1
                summary["items"].append({"plan_id": plan_id, "item_key": item_key, "result": "insufficient_funds"})
                break
            decision = _decide_item(conn, account_id, item, now)
            if str(decision.level) == "red":
                if _needs_verification(
                    conn,
                    plan_id,
                    reason="POLICY_RED",
                    detail={"item_key": item_key, "rule_id": decision.rule_id, "reason_code": decision.reason_code},
                    now_ms=now,
                ):
                    summary["needs_verification"] += 1
                summary["items"].append(
                    {
                        "plan_id": plan_id,
                        "item_key": item_key,
                        "result": "policy_red",
                        "rule_id": decision.rule_id,
                    }
                )
                break
            try:
                result = _run_awaitable(
                    pay_item(
                        plan_id=plan_id,
                        item_key=item_key,
                        session_id=f"plan:{plan_id}:{item_key}",
                    )
                )
            except BankError as exc:
                summary["items"].append(
                    {
                        "plan_id": plan_id,
                        "item_key": item_key,
                        "result": "error",
                        "code": getattr(exc, "code", "ERROR"),
                        "user_text": getattr(exc, "user_text", str(exc)),
                    }
                )
                continue
            outcome = str(result.get("status") or "")
            summary["items"].append(
                {
                    "plan_id": plan_id,
                    "item_key": item_key,
                    "result": outcome,
                    "level": str(decision.level),
                    "rule_id": decision.rule_id,
                }
            )
            if outcome == "paid":
                summary["paid"] += 1
            elif outcome == "waiting_factor":
                if _needs_verification(
                    conn,
                    plan_id,
                    reason="FACTOR_REQUIRED",
                    detail={"item_key": item_key, "required": list(result.get("required") or [])},
                    now_ms=now,
                ):
                    summary["needs_verification"] += 1
                break
            elif outcome == "declined":
                summary["revoked"] += revoke_authorization(
                    conn, plan_id, reason="用户拒绝付款", now_ms=now
                )
                break
            else:
                # failed / no_confirmation：不再继续自动付，交给用户处理
                if _needs_verification(
                    conn,
                    plan_id,
                    reason="PAYMENT_FAILED",
                    detail={"item_key": item_key, "status": outcome, "error_code": result.get("error_code")},
                    now_ms=now,
                ):
                    summary["needs_verification"] += 1
                break
        _maybe_use_authorization(conn, plan_id, now_ms=now)
    return summary


__all__ = [
    "AUTH_BOUNDARY",
    "AUTH_STATUSES",
    "BUDGET_BOUNDARY",
    "BUDGET_STATUSES",
    "BudgetNotCovered",
    "DEFAULT_BUDGET_FEN",
    "DEFAULT_ITEMS",
    "PAY_BOUNDARY",
    "RESERVATION_SCOPE",
    "TERMINAL_BUDGET_STATUSES",
    "TRIGGER_DAYS_BEFORE",
    "account_summary",
    "authorization",
    "authorize_plan",
    "budgets",
    "create_plan",
    "expire_authorizations",
    "on_plan_updated",
    "parse_birthday",
    "pay_item",
    "plan_with_budgets",
    "reconcile",
    "release_reserved",
    "revoke_authorization",
    "run_due_authorized",
    "trigger_at",
]
