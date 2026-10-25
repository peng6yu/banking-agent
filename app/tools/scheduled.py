
"""定时转账（场景 1 补齐）：预约不立刻动钱，到点由系统时钟执行。

四条边界：
1. 预约本身不动钱——只在 scheduled_transfers 里留一行，到点执行时才走 hold/commit；
2. 预约是资金指令，金额在创建期就判档（app/policy/rules.py 的 R06/R08）：
   小额黄灯确认即可，超过单笔上限要人脸+短信；验过了就把 factor_verified_ms 记在行上，
   到点执行凭这条记录放行，不被当日额度线拦死；
3. 当日额度不够不算失败：这笔让出今天、顺延到下一个自然日，status 仍是 scheduled，
   审计写 scheduled.deferred；顺延有上限（MAX_DEFERRALS），超过上限才明确失败；
4. 取消只对还没执行的预约生效，已执行的只能靠冲正（reverse_transfer）；
5. 一次执行先抢**租约**（claim_token + claimed_ms），抢不到就跳过：两个进程同时 tick、
   或者同一进程重入，都只会有一个真的动钱；持有者中途崩掉，租约过期后由下一轮接管；
6. 创建期验过的因子**有保质期**（AUTH_TTL_MS，30 天）：过期后这笔不再享受「越额度放行」，
   回到普通额度口径排队（额度不够就顺延）。验过的人脸不能当永久通行证。

执行由 app/planner/ticker.py 的节拍器负责，它是一个系统时钟驱动的受信组件，
不经模型、也不经意图层——令牌在这里由系统自己签发并留 token.issued 审计。
这是全仓唯一的自签令牌例外：写工具的令牌本该由 planner 签，但到点执行没有 planner 在场。
"""

from __future__ import annotations

import calendar
import datetime
import logging
import sqlite3
import uuid
from typing import Any, Final

import app.audit as audit  # noqa: PLR0402
import app.db as db  # noqa: PLR0402
import app.funds as funds  # noqa: PLR0402
import app.ids as ids  # noqa: PLR0402
from app.clock import PolicyDenied, ValidationError, now_ms
from app.ids import idempotency_key
from app.models import Action, ToolResult
from app.money import fen_to_yuan_str
from app.policy import PolicyContext, decide
from app.policy.rules import ABSOLUTE_DAILY_CAP_FEN, DAILY_RISK_THRESHOLD_FEN
from app.security import factors as factors_mod
from app.security import token as token_mod
from app.tools.base import (
    ToolContext,
    build_action,
    fail_result,
    ok_result,
    payee_exists,
    payee_name,
    primary_account,
    require_token,
)

log = logging.getLogger(__name__)

RECURRENCES: Final[tuple[str, ...]] = ("once", "monthly")
MAX_SCHEDULE_AHEAD_MS: Final[int] = 366 * 24 * 3600 * 1000
SCHEDULED_TEXT: Final[str] = "已经帮你记下了，到时间会自动转。"
# 当日额度不够时的顺延上限：最多让 7 天，还排不进去就明确失败，不能无限期挂着。
MAX_DEFERRALS: Final[int] = 7
# 领取租约：一轮执行最多占这么久，超时视为持有者已崩、允许下一轮接管
CLAIM_LEASE_MS: Final[int] = 10 * 60 * 1000
# 创建期因子验证的保质期：过期后不再放行当日额度线，回到普通口径
AUTH_TTL_MS: Final[int] = 30 * 24 * 3600 * 1000


def _new_id(prefix: str) -> str:
    return ids.new_id(prefix)


def _read_due(params: dict[str, Any]) -> int:
    due = params.get("due_ms")
    if not isinstance(due, int):
        raise ValidationError("缺少执行时间", reason="MISSING_DUE_MS", field="due_ms")
    now = now_ms()
    if due <= now:
        raise ValidationError("执行时间必须晚于现在", reason="DUE_IN_PAST", field="due_ms")
    if due - now > MAX_SCHEDULE_AHEAD_MS:
        raise ValidationError("执行时间太远了", reason="DUE_TOO_FAR", field="due_ms")
    return due


def schedule_transfer(
    ctx: ToolContext, params: dict[str, Any], token: str | None = None
) -> ToolResult:
    """预约一笔转账：只写一行指令，不动钱。

    创建期把金额判清楚（判档在 policy，见 app/policy/rules.py）：超过单笔上限的预约
    是红灯，令牌等级记在 factor_verified_ms 上，到点执行凭它放行当日额度线。
    令牌在这里必须真验七步——原来只判非空，等于谁拿个字符串都能排期。
    """
    require_token("schedule_transfer", params, token)
    # 老库（演示库里那份 data/bank.db）第一次被这个版本碰到时补列，不用重建库。
    db.ensure_columns(ctx.conn)
    try:
        due_ms = _read_due(params)
    except ValidationError as exc:
        return fail_result(str(exc.fields.get("reason") or exc.code), exc.user_text)
    account = primary_account(ctx.conn, ctx.account_id)
    payee_id = str(params.get("payee_id", ""))
    if not payee_exists(ctx.conn, payee_id):
        return fail_result("PAYEE_NOT_FOUND", "通讯录里没有这个收款人。")
    amount_fen = int(params["amount_fen"])
    recurrence = str(params.get("recurrence") or "once")
    if recurrence not in RECURRENCES:
        raise ValidationError("不认识的重复方式", reason="BAD_RECURRENCE", recurrence=recurrence)
    # 动作的参数用整包 params：令牌就是对整包参数签的指纹（app/planner/runner.issue_token），
    # 这里少取一个键，验签就对不上。
    action = build_action("schedule_transfer", params, str(account["id"]), amount_fen)
    payload = token_mod.verify(ctx.conn, str(token), action, secret=ctx.secret)
    # 红灯令牌 = 用户已经过人脸+短信（policy 的红牌都要因子，见 app/planner/executor._guard）。
    # 把这件事记在预约行上，到点执行凭它放行当日额度线。
    factor_verified_ms = now_ms() if payload.level == "red" else 0
    key = idempotency_key(ctx.session_key(), ctx.node_key(), str(params.get("note") or ""))
    row_id = _new_id("sch")
    with _tx(ctx.conn):
        ctx.conn.execute(
            "INSERT INTO scheduled_transfers (id, user_id, account_id, payee_id, amount_fen, note,"
            " due_ms, recurrence, status, created_ms, idempotency_key, factor_verified_ms)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'scheduled', ?, ?, ?)",
            (row_id, str(account["user_id"]), str(account["id"]), payee_id, amount_fen,
             str(params.get("note") or ""), due_ms, recurrence, now_ms(), key, factor_verified_ms),
        )
        audit.append(
            ctx.conn,
            "scheduled.created",
            {"scheduled_id": row_id, "payee_id": payee_id, "amount_fen": amount_fen,
             "due_ms": due_ms, "recurrence": recurrence,
             "factor_verified_ms": factor_verified_ms,
             "params_hash": token_mod.params_hash(action.params)},
            session_id=ctx.session_key(),
        )
    who = payee_name(ctx.conn, payee_id)
    when = _time_text(due_ms)
    extra = "，每月重复" if recurrence == "monthly" else ""
    return ok_result(
        {
            "scheduled_id": row_id,
            "payee_id": payee_id,
            "payee_name": who,
            "amount_fen": amount_fen,
            "amount_yuan": fen_to_yuan_str(amount_fen),
            "due_ms": due_ms,
            "recurrence": recurrence,
            "status": "scheduled",
            "summary": f"已预约 {when} 向 {who} 转出 {fen_to_yuan_str(amount_fen)} 元{extra}。{SCHEDULED_TEXT}",
        }
    )


