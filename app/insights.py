"""助手主动提醒的「事实层」：洞察目录 + 确定性探测器。

为什么单独一个模块：
1. 探测器只读账务表（transactions/accounts/payees/subs/scheduled_transfers/
   savings_goals/monitor_rules），纯 SQL + 本地时间计算，不调模型、不猜；
2. 每条洞察都产出 facts（数字的唯一出处）与 actions（下一步能点什么），
   正文里的每个数字都必须来自 facts —— 语言层白名单靠这一点成立；
3. 「探测」与「频控/落库/反馈」分开：app/messages.py 管每天说几条、同一类
   隔多久、忽略几次静默；这里只管「账本里现在有什么值得说」。

新增一条洞察要同时改三处，缺一处就会被测试拦下：
- CATALOG 加一行（用户问「你凭什么想到这个」时要能摊开数据来源）；
- 写一个 _xxx_candidate 函数并登记进 _PROBES；
- 在 tests/test_messages.py 里配「该报」和「不该报」两个用例。

排序有意按「先安全、后省钱、再习惯」：每日封顶 3 条，排在前面的先被说出。
"""

from __future__ import annotations

import sqlite3
import time
from typing import Any, Callable, Final

from app.money import INTERNAL_CATEGORIES, fen_to_yuan_str

DAY_MS: Final[int] = 24 * 3600 * 1000

#: 收款人重名不设门槛：两个同名不同卡号，转错一次就够麻烦。
#: 以下是各探测器的门槛，集中在这里，方便「为什么是 30%」这类追问时一眼看到。
HIKE_PCT: Final[int] = 30
PACE_PCT: Final[int] = 20
NIGHT_MIN_FEN: Final[int] = 50_000
PAYDAY_MIN_FEN: Final[int] = 500_000
CASH_GAP_DAYS: Final[int] = 7
GOAL_BEHIND_PCT: Final[int] = 10
BUDGET_USED_PCT: Final[int] = 80
BUDGET_EARLY_PCT: Final[int] = 60

#: 洞察目录：id 与探测器一一对应；severity 只有 info/warning 两档（messages 表有 CHECK）。
CATALOG: Final[tuple[dict[str, str], ...]] = (
    {"id": "I3_night_large", "title": "凌晨的大额消费",
     "why": "本月 0-5 点出现单笔 500 元以上的支出",
     "source": "transactions（按本地时间取 0-5 点）", "severity": "warning"},
    {"id": "I7_payee_duplicate", "title": "重名收款人",
     "why": "收款人列表里有同名、但卡号尾号不同的记录",
     "source": "payees（按姓名分组看卡号尾号）", "severity": "warning"},
    {"id": "I5_cash_gap", "title": "未来几天活期可能不够",
     "why": "未来 7 天内到期的预约转账 + 订阅代扣，合计超过活期余额",
     "source": "scheduled_transfers + subs + accounts", "severity": "warning"},
    {"id": "I1_subscription_hike", "title": "订阅扣费涨价",
     "why": "同一个商户本月的订阅扣款比上月高 30% 以上",
     "source": "transactions（分类=订阅）", "severity": "warning"},
    {"id": "I8_budget_pace", "title": "预算用得太快",
     "why": "本期预算已用掉 80% 以上，而时间才过 60% 以内",
     "source": "monitor_rules（budget）+ transactions", "severity": "info"},
    {"id": "I6_goal_behind", "title": "储蓄目标落后",
     "why": "储蓄进度比按日期算的应达进度低 10 个百分点以上",
     "source": "savings_goals（目标金额、截止日、已存）", "severity": "info"},
    {"id": "I4_payday_saving", "title": "发薪先存一笔",
     "why": "本月有一笔 5000 元以上的收入到账",
     "source": "transactions（direction=in，排除转账/理财）", "severity": "info"},
    {"id": "I2_spending_pace", "title": "花得比上月快",
     "why": "本月至今的支出（不含转账/理财）比上月同期高 20% 以上",
     "source": "transactions（排除账户内部搬运）", "severity": "info"},
)


# ---------------------------------------------------------------- 时间小工具


def _local_time(ms: int) -> time.struct_time:
    return time.localtime(ms / 1000)


def _date_ms(year: int, month: int, day: int) -> int:
    # mktime 会把越界的 day/month 归一化：算上周一、下月 1 日都靠这个性质。
    return int(time.mktime((year, month, day, 0, 0, 0, 0, 0, -1)) * 1000)


