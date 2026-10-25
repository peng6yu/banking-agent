"""自然语言里两条「助手型」捷径：储蓄目标和提醒。

为什么要一个独立入口，而不是塞进意图表：
1. 意图是给模型填槽位用的，储蓄/提醒的槽位（每月存多少、哪一天提醒）模型经常漏，
   漏了就只能一轮轮反问——用户看到的正是「你要转给谁」这种答非所问；
2. 这两件事都是确定性操作，不涉及资金动作，也不需要 DAG：能算的就不该让模型猜；
3. 不新增 intent 名，避免牵动知识库意图规则、schema 断言和工具计数（35 个）。

所以这里是一条「识别即执行」的快路：命中就返回一句人话 + 可点选项，
没命中就返回 None，后面照常走模型解析。
"""

from __future__ import annotations

import calendar
import json
import math
import re
import time
from datetime import date, datetime, timedelta
from typing import Any, Final

import app.audit as audit
import app.plans as plans
import app.proactive as proactive
import app.reminder_time as reminder_time
from app.money import fen_to_yuan_str

# 命中提醒的动词；「生日」和「转账」不在此列：生日计划有独立链路，
# 「提醒我转账」如果只建个提醒反而把真正的资金动作吞掉了。
REMINDER_MARKERS: Final[tuple[str, ...]] = ("提醒", "记得", "别忘", "叫我", "到点", "闹钟")
REMINDER_EXCLUDES: Final[tuple[str, ...]] = (
    "生日", "转账", "转", "汇款", "付款", "支付", "申购", "赎回", "还款",
)

# 「储蓄」不能单独当关键词：储蓄卡=借记卡，「查一下储蓄卡余额」会被吞成储蓄目标。
SAVINGS_MARKERS: Final[tuple[str, ...]] = (
    "存钱", "攒钱", "存款", "存够", "存下", "每月存", "月存", "攒够", "存一笔", "存点钱",
    "存了", "记录存", "已存",
)
# 「储蓄」只在谈论目标/计划时才是指储蓄目标。
_SAVINGS_GOAL_RE: Final[re.Pattern[str]] = re.compile(
    r"储蓄目标|储蓄计划|储蓄进度|目标.{0,6}(储蓄|攒钱|存钱)|计划.{0,6}(储蓄|攒钱|存钱)"
)
# 问余额/账单/流水的是查账，不是记存款——哪怕句子里出现了「存款」「储蓄」。
_BALANCE_QUERY_RE: Final[re.Pattern[str]] = re.compile(r"余额|账单|流水|明细|额度|利息|交易记录")
_CARD_RE: Final[re.Pattern[str]] = re.compile(r"储蓄卡|借记卡|信用卡|工资卡")
SAVINGS_EXCLUDES: Final[tuple[str, ...]] = ("理财", "基金", "赎回", "转账", "汇款", "还款")

_CN_DIGITS: Final[dict[str, str]] = {
    "零": "0", "一": "1", "二": "2", "两": "2", "三": "3", "四": "4",
    "五": "5", "六": "6", "七": "7", "八": "8", "九": "9", "十": "10",
}


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def _to_int(value: str) -> int:
    value = _CN_DIGITS.get(value, value)
    try:
        return int(value)
    except ValueError:
        return 0


def _money_fen(number: str, unit: str | None) -> int:
    """「100」「1.5」「2万」→ 分；只接受正数。"""
    try:
        amount = float(_CN_DIGITS.get(number, number))
    except ValueError:
        return 0
    if unit == "万":
        amount *= 10000
    elif unit == "千":
        amount *= 1000
    fen = int(round(amount * 100))
    return fen if fen > 0 else 0


_AMOUNT = r"(\d+(?:\.\d+)?|[零一二两三四五六七八九十]+)"


def _match_money(pattern: str, text: str) -> int:
    match = re.search(pattern, text)
    if not match:
        return 0
    unit = match.group(2) if match.lastindex and match.lastindex >= 2 else None
    return _money_fen(match.group(1), unit)


