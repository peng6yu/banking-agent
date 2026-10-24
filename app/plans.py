"""跨场景计划：落在数据库里的多步骤任务（P1-9）。

和 app/planner/dag.py 的 DAG 不是一回事，这一点必须在代码里说清楚：

- DAG 是「一次对话里把一句话拆成几个工具调用」，跑完就结束，状态在内存；
- 这里的 Plan 是「跨天、跨会话、跨业务的任务」，状态全在 plans / plan_steps 两张表里，
  服务重启之后还在，可以由后台节拍推进，也可以由用户手动推一次。

为什么 action 步不自动执行（重要，不是没做完）：
用户在这个计划里按过一次「同意」，不等于几天后系统可以自己动钱。真实银行的
预约转账也要在到期前重新核对余额、限额、卡片状态和授权是否还有效。所以后台遇到
action 步时一律交回用户：写一条待确认提醒，把这一步标成 skipped，计划转 paused。
动钱这件事只在助手的确认卡片里发生——那里有策略裁决、能力令牌和审计。

check 步的条件不满足时不会一直原地重试：按 repeat_ms 往后推下一次检查时间，
连续 max_attempts 次都没满足就标 failed 并写审计，让人看见「这个计划卡住了」。

对应文档：docs/交付/设计系统.md 的状态口径、README「已知限制」第 9 条。
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Final

from app import audit, clock, db, ids, proactive

PLAN_STATUSES: Final[tuple[str, ...]] = ("active", "paused", "completed", "cancelled", "failed")
STEP_KINDS: Final[tuple[str, ...]] = ("check", "action", "reminder")
STEP_STATES: Final[tuple[str, ...]] = ("pending", "running", "done", "failed", "skipped")
CHECK_KINDS: Final[tuple[str, ...]] = ("savings_progress", "balance_at_least", "upcoming_charge")

MAX_STEPS: Final[int] = 12
MAX_TITLE: Final[int] = 60
MAX_ATTEMPTS: Final[int] = 10
DEFAULT_MAX_ATTEMPTS: Final[int] = 3
DEFAULT_REPEAT_MS: Final[int] = 24 * 60 * 60 * 1000
DEFAULT_LIMIT: Final[int] = 20
MAX_LIMIT: Final[int] = 50

REASON_PLAN_REMINDER: Final[str] = "plan_reminder"
REASON_PLAN_ACTION: Final[str] = "plan_action_pending"

ACTION_BOUNDARY: Final[str] = (
    "计划的资金操作不会在后台自动执行：到期时系统只提醒你，真正扣款要在助手里"
    "重新核对参数、过策略裁决、拿能力令牌之后才会发生。"
)
CHECK_UNMET: Final[str] = "条件还没满足，已排到下次检查。"
CHECK_FAILED: Final[str] = "连续 {n} 次检查都没满足条件，这一步先标为失败。"
ACTION_HANDOFF: Final[str] = "到期了，但这一步要动钱：已生成待确认提醒，等你回来确认。"


# --------------------------------------------------------------------------- 读写


def _time_text(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d %H:%M")


def _dump(payload: Any) -> str:
    return json.dumps(payload or {}, ensure_ascii=False, sort_keys=True)


def _load(text: Any) -> dict[str, Any]:
    if not text:
        return {}
    try:
        value = json.loads(text)
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _text(value: Any, field: str, *, max_len: int = MAX_TITLE) -> str:
    text = str(value or "").strip()
    if not text:
        raise clock.ValidationError("计划内容不能为空", field=field)
    if len(text) > max_len:
        raise clock.ValidationError("计划内容太长", field=field, max_len=max_len)
    return text


def step_view(row: Any) -> dict[str, Any]:
    payload = _load(row["payload_json"])
    return {
        "step_id": str(row["id"]),
        "seq": int(row["seq"]),
        "kind": str(row["kind"]),
        "title": str(row["title"]),
        "state": str(row["state"]),
        "due_ms": int(row["due_ms"]),
        "due_text": _time_text(int(row["due_ms"])),
        "attempts": int(row["attempts"]),
        "max_attempts": int(row["max_attempts"]),
        "last_error": str(row["last_error"] or ""),
        "payload": payload,
        "needs_user": str(row["state"]) == "skipped" and payload.get("tool") is not None,
    }


def plan_view(conn, row: Any) -> dict[str, Any]:
    steps = [
        step_view(item)
        for item in conn.execute(
            "SELECT * FROM plan_steps WHERE plan_id = ? ORDER BY seq", (str(row["id"]),)
        ).fetchall()
    ]
    done = sum(1 for item in steps if item["state"] in ("done", "skipped"))
    return {
        "plan_id": str(row["id"]),
        "title": str(row["title"]),
        "status": str(row["status"]),
        "created_ms": int(row["created_ms"]),
        "updated_ms": int(row["updated_ms"]),
        "meta": _load(row["meta_json"]),
        "steps": steps,
        "step_count": len(steps),
        "finished_steps": done,
        "boundary": ACTION_BOUNDARY,
    }


def validate_step(raw: Any, *, now: int) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise clock.ValidationError("计划步骤格式不对", field="steps")
    kind = str(raw.get("kind") or "").strip()
    if kind not in STEP_KINDS:
        raise clock.ValidationError("计划步骤类型不认识", kind=kind, allowed=list(STEP_KINDS))
    payload = raw.get("payload") or {}
    if not isinstance(payload, dict):
        raise clock.ValidationError("计划步骤的 payload 必须是对象", field="payload")
    due_ms = raw.get("due_ms")
    if due_ms is None:
        due_in = raw.get("due_in_ms")
        due_ms = now + int(due_in) if due_in is not None else now
    due_ms = int(due_ms)
    if due_ms < 0:
        raise clock.ValidationError("计划时间不能是负数", field="due_ms")
    max_attempts = int(raw.get("max_attempts") or DEFAULT_MAX_ATTEMPTS)
    if max_attempts < 1 or max_attempts > MAX_ATTEMPTS:
        raise clock.ValidationError("重试次数超出范围", field="max_attempts", minimum=1, maximum=MAX_ATTEMPTS)
    if kind == "check":
        check = str(payload.get("check") or "").strip()
        if check not in CHECK_KINDS:
            raise clock.ValidationError("检查条件不认识", field="payload.check", allowed=list(CHECK_KINDS))
        if check == "savings_progress" and not payload.get("goal_id"):
            raise clock.ValidationError("检查储蓄进度需要 goal_id", field="payload.goal_id")
        if check == "balance_at_least" and int(payload.get("amount_fen") or 0) <= 0:
            raise clock.ValidationError("检查余额需要正数金额", field="payload.amount_fen")
    if kind == "action":
        # 允许建，但执行时一定交回用户；这里只校验参数形状，避免计划里躺着一个空动作。
        tool = str(payload.get("tool") or "").strip()
        if not tool:
            raise clock.ValidationError("资金动作必须写清用哪个工具", field="payload.tool")
        if not isinstance(payload.get("params") or {}, dict):
            raise clock.ValidationError("资金动作的参数必须是对象", field="payload.params")
    if kind == "reminder":
        _text(payload.get("text") or raw.get("title"), "payload.text", max_len=200)
    return {
        "kind": kind,
        "title": _text(raw.get("title"), "title"),
        "payload": payload,
        "due_ms": due_ms,
        "max_attempts": max_attempts,
    }


def create(
    conn,
    *,
    user_id: str,
    title: str,
    steps: list[dict[str, Any]],
    meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """建计划：一次写入 plans + plan_steps，写 plan.created 审计。"""
    if not steps:
        raise clock.ValidationError("计划至少要有一个步骤", field="steps")
    if len(steps) > MAX_STEPS:
        raise clock.ValidationError("计划步骤太多", field="steps", max=MAX_STEPS)
    now = clock.now_ms()
    clean = [validate_step(item, now=now) for item in steps]
    plan_id = ids.new_id("pl")
    with db.tx(conn):
        conn.execute(
            "INSERT INTO plans (id, user_id, title, status, meta_json, created_ms, updated_ms)"
            " VALUES (?, ?, ?, 'active', ?, ?, ?)",
            (plan_id, user_id, _text(title, "title"), _dump(meta), now, now),
        )
        for seq, item in enumerate(clean, start=1):
            conn.execute(
                "INSERT INTO plan_steps (id, plan_id, seq, kind, title, payload_json, state,"
                " due_ms, attempts, max_attempts, last_error, updated_ms)"
                " VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, 0, ?, '', ?)",
                (
                    ids.new_id("ps"),
                    plan_id,
                    seq,
                    item["kind"],
                    item["title"],
                    _dump(item["payload"]),
                    item["due_ms"],
                    item["max_attempts"],
                    now,
                ),
            )
        seq = audit.append(
            conn,
            "plan.created",
            {"plan_id": plan_id, "title": _text(title, "title"), "steps": len(clean), "source": "api"},
        )
    view = view_plan(conn, plan_id)
    if view is not None:
        view["audit_seq"] = seq
    return view or {}


def view_plan(conn, plan_id: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM plans WHERE id = ?", (plan_id,)).fetchone()
    return plan_view(conn, row) if row is not None else None


def list_plans(
    conn, *, user_id: str | None = None, status: str | None = None, limit: int = DEFAULT_LIMIT
) -> list[dict[str, Any]]:
    if limit < 1 or limit > MAX_LIMIT:
        raise clock.ValidationError("limit 超出范围", field="limit", minimum=1, maximum=MAX_LIMIT)
    sql = "SELECT * FROM plans WHERE 1=1"
    args: list[Any] = []
    if user_id:
        sql += " AND user_id = ?"
        args.append(user_id)
    if status:
        if status not in PLAN_STATUSES:
            raise clock.ValidationError("计划状态不认识", status=status, allowed=list(PLAN_STATUSES))
        sql += " AND status = ?"
        args.append(status)
    sql += " ORDER BY updated_ms DESC LIMIT ?"
    args.append(limit)
    return [plan_view(conn, row) for row in conn.execute(sql, args).fetchall()]


def update(
    conn,
    plan_id: str,
    *,
    user_id: str | None = None,
    title: str | None = None,
    status: str | None = None,
    steps: list[dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    """改计划：只动传了的字段；步骤只能改还没跑的（pending）那几步。"""
    row = conn.execute("SELECT * FROM plans WHERE id = ?", (plan_id,)).fetchone()
    if row is None:
        return None
    if user_id and str(row["user_id"]) != user_id:
        return None
    now = clock.now_ms()
    changes: list[str] = []
    with db.tx(conn):
        if title is not None:
            conn.execute(
                "UPDATE plans SET title = ?, updated_ms = ? WHERE id = ?",
                (_text(title, "title"), now, plan_id),
            )
            changes.append("title")
        if status is not None:
            if status not in PLAN_STATUSES:
                raise clock.ValidationError("计划状态不认识", status=status, allowed=list(PLAN_STATUSES))
            conn.execute(
                "UPDATE plans SET status = ?, updated_ms = ? WHERE id = ?", (status, now, plan_id)
            )
            changes.append("status")
        for patch in steps or []:
            seq = int(patch.get("seq") or 0)
            target = conn.execute(
                "SELECT * FROM plan_steps WHERE plan_id = ? AND seq = ?", (plan_id, seq)
            ).fetchone()
            if target is None:
                raise clock.ValidationError("计划里没有这一步", seq=seq)
            if str(target["state"]) != "pending":
                raise clock.ValidationError("这一步已经跑过了，改不了", seq=seq, state=str(target["state"]))
            fields: list[str] = []
            args: list[Any] = []
            if patch.get("title") is not None:
                fields.append("title = ?")
                args.append(_text(patch.get("title"), "title"))
            if patch.get("due_ms") is not None:
                fields.append("due_ms = ?")
                args.append(int(patch["due_ms"]))
            if patch.get("max_attempts") is not None:
                limit = int(patch["max_attempts"])
                if limit < 1 or limit > MAX_ATTEMPTS:
                    raise clock.ValidationError("重试次数超出范围", field="max_attempts")
                fields.append("max_attempts = ?")
                args.append(limit)
            if patch.get("payload") is not None:
                merged = {**_load(target["payload_json"]), **dict(patch["payload"])}
                fields.append("payload_json = ?")
                args.append(_dump(merged))
            if not fields:
                continue
            fields.append("updated_ms = ?")
            args.append(now)
            args.extend([plan_id, seq])
            conn.execute(
                f"UPDATE plan_steps SET {', '.join(fields)} WHERE plan_id = ? AND seq = ?", args
            )
            changes.append(f"step:{seq}")
        if changes:
            conn.execute("UPDATE plans SET updated_ms = ? WHERE id = ?", (now, plan_id))
            audit.append(
                conn, "plan.updated", {"plan_id": plan_id, "changes": changes, "source": "api"}
            )
    return view_plan(conn, plan_id)


# --------------------------------------------------------------------------- 执行


def _check_met(conn, payload: dict[str, Any]) -> tuple[bool, dict[str, Any]]:
    check = str(payload.get("check") or "")
    if check == "savings_progress":
        goal = conn.execute(
            "SELECT id, name, saved_fen, target_fen, status FROM savings_goals WHERE id = ?",
            (str(payload.get("goal_id")),),
        ).fetchone()
        if goal is None:
            raise clock.ValidationError("检查条件里的目标不存在", goal_id=payload.get("goal_id"))
        met = str(goal["status"]) == "achieved" or int(goal["saved_fen"]) >= int(goal["target_fen"])
        return met, {
            "goal_id": str(goal["id"]),
            "saved_fen": int(goal["saved_fen"]),
            "target_fen": int(goal["target_fen"]),
        }
    if check == "balance_at_least":
        account = conn.execute(
            "SELECT id, name, tail, balance_fen FROM accounts"
            " WHERE (? IS NULL OR id = ?) ORDER BY balance_fen DESC LIMIT 1",
            (payload.get("account_id"), payload.get("account_id")),
        ).fetchone()
        if account is None:
            raise clock.ValidationError("检查条件里的账户不存在")
        need = int(payload.get("amount_fen") or 0)
        return int(account["balance_fen"]) >= need, {
            "account_id": str(account["id"]),
            "balance_fen": int(account["balance_fen"]),
            "need_fen": need,
        }
    if check == "upcoming_charge":
        within = int(payload.get("within_days") or 7)
        account = conn.execute("SELECT id FROM accounts ORDER BY balance_fen DESC LIMIT 1").fetchone()
        upcoming = proactive_upcoming(conn, str(account["id"]) if account else None, within)
        return upcoming["count"] > 0, {"count": upcoming["count"], "within_days": within}
    raise clock.ValidationError("检查条件不认识", check=check)


def proactive_upcoming(conn, account_id: str | None, within_days: int) -> dict[str, Any]:
    """惰性导入，避免 app.plans 与 app.subscriptions 在模块层互相拖。"""
    from app import subscriptions

    return subscriptions.upcoming(conn, account_id, within_days=within_days)


def _next_due(row: Any, now: int) -> int:
    payload = _load(row["payload_json"])
    repeat = int(payload.get("repeat_ms") or DEFAULT_REPEAT_MS)
    return max(int(row["due_ms"]) + repeat, now + 1000)


def _run_step(conn, row: Any, now: int) -> dict[str, Any]:
    """跑一步，返回 {outcome, detail}；所有状态变更都在调用方的事务里。"""
    plan_id = str(row["plan_id"])
    step_id = str(row["id"])
    payload = _load(row["payload_json"])
    kind = str(row["kind"])
    if kind == "check":
        met, evidence = _check_met(conn, payload)
        if met:
            conn.execute(
                "UPDATE plan_steps SET state='done', attempts=attempts+1, last_error='', updated_ms=?"
                " WHERE id = ?",
                (now, step_id),
            )
            audit.append(
                conn, "plan.step_done", {"plan_id": plan_id, "step_id": step_id, "check": payload.get("check"), "evidence": evidence}
            )
            return {"outcome": "done", "detail": evidence}
        attempts = int(row["attempts"]) + 1
        if attempts >= int(row["max_attempts"]):
            conn.execute(
                "UPDATE plan_steps SET state='failed', attempts=?, last_error=?, updated_ms=?"
                " WHERE id = ?",
                (attempts, CHECK_FAILED.format(n=attempts), now, step_id),
            )
            audit.append(
                conn,
                "plan.step_failed",
                {"plan_id": plan_id, "step_id": step_id, "reason": "CHECK_NOT_MET", "attempts": attempts, "evidence": evidence},
            )
            return {"outcome": "failed", "detail": {"attempts": attempts, **evidence}}
        # 状态必须显式退回 pending：领取时写的是 running，忘了退回来这一步就永远卡住。
        conn.execute(
            "UPDATE plan_steps SET state='pending', attempts=?, due_ms=?, last_error=?, updated_ms=?"
            " WHERE id = ?",
            (attempts, _next_due(row, now), CHECK_UNMET, now, step_id),
        )
        return {"outcome": "pending", "detail": {"attempts": attempts, **evidence}}
    if kind == "reminder":
        text = str(payload.get("text") or row["title"])
        alert_id = proactive.create_alert(
            conn,
            goal_id=payload.get("goal_id"),
            user_id=str(conn.execute("SELECT user_id FROM plans WHERE id=?", (plan_id,)).fetchone()["user_id"]),
            reason=REASON_PLAN_REMINDER,
            evidence={"plan_id": plan_id, "step_id": step_id, "title": str(row["title"]), "text": text},
            suggestion=text,
            now=now,
        )
        conn.execute(
            "UPDATE plan_steps SET state='done', attempts=attempts+1, last_error='', updated_ms=?"
            " WHERE id = ?",
            (now, step_id),
        )
        audit.append(
            conn, "plan.step_done", {"plan_id": plan_id, "step_id": step_id, "alert_id": alert_id}
        )
        return {"outcome": "done", "detail": {"alert_id": alert_id}}
    # action：后台绝不动钱，只交回用户。
    alert_id = proactive.create_alert(
        conn,
        goal_id=payload.get("goal_id"),
        user_id=str(conn.execute("SELECT user_id FROM plans WHERE id=?", (plan_id,)).fetchone()["user_id"]),
        reason=REASON_PLAN_ACTION,
        evidence={
            "plan_id": plan_id,
            "step_id": step_id,
            "tool": payload.get("tool"),
            "params": payload.get("params") or {},
            "title": str(row["title"]),
        },
        suggestion=str(payload.get("prompt") or ACTION_BOUNDARY),
        now=now,
    )
    conn.execute(
        "UPDATE plan_steps SET state='skipped', attempts=attempts+1, last_error=?, updated_ms=?"
        " WHERE id = ?",
        (ACTION_HANDOFF, now, step_id),
    )
    conn.execute("UPDATE plans SET status='paused', updated_ms=? WHERE id=?", (now, plan_id))
    audit.append(
        conn,
        "plan.updated",
        {"plan_id": plan_id, "step_id": step_id, "reason": "ACTION_REQUIRES_USER", "alert_id": alert_id},
    )
    return {"outcome": "skipped", "detail": {"alert_id": alert_id, "needs_user": True}}


def _settle_plan(conn, plan_id: str, now: int) -> None:
    plan = conn.execute("SELECT status FROM plans WHERE id = ?", (plan_id,)).fetchone()
    if plan is None or str(plan["status"]) != "active":
        # 已经挂起/取消的计划不在这里改状态：action 步交回用户后计划是 paused，
        # 如果按「所有步骤都终态」判，会把暂停误判成完成。
        return
    rows = conn.execute(
        "SELECT state FROM plan_steps WHERE plan_id = ?", (plan_id,)
    ).fetchall()
    states = {str(row["state"]) for row in rows}
    if not states:
        return
    if states <= {"done", "skipped"}:
        conn.execute("UPDATE plans SET status='completed', updated_ms=? WHERE id=? AND status <> 'cancelled'", (now, plan_id))
    elif states == {"failed"} or (states <= {"done", "skipped", "failed"} and "failed" in states):
        conn.execute("UPDATE plans SET status='failed', updated_ms=? WHERE id=? AND status='active'", (now, plan_id))


def run_due(conn, *, now: int | None = None, plan_id: str | None = None) -> dict[str, Any]:
    """推进到点的计划步骤；同一秒内重复调用不会重复执行（状态机 + 事务）。"""
    stamp = clock.now_ms() if now is None else int(now)
    sql = (
        "SELECT s.* FROM plan_steps s JOIN plans p ON p.id = s.plan_id"
        " WHERE s.state='pending' AND s.due_ms <= ? AND p.status='active'"
    )
    args: list[Any] = [stamp]
    if plan_id:
        sql += " AND s.plan_id = ?"
        args.append(plan_id)
    sql += " ORDER BY s.due_ms, s.seq LIMIT ?"
    with db.tx(conn):
        rows = conn.execute(sql, [*args, MAX_STEPS * 4]).fetchall()
        executed: list[dict[str, Any]] = []
        for row in rows:
            claimed = conn.execute(
                "UPDATE plan_steps SET state='running', updated_ms=? WHERE id=? AND state='pending'",
                (stamp, str(row["id"])),
            )
            if claimed.rowcount != 1:
                continue
            fresh = conn.execute("SELECT * FROM plan_steps WHERE id=?", (str(row["id"]),)).fetchone()
            result = _run_step(conn, fresh, stamp)
            executed.append(
                {
                    "plan_id": str(row["plan_id"]),
                    "step_id": str(row["id"]),
                    "seq": int(row["seq"]),
                    "kind": str(row["kind"]),
                    "outcome": result["outcome"],
                    "detail": result["detail"],
                }
            )
            _settle_plan(conn, str(row["plan_id"]), stamp)
    return {"executed": executed, "count": len(executed), "now_ms": stamp}


__all__ = [
    "ACTION_BOUNDARY",
    "CHECK_KINDS",
    "PLAN_STATUSES",
    "STEP_KINDS",
    "STEP_STATES",
    "create",
    "list_plans",
    "plan_view",
    "run_due",
    "step_view",
    "update",
    "validate_step",
    "view_plan",
]