def day_start_ms(ms: int) -> int:
    lt = _local_time(ms)
    return _date_ms(lt.tm_year, lt.tm_mon, lt.tm_mday)


def _period_start_ms(ms: int) -> int:
    lt = _local_time(ms)
    return _date_ms(lt.tm_year, lt.tm_mon, 1)


def _next_month_start(ms: int) -> int:
    lt = _local_time(ms)
    return _date_ms(lt.tm_year, lt.tm_mon + 1, 1)


def _prev_month_start(ms: int) -> int:
    lt = _local_time(ms)
    return _date_ms(lt.tm_year, lt.tm_mon - 1, 1)


def _week_start_ms(ms: int) -> int:
    lt = _local_time(ms)
    return _date_ms(lt.tm_year, lt.tm_mon, lt.tm_mday - lt.tm_wday)


def _month_tag(ms: int) -> str:
    lt = _local_time(ms)
    return f"{lt.tm_year}-{lt.tm_mon:02d}"


def _date_text_ms(text: str) -> int | None:
    """把 YYYY-MM-DD 解析成当天 0 点；坏日期（历史脏数据）当作探不到，不抛。"""
    try:
        parsed = time.strptime(str(text)[:10], "%Y-%m-%d")
    except (TypeError, ValueError):
        return None
    return int(time.mktime((parsed.tm_year, parsed.tm_mon, parsed.tm_mday, 0, 0, 0, 0, 0, -1)) * 1000)


def _ask(label: str, prompt: str) -> dict[str, str]:
    return {"key": "ask", "kind": "ask", "label": label, "prompt": prompt}


def _goto(label: str, view: str) -> dict[str, str]:
    return {"key": "goto", "kind": "goto", "label": label, "view": view}


# ---------------------------------------------------------------- 安全类


def _night_candidate(conn: sqlite3.Connection, user_id: str, now: int) -> dict[str, Any] | None:
    """凌晨大额：0-5 点、单笔 500 元以上。只提醒核实，不判定盗刷。"""
    rows = conn.execute(
        "SELECT t.id, t.counterparty, t.amount_fen, t.ts_ms FROM transactions t"
        " JOIN accounts a ON a.id = t.account_id"
        " WHERE a.user_id = ? AND t.direction = 'out' AND t.ts_ms >= ? AND t.ts_ms < ?"
        " AND t.amount_fen >= ?"
        " AND CAST(strftime('%H', t.ts_ms / 1000, 'unixepoch', 'localtime') AS INTEGER) BETWEEN 0 AND 5",
        (user_id, _period_start_ms(now), now, NIGHT_MIN_FEN),
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
        "min_yuan": fen_to_yuan_str(NIGHT_MIN_FEN),
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
            _goto("去账单里核对", "bill"),
            _ask("让助手帮我冻结卡", "帮我先把这张卡临时冻结，我要核对几笔凌晨的消费"),
        ],
    }


def _payee_candidate(conn: sqlite3.Connection, user_id: str, now: int) -> dict[str, Any] | None:
    """同名收款人：姓名相同但卡号尾号不同。转错人比多花一笔更难追。"""
    rows = conn.execute(
        "SELECT name, COUNT(DISTINCT card_tail) AS n FROM payees"
        " WHERE user_id = ? GROUP BY name HAVING COUNT(DISTINCT card_tail) > 1"
        " ORDER BY n DESC, name LIMIT 1",
        (user_id,),
    ).fetchall()
    if not rows:
        return None
    name = str(rows[0]["name"])
    count = int(rows[0]["n"])
    tails = [
        str(row["card_tail"])
        for row in conn.execute(
            "SELECT DISTINCT card_tail FROM payees WHERE user_id = ? AND name = ?"
            " ORDER BY card_tail LIMIT 2",
            (user_id, name),
        )
    ]
    if len(tails) < 2:
        return None
    facts = {"name": name, "count": str(count), "tail_a": tails[0], "tail_b": tails[1]}
    return {
        "insight_id": "I7_payee_duplicate",
        "dedupe_key": f"I7:{name}:{tails[0]}:{tails[1]}",
        "severity": "warning",
        "title": f"「{name}」不止一个，转账前先看清尾号",
        "body": (
            f"收款人里有 {facts['count']} 个「{name}」，卡号尾号不一样（比如 {facts['tail_a']} 和 {facts['tail_b']}）。"
            "转错人追回来很麻烦：下次转账时先看尾号，或者给每个收款人加一个能区分的备注。"
        ),
        "facts": facts,
        "actions": [
            _ask("让助手帮我区分开", f"帮我把两个叫{name}的收款人加上备注，别转错"),
            _goto("去 AI 助手说清楚", "assistant"),
        ],
    }