def _add_months(day: date, months: int) -> date:
    month_index = day.month - 1 + max(0, months)
    year = day.year + month_index // 12
    month = month_index % 12 + 1
    last = calendar.monthrange(year, month)[1]
    return date(year, month, min(day.day, last))


def _parse_monthly(text: str) -> int:
    patterns = (
        r"(?:每(?:个)?月|按?月)(?:存|攒|存下|存上|存款|储蓄)?\s*" + _AMOUNT + r"\s*(万|千)?",
        _AMOUNT + r"\s*(万|千)?\s*(?:元|块)?\s*/\s*月",
        r"月存\s*" + _AMOUNT + r"\s*(万|千)?",
    )
    for pattern in patterns:
        fen = _match_money(pattern, text)
        if fen:
            return fen
    return 0


def _parse_target(text: str) -> int:
    patterns = (
        r"(?:目标|存够|攒够|存到|总共|一共|总额)(?:是|为)?\s*" + _AMOUNT + r"\s*(万|千)?",
        r"(?:半年|一年|两年|\d+\s*个?月|\d+\s*年)(?:内|后)?\s*(?:存|攒)(?:够|到|下)?\s*" + _AMOUNT + r"\s*(万|千)?",
    )
    for pattern in patterns:
        fen = _match_money(pattern, text)
        if fen:
            return fen
    return 0


def _parse_saved(text: str) -> int:
    return _match_money(r"(?:已经|已)存(?:了|下|入)?\s*" + _AMOUNT + r"\s*(万|千)?", text)


def _parse_target_date(text: str, *, today: date, months_hint: int = 0) -> date:
    explicit = re.search(r"(\d{4})[-/年](\d{1,2})[-/月](\d{1,2})", text)
    if explicit:
        try:
            picked = date(int(explicit.group(1)), int(explicit.group(2)), int(explicit.group(3)))
            if picked > today:
                return picked
        except ValueError:
            pass
    if "半年" in text:
        return _add_months(today, 6)
    if "一年" in text or "明年" in text:
        return _add_months(today, 12)
    if "两年" in text:
        return _add_months(today, 24)
    years = re.search(r"(\d+)\s*年", text)
    if years and "每月" not in text:
        return _add_months(today, max(1, _to_int(years.group(1))) * 12)
    months = re.search(r"(?<!每)(\d+)\s*个?月", text)
    if months:
        return _add_months(today, max(1, _to_int(months.group(1))))
    if months_hint > 0:
        return _add_months(today, months_hint)
    return _add_months(today, 12)


def _parse_purpose(text: str) -> str:
    match = re.search(r"(?:为了|为|想买|准备买|攒钱买)\s*([^，。；;、]{1,12})", text)
    if not match:
        return ""
    return match.group(1).strip(" 的")


_SAVINGS_AMOUNT_RE: Final[re.Pattern[str]] = re.compile(r"(?:存|攒)[^，。；;]{0,8}?\d")


def detect(text: str) -> str | None:
    """返回 'reminder' / 'savings' / None；只做关键词判定，不碰数据库。"""
    clean = _clean(text)
    if not clean:
        return None
    # 提醒优先：「提醒我每月存 100」是提醒，不是建储蓄目标。
    if any(word in clean for word in REMINDER_MARKERS) and not any(
        word in clean for word in REMINDER_EXCLUDES
    ):
        return "reminder"
    if any(word in clean for word in SAVINGS_EXCLUDES):
        return None
    goalish = bool(_SAVINGS_GOAL_RE.search(clean))
    # 提到卡或问余额的默认是查账；只有同时明确在说储蓄目标才放行。
    if (_CARD_RE.search(clean) or _BALANCE_QUERY_RE.search(clean)) and not goalish:
        return None
    if goalish or any(word in clean for word in SAVINGS_MARKERS):
        return "savings"
    # 「半年内存 3000 元」这类没有关键词、但明显是存钱目标的说法。
    if _SAVINGS_AMOUNT_RE.search(clean):
        return "savings"
    return None


# ------------------------------------------------------------------ 储蓄目标


