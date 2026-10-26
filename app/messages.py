"""助手主动提醒（消息）基座：探测 → 频控 → 落库 → 反馈。

为什么单独做一层：提醒最容易变成噪音。没有出处、没有去重、没有封顶，
同一条涨价就会天天冒出来，用户两周内学会无视全部提醒——这比不提醒更糟。
所以这里把关三层：

1. **事实层**（探测器）：只读账务表，确定性计算，不调模型、不猜。
   每条提醒都带 facts（数字的唯一出处），正文里的每个数字都必须来自 facts。
2. **时机层**（频控）：每日封顶 3 条；同一类洞察 7 天内不重复；
   用户忽略 3 次或点「不再提醒」→ 这一类长期静默（message_insight_state）。
3. **语言层**：文案由 facts 拼出来，只描述事实与可选动作，不替用户下结论；
   「值得核实」不等于「欺诈认定」，「可以这样处理」不等于「必须这样」。

和 proactive 的分工：proactive 管「目标/规则/提醒」的持久化与定时推进；
这里管「助手今天想主动跟你说哪三件事」，可被采纳/忽略，也可整类静默。
"""

from __future__ import annotations

import json
import sqlite3
import time
from typing import Any, Final

from app import audit, db, ids
from app.clock import NotFound, ValidationError, now_ms
from app.money import INTERNAL_CATEGORIES, fen_to_yuan_str

#: 每天最多主动说三条：再多就从「提醒」变成「骚扰」。
MAX_PER_DAY: Final[int] = 3
#: 同一类洞察 7 天内不重复：事实没变就不该天天弹。
QUIET_MS: Final[int] = 7 * 24 * 3600 * 1000
#: 连续忽略 3 次视为「这类我不想看」。
MUTE_AFTER_IGNORES: Final[int] = 3
#: 「不再提醒」写成很远的将来，而不是删掉状态：以后想恢复只改这一行。
MUTE_FOREVER_MS: Final[int] = 100 * 365 * 24 * 3600 * 1000
DAY_MS: Final[int] = 24 * 3600 * 1000

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

#: 洞察目录：用户问「你凭什么想到这个」时，页面能直接摊开数据来源。
CATALOG: Final[tuple[dict[str, str], ...]] = (
    {
        "id": "I1_subscription_hike",
        "title": "订阅扣费涨价",
        "why": "同一个商户本月的订阅扣款比上月高 30% 以上",
        "source": "transactions（分类=订阅）",
        "severity": "warning",
    },
    {
        "id": "I2_spending_pace",
        "title": "花得比上月快",
        "why": "本月至今的支出（不含转账/理财）比上月同期高 20% 以上",
        "source": "transactions（排除账户内部搬运）",
        "severity": "info",
    },
    {
        "id": "I3_night_large",
        "title": "凌晨的大额消费",
        "why": "本月 0-5 点出现单笔 500 元以上的支出",
        "source": "transactions（按本地时间取 0-5 点）",
        "severity": "warning",
    },
)


# ---------------------------------------------------------------- 时间小工具


def _local_time(ms: int) -> time.struct_time:
    return time.localtime(ms / 1000)


def _date_ms(year: int, month: int, day: int) -> int:
    return int(time.mktime((year, month, day, 0, 0, 0, 0, 0, -1)) * 1000)


def _period_start_ms(ms: int) -> int:
    lt = _local_time(ms)
    return _date_ms(lt.tm_year, lt.tm_mon, 1)


def _prev_month_start(ms: int) -> int:
    lt = _local_time(ms)
    year, month = (lt.tm_year, lt.tm_mon - 1) if lt.tm_mon > 1 else (lt.tm_year - 1, 12)
    return _date_ms(year, month, 1)


def _month_tag(ms: int) -> str:
    lt = _local_time(ms)
    return f"{lt.tm_year}-{lt.tm_mon:02d}"


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
        (user_id, _date_ms(*_local_time(now)[:3])),
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


# ---------------------------------------------------------------- 洞察探测