def list_scheduled(
    ctx: ToolContext, params: dict[str, Any], token: str | None = None
) -> ToolResult:
    """列出还没执行的预约转账。"""
    # 必须按当前用户过滤：这张表跨用户，早先的 SQL 没有 user/account 条件，
    # 会把别人名下的预约转账也列出来（越权读）。现在从当前账户反查 user_id。
    acct = primary_account(ctx.conn, ctx.account_id)
    rows = ctx.conn.execute(
        "SELECT s.id, s.amount_fen, s.due_ms, s.recurrence, s.status,"
        "       s.needs_verification, p.name AS payee"
        " FROM scheduled_transfers s JOIN payees p ON p.id = s.payee_id"
        " JOIN accounts a ON a.id = s.account_id"
        " WHERE s.status = 'scheduled' AND a.user_id = ? ORDER BY s.due_ms",
        (str(acct["user_id"]),),
    ).fetchall()
    items = [
        {
            "scheduled_id": str(row["id"]),
            "payee_name": str(row["payee"]),
            "amount_fen": int(row["amount_fen"]),
            "amount_yuan": fen_to_yuan_str(int(row["amount_fen"])),
            "due_ms": int(row["due_ms"]),
            "due_text": _time_text(int(row["due_ms"])),
            "recurrence": str(row["recurrence"]),
            # 到点复判升级成红灯但还没强验证：前端要明确提示「需要再验一次」，
            # 别让用户以为它只是「还没到点」。
            "needs_verification": bool(row["needs_verification"]),
        }
        for row in rows
    ]
    if not items:
        return ok_result({"count": 0, "items": [], "summary": "现在没有待执行的预约转账。"})
    return ok_result(
        {"count": len(items), "items": items, "summary": f"你有 {len(items)} 笔待执行的预约转账。"}
    )


def cancel_scheduled(
    ctx: ToolContext, params: dict[str, Any], token: str | None = None
) -> ToolResult:
    """取消一笔还没执行的预约；已经执行过的取消不了。"""
    require_token("cancel_scheduled", params, token)
    row_id = str(params.get("scheduled_id") or "").strip()
    row = ctx.conn.execute(
        "SELECT id, status FROM scheduled_transfers WHERE id = ?", (row_id,)
    ).fetchone()
    if row is None:
        return fail_result("SCHEDULED_NOT_FOUND", "没找到这条预约。")
    if str(row["status"]) == "cancelled":
        return ok_result({"scheduled_id": row_id, "status": "cancelled", "idempotent": True,
                          "summary": "这条预约已经取消过了。"})
    if str(row["status"]) != "scheduled":
        return fail_result(
            "SCHEDULED_NOT_CANCELLABLE",
            "这笔预约已经执行过了，取消不了。已经出账的钱只能做账务冲正（本地反向记账），"
            "这不是把钱从对方那里追回来。",
        )
    with _tx(ctx.conn):
        ctx.conn.execute(
            "UPDATE scheduled_transfers SET status = 'cancelled' WHERE id = ? AND status = 'scheduled'",
            (row_id,),
        )
        audit.append(ctx.conn, "scheduled.cancelled", {"scheduled_id": row_id},
                     session_id=ctx.session_key())
    return ok_result({"scheduled_id": row_id, "status": "cancelled", "summary": "这条预约已经取消了。"})


def execute_due(
    conn: sqlite3.Connection, now: int | None = None, secret: str | None = None
) -> list[dict[str, Any]]:
    """把到点的预约真正转出去。由系统时钟调用，不经模型。

    钱一律经 app/funds 的 hold/commit，不在这个函数里直接改余额——定时执行也不能例外。
    """
    stamp = now if now is not None else now_ms()
    # 老库第一次被这个版本碰到时补列（factor_verified_ms / defer_count），不用重建库。
    db.ensure_columns(conn)
    rows = conn.execute(
        "SELECT * FROM scheduled_transfers WHERE status = 'scheduled' AND due_ms <= ?"
        " AND needs_verification = 0 ORDER BY due_ms",
        (stamp,),
    ).fetchall()
    done: list[dict[str, Any]] = []
    for row in rows:
        sched_id = str(row["id"])
        token = _claim(conn, sched_id, stamp)
        if token is None:
            # 另一个进程/线程刚抢走（或正持有租约）：这一轮什么都不做，下一轮再看。
            log.info("scheduled.claim_skipped id=%s", sched_id)
            done.append({"scheduled_id": sched_id, "status": "skipped",
                         "reason": "CLAIMED_BY_ANOTHER_WORKER"})
            continue
        try:
            result = _execute_one(conn, row, stamp, secret)
        finally:
            _release_claim(conn, sched_id, token)
        done.append(result)
    return done