def _savings_choices(name: str) -> list[dict[str, str]]:
    return [
        {"label": "看看我的储蓄目标", "value": "我有哪些储蓄目标"},
        {"label": "记录一笔存款", "value": "记录存了 100 元"},
        {"label": "取消这个目标", "value": f"取消储蓄计划 {name}"},
    ]


def _active_goals(conn, user_id: str) -> list[dict[str, Any]]:
    return proactive.list_goals(conn, user_id=user_id, status="active", limit=20)


def _yuan_label(fen: int) -> str:
    text = f"{fen / 100:.2f}".rstrip("0").rstrip(".")
    return text or "0"


def _savings_summary(goal: dict[str, Any]) -> str:
    return (
        f"已建好储蓄目标「{goal['name']}」：目标 {goal['target_yuan']} 元，"
        f"目标日 {goal['target_date']}，每月需要存 {goal['required_per_period_yuan']} 元。"
        f"现在已存 {goal['saved_yuan']} 元，还差 {goal['remaining_yuan']} 元。"
    )


def _handle_savings(conn, text: str, user_id: str) -> dict[str, Any]:
    clean = _clean(text)
    goals = _active_goals(conn, user_id)

    if re.search(r"(取消|停掉|结束|终止|不要了|删掉).{0,6}(储蓄|存钱|攒钱|目标|计划)", clean) or clean.startswith("取消储蓄计划"):
        wanted = re.sub(r"^(取消|停掉|结束|终止|不要了|删掉)\s*(储蓄|存钱|攒钱)?\s*(计划|目标)?\s*", "", clean).strip(" 「」\"'")
        matches = [goal for goal in goals if wanted and wanted in str(goal["name"])]
        if not matches and len(goals) == 1:
            matches = goals
        if not matches and goals:
            return {
                "intent": "assist",
                "action": "savings_cancel_choose",
                "summary": "你有不止一个储蓄目标，要取消哪一个？",
                "choices": [
                    {"label": g["name"], "value": f"取消储蓄计划 {g['name']}"} for g in goals[:6]
                ],
                "data": {"goals": goals},
            }
        if not matches:
            return {
                "intent": "assist",
                "action": "savings_cancel_none",
                "summary": "现在没有进行中的储蓄目标，不用取消。",
                "choices": [{"label": "建一个储蓄目标", "value": "帮我建一个每月存 500 元的储蓄计划"}],
                "data": {},
            }
        goal = matches[0]
        updated = proactive.update_goal(conn, str(goal["goal_id"]), user_id=user_id, status="cancelled")
        return {
            "intent": "assist",
            "action": "savings_cancel",
            "summary": f"已取消储蓄目标「{goal['name']}」，钱一分没动；想重新开始时再建一个就行。",
            "choices": [
                {"label": "再建一个", "value": "帮我建一个每月存 500 元的储蓄计划"},
                {"label": "看看还有哪些目标", "value": "我有哪些储蓄目标"},
            ],
            "data": {"goal": updated},
        }

    if re.search(r"(记录|记一笔|又存|已经存|存了|存进)", clean) and not re.search(r"(每月|月存|目标|计划|建|设置)", clean):
        fen = _match_money(r"(?:记录|记一笔|又存|已经存|存了|存进)(?:了|下|入)?\s*" + _AMOUNT + r"\s*(万|千)?", clean)
        if not fen:
            fen = _match_money(_AMOUNT + r"\s*(万|千)?\s*(?:元|块)", clean)
        if fen <= 0:
            return {
                "intent": "assist",
                "action": "savings_record_ask",
                "summary": "这次存了多少？说个金额就行，比如「记录存了 200 元」。",
                "choices": [{"label": "200 元", "value": "记录存了 200 元"}, {"label": "500 元", "value": "记录存了 500 元"}],
                "data": {},
            }
        if not goals:
            return {
                "intent": "assist",
                "action": "savings_record_none",
                "summary": "还没有储蓄目标。先建一个，再往里记存款。",
                "choices": [{"label": "建一个储蓄目标", "value": "帮我建一个每月存 500 元的储蓄计划"}],
                "data": {},
            }
        goal = goals[0]
        saved = int(goal["saved_fen"]) + fen
        if saved > int(goal["target_fen"]):
            saved = int(goal["target_fen"])
        updated = proactive.update_goal(conn, str(goal["goal_id"]), user_id=user_id, saved_fen=saved)
        now_saved = fen_to_yuan_str(saved)
        return {
            "intent": "assist",
            "action": "savings_record",
            "summary": f"记下了：目标「{goal['name']}」现在已存 {now_saved} 元。",
            "choices": [
                {"label": "看看进度", "value": "我有哪些储蓄目标"},
                {"label": "再记一笔", "value": "记录存了 200 元"},
            ],
            "data": {"goal": updated},
        }

    if re.search(r"(有哪些|看看|查看|进度|怎么样了|列表|我的).{0,4}(储蓄|存钱|攒钱|目标)", clean):
        if not goals:
            return {
                "intent": "assist",
                "action": "savings_list",
                "summary": "还没有储蓄目标。想存多少、什么时候用？我帮你算每月要存多少。",
                "choices": [
                    {"label": "每月存 500，一年", "value": "帮我建一个每月存 500 元、一年内存够 6000 元的储蓄计划"},
                    {"label": "半年存 3000", "value": "帮我建一个半年内存 3000 元的储蓄计划"},
                ],
                "data": {"goals": []},
            }
        lines = [
            f"「{g['name']}」已存 {g['saved_yuan']} / {g['target_yuan']} 元，"
            f"每月需存 {g['required_per_period_yuan']} 元，目标日 {g['target_date']}"
            for g in goals[:5]
        ]
        return {
            "intent": "assist",
            "action": "savings_list",
            "summary": "你有这些储蓄目标：" + "；".join(lines) + "。",
            "choices": [
                {"label": "记录一笔存款", "value": "记录存了 100 元"},
                {"label": f"取消「{goals[0]['name']}」", "value": f"取消储蓄计划 {goals[0]['name']}"},
            ],
            "data": {"goals": goals},
        }

    monthly = _parse_monthly(clean)
    target = _parse_target(clean)
    saved = _parse_saved(clean)
    if not monthly and not target:
        return {
            "intent": "assist",
            "action": "savings_ask",
            "summary": "想存多少？告诉我「每月存多少」和「目标多少、多久存够」，我直接帮你建。",
            "choices": [
                {"label": "每月 500，一年 6000", "value": "帮我建一个每月存 500 元、一年内存够 6000 元的储蓄计划"},
                {"label": "每月 1000，半年 6000", "value": "帮我建一个每月存 1000 元、半年内存够 6000 元的储蓄计划"},
            ],
            "data": {},
        }
    if not monthly and target:
        hint_months = 12
        monthly = max(1, math.ceil(target / hint_months))
    if not target:
        target = max(monthly, monthly * 12)
    months_hint = 0
    if monthly and target:
        months_hint = max(1, math.ceil(target / monthly))
    target_date = _parse_target_date(clean, today=date.today(), months_hint=months_hint)
    if saved > target:
        saved = 0
    purpose = _parse_purpose(clean)
    name = purpose or f"每月存 {_yuan_label(monthly)} 元"
    name = name[:20]
    created = proactive.create_goal(
        conn,
        user_id=user_id,
        name=name,
        target_fen=target,
        target_date=target_date.isoformat(),
        saved_fen=saved,
        period="monthly",
    )
    goal = created["goal"]
    return {
        "intent": "assist",
        "action": "savings_create",
        "summary": _savings_summary(goal),
        "choices": _savings_choices(str(goal["name"])),
        "data": {"goal": goal, "audit_seq": created.get("audit_seq")},
    }