def _cash_gap_candidate(conn: sqlite3.Connection, user_id: str, now: int) -> dict[str, Any] | None:
    """未来 7 天要出去的钱 vs 活期余额：不是预测，是已排期的必然支出。"""
    horizon = now + CASH_GAP_DAYS * DAY_MS
    due_fen = int(conn.execute(
        "SELECT COALESCE(SUM(amount_fen), 0) AS fen FROM scheduled_transfers"
        " WHERE user_id = ? AND status = 'scheduled' AND due_ms <= ?",
        (user_id, horizon),
    ).fetchone()["fen"])
    subs_fen = int(conn.execute(
        "SELECT COALESCE(SUM(s.amount_fen), 0) AS fen FROM subs s"
        " JOIN accounts a ON a.id = s.account_id"
        " WHERE a.user_id = ? AND s.status = 'active' AND s.next_charge_ms <= ?",
        (user_id, horizon),
    ).fetchone()["fen"])
    total = due_fen + subs_fen
    if total <= 0:
        return None
    balance_fen = int(conn.execute(
        "SELECT COALESCE(SUM(balance_fen), 0) AS fen FROM accounts"
        " WHERE user_id = ? AND type = 'debit'",
        (user_id,),
    ).fetchone()["fen"])
    if total <= balance_fen:
        return None
    facts = {
        "days": str(CASH_GAP_DAYS),
        "due_yuan": fen_to_yuan_str(due_fen),
        "subs_yuan": fen_to_yuan_str(subs_fen),
        "total_yuan": fen_to_yuan_str(total),
        "balance_yuan": fen_to_yuan_str(balance_fen),
        "short_yuan": fen_to_yuan_str(total - balance_fen),
    }
    parts: list[str] = []
    if due_fen:
        parts.append(f"预约转账 {facts['due_yuan']} 元")
    if subs_fen:
        parts.append(f"订阅代扣 {facts['subs_yuan']} 元")
    return {
        "insight_id": "I5_cash_gap",
        "dedupe_key": f"I5:{_month_tag(now)}",
        "severity": "warning",
        "title": f"接下来 {facts['days']} 天要扣的钱，活期可能不够",
        "body": (
            f"未来 {facts['days']} 天有 {facts['total_yuan']} 元要出去（{'、'.join(parts)}），"
            f"活期余额是 {facts['balance_yuan']} 元，还差 {facts['short_yuan']} 元。"
            "可以先把钱挪进来，或者把预约转账改到工资到账之后。这不是催款，只是提前打个招呼。"
        ),
        "facts": facts,
        "actions": [
            _goto("去计划里看一眼", "plans"),
            _ask("让助手帮我重排预约", "帮我把这个月的预约转账重新排一下，别让余额不够"),
        ],
    }


# ---------------------------------------------------------------- 花钱类


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
        if cur <= 0 or prev <= 0 or cur * 100 < prev * (100 + HIKE_PCT):
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
            _ask("让助手帮我停掉", f"帮我把{merchant}的银行侧代扣停掉"),
            _goto("去看看订阅", "manage"),
        ],
    }


def _spend_since(conn: sqlite3.Connection, user_id: str, start: int, end: int) -> int:
    exclude = ",".join("?" * len(INTERNAL_CATEGORIES))
    sql = (
        "SELECT COALESCE(SUM(t.amount_fen), 0) AS fen"
        " FROM transactions t JOIN accounts a ON a.id = t.account_id"
        " WHERE a.user_id = ? AND t.direction = 'out' AND t.category NOT IN (" + exclude + ")"
        " AND t.ts_ms >= ? AND t.ts_ms < ?"
    )
    return int(conn.execute(sql, (user_id, *INTERNAL_CATEGORIES, start, end)).fetchone()["fen"])