def _claim(conn: sqlite3.Connection, sched_id: str, stamp: int) -> str | None:
    """原子抢租约；抢到返回 token，抢不到返回 None。

    用一条 UPDATE … WHERE 拿行锁：SQLite 里同一行只有一个写者能成功，rowcount 就是裁决结果。
    判断条件里带上 status='scheduled'，所以别人刚取消/刚执行完的行不会被抢到；
    租约过期（claimed_ms 太旧）说明持有者死了，允许接管。

    还要再确认一次 due_ms <= stamp：进入循环的那份 rows 是**开始时的快照**，
    期间别的进程可能已经把这行顺延到明天（额度不够）。只按 ID+status 抢，会把
    「明天才该执行的行」当今天执行，然后用上一轮的幂等键重放到一份已释放的 hold 上，
    把一条本来只是顺延的预约误判成失败。这个条件让抢行重新读一次库里的真实到期时间。
    同理要排除 needs_verification=1：进入循环的快照可能早于「到点复判为红、已挂起待验证」，
    不排除的话旧 worker 会绕过挂起状态直接再执行一次。
    """
    token = uuid.uuid4().hex[:12]
    with _tx(conn):
        cur = conn.execute(
            "UPDATE scheduled_transfers SET claim_token = ?, claimed_ms = ?"
            " WHERE id = ? AND status = 'scheduled' AND due_ms <= ?"
            " AND needs_verification = 0"
            " AND (claim_token = '' OR claimed_ms <= ?)",
            (token, stamp, sched_id, stamp, stamp - CLAIM_LEASE_MS),
        )
        if cur.rowcount != 1:
            return None
    return token


def _release_claim(conn: sqlite3.Connection, sched_id: str, token: str) -> None:
    """放掉租约（只放自己那把，别把别人刚抢到的清掉）。"""
    with _tx(conn):
        conn.execute(
            "UPDATE scheduled_transfers SET claim_token = '', claimed_ms = 0"
            " WHERE id = ? AND claim_token = ?",
            (sched_id, token),
        )