# ------------------------------------------------------------------ 提醒


def _clean_reminder_title(text: str) -> str:
    title = _clean(text)
    patterns = (
        r"^(帮我|请|麻烦|给我|设置|建一个|建立|定一个|添加|加一个|新增)+",
        r"(提醒我|提醒一下|提醒|记得|别忘了|别忘|叫我|到点|到时候)+",
        r"(每(?:个)?月\s*\d+\s*[号日])",
        r"(每(?:周|星期)[一二三四五六日天])",
        r"(每天|每日)",
        r"((?:上午|下午|晚上|中午)?\s*\d{1,2}\s*[点:：]\s*\d{0,2}\s*分?)",
        r"(明天|后天|大后天|今天|下周[一二三四五六日天]|\d{4}-\d{1,2}-\d{1,2})",
    )
    for _ in range(4):
        before = title
        for pattern in patterns:
            title = re.sub(pattern, "", title)
        if title == before:
            break
    title = title.strip(" ，。；;、:：-—")
    return title[:40] or "你安排的事"


def _parse_clock(text: str) -> tuple[int, int] | None:
    match = re.search(r"(上午|下午|晚上|中午)?\s*(\d{1,2})\s*[点:：]\s*(\d{1,2})?\s*分?", text)
    if not match:
        return None
    hour = int(match.group(2))
    minute = int(match.group(3) or 0)
    period = match.group(1) or ""
    if period in ("下午", "晚上") and hour < 12:
        hour += 12
    if period == "中午" and hour < 11:
        hour += 12
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return hour, minute


