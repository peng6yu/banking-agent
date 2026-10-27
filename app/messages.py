"""助手主动提醒（消息）基座：频控 → 落库 → 反馈。

为什么需要这一层：提醒最容易变成噪音。没有去重、没有封顶，同一条涨价就会
天天冒出来，用户两周内学会无视全部提醒——这比不提醒更糟。所以这里把关：

1. **时机层**（本模块）：每日封顶 3 条；同一类洞察 7 天内不重复；
   用户忽略 3 次或点「不再提醒」→ 这一类长期静默（message_insight_state）。
2. **事实层**（app/insights.py）：探测器只读账务表，确定性计算，不调模型、不猜。
   每条提醒都带 facts（数字的唯一出处），正文里的每个数字都必须来自 facts。
3. **语言层**：文案由 facts 拼出来，只描述事实与可选动作，不替用户下结论；
   「值得核实」不等于「欺诈认定」，「可以这样处理」不等于「必须这样」。

和 proactive 的分工：proactive 管「目标/规则/提醒」的持久化与定时推进；
这里管「助手今天想主动跟你说哪三件事」，可被采纳/忽略，也可整类静默。
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Final

from app import audit, db, ids
from app.clock import NotFound, ValidationError, now_ms
from app.insights import CATALOG, DAY_MS, day_start_ms, detect_insights

#: 每天最多主动说三条：再多就从「提醒」变成「骚扰」。
MAX_PER_DAY: Final[int] = 3
#: 同一类洞察 7 天内不重复：事实没变就不该天天弹。
QUIET_MS: Final[int] = 7 * 24 * 3600 * 1000
#: 连续忽略 3 次视为「这类我不想看」。
MUTE_AFTER_IGNORES: Final[int] = 3
#: 「不再提醒」写成很远的将来，而不是删掉状态：以后想恢复只改这一行。
MUTE_FOREVER_MS: Final[int] = 100 * 365 * DAY_MS

STATUS_LABELS: Final[dict[str, str]] = {
    "new": "待你看",
    "adopted": "已采纳",
    "dismissed": "已忽略",
}
FEEDBACK_ACTIONS: Final[tuple[str, ...]] = ("adopted", "dismissed", "muted")
FEEDBACK_LABELS: Final[dict[str, str]] = {
    "adopted": "我去处理",
    "dismissed": "这次不用",
    "muted": "这类别再提醒",
}


# ---------------------------------------------------------------- 用户与频控


def first_user_id(conn: sqlite3.Connection) -> str:
    """演示库是单用户；没有用户就没有财务主体，直接 400 而不是猜。"""
    row = conn.execute("SELECT id FROM users ORDER BY created_ms, id LIMIT 1").fetchone()
    if row is None:
        raise ValidationError("还没有用户数据，无法生成提醒", reason="NO_USERS")
    return str(row["id"])


def _today_count(conn: sqlite3.Connection, user_id: str, now: int) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM messages WHERE user_id = ? AND created_ms >= ?",
        (user_id, day_start_ms(now)),
    ).fetchone()
    return int(row["n"] if row else 0)


def _exists(conn: sqlite3.Connection, user_id: str, dedupe_key: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM messages WHERE user_id = ? AND dedupe_key = ? LIMIT 1",
        (user_id, dedupe_key),
    ).fetchone()
    return row is not None


def _insight_state(conn: sqlite3.Connection, user_id: str, insight_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM message_insight_state WHERE user_id = ? AND insight_id = ?",
        (user_id, insight_id),
    ).fetchone()


def _is_muted(conn: sqlite3.Connection, user_id: str, insight_id: str, now: int) -> bool:
    row = _insight_state(conn, user_id, insight_id)
    return bool(row and int(row["muted_ms"] or 0) > now)


def _is_quiet(conn: sqlite3.Connection, user_id: str, insight_id: str, now: int) -> bool:
    row = conn.execute(
        "SELECT MAX(created_ms) AS last FROM messages WHERE user_id = ? AND insight_id = ?",
        (user_id, insight_id),
    ).fetchone()
    return bool(row and row["last"] and int(row["last"]) > now - QUIET_MS)


def muted_insights(conn: sqlite3.Connection, user_id: str, now: int | None = None) -> list[str]:
    now = now if now is not None else now_ms()
    rows = conn.execute(
        "SELECT insight_id FROM message_insight_state WHERE user_id = ? AND muted_ms > ?",
        (user_id, now),
    ).fetchall()
    return [str(row["insight_id"]) for row in rows]


# ---------------------------------------------------------------- 落库与生成


def _insert_message(conn: sqlite3.Connection, user_id: str, item: dict[str, Any], now: int) -> str:
    message_id = ids.new_id("m")
    conn.execute(
        "INSERT INTO messages (id, user_id, insight_id, dedupe_key, severity, title, body,"
        " facts_json, actions_json, status, created_ms, expires_ms)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'new', ?, ?)",
        (
            message_id,
            user_id,
            item["insight_id"],
            item["dedupe_key"],
            item["severity"],
            item["title"],
            item["body"],
            json.dumps(item["facts"], ensure_ascii=False, sort_keys=True),
            json.dumps(item["actions"], ensure_ascii=False),
            now,
            now + 30 * DAY_MS,
        ),
    )
    audit.append(
        conn,
        "message.generated",
        {"message_id": message_id, "insight_id": item["insight_id"], "dedupe_key": item["dedupe_key"]},
    )
    return message_id


def generate(conn: sqlite3.Connection, user_id: str | None = None, now: int | None = None) -> dict[str, Any]:
    """跑一轮探测并按频控落库；重复调用幂等（去重键 + 每日封顶）。"""
    now = now if now is not None else now_ms()
    uid = user_id or first_user_id(conn)
    used = _today_count(conn, uid, now)
    created: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []
    for item in detect_insights(conn, uid, now):
        insight_id = item["insight_id"]
        # 顺序有意：先说「已经说过了」再提封顶。连点两次「现在想一想」时，
        # 若先判封顶，用户会收到「今天说够三条了」——可这三条正是刚才那三条，
        # 听起来像又生成了新消息，实际上一条也没多。去重/静默在前，封顶只管真正的新事。
        if _is_muted(conn, uid, insight_id, now):
            skipped.append({"insight_id": insight_id, "reason": "MUTED"})
        elif _is_quiet(conn, uid, insight_id, now):
            skipped.append({"insight_id": insight_id, "reason": "QUIET"})
        elif _exists(conn, uid, item["dedupe_key"]):
            skipped.append({"insight_id": insight_id, "reason": "DUPLICATE"})
        elif used >= MAX_PER_DAY:
            skipped.append({"insight_id": insight_id, "reason": "DAILY_CAP"})
        else:
            with db.tx(conn):
                message_id = _insert_message(conn, uid, item, now)
            used += 1
            created.append({"message_id": message_id, "insight_id": insight_id, "title": item["title"]})
    return {
        "created": created,
        "skipped": skipped,
        "used": used,
        "cap": MAX_PER_DAY,
    }


def review(conn: sqlite3.Connection, user_id: str | None = None, now: int | None = None) -> dict[str, Any]:
    """写完一笔账、新建一条计划或目标之后，当场复核一遍助手提醒。

    为什么需要它：账单那三条规则（拆单、凌晨境外、订阅涨价）是读时算的，加了流水
    打开页面就能看见；但「主动推给你」的提醒以前只有用户点「现在想一想」才生成，
    新账要等到那时才被看见。用户加完一笔就以为系统已经看过了，这是谎报。

    频控一道不少：每天最多三条、同类 7 天不重复、点过「这类别再提醒」的继续静默，
    所以每笔写操作都调一次也不会刷屏。返回的是给调用方的摘要，不是全部消息。
    """
    result = generate(conn, user_id, now)
    return {
        "created": len(result["created"]),
        "titles": [str(item["title"]) for item in result["created"]],
        "used": result["used"],
        "cap": result["cap"],
    }


# ---------------------------------------------------------------- 读与反馈


def list_messages(conn: sqlite3.Connection, user_id: str, *, limit: int = 20) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM messages WHERE user_id = ? ORDER BY created_ms DESC, id DESC LIMIT ?",
        (user_id, max(1, min(int(limit), 50))),
    ).fetchall()


def get_message(conn: sqlite3.Connection, user_id: str, message_id: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM messages WHERE id = ? AND user_id = ?", (message_id, user_id)
    ).fetchone()
    if row is None:
        raise NotFound("这条提醒不存在，可能已经被清理掉了。", reason="MESSAGE_NOT_FOUND")
    return row


def counts(conn: sqlite3.Connection, user_id: str) -> dict[str, int]:
    rows = conn.execute(
        "SELECT status, COUNT(*) AS n FROM messages WHERE user_id = ? GROUP BY status", (user_id,)
    ).fetchall()
    out = {"new": 0, "adopted": 0, "dismissed": 0, "total": 0}
    for row in rows:
        key = str(row["status"])
        out[key] = int(row["n"])
        out["total"] += int(row["n"])
    return out


def daily_usage(conn: sqlite3.Connection, user_id: str, now: int | None = None) -> dict[str, int]:
    now = now if now is not None else now_ms()
    return {"used": _today_count(conn, user_id, now), "cap": MAX_PER_DAY}


def _bump_state(conn: sqlite3.Connection, user_id: str, insight_id: str, now: int, *, mute: bool) -> None:
    row = _insight_state(conn, user_id, insight_id)
    ignores = int(row["ignore_count"] or 0) if row else 0
    if not mute:
        ignores += 1
        mute = ignores >= MUTE_AFTER_IGNORES
    muted_ms = now + MUTE_FOREVER_MS if mute else int(row["muted_ms"] or 0) if row else 0
    if row is None:
        conn.execute(
            "INSERT INTO message_insight_state (user_id, insight_id, ignore_count, muted_ms, last_ms)"
            " VALUES (?, ?, ?, ?, ?)",
            (user_id, insight_id, ignores, muted_ms, now),
        )
    else:
        conn.execute(
            "UPDATE message_insight_state SET ignore_count = ?, muted_ms = ?, last_ms = ?"
            " WHERE user_id = ? AND insight_id = ?",
            (ignores, muted_ms, now, user_id, insight_id),
        )


def feedback(
    conn: sqlite3.Connection, user_id: str, message_id: str, action: str, note: str = "", now: int | None = None
) -> sqlite3.Row:
    """采纳 / 忽略 / 不再提醒。同一动作重复提交幂等，不重复记反馈。"""
    if action not in FEEDBACK_ACTIONS:
        raise ValidationError("不认识的反馈动作", allowed=list(FEEDBACK_ACTIONS))
    now = now if now is not None else now_ms()
    row = get_message(conn, user_id, message_id)
    status = "dismissed" if action in ("dismissed", "muted") else "adopted"
    if str(row["status"]) == status and action != "muted":
        return row
    with db.tx(conn):
        conn.execute("UPDATE messages SET status = ? WHERE id = ?", (status, message_id))
        if action in ("dismissed", "muted"):
            _bump_state(conn, user_id, str(row["insight_id"]), now, mute=action == "muted")
        # 反馈事实只留一份：审计里这条就是权威记录（动作、备注、时间都在），
        # 不再另建一张没人读的 message_feedback 表。
        audit.append(
            conn,
            "message.feedback",
            {
                "message_id": message_id,
                "insight_id": str(row["insight_id"]),
                "action": action,
                "note": str(note or "")[:300],
            },
        )
    return get_message(conn, user_id, message_id)


__all__ = [
    "MAX_PER_DAY",
    "QUIET_MS",
    "MUTE_AFTER_IGNORES",
    "CATALOG",
    "STATUS_LABELS",
    "FEEDBACK_LABELS",
    "first_user_id",
    "detect_insights",
    "generate",
    "review",
    "list_messages",
    "get_message",
    "counts",
    "daily_usage",
    "muted_insights",
    "feedback",
]