def _execute_one(
    conn: sqlite3.Connection, row: sqlite3.Row, stamp: int, secret: str | None = None
) -> dict[str, Any]:
    """执行单条预约：到点复判、签系统令牌、建 hold、commit、写审计。

    四条边界：
    1. 到点执行是系统受信组件，不经模型，令牌只能在这里自签。写工具的令牌本该由
       planner 签，但到点执行时没有 planner 在场——这是全仓唯一的自签例外。
    2. **创建时的判档不算数**：到点那天按用户维度的当前日累计重新裁决（R06/R07/R08）。
       预约机制不能成为绕过日累计的旁路——用户三周前验过一次人脸，不代表他今天
       累计转了几万还可以继续。复判是红的、而创建期的因子验证又缺失或过期，
       这笔就停在 needs_verification 等用户补一次强验证：不自动执行、也不静默顺延，
       因为「默默等一天」会让用户以为排上了，实际上永远排不上。
    3. 创建期验过因子且仍在保质期（AUTH_TTL_MS）内 → 发红牌令牌，red 档允许越过
       日累计分级线（这正是强验证换来的额度），但仍受绝对每日限额（如有）约束。
       授权用一次就作废：执行成功即清 factor_verified_ms，下个月想再越线得再验。
    4. 撞了分级线不是失败：让出今天、顺延到明天，status 仍是 scheduled，
       审计写 scheduled.deferred；顺延有上限，超过上限才明确失败。
    """
    sched_id = str(row["id"])
    payee_id = str(row["payee_id"])
    amount_fen = int(row["amount_fen"])
    verified_at = int(row["factor_verified_ms"] or 0)
    verified = verified_at > 0 and (stamp - verified_at) <= AUTH_TTL_MS
    if verified_at > 0 and not verified:
        # 验过的人脸不是永久通行证：超过 AUTH_TTL_MS 就不再放行当日额度线。
        # 不直接判失败——这笔预约本身还有效，只是回到普通额度口径排队。
        with _tx(conn):
            audit.append(
                conn,
                "scheduled.auth_expired",
                {"scheduled_id": sched_id, "verified_ms": verified_at,
                 "age_days": round((stamp - verified_at) / 86400000, 1),
                 "ttl_days": round(AUTH_TTL_MS / 86400000, 1)},
                session_id=f"sched:{sched_id}",
            )
        log.warning("scheduled.auth_expired id=%s age_ms=%s", sched_id, stamp - verified_at)
    action = build_action(
        "scheduled_transfer",
        {"payee_id": payee_id, "amount_fen": amount_fen, "due_ms": int(row["due_ms"])},
        str(row["account_id"]),
        amount_fen,
    )
    session_id = f"sched:{sched_id}"
    # 到点复判：只读、不改钱，真正的原子性仍由 funds.commit 保证。
    decision = _fresh_decision(conn, action, stamp)
    if decision.level == "red" and not verified:
        return _need_verification(conn, row, stamp, session_id=session_id, decision=decision)
    # 红牌（且已验证）→ 不设分级线，允许越过；黄牌 → 仍按分级线保护。
    risk_fen = None if decision.level == "red" else DAILY_RISK_THRESHOLD_FEN
    try:
        tok = token_mod.issue_audited(
            conn, action, session_id=session_id,
            level="red" if decision.level == "red" else "yellow", secret=secret
        )
        hold = funds.initiate(
            conn, action, session_id=session_id, node_id="due", token=tok, payee_id=payee_id, secret=secret
        )
        if hold.reused or hold.status != "held":
            # 同一个幂等键已经有一份 hold 了：说明这一行的这份参数被处理过。
            # 要么是另一个 worker 刚提交（那就不能再扣一次），要么是上一轮撞额度
            # 顺延后把 hold 放掉了（再 commit 只会拿到 HoldExpired）。
            # 两种情况都不该把预约标记成失败——它只是这一轮轮不到执行。
            log.info("scheduled.hold_replayed id=%s status=%s", sched_id, hold.status)
            return {"scheduled_id": sched_id, "status": "skipped", "reason": "ALREADY_HANDLED"}
        try:
            outcome = funds.commit(
                conn, hold.hold_id,
                daily_risk_threshold_fen=risk_fen,
                absolute_daily_cap_fen=ABSOLUTE_DAILY_CAP_FEN,
            )
        except PolicyDenied as exc:
            funds.release(conn, hold.hold_id, reason=exc.code, session_id=session_id)
            if exc.code == "DAILY_RISK_UPGRADE_REQUIRED":
                # 复判之后、提交之前，别的交易把日累计推过了线（并发时序）。
                # 这里不能沿用创建期的黄牌，也不能悄悄顺延：转人工/强验证。
                return _need_verification(
                    conn, row, stamp, session_id=session_id, decision=decision,
                    error_code=exc.code,
                )
            if exc.code == "DAILY_ABSOLUTE_CAP_EXCEEDED":
                # 绝对限额是硬线：因子也突破不了，明天额度重算。
                return _defer(conn, row, stamp, session_id=session_id,
                              reason=exc.code)
            raise
        with _tx(conn):
            conn.execute(
                "UPDATE scheduled_transfers SET status = ?, executed_ms = ?,"
                " needs_verification = 0, factor_verified_ms = 0 WHERE id = ?",
                ("executed" if str(row["recurrence"]) == "once" else "scheduled", stamp, sched_id),
            )
            if str(row["recurrence"]) == "monthly":
                conn.execute(
                    "UPDATE scheduled_transfers SET due_ms = ? WHERE id = ?",
                    (_next_month_ms(int(row["due_ms"])), sched_id),
                )
            # 一次性授权用完即焚：这一笔的强验证不复制给下一期、也不复制给别的交易。
            audit.append(
                conn,
                "scheduled.executed",
                {"scheduled_id": sched_id, "hold_id": hold.hold_id,
                 "amount_fen": amount_fen, "balance_after_fen": int(outcome.balance_after_fen),
                 "level": decision.level, "rule_id": decision.rule_id,
                 "verified_factor_used": bool(verified)},
                session_id=session_id,
            )
        return {"scheduled_id": sched_id, "status": "executed", "amount_fen": amount_fen}
    except Exception as exc:  # noqa: BLE001 - 一条失败不能拖垮整批，但要留痕
        log.warning("scheduled.execute_failed id=%s err=%s", sched_id, exc)
        with _tx(conn):
            conn.execute(
                "UPDATE scheduled_transfers SET status = 'failed', executed_ms = ? WHERE id = ?",
                (stamp, sched_id),
            )
            # 失败也要进审计链：只把状态改成 failed 而不留痕，
            # 事后既查不到原因，也和"审计覆盖所有资金动作"的承诺对不上。
            audit.append(
                conn,
                "scheduled.failed",
                {"scheduled_id": sched_id, "amount_fen": amount_fen,
                 "error_code": type(exc).__name__, "error": str(exc)[:300]},
                session_id=session_id,
            )
        return {"scheduled_id": sched_id, "status": "failed", "error": str(exc)}


def _fresh_decision(
    conn: sqlite3.Connection, action: Action, stamp: int
) -> Any:
    """按当前用户维度的日累计重新裁决这笔预约，不碰钱。

    口径必须和 planner 一致：spent 走 funds.spent_today_fen（同一用户所有账户），
    否则预约这条旁路会和在线转账用两把尺子量同一天。
    """
    spent = funds.spent_today_fen(conn, action.account_id, at_ms=stamp)
    return decide(action, PolicyContext(spent_today_fen=spent, now_ms=stamp))


def _need_verification(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    stamp: int,
    *,
    session_id: str,
    decision: Any,
    error_code: str = "",
) -> dict[str, Any]:
    """把预约挂到「等待强验证」：不动钱、不顺延、不判失败。

    这是人工/用户介入点：用户补一次人脸+短信，走到 verify_and_execute 才继续。
    needs_verification 是持久列，进程重启后仍然待验证，不会被 ticker 悄悄重跑。
    """
    sched_id = str(row["id"])
    with _tx(conn):
        conn.execute(
            "UPDATE scheduled_transfers SET needs_verification = 1 WHERE id = ?",
            (sched_id,),
        )
        audit.append(
            conn,
            "scheduled.needs_verification",
            {
                "scheduled_id": sched_id,
                "amount_fen": int(row["amount_fen"]),
                "level": decision.level,
                "rule_id": decision.rule_id,
                "reason_code": decision.reason_code,
                "required_factors": list(decision.required_factors),
                "trigger_error_code": error_code,
            },
            session_id=session_id,
        )
    log.info("scheduled.needs_verification id=%s rule=%s", sched_id, decision.rule_id)
    return {
        "scheduled_id": sched_id,
        "status": "needs_verification",
        "level": decision.level,
        "rule_id": decision.rule_id,
        "reason_code": decision.reason_code,
        "required_factors": list(decision.required_factors),
        "amount_fen": int(row["amount_fen"]),
    }


def challenge_scheduled(
    conn: sqlite3.Connection,
    scheduled_id: str,
    factor: str,
    *,
    secret: str | None = None,
    now: int | None = None,
) -> dict[str, Any]:
    """给一笔「到点复判变红」的预约签发强验证挑战。

    挑战绑定 sched:<id>，所以它只能用来验这一笔预约，不能被拿去当别的交易的证明。
    """
    stamp = int(now if now is not None else now_ms())
    db.ensure_columns(conn)
    row = conn.execute(
        "SELECT * FROM scheduled_transfers WHERE id = ?", (scheduled_id,)
    ).fetchone()
    if row is None:
        raise ValidationError("没找到这条预约", scheduled_id=scheduled_id)
    if str(row["status"]) != "scheduled":
        raise ValidationError("这条预约已经不在待执行状态", status=str(row["status"]))
    action = build_action(
        "scheduled_transfer",
        {"payee_id": str(row["payee_id"]), "amount_fen": int(row["amount_fen"]),
         "due_ms": int(row["due_ms"])},
        str(row["account_id"]),
        int(row["amount_fen"]),
    )
    decision = _fresh_decision(conn, action, stamp)
    kind = str(factor or "").strip().lower()
    if kind not in decision.required_factors:
        raise ValidationError(
            "当前这笔预约不需要这个验证因子", factor=kind,
            required=list(decision.required_factors),
        )
    session_id = f"sched:{scheduled_id}"
    return factors_mod.issue(
        conn,
        session_id=session_id,
        confirmation_id=session_id,
        factor=kind,
        secret=secret,
        now=stamp,
    )