def _parse_repeat(text: str) -> dict[str, Any]:
    monthly = re.search(r"每(?:个)?月\s*(\d{1,2})\s*[号日]", text)
    if monthly:
        return {"repeat": "monthly", "day": max(1, min(31, int(monthly.group(1))))}
    weekly = re.search(r"每(?:周|星期)([一二三四五六日天])", text)
    if weekly:
        return {"repeat": "weekly", "weekday": reminder_time.WEEKDAYS[weekly.group(1)]}
    if "每天" in text or "每日" in text:
        return {"repeat": "daily"}
    return {"repeat": "none"}


def _parse_once_date(text: str, today: date) -> date | None:
    explicit = re.search(r"(\d{4})-(\d{1,2})-(\d{1,2})", text)
    if explicit:
        try:
            return date(int(explicit.group(1)), int(explicit.group(2)), int(explicit.group(3)))
        except ValueError:
            return None
    if "大后天" in text:
        return today + timedelta(days=3)
    if "后天" in text:
        return today + timedelta(days=2)
    if "明天" in text:
        return today + timedelta(days=1)
    if "今天" in text:
        return today
    next_week = re.search(r"下周([一二三四五六日天])", text)
    if next_week:
        target = reminder_time.WEEKDAYS[next_week.group(1)]
        delta = (target - today.weekday()) % 7
        return today + timedelta(days=delta + 7)
    return None


def _first_due(text: str, repeat: dict[str, Any], clock: tuple[int, int], now: int) -> int:
    today = date.today()
    if repeat.get("repeat") and repeat["repeat"] != "none":
        return reminder_time.next_after(
            now,
            repeat=str(repeat["repeat"]),
            hour=clock[0],
            minute=clock[1],
            day=repeat.get("day"),
            weekday=repeat.get("weekday"),
        )
    day = _parse_once_date(text, today) or (today + timedelta(days=1))
    stamp = int(time.mktime((day.year, day.month, day.day, clock[0], clock[1], 0, 0, 0, -1)) * 1000)
    if stamp <= now:
        stamp = int(
            time.mktime((day.year, day.month, day.day + 1, clock[0], clock[1], 0, 0, 0, -1)) * 1000
        )
    return stamp