def _spending_candidate(conn: sqlite3.Connection, user_id: str, now: int) -> dict[str, Any] | None:
    """支出节奏：本月至今 vs 上月同一天数窗口，排除转账/理财。"""
    cur_start = _period_start_ms(now)
    prev_start = _prev_month_start(now)
    prev_end = min(prev_start + (now - cur_start), cur_start)
    cur = _spend_since(conn, user_id, cur_start, now)
    prev = _spend_since(conn, user_id, prev_start, prev_end)
    if prev <= 0 or cur * 100 < prev * (100 + PACE_PCT):
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
            _goto("看支出分类", "bill"),
            _ask("让助手帮我定预算", "帮我看下这个月哪类支出涨得最多，能不能定个预算"),
        ],
    }


def _budget_candidate(conn: sqlite3.Connection, user_id: str, now: int) -> dict[str, Any] | None:
    """预算节奏：已用 80% 而时间还没过 60%，比「已经超了」更早一步。"""
    rows = conn.execute(
        "SELECT r.id, r.threshold, r.period, g.name FROM monitor_rules r"
        " JOIN savings_goals g ON g.id = r.goal_id"
        " WHERE g.user_id = ? AND r.enabled = 1 AND r.kind = 'budget'",
        (user_id,),
    ).fetchall()
    best: dict[str, Any] | None = None
    for row in rows:
        period = str(row["period"])
        if period == "weekly":
            start = _week_start_ms(now)
            end = start + 7 * DAY_MS
        else:
            start = _period_start_ms(now)
            end = _next_month_start(now)
        span = end - start
        if span <= 0:
            continue
        spent = _spend_since(conn, user_id, start, now)
        threshold = int(row["threshold"] or 0)
        if threshold <= 0:
            continue
        used_pct = int(round(spent * 100 / threshold))
        elapsed_pct = int(round((now - start) * 100 / span))
        if used_pct < BUDGET_USED_PCT or elapsed_pct > BUDGET_EARLY_PCT:
            continue
        item = {
            "rule_id": str(row["id"]),
            "period": period,
            "spent": spent,
            "threshold": threshold,
            "used_pct": used_pct,
            "elapsed_pct": elapsed_pct,
        }
        if best is None or used_pct > best["used_pct"]:
            best = item
    if best is None:
        return None
    label = "这周" if best["period"] == "weekly" else "这个月"
    facts = {
        "budget_yuan": fen_to_yuan_str(best["threshold"]),
        "spent_yuan": fen_to_yuan_str(best["spent"]),
        "used_pct": str(best["used_pct"]),
        "elapsed_pct": str(best["elapsed_pct"]),
    }
    dedupe = (
        f"I8:{best['rule_id']}:{_week_start_ms(now)}" if best["period"] == "weekly"
        else f"I8:{best['rule_id']}:{_month_tag(now)}"
    )
    return {
        "insight_id": "I8_budget_pace",
        "dedupe_key": dedupe,
        "severity": "info",
        "title": f"{label}的预算已经用掉 {facts['used_pct']}%",
        "body": (
            f"预算 {facts['budget_yuan']} 元，已经花掉 {facts['spent_yuan']} 元（{facts['used_pct']}%），"
            f"而{label}才过 {facts['elapsed_pct']}%。按这个速度会超；可以先从最容易收的那一类停一停，"
            "不用现在就做决定。"
        ),
        "facts": facts,
        "actions": [
            _goto("看这个月花在哪", "bill"),
            _ask("让助手帮我收一收", "帮我看下这个月预算还剩多少，哪类支出还能省"),
        ],
    }


# ---------------------------------------------------------------- 存钱类