def verify_and_execute(
    conn: sqlite3.Connection,
    scheduled_id: str,
    *,
    proofs: Any,
    secret: str | None = None,
    now: int | None = None,
) -> dict[str, Any]:
    """用户/操作员补一次强验证，然后立刻尝试执行这笔预约。

    只有验过了才写 factor_verified_ms；失败保持 needs_verification，并留审计。
    恢复执行时会**重新裁决**（execute_due 内部再算一次），不会拿旧判档直接放行。
    """
    stamp = int(now if now is not None else now_ms())
    db.ensure_columns(conn)
    row = conn.execute(
        "SELECT * FROM scheduled_transfers WHERE id = ?", (scheduled_id,)
    ).fetchone()
    if row is None:
        raise ValidationError("没找到这条预约", scheduled_id=scheduled_id)
    if str(row["status"]) != "scheduled":
        return {"ok": False, "scheduled_id": scheduled_id,
                "status": str(row["status"]), "reason": "SCHEDULED_NOT_ACTIVE"}
    action = build_action(
        "scheduled_transfer",
        {"payee_id": str(row["payee_id"]), "amount_fen": int(row["amount_fen"]),
         "due_ms": int(row["due_ms"])},
        str(row["account_id"]),
        int(row["amount_fen"]),
    )
    decision = _fresh_decision(conn, action, stamp)
    session_id = f"sched:{scheduled_id}"
    if decision.level == "red":
        result = factors_mod.verify(
            conn,
            session_id=session_id,
            confirmation_id=session_id,
            required=list(decision.required_factors),
            proofs=proofs,
            secret=secret,
            now=stamp,
        )
        if not result.get("ok"):
            with _tx(conn):
                audit.append(
                    conn,
                    "scheduled.verification_failed",
                    {"scheduled_id": scheduled_id,
                     "missing": list(result.get("missing") or []),
                     "invalid": list(result.get("invalid") or [])},
                    session_id=session_id,
                )
            return {"ok": False, "scheduled_id": scheduled_id,
                    "status": "needs_verification", "reason": "FACTOR_NOT_VERIFIED",
                    "missing": list(result.get("missing") or []),
                    "invalid": list(result.get("invalid") or [])}
        with _tx(conn):
            conn.execute(
                "UPDATE scheduled_transfers SET factor_verified_ms = ?, needs_verification = 0"
                " WHERE id = ?",
                (stamp, scheduled_id),
            )
            audit.append(
                conn,
                "scheduled.verified",
                {"scheduled_id": scheduled_id, "factors": list(decision.required_factors),
                 "rule_id": decision.rule_id},
                session_id=session_id,
            )
    elif int(row["needs_verification"] or 0):
        with _tx(conn):
            conn.execute(
                "UPDATE scheduled_transfers SET needs_verification = 0 WHERE id = ?",
                (scheduled_id,),
            )
            audit.append(
                conn,
                "scheduled.verification_waived",
                {"scheduled_id": scheduled_id, "rule_id": decision.rule_id},
                session_id=session_id,
            )
    results = execute_due(conn, now=stamp, secret=secret)
    mine = [r for r in results if r.get("scheduled_id") == scheduled_id]
    return {"ok": True, "scheduled_id": scheduled_id,
            "status": (mine[0]["status"] if mine else "not_due"),
            "result": mine[0] if mine else None}


def _next_month_ms(due_ms: int) -> int:
    """按月重复的下一期：按日历月推进，日期不存在就钳到当月最后一天。

    以前是固定 +30 天，1 月 31 日的月付会漂到 3 月 2 日、直接跳过 2 月；
    真实的月付是「每月同一天」，2 月没有 31 号就落在 28/29 号。
    时间用本地墙上时间再折回时间戳，和 _next_day_ms 同一套口径。
    """
    local = datetime.datetime.fromtimestamp(due_ms / 1000)
    year, month = local.year, local.month + 1
    if month > 12:
        year, month = year + 1, month - 12
    last_day = calendar.monthrange(year, month)[1]
    nxt = local.replace(year=year, month=month, day=min(local.day, last_day))
    return int(nxt.timestamp() * 1000)