def _create_reminder(
    conn,
    *,
    text: str,
    user_id: str,
    due_ms: int | None = None,
    repeat: str = "none",
    hour: int = 9,
    minute: int = 0,
    day: int | None = None,
    weekday: int | None = None,
    session_id: str | None = None,
) -> dict[str, Any]:
    now = int(time.time() * 1000)
    if repeat not in reminder_time.REPEATS:
        repeat = "none"
    if due_ms is None:
        if repeat != "none":
            due_ms = reminder_time.next_after(
                now, repeat=repeat, hour=hour, minute=minute, day=day, weekday=weekday
            )
        else:
            today = date.today()
            due_ms = int(
                time.mktime((today.year, today.month, today.day + 1, hour, minute, 0, 0, 0, -1)) * 1000
            )
    payload = {
        "text": text,
        "repeat": repeat,
        "hour": int(hour),
        "minute": int(minute),
    }
    if day is not None:
        payload["day"] = int(day)
    if weekday is not None:
        payload["weekday"] = int(weekday)
    plan = plans.create(
        conn,
        user_id=user_id,
        title=f"提醒：{text}",
        steps=[{"kind": "reminder", "title": text, "payload": payload, "due_ms": int(due_ms)}],
        meta={"source": "assist", "reminder": True, "repeat": repeat},
    )
    label = reminder_time.label(repeat, hour, minute, day, weekday)
    return {
        "ok": True,
        "plan": plan,
        "due_ms": int(due_ms),
        "due_text": reminder_time.time_text(due_ms),
        "repeat": repeat,
        "repeat_label": label,
    }


def create_reminder(
    conn,
    *,
    text: str,
    user_id: str | None = None,
    due_ms: int | None = None,
    repeat: str = "none",
    hour: int = 9,
    minute: int = 0,
    day: int | None = None,
    weekday: int | None = None,
    session_id: str | None = None,
) -> dict[str, Any]:
    """给 API 用的显式入口：文本已由用户确认，不再猜测。"""
    owner = user_id or proactive.default_user_id(conn)
    return _create_reminder(
        conn,
        text=_clean(text)[:40] or "你安排的事",
        user_id=owner,
        due_ms=due_ms,
        repeat=repeat,
        hour=hour,
        minute=minute,
        day=day,
        weekday=weekday,
        session_id=session_id,
    )


def _active_reminders(conn, user_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT p.*, s.payload_json AS step_payload FROM plans p"
        " JOIN plan_steps s ON s.plan_id = p.id"
        " WHERE p.user_id = ? AND p.status = 'active' AND s.kind = 'reminder'"
        " GROUP BY p.id ORDER BY p.updated_ms DESC LIMIT 20",
        (user_id,),
    ).fetchall()
    result = []
    for row in rows:
        meta = json.loads(row["meta_json"] or "{}")
        if not meta.get("reminder"):
            continue
        view = plans.plan_view(conn, row)
        payload = json.loads(row["step_payload"] or "{}")
        # 计划标题带了「提醒：」前缀，选项里再拼一次就变成「取消提醒 提醒：开会」。
        raw = str(payload.get("text") or view.get("title") or "").strip()
        if raw.startswith("提醒："):
            raw = raw[3:]
        view["title"] = raw or "你安排的事"
        view["repeat"] = str(payload.get("repeat") or "none")
        result.append(view)
    return result


def active_reminders(conn, user_id: str) -> list[dict[str, Any]]:
    """页面用的提醒列表：下次时间、重复规则、可取消的计划号。

    和聊天快路共用同一份落库数据（plans + plan_steps）。页面自己再拼一遍，
    就会出现「聊天里建了提醒、页面上看不到」这种前后不一致。
    """
    items: list[dict[str, Any]] = []
    for plan in _active_reminders(conn, user_id):
        step = next(
            (item for item in plan.get("steps") or [] if str(item.get("kind")) == "reminder"), None
        )
        if step is None:
            continue
        payload = step.get("payload") or {}
        repeat = str(payload.get("repeat") or "none")
        day = payload.get("day")
        weekday = payload.get("weekday")
        items.append(
            {
                "plan_id": str(plan.get("plan_id") or ""),
                "title": str(plan.get("title") or "提醒"),
                "repeat": repeat,
                "repeat_label": reminder_time.label(
                    repeat,
                    int(payload.get("hour") or 9),
                    int(payload.get("minute") or 0),
                    int(day) if day is not None else None,
                    int(weekday) if weekday is not None else None,
                ),
                "due_ms": int(step.get("due_ms") or 0),
                "due_text": str(step.get("due_text") or ""),
                "state": str(step.get("state") or ""),
            }
        )
    return items