def _hike_candidate(conn: sqlite3.Connection, user_id: str, now: int) -> dict[str, Any] | None:
    """订阅类扣款：本月已扣 vs 上月同商户，涨 30% 以上才说。"""
    cur_start = _period_start_ms(now)
    prev_start = _prev_month_start(now)
    rows = conn.execute(
        "SELECT t.counterparty AS merchant,"
        " SUM(CASE WHEN t.ts_ms >= ? THEN t.amount_fen ELSE 0 END) AS cur_fen,"
        " SUM(CASE WHEN t.ts_ms < ? THEN t.amount_fen ELSE 0 END) AS prev_fen"
        " FROM transactions t JOIN accounts a ON a.id = t.account_id"
        " WHERE a.user_id = ? AND t.direction = 'out' AND t.category = '订阅'"
        " AND t.ts_ms >= ? AND t.ts_ms < ?"
        " GROUP BY t.counterparty",
        (cur_start, cur_start, user_id, prev_start, now),
    ).fetchall()
    best: dict[str, Any] | None = None
    for row in rows:
        cur, prev = int(row["cur_fen"] or 0), int(row["prev_fen"] or 0)
        if cur <= 0 or prev <= 0 or cur * 10 < prev * 13:
            continue
        if best is None or cur - prev > best["_delta"]:
            best = {"_delta": cur - prev, "merchant": str(row["merchant"]), "cur": cur, "prev": prev}
    if best is None:
        return None
    pct = int(round((best["cur"] - best["prev"]) * 100 / best["prev"]))
    merchant = best["merchant"]
    facts = {
        "merchant": merchant,
        "previous_yuan": fen_to_yuan_str(best["prev"]),
        "current_yuan": fen_to_yuan_str(best["cur"]),
        "hike_pct": str(pct),
    }
    return {
        "insight_id": "I1_subscription_hike",
        "dedupe_key": f"I1:{merchant}:{_month_tag(now)}",
        "severity": "warning",
        "title": f"{merchant}这个月的扣费涨了 {facts['hike_pct']}%",
        "body": (
            f"{merchant}的订阅扣费从 {facts['previous_yuan']} 元涨到 {facts['current_yuan']} 元"
            f"（+{facts['hike_pct']}%）。如果这不是你要的套餐，可以去业务管理把银行侧代扣停掉；"
            "停的是银行代扣，会员本身还要去商户那边退订。"
        ),
        "facts": facts,
        "actions": [
            {"key": "ask", "kind": "ask", "label": "让助手帮我停掉",
             "prompt": f"帮我把{merchant}的银行侧代扣停掉"},
            {"key": "goto", "kind": "goto", "label": "去看看订阅", "view": "manage"},
        ],
    }


def _spending_candidate(conn: sqlite3.Connection, user_id: str, now: int) -> dict[str, Any] | None:
    """支出节奏：本月至今 vs 上月同一天数窗口，排除转账/理财。"""
    cur_start = _period_start_ms(now)
    prev_start = _prev_month_start(now)
    prev_end = min(prev_start + (now - cur_start), cur_start)
    exclude = ",".join("?" * len(INTERNAL_CATEGORIES))
    sql = (
        "SELECT COALESCE(SUM(t.amount_fen), 0) AS fen"
        " FROM transactions t JOIN accounts a ON a.id = t.account_id"
        " WHERE a.user_id = ? AND t.direction = 'out' AND t.category NOT IN (" + exclude + ")"
        " AND t.ts_ms >= ? AND t.ts_ms < ?"
    )
    base = (user_id, *INTERNAL_CATEGORIES)
    cur = int(conn.execute(sql, (*base, cur_start, now)).fetchone()["fen"])
    prev = int(conn.execute(sql, (*base, prev_start, prev_end)).fetchone()["fen"])
    if prev <= 0 or cur * 10 < prev * 12:
        return None
    pct = int(round((cur - prev) * 100 / prev))
    facts = {
        "spent_yuan": fen_to_yuan_str(cur),
        "previous_yuan": fen_to_yuan_str(prev),
        "hike_pct": str(pct),
    }
    return {
        "insight_id": "I2_spending_pace",
        "dedupe_key": f"I2:{_month_tag(now)}",
        "severity": "info",
        "title": f"这个月比上月同期多花了 {facts['hike_pct']}%",
        "body": (
            f"本月到目前为止花了 {facts['spent_yuan']} 元，上月同期是 {facts['previous_yuan']} 元"
            f"（+{facts['hike_pct']}%，都不含转账和理财）。想管住的话，可以先去账单分析看看"
            "是哪一类涨得最多。"
        ),
        "facts": facts,
        "actions": [
            {"key": "goto", "kind": "goto", "label": "看支出分类", "view": "bill"},
            {"key": "ask", "kind": "ask", "label": "让助手帮我定预算",
             "prompt": "帮我看下这个月哪类支出涨得最多，能不能定个预算"},
        ],
    }