def _next_day_ms(due_ms: int) -> int:
    """顺延到下一个自然日的同一时刻（本地时区）。

    用「明天的同一墙上时间」再折回时间戳，跨夏令时也不会漂一小时。
    """
    local = datetime.datetime.fromtimestamp(due_ms / 1000)
    tomorrow = local.date() + datetime.timedelta(days=1)
    return int(datetime.datetime.combine(tomorrow, local.time()).timestamp() * 1000)


def _defer(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    stamp: int,
    *,
    session_id: str,
    reason: str = "DAILY_RISK_THRESHOLD",
) -> dict[str, Any]:
    """当日额度不够：这笔顺延到下一个自然日，status 仍是 scheduled。

    预约不能因为「今天额度满了」就永久失败——用户会以为产品坏了。
    顺延有上限（MAX_DEFERRALS）：一直排不进去就明确失败并留痕。
    """
    sched_id = str(row["id"])
    amount_fen = int(row["amount_fen"])
    old_due = int(row["due_ms"])
    count = int(row["defer_count"] or 0) + 1
    if count > MAX_DEFERRALS:
        with _tx(conn):
            conn.execute(
                "UPDATE scheduled_transfers SET status = 'failed', executed_ms = ? WHERE id = ?",
                (stamp, sched_id),
            )
            audit.append(
                conn,
                "scheduled.deferred",
                {"scheduled_id": sched_id, "amount_fen": amount_fen,
                 "reason": reason, "original_due_ms": old_due,
                 "defer_count": count, "gave_up": True},
                session_id=session_id,
            )
        log.warning("scheduled.defer_gave_up id=%s count=%s", sched_id, count)
        return {"scheduled_id": sched_id, "status": "failed",
                "error": f"连续 {MAX_DEFERRALS} 天额度都不够，这笔预约没有转出去。"}
    new_due = _next_day_ms(old_due)
    with _tx(conn):
        conn.execute(
            "UPDATE scheduled_transfers SET due_ms = ?, defer_count = ? WHERE id = ?",
            (new_due, count, sched_id),
        )
        audit.append(
            conn,
            "scheduled.deferred",
            {"scheduled_id": sched_id, "amount_fen": amount_fen,
             "reason": reason, "original_due_ms": old_due,
             "due_ms": new_due, "defer_count": count},
            session_id=session_id,
        )
    log.info("scheduled.deferred id=%s due_ms=%s count=%s", sched_id, new_due, count)
    return {"scheduled_id": sched_id, "status": "scheduled", "deferred_to_ms": new_due,
            "defer_count": count, "amount_fen": amount_fen}


def _time_text(ms: int) -> str:
    stamp = datetime.datetime.fromtimestamp(ms / 1000, tz=datetime.UTC).astimezone()
    return stamp.strftime("%Y-%m-%d %H:%M")


def required_factors_for(
    conn: sqlite3.Connection, scheduled_id: str, *, now: int | None = None
) -> list[str]:
    """只读：这笔预约到点复判后需要哪些强验证因子；算不出来就给空表。

    为什么要有这个函数：预约被挂到 needs_verification 之后，页面上只显示
    「等待你补验证」是不够的——用户还得知道要补哪几个因子、能不能在页面上补。
    入口在 /api/scheduled/{id}/challenge，参数正是这里的因子名；把因子算一遍
    交给页面，用户才不用猜。这里不写库、不发令牌、不动钱。
    """
    try:
        row = conn.execute(
            "SELECT * FROM scheduled_transfers WHERE id = ?", (scheduled_id,)
        ).fetchone()
        if row is None:
            return []
        stamp = int(now if now is not None else now_ms())
        action = build_action(
            "scheduled_transfer",
            {
                "payee_id": str(row["payee_id"]),
                "amount_fen": int(row["amount_fen"]),
                "due_ms": int(row["due_ms"]),
            },
            str(row["account_id"]),
            int(row["amount_fen"]),
        )
        decision = _fresh_decision(conn, action, stamp)
        if getattr(decision, "level", "") != "red":
            return []
        return [str(item) for item in getattr(decision, "required_factors", [])]
    except Exception:  # noqa: BLE001 - 只读探针，算不出来就当没有
        log.warning("scheduled.required_factors_failed id=%s", scheduled_id)
        return []


def _tx(conn: sqlite3.Connection) -> Any:
    return db.tx(conn)