def _handle_reminder(conn, text: str, user_id: str) -> dict[str, Any]:
    clean = _clean(text)
    reminders = _active_reminders(conn, user_id)

    if re.search(r"(取消|停掉|结束|删掉|不要).{0,8}提醒", clean) or clean.startswith("取消这条提醒"):
        wanted = re.sub(r"^(取消|停掉|结束|删掉|不要)\s*(这条|这个)?\s*提醒\s*", "", clean).strip(" 「」\"'")
        matches = [p for p in reminders if wanted and wanted in str(p["title"])]
        if not matches and len(reminders) == 1:
            matches = reminders
        if not matches and reminders:
            return {
                "intent": "assist",
                "action": "reminder_cancel_choose",
                "summary": "你有不止一个提醒，要取消哪一个？",
                "choices": [{"label": p["title"], "value": f"取消提醒 {p['title']}"} for p in reminders[:6]],
                "data": {"reminders": reminders},
            }
        if not matches:
            return {
                "intent": "assist",
                "action": "reminder_cancel_none",
                "summary": "现在没有进行中的提醒。",
                "choices": [{"label": "建一个提醒", "value": "提醒我明天上午 9 点开会"}],
                "data": {},
            }
        plan = matches[0]
        # _active_reminders 返回的是 plan_view，主键叫 plan_id，不是 id
        plans.update(conn, str(plan["plan_id"]), user_id=user_id, status="cancelled")
        return {
            "intent": "assist",
            "action": "reminder_cancel",
            "summary": f"已取消提醒「{plan['title']}」。",
            "choices": [{"label": "看看还有哪些提醒", "value": "我有哪些提醒"}],
            "data": {"plan": plan},
        }

    if re.search(r"(有哪些|看看|查看|列表|我的).{0,4}提醒", clean):
        if not reminders:
            return {
                "intent": "assist",
                "action": "reminder_list",
                "summary": "现在没有进行中的提醒。要建一个吗？",
                "choices": [{"label": "建一个提醒", "value": "提醒我明天上午 9 点开会"}],
                "data": {"reminders": []},
            }
        lines = [f"「{p['title']}」下次 {p.get('due_text') or '—'}" for p in reminders[:5]]
        return {
            "intent": "assist",
            "action": "reminder_list",
            "summary": "你有这些提醒：" + "；".join(lines) + "。",
            "choices": [
                {"label": f"取消「{reminders[0]['title']}」", "value": f"取消提醒 {reminders[0]['title']}"}
            ],
            "data": {"reminders": reminders},
        }

    repeat = _parse_repeat(clean)
    clock = _parse_clock(clean) or (9, 0)
    now = int(time.time() * 1000)
    due_ms = _first_due(clean, repeat, clock, now)
    title = _clean_reminder_title(clean)
    created = _create_reminder(
        conn,
        text=title,
        user_id=user_id,
        due_ms=due_ms,
        repeat=str(repeat["repeat"]),
        hour=clock[0],
        minute=clock[1],
        day=repeat.get("day"),
        weekday=repeat.get("weekday"),
    )
    label = created["repeat_label"]
    when = created["due_text"]
    summary = f"已建好提醒：{label}，提醒你「{title}」。下一次：{when}。"
    if repeat["repeat"] != "none":
        summary += "这条会一直重复，直到你取消。"
    return {
        "intent": "assist",
        "action": "reminder_create",
        "summary": summary,
        "choices": [
            {"label": "看看我的提醒", "value": "我有哪些提醒"},
            {"label": "取消这条提醒", "value": f"取消提醒 {title}"},
        ],
        "data": created,
    }


def handle(conn, text: str, *, session_id: str | None = None) -> dict[str, Any] | None:
    """识别并执行；没命中返回 None，调用方继续走模型解析。"""
    kind = detect(text)
    if kind is None:
        return None
    user_id = proactive.default_user_id(conn)
    if kind == "reminder":
        return _handle_reminder(conn, text, user_id)
    return _handle_savings(conn, text, user_id)


__all__ = ["detect", "handle", "create_reminder", "active_reminders"]