def _goal_candidate(conn: sqlite3.Connection, user_id: str, now: int) -> dict[str, Any] | None:
    """储蓄目标：进度 vs 按日期应达进度。只看仍在进行、且还有截止日的目标。"""
    rows = conn.execute(
        "SELECT * FROM savings_goals WHERE user_id = ? AND status = 'active'",
        (user_id,),
    ).fetchall()
    best: dict[str, Any] | None = None
    for row in rows:
        target = int(row["target_fen"] or 0)
        saved = int(row["saved_fen"] or 0)
        if target <= 0 or saved >= target:
            continue
        deadline = _date_text_ms(row["target_date"])
        created = int(row["created_ms"] or 0)
        if deadline is None or created <= 0 or deadline <= created:
            continue
        expected = int(round(max(0.0, min(1.0, (now - created) / (deadline - created))) * 100))
        progress = int(round(saved * 100 / target))
        if expected - progress < GOAL_BEHIND_PCT:
            continue
        gap = max(0, int(target * expected / 100) - saved)
        item = {
            "name": str(row["name"] or "储蓄目标"),
            "saved": saved,
            "target": target,
            "progress": progress,
            "expected": expected,
            "days_left": max(0, (deadline - now) // DAY_MS),
            "gap": gap,
        }
        if best is None or expected - progress > best["expected"] - best["progress"]:
            best = item
    if best is None:
        return None
    facts = {
        "name": best["name"],
        "saved_yuan": fen_to_yuan_str(best["saved"]),
        "target_yuan": fen_to_yuan_str(best["target"]),
        "progress_pct": str(best["progress"]),
        "expected_pct": str(best["expected"]),
        "days_left": str(best["days_left"]),
        "gap_yuan": fen_to_yuan_str(best["gap"]),
    }
    return {
        "insight_id": "I6_goal_behind",
        "dedupe_key": f"I6:{best['name']}:{_month_tag(now)}",
        "severity": "info",
        "title": f"「{facts['name']}」比计划慢了一点",
        "body": (
            f"「{facts['name']}」已经存了 {facts['saved_yuan']} 元，目标是 {facts['target_yuan']} 元"
            f"（{facts['progress_pct']}%）；按日期算现在该到 {facts['expected_pct']}%，还剩 {facts['days_left']} 天。"
            f"想追上可以一次补 {facts['gap_yuan']} 元；补不上也没关系，把截止日往后调一调同样是办法。"
        ),
        "facts": facts,
        "actions": [
            _goto("去计划里看一眼", "plans"),
            _ask("让助手帮我补上", f"帮我把「{facts['name']}」这个月的储蓄补上"),
        ],
    }


def _payday_candidate(conn: sqlite3.Connection, user_id: str, now: int) -> dict[str, Any] | None:
    """发薪先存：本月到账的最大一笔收入，建议先留一成。不是催存，只是给个顺序。"""
    exclude = ",".join("?" * len(INTERNAL_CATEGORIES))
    row = conn.execute(
        "SELECT t.counterparty, t.amount_fen FROM transactions t JOIN accounts a ON a.id = t.account_id"
        " WHERE a.user_id = ? AND t.direction = 'in' AND t.category NOT IN (" + exclude + ")"
        " AND t.ts_ms >= ? AND t.ts_ms < ? AND t.amount_fen >= ?"
        " ORDER BY t.amount_fen DESC, t.id LIMIT 1",
        (user_id, *INTERNAL_CATEGORIES, _period_start_ms(now), now, PAYDAY_MIN_FEN),
    ).fetchone()
    if row is None:
        return None
    income = int(row["amount_fen"])
    suggest = max(10_000, income // 10 // 1000 * 1000)
    facts = {
        "source": str(row["counterparty"] or "本月收入"),
        "income_yuan": fen_to_yuan_str(income),
        "suggest_yuan": fen_to_yuan_str(suggest),
    }
    return {
        "insight_id": "I4_payday_saving",
        "dedupe_key": f"I4:{_month_tag(now)}",
        "severity": "info",
        "title": "这个月有一笔收入到账，可以先留一笔",
        "body": (
            f"{facts['source']}这个月到账 {facts['income_yuan']} 元。如果这是工资，可以先把 "
            f"{facts['suggest_yuan']} 元（大约一成）放进储蓄目标，剩下的再安排花销："
            "先存后花，比月底看剩多少更稳。"
        ),
        "facts": facts,
        "actions": [
            _goto("去计划里设目标", "plans"),
            _ask("让助手帮我存一笔", "帮我把这个月工资的一成存进储蓄目标"),
        ],
    }


#: 顺序就是「同一天额度有限时，先说什么」的顺序：安全 → 花钱 → 存钱。
_PROBES: Final[tuple[Callable[[sqlite3.Connection, str, int], dict[str, Any] | None], ...]] = (
    _night_candidate,
    _payee_candidate,
    _cash_gap_candidate,
    _hike_candidate,
    _budget_candidate,
    _goal_candidate,
    _payday_candidate,
    _spending_candidate,
)


def detect_insights(conn: sqlite3.Connection, user_id: str, now: int) -> list[dict[str, Any]]:
    """按重要度跑全部探测器；任何一个探不到就跳过，不编。"""
    found: list[dict[str, Any]] = []
    for probe in _PROBES:
        item = probe(conn, user_id, now)
        if item is not None:
            found.append(item)
    return found


__all__ = ["CATALOG", "DAY_MS", "day_start_ms", "detect_insights"]