def _night_candidate(conn: sqlite3.Connection, user_id: str, now: int) -> dict[str, Any] | None:
    """凌晨大额：0-5 点、单笔 500 元以上。只提醒核实，不判定盗刷。"""
    cur_start = _period_start_ms(now)
    rows = conn.execute(
        "SELECT t.id, t.counterparty, t.amount_fen, t.ts_ms FROM transactions t"
        " JOIN accounts a ON a.id = t.account_id"
        " WHERE a.user_id = ? AND t.direction = 'out' AND t.ts_ms >= ? AND t.ts_ms < ?"
        " AND t.amount_fen >= 50000"
        " AND CAST(strftime('%H', t.ts_ms / 1000, 'unixepoch', 'localtime') AS INTEGER) BETWEEN 0 AND 5",
        (user_id, cur_start, now),
    ).fetchall()
    if not rows:
        return None
    total = sum(int(row["amount_fen"]) for row in rows)
    largest = max(int(row["amount_fen"]) for row in rows)
    facts = {
        "count": str(len(rows)),
        "total_yuan": fen_to_yuan_str(total),
        "largest_yuan": fen_to_yuan_str(largest),
        # 门槛也是「依据」的一部分：正文里出现的每个数字都要能在 facts 里查到，
        # 否则语言层白名单形同虚设（用户没法复核 500 是从哪来的）。
        "min_yuan": fen_to_yuan_str(50000),
    }
    return {
        "insight_id": "I3_night_large",
        "dedupe_key": f"I3:{_month_tag(now)}",
        "severity": "warning",
        "title": f"凌晨有 {facts['count']} 笔大额消费，先确认是不是你本人",
        "body": (
            f"本月凌晨时段出现了 {facts['count']} 笔单笔超过 {facts['min_yuan']} 元的支出，合计 {facts['total_yuan']} 元，"
            f"最大一笔 {facts['largest_yuan']} 元。这不是欺诈认定，只是一条值得核实的线索："
            "如果不是你本人操作，先去业务管理把卡临时冻结，再逐笔确认。"
        ),
        "facts": facts,
        "actions": [
            {"key": "goto", "kind": "goto", "label": "去账单里核对", "view": "bill"},
            {"key": "ask", "kind": "ask", "label": "让助手帮我冻结卡",
             "prompt": "帮我先把这张卡临时冻结，我要核对几笔凌晨的消费"},
        ],
    }


def detect_insights(conn: sqlite3.Connection, user_id: str, now: int) -> list[dict[str, Any]]:
    """按重要度跑三个探测器；任何一个探不到就跳过，不编。"""
    found: list[dict[str, Any]] = []
    for probe in (_night_candidate, _hike_candidate, _spending_candidate):
        item = probe(conn, user_id, now)
        if item is not None:
            found.append(item)
    return found


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
        "generated_ms": now,
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
        conn.execute(
            "INSERT INTO message_feedback (message_id, user_id, action, note, created_ms)"
            " VALUES (?, ?, ?, ?, ?)",
            (message_id, user_id, action, str(note or "")[:300], now),
        )
        if action in ("dismissed", "muted"):
            _bump_state(conn, user_id, str(row["insight_id"]), now, mute=action == "muted")
        audit.append(
            conn,
            "message.feedback",
            {"message_id": message_id, "insight_id": str(row["insight_id"]), "action": action},
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
    "detect",
    "generate",
    "list_messages",
    "get_message",
    "counts",
    "daily_usage",
    "muted_insights",
    "feedback",
]
