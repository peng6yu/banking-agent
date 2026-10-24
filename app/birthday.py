"""生日跨场景计划：一笔 1000 元预算，拆成鲜花与蛋糕两项，生日前两天触发。

三条设计边界（写在代码里，而不是只写进文档）：

1. **预算预留是账本记录，不是资金冻结。** 这里不建 holds 行、不扣余额，也不会像
   5 分钟的临时 hold 那样"过期自动释放"。拿短 hold 冒充一个月的长期预留，等于把
   计划语义写成资金占用语义，对账时根本解释不通；真实预授权需要商户侧协议，本演示
   只做 contest_simulation 级别的预算占用（plan_budgets 表）。
2. **到点后台只检查、不代扣。** 生日前两天的步骤是：核对余额是否够预算 → 把两个
   待确认项目推给用户。真正的付款走助手的确认卡片：策略裁决 → 用户确认/多因子 →
   能力令牌 → 工具执行，和手动转账同一条路径，后台调度绕不过去。
3. **重复触发不重复下单。** 预算行有状态机（reserved/paying/committed/declined/
   failed/released），领取是一次带条件的 UPDATE，抢不到就跳过；已 committed 的
   项目再被触发只返回 already_paid，不会再建一张执行图。
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import json
import logging
from typing import Any, Final

from app import audit, clock, db
from app.clock import ValidationError
from app import ids
from app.ids import new_id
from app.money import fen_to_yuan_str
from app.planner import executor
from app.planner.dag import DAGNode, Dag
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
    "这里的「预算预留」是账本上的占用，不是资金冻结：不扣余额、不建 5 分钟临时 hold，"
    "也不会自动释放成现金。真实银行的预授权要商户参与，本演示只做记账级预算控制。"
)
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
        "boundary": BUDGET_BOUNDARY,
    }
    view["items"] = items
    view["item_count"] = len(items)
    view["needs_user"] = [item["item_key"] for item in items if item["needs_user"]]
    return view


def create_plan(
    conn,
    *,
    user_id: str,
    birthday: str,
    budget_fen: int = DEFAULT_BUDGET_FEN,
    items: list[dict[str, Any]] | None = None,
    title: str | None = None,
) -> dict[str, Any]:
    """建生日计划：预算 + 两个项目 + 生日前两天的检查/提醒步骤，全部落库。"""
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
    meta = {
        "kind": "birthday",
        "birthday": day.isoformat(),
        "budget_fen": budget,
        "trigger_ms": trigger_ms,
        "trigger_text": _time_text(trigger_ms),
        "items": [{k: item[k] for k in ("key", "label", "merchant", "amount_fen")} for item in clean],
        "summary": f"生日 {day.isoformat()}，预算 {fen_to_yuan_str(budget)} 元，"
        f"生日前 {TRIGGER_DAYS_BEFORE} 天（{_time_text(trigger_ms)}）提醒，付款要你确认。",
    }
    plan_title = str(title or "").strip() or f"{day.month} 月 {day.day} 日生日计划"
    created = plans_mod.create(
        conn, user_id=user_id, title=plan_title[:MAX_TITLE], steps=steps, meta=meta
    )
    plan_id = str(created["plan_id"])
    now = _now_ms()
    seq = 0
    with db.tx(conn):
        for item in clean:
            conn.execute(
                "INSERT INTO plan_budgets (id, plan_id, item_key, label, merchant, amount_fen,"
                " extra_json, status, reason, created_ms, updated_ms)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, 'reserved', '', ?, ?)",
                (
                    new_id("pb"),
                    plan_id,
                    item["key"],
                    item["label"],
                    item["merchant"],
                    item["amount_fen"],
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
                "budget_fen": budget,
                "reserved_fen": sum(item["amount_fen"] for item in clean),
                "items": [item["key"] for item in clean],
                "trigger_ms": trigger_ms,
                "scope": "bookkeeping_reservation",
            },
            session_id=f"plan:{plan_id}",
        )
    view = plan_with_budgets(conn, plan_id) or {}
    view["audit_seq"] = seq
    return view


def _claim(conn, plan_id: str, item_key: str) -> bool:
    """领走一项预算：带条件 UPDATE，抢不到就是别人已经在付了。"""
    cursor = conn.execute(
        "UPDATE plan_budgets SET status = 'paying', updated_ms = ?"
        " WHERE plan_id = ? AND item_key = ? AND status = 'reserved'",
        (_now_ms(), plan_id, item_key),
    )
    return cursor.rowcount == 1


def _finish(conn, plan_id: str, item_key: str, status: str, reason: str = "") -> None:
    if status not in BUDGET_STATUSES:
        raise ValidationError("未知的预算状态", status=status)
    conn.execute(
        "UPDATE plan_budgets SET status = ?, reason = ?, updated_ms = ?"
        " WHERE plan_id = ? AND item_key = ?",
        (status, reason, _now_ms(), plan_id, item_key),
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
            with db.tx(conn):
                _finish(conn, plan_id, item_key, "committed", summary)
                _mark_step(conn, plan_id, item_key, "done")
                audit.append(
                    conn,
                    "plan.item_paid",
                    {
                        "plan_id": plan_id,
                        "item_key": item_key,
                        "amount_fen": int(row["amount_fen"]),
                        "confirmation_id": cid,
                        "summary": summary,
                    },
                    session_id=f"plan:{plan_id}",
                )
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
        with db.tx(conn):
            _finish(conn, plan_id, item_key, "failed", f"{error_code}:{last_error}")
            _mark_step(conn, plan_id, item_key, "failed", last_error)
            audit.append(
                conn,
                "plan.item_failed",
                {
                    "plan_id": plan_id,
                    "item_key": item_key,
                    "amount_fen": int(row["amount_fen"]),
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


__all__ = [
    "BUDGET_BOUNDARY",
    "BUDGET_STATUSES",
    "DEFAULT_BUDGET_FEN",
    "DEFAULT_ITEMS",
    "PAY_BOUNDARY",
    "TRIGGER_DAYS_BEFORE",
    "budgets",
    "create_plan",
    "parse_birthday",
    "pay_item",
    "plan_with_budgets",
    "trigger_at",
]
