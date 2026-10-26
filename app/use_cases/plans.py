"""跨场景计划与生日计划：建 / 改 / 推进 / 付款。

生日付款是这个包里唯一真花钱的动作，所以它**不在**这里开事务：birthday.pay_item
自己按「确认 -> 策略 -> 令牌 -> 执行」走完整条路径，外面再包一层事务只会把执行器
的写入拖长，还会把已经提交的确认卡状态一起回滚掉。
"""

from __future__ import annotations

from typing import Any

import app.birthday as birthday
import app.db as db
import app.plans as plans
import app.proactive as proactive
from app.clock import BankError, NotFound
from app.use_cases.base import Outcome, fail, ok, write_case


@write_case
def plan_create(
    conn: Any,
    *,
    title: str,
    steps: list[dict[str, Any]],
    meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """建跨场景计划：步骤落库，后台节拍按到点时间推进（资金动作只提醒、不代扣）。"""
    return {
        "plan": plans.create(
            conn,
            user_id=proactive.default_user_id(conn),
            title=title,
            steps=steps,
            meta=meta or {},
        )
    }


@write_case
def plan_update(
    conn: Any,
    plan_id: str,
    *,
    title: str | None = None,
    status: str | None = None,
    steps: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """改计划：标题、状态（用来取消/暂停）、还没跑过的步骤。"""
    updated = plans.update(
        conn,
        plan_id,
        user_id=proactive.default_user_id(conn),
        title=title,
        status=status,
        steps=steps,
    )
    if updated is None:
        raise NotFound()
    return {"plan": updated}


def plan_run_due(*, plan_id: str | None = None) -> Outcome:
    """手动推进到点的计划步骤：先跑已授权的生日付款，再推进通用计划。

    顺序和后台 ticker.run_plans_once 一致。手动触发不是旁路——自动付款仍按最新
    日累计重新裁决，红灯/参数漂移/取消都不扣。

    这里**不包**外层事务：生日付款会经由线程池再开一条连接写同一份库，外层事务
    拿着写锁就会把它锁死（sqlite3.OperationalError: database is locked）。两段各
    自带着自己的事务提交，失败时已经扣下的付款不回滚——那本来也是真实发生过的账。
    """
    try:
        with db.open_conn() as conn:
            authorized = birthday.run_due_authorized(conn, plan_id=plan_id)
            result = plans.run_due(conn, plan_id=plan_id)
    except BankError as err:
        return fail(err)
    return ok(
        {
            "executed": result["executed"],
            "count": result["count"],
            "authorized": authorized,
        }
    )


@write_case
def birthday_create(
    conn: Any,
    *,
    birthday_date: str,
    budget_fen: int = 100_000,
    items: list[dict[str, Any]] | None = None,
    title: str | None = None,
    account_id: str | None = None,
    save_authorization: bool = False,
) -> dict[str, Any]:
    """建生日计划：预算、鲜花与蛋糕两项、生日前两天到点提醒，全部落库。"""
    return {
        "plan": birthday.create_plan(
            conn,
            user_id=proactive.default_user_id(conn),
            birthday=birthday_date,
            budget_fen=budget_fen,
            items=items or None,
            title=title,
            account_id=account_id,
            save_authorization=save_authorization,
        )
    }


async def birthday_pay(
    *,
    plan_id: str,
    item_key: str,
    session_id: str | None = None,
    approve: bool = True,
    proofs: list[dict[str, Any]] | None = None,
) -> Outcome:
    """付生日计划里的一项：确认 -> 策略 -> 令牌 -> 执行，和手动付款同一条路径。

    缺省会话带 :pay 后缀：后台到点自动付款用的是 plan:{id}:{key}，两边不能共用
    同一轮执行，否则用户这边的待验证确认卡会被后台调度「接手」。
    """
    try:
        data = await birthday.pay_item(
            plan_id=plan_id,
            item_key=item_key,
            session_id=session_id or f"plan:{plan_id}:{item_key}:pay",
            approve=approve,
            proofs=proofs,
        )
    except BankError as err:
        return fail(err)
    return ok(data)


__all__ = ["plan_create", "birthday_create", "birthday_pay", "plan_run_due", "plan_update"]
