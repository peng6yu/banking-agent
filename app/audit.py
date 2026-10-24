"""审计哈希链：只追加、不可改、可验证。

对应文档：docs/03_数据库与种子数据.md 3.2 第 7 张表、docs/04_核心模块实现规格.md 4.7。

哈希材料必须逐字节一致（否则演示时校验会挂）：
    material = prev_hash + '|' + str(ts_ms) + '|' + event_type + '|' + payload_json
    hash     = sha256(material.encode('utf-8')).hexdigest()
payload_json 固定 json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(',',':'))。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any, Final

from app.clock import ValidationError, now_ms
from app.log import log

GENESIS: Final[str] = "GENESIS"

# docs/03 3.2：event_type 是闭集，写错名字说明调用方有 bug，当场拦
EVENT_TYPES: Final[frozenset[str]] = frozenset(
    {
        "session.start",
        "intent.parsed",
        "dag.built",
        # 同一会话里已有没走完的写轮次：这一轮被挡住也必须留痕，
        # 否则用户问「为什么我这句话没执行」时，库里只有一句 intent.parsed。
        "run.conflict",
        # 提问那一轮被用户的回答取代：它没授权、没动钱，但「消失」也必须留痕，
        # 否则事后只能看到一轮 cancelled 的 run，不知道是用户取消还是被回答取代。
        "run.superseded",
        "policy.decided",
        "confirm.requested",
        "confirm.approved",
        "confirm.rejected",
        # 超时和「用户说不」必须分开：以前超时也记成 confirm.rejected，
        # 事后查审计分不清是用户拒绝还是没人点。
        "confirm.expired",
        # 策略拦截既不是「用户拒绝」也不是「超时」：卡被规则作废了，用户没有机会说不。
        # 单独一类，复盘时能直接数出「被策略挡了多少次」。
        "policy.blocked",
        # 多因子：挑战签发、校验通过、校验失败三条都要留痕
        "factor.challenge_issued",
        "factor.verified",
        "factor.rejected",
        "token.issued",
        "token.rejected",
        "transfer.initiated",
        "transfer.committed",
        "transfer.released",
        "transfer.reversed",
        "scheduled.created",
        "scheduled.cancelled",
        "scheduled.executed",
        "scheduled.deferred",
        "tool.executed",
        # 黄档操作在提交时发现日累计已被别的会话推过红线：重新裁决并升级为红
        "policy.upgraded",
        "injection.detected",
        "breaker.open",
        # 阶段 3 知识层：本轮检索了哪些知识条目/来源；知识降级或冲突时记 warning
        "knowledge.retrieved",
        "knowledge.warning",
        # 阶段 5 主动财务管理：目标、监测运行、提醒的生命周期
        "savings_goal.created",
        "savings_goal.updated",
        "proactive.monitor_run",
        "proactive.alert_created",
        "proactive.alert_resolved",
        # 到点执行真的失败了（钱没转出去）：必须留痕，否则库里只剩一个 failed 状态，
        # 查不出是哪一步炸的，用户问"为什么没转"时答不上来。
        "scheduled.failed",
    # 创建期验过的因子过了保质期：这笔不再享受越额度放行，回到普通口径排队
    "scheduled.auth_expired",
    # 到点复判为红且缺少有效强验证：挂起等用户补验证（持久列 needs_verification）。
    # 这三类事件是「预约不能绕过日累计」这条边界的审计证据，缺了就只能看状态列。
    "scheduled.needs_verification",
    "scheduled.verified",
    "scheduled.verification_failed",
    "scheduled.verification_waived",
        # 生日计划：预算占用、单项付款成功/失败/被拒都要留痕，
        # 否则库里只剩一个 plan_budgets.status，查不出「为什么这束花没买成」。
        "plan.budget_reserved",
        "plan.item_paid",
        "plan.item_failed",
        "plan.item_declined",
        # 模拟支出注入：竞赛演示用的假流水，语义上与真实扣款无关，必须单独一类，
        # 否则审计链里分不清「演示注入」和「工具真的动了钱」。
        "proactive.simulated_expense",
        # 适当性问卷：画像建立/更新时留痕（买基金前必须先有画像）
        "suitability.assessed",
        # 申购前的适配性闸门：没测过 / 等级不够 / 通过，三种结果都要能查
        "suitability.checked",
        # 订阅：从流水推断出的潜在订阅（不是已确认事实，audit 里要能区分）
        "subscription.inferred",
        # 跨场景计划：计划与步骤的生命周期
        "plan.created",
        "plan.updated",
        "plan.step_done",
        "plan.step_failed",
        # 人工接管（handoff）：建单、领取、拒绝、补充信息、取消、恢复成功/失败。
        # 「恢复失败」必须单独一类：它是「转人工之后仍然没救回来」的证据，
        # 和操作员拒绝、用户取消不是一回事，混在一起复盘时数不出来。
        "handoff.created",
        "handoff.claimed",
        "handoff.rejected",
        "handoff.need_info",
        "handoff.cancelled",
        "handoff.resumed",
        "handoff.resume_failed",
        "session.end",
    }
)


def _material(prev_hash: str, ts_ms: int, event_type: str, payload_json: str) -> str:
    """拼哈希材料，字段顺序固定不要动。"""
    return prev_hash + "|" + str(ts_ms) + "|" + event_type + "|" + payload_json


def _digest(material: str) -> str:
    """材料的 sha256 十六进制摘要。"""
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def payload_to_json(payload: dict[str, Any]) -> str:
    """审计内容的唯一序列化方式；不可序列化就报错，绝不用 default=str 蒙混。"""
    try:
        return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise ValidationError("审计内容无法序列化", error=str(exc)) from exc


def append(
    conn: sqlite3.Connection,
    event_type: str,
    payload: dict[str, Any],
    *,
    session_id: str | None = None,
    task_id: str | None = None,
) -> int:
    """追加一条审计记录，返回自增 seq。"""
    if event_type not in EVENT_TYPES:
        raise ValidationError("未知的审计事件类型", event_type=event_type)
    payload_json = payload_to_json(payload)
    prev_hash = chain_head(conn)
    ts_ms = now_ms()
    hash_value = _digest(_material(prev_hash, ts_ms, event_type, payload_json))
    cursor = conn.execute(
        "INSERT INTO audit_log (ts_ms, session_id, task_id, event_type, payload_json, prev_hash, hash)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (ts_ms, session_id, task_id, event_type, payload_json, prev_hash, hash_value),
    )
    return int(cursor.lastrowid)


def verify_chain(conn: sqlite3.Connection) -> bool:
    """逐行重算整条链；任一处对不上就 log 出断点 seq 并返回 False。"""
    prev_hash = GENESIS
    rows = conn.execute(
        "SELECT seq, ts_ms, event_type, payload_json, prev_hash, hash FROM audit_log ORDER BY seq"
    ).fetchall()
    for row in rows:
        if row["prev_hash"] != prev_hash:
            log.error(
                "audit.chain_break",
                seq=row["seq"],
                reason="PREV_HASH_MISMATCH",
                expect=prev_hash,
                actual=row["prev_hash"],
            )
            return False
        expect = _digest(_material(prev_hash, row["ts_ms"], row["event_type"], row["payload_json"]))
        if expect != row["hash"]:
            log.error("audit.chain_break", seq=row["seq"], reason="HASH_MISMATCH", expect=expect)
            return False
        prev_hash = row["hash"]
    return True


def chain_head(conn: sqlite3.Connection) -> str:
    """最新一条的 hash，空表返回 GENESIS。"""
    row = conn.execute("SELECT hash FROM audit_log ORDER BY seq DESC LIMIT 1").fetchone()
    if row is None:
        return GENESIS
    return str(row["hash"])


def count_events(conn: sqlite3.Connection) -> int:
    """审计条数，给演示与自检用。"""
    row = conn.execute("SELECT COUNT(*) AS n FROM audit_log").fetchone()
    return int(row["n"])


__all__ = [
    "GENESIS",
    "EVENT_TYPES",
    "append",
    "verify_chain",
    "chain_head",
    "payload_to_json",
    "count_events",
]