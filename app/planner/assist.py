"""自然语言里两条「助手型」捷径：储蓄目标和提醒。

为什么要一个独立入口，而不是塞进意图表：
1. 意图是给模型填槽位用的，储蓄/提醒的槽位（每月存多少、哪一天提醒）模型经常漏，
   漏了就只能一轮轮反问——用户看到的正是「你要转给谁」这种答非所问；
2. 这两件事都是确定性操作，不涉及资金动作，也不需要 DAG：能算的就不该让模型猜；
   但「提醒」必须挂在资金时间点上（还款/扣费/预约/预算/储蓄/额度），见 app/scope.py；
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
import app.scope as scope
from app.clock import ValidationError
import app.proactive as proactive
import app.reminder_time as reminder_time
import app.memory as memory
import app.subscriptions as subscriptions
from app.money import fen_to_yuan_str

# 命中提醒的动词；「生日」和「转账」不在此列：生日计划有独立链路，
# 「提醒我转账」如果只建个提醒反而把真正的资金动作吞掉了。
REMINDER_MARKERS: Final[tuple[str, ...]] = ("提醒", "记得", "别忘", "叫我", "到点", "闹钟")
REMINDER_EXCLUDES: Final[tuple[str, ...]] = (
    "生日", "转账", "转", "汇款", "付款", "支付", "申购", "赎回", "还款",
)
# 例外：「提醒我明天上午 9 点那笔预约转账」有提醒意图、有资金锚点、又没说金额与收款人，
# 是在给一笔既存的钱设时间点，不是要现在转钱。只要出现金额或「给某人转」就仍判给转账链路。
_REMINDER_TRANSFER_RE: Final[re.Pattern[str]] = re.compile(
    r"\d+(?:\.\d+)?\s*(?:元|块|万)"
    r"|(?:转给|汇给|打给|付给)"
    r"|给[\u4e00-\u9fa5]{2,4}\s*(?:转|汇|打|付)"
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

# 「计划」只在这里接两件事：列出来、结束掉。新建仍交给模型 + 生日/营销链路，
# 否则「帮我建个生日计划」会被快路吞成一句话建计划，金额和日期全问不出来。
_PLAN_LIST_RE: Final[re.Pattern[str]] = re.compile(
    r"(有哪些|看看|查看|列出|我的|所有|还有|几个).{0,6}计划"
)
# 取消的动词可能在前也可能在后：「取消生日计划」和「把生日计划取消掉」都得认。
_PLAN_CANCEL_RE: Final[re.Pattern[str]] = re.compile(
    r"(取消|停掉|结束|删掉|放弃|终止|不想要).{0,10}计划"
    r"|计划.{0,10}(取消|停掉|结束|删掉|放弃|终止|不要了)"
)
# 「帮我核实这几笔」不能只把同一句话再丢回模型：模型手里没有逐笔流水，
# 只会回一句「这看起来像订阅」。所以核实要落到账上真实的每一笔上。
_SUB_CHECK_MARKERS: Final[tuple[str, ...]] = (
    "核实", "核对", "对一下", "查一下", "查查", "看看", "确认一下", "分析一下", "查查账",
)
_SUB_OBJECTS: Final[tuple[str, ...]] = ("订阅", "代扣", "扣款", "续费", "这几笔", "那几笔", "那笔")
# 用户在聊天里直接下结论：两种都算处理完，和业务管理页的按钮同义。
_SUB_NOT_RE: Final[re.Pattern[str]] = re.compile(r"不是我(?:订|定|买)的?|不是我订阅|没订过|不是我订的")
_SUB_YES_RE: Final[re.Pattern[str]] = re.compile(r"是我(?:订|定|买)的?|确实是我|是我订阅|我订的")
_SUB_LIST_RE: Final[re.Pattern[str]] = re.compile(
    r"疑似订阅|(?:有哪些|几条|多少个|列出|所有|还有).{0,6}(?:订阅|代扣)"
    r"|(?:订阅|代扣).{0,6}(?:有哪些|几条|列表)"
)
PLAN_STATUS_LABELS: Final[dict[str, str]] = {
    "active": "进行中",
    "paused": "等你确认",
    "completed": "已完成",
    "cancelled": "已结束",
    "failed": "卡住了",
}

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
    """返回 'reminder' / 'savings' / 'plan' / 'subscription' / 'memory' / None；只做关键词判定，不碰数据库。"""
    clean = _clean(text)
    if not clean:
        return None
    # 长期记忆排在最前面：「你记得我什么」里有「记得」，「忘掉记忆「X」」里有「忘」，
    # 放到提醒后面会被当成建提醒/取消提醒。显式记忆是确定性读写，不该交给模型猜。
    if _MEM_REMEMBER_RE.search(clean) and not any(word in clean for word in ("提醒", "闹钟", "到点")):
        return "memory"
    if _MEM_LIST_RE.search(clean) or _MEM_FORGET_RE.search(clean):
        return "memory"
    # 提醒优先：「提醒我每月存 100」是提醒，不是建储蓄目标。
    if any(word in clean for word in REMINDER_MARKERS):
        if not any(word in clean for word in REMINDER_EXCLUDES):
            return "reminder"
        # 有锚点、又没有金额/收款人：这是给既存的钱设时间点，不是要现在动钱。
        if scope.reminder_anchor(clean) and not _REMINDER_TRANSFER_RE.search(clean):
            return "reminder"
    # 逐笔核实订阅：要有动作词 + 对象词，或者用户直接下结论（「不是我订的」）。
    # 放在储蓄判断之前：这两句的用词不重叠，但「核实」比「存」更该优先。
    if (
        (any(word in clean for word in _SUB_CHECK_MARKERS) and any(word in clean for word in _SUB_OBJECTS))
        or _SUB_NOT_RE.search(clean)
        or _SUB_YES_RE.search(clean)
        or _SUB_LIST_RE.search(clean)
    ):
        return "subscription"
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
    # 计划只接「列出来」和「结束掉」；新建计划仍要模型去凑参数。
    if _PLAN_LIST_RE.search(clean) or _PLAN_CANCEL_RE.search(clean):
        return "plan"
    return None


# ------------------------------------------------------------------ 长期记忆
# 「记住 / 忘掉 / 你记得我什么」是确定性操作：说一句就写、就删、就列，不用模型猜。
_MEM_LIST_RE: Final[re.Pattern[str]] = re.compile(
    r"你(?:都)?记得我(?:什么|啥)|我的记忆|你(?:还)?了解我什么|你记住了我(?:什么|啥)|我有哪些记忆"
)
_MEM_FORGET_RE: Final[re.Pattern[str]] = re.compile(r"^(?:帮我)?(?:忘掉|忘了|忘记|别记着|不要记着|删掉记忆)")
_MEM_REMEMBER_RE: Final[re.Pattern[str]] = re.compile(r"^(?:帮我|请)?(?:记住|记一下|记着|记下来)")
_MEM_PREFERENCE_WORDS: Final[tuple[str, ...]] = ("喜欢", "不喜欢", "讨厌", "偏好", "习惯", "口味", "忌口")
_MEM_KEY_CHARS: Final[int] = memory.KEY_MAX
_MEM_VALUE_CHARS: Final[int] = memory.VALUE_MAX


def _memory_line(fact: dict[str, Any]) -> str:
    """一条记忆的人话；键和值相同（「记住我不喜欢电话推销」）时不重复念两遍。"""
    if str(fact["key"]) == str(fact["value"]):
        return f"[{fact['kind_label']}] {fact['key']}"
    return f"[{fact['kind_label']}] {fact['key']}：{fact['value']}"


def _memory_choices(facts: list[dict[str, Any]]) -> list[dict[str, str]]:
    return [
        {"label": f"忘掉「{fact['key']}」", "value": f"忘掉记忆「{fact['key']}」"}
        for fact in facts[:8]
    ]


def _handle_memory(conn, text: str, user_id: str) -> dict[str, Any]:
    """查看 / 忘掉 / 记住：三条都不建图、不动钱，只动 agent_memory 这一张表。"""
    clean = _clean(text)
    facts = memory.all_facts(conn, user_id=user_id, limit=50)
    if _MEM_LIST_RE.search(clean):
        if not facts:
            return {
                "summary": "我现在还没有关于你的记忆。你说一句「记住我不喜欢电话推销」这种，我就记下来，以后按你的习惯来。",
                "action": "memory_list",
                "choices": [_MEM_CHOICE_HINT],
                "data": {"memories": []},
            }
        lines = [f"{index}）{_memory_line(fact)}" for index, fact in enumerate(facts, start=1)]
        return {
            "summary": "我记着这些关于你的事（都是你自己说过的）：\n" + "\n".join(lines),
            "action": "memory_list",
            "choices": _memory_choices(facts),
            "data": {"memories": facts},
        }
    if _MEM_FORGET_RE.search(clean):
        wanted = clean
        for prefix in ("帮我", "忘掉", "忘了", "忘记", "别记着", "不要记着", "删掉", "记忆", "关于", "的", "「", "」"):
            wanted = wanted.replace(prefix, "")
        wanted = wanted.strip()
        hits = [fact for fact in facts if wanted and (wanted in fact["key"] or fact["key"] in wanted)]
        if not hits and wanted:
            hits = [fact for fact in facts if wanted in fact["value"]]
        if not hits:
            return {
                "summary": (
                    f"我没找到和「{wanted}」对得上的记忆。"
                    if wanted
                    else "要忘掉哪一条？说个关键词，或者先问「你记得我什么」。"
                ),
                "action": "memory_forget_none",
                "choices": _memory_choices(facts),
                "data": {"memories": facts},
            }
        if len(hits) > 1:
            return {
                "summary": f"有 {len(hits)} 条都对得上，要忘掉哪一条？",
                "action": "memory_forget_choose",
                "choices": _memory_choices(hits),
                "data": {"memories": hits},
            }
        removed = memory.forget(conn, user_id=user_id, key=hits[0]["key"])
        rest = memory.all_facts(conn, user_id=user_id, limit=50)
        return {
            "summary": (
                f"忘掉了「{hits[0]['key']}」这条记忆，以后不再拿它当默认。"
                if removed.get("removed")
                else f"「{hits[0]['key']}」这条刚才已经被忘掉了。"
            ),
            "action": "memory_forget",
            "choices": _memory_choices(rest),
            "data": {"forgotten": hits[0]["key"], "memories": rest},
        }
    if _MEM_REMEMBER_RE.search(clean):
        body = _MEM_REMEMBER_RE.sub("", clean, count=1)
        for filler in ("了", "要", "我", "你", "：", ":", "，", ",", " "):
            body = body.replace(filler, "", 1) if body.startswith(filler) else body
        body = body.strip()
        if not body:
            return {
                "summary": "要我记住什么？比如「记住我不喜欢电话推销」「记住我常用招商银行那张卡」。",
                "action": "memory_remember_empty",
                "choices": [
                    {"label": "记住我不喜欢电话推销", "value": "记住我不喜欢电话推销"},
                    {"label": "你记得我什么", "value": "你记得我什么"},
                ],
                "data": {"memories": facts},
            }
        if not scope.is_money_text(body):
            return {
                "summary": scope.MEMORY_BOUNDARY,
                "action": "memory_out_of_scope",
                "choices": [
                    {"label": "记住我常用招商银行那张卡", "value": "记住我常用招商银行那张卡"},
                    {"label": "记住我记账喜欢按周看", "value": "记住我记账喜欢按周看"},
                    {"label": "你记得我什么", "value": "你记得我什么"},
                ],
                "data": {"memories": facts},
            }
        key = body[:_MEM_KEY_CHARS]
        kind = "preference" if any(word in body for word in _MEM_PREFERENCE_WORDS) else "fact"
        fact = memory.remember(
            conn,
            user_id=user_id,
            key=key,
            value=body[:_MEM_VALUE_CHARS],
            kind=kind,
            source="user_said",
        )
        return {
            "summary": (
                f"记住了：{_memory_line(fact)}。以后我按这个默认来；不想让我记着就说「忘掉「{fact['key']}」」。"
            ),
            "action": "memory_remember",
            "choices": _memory_choices(memory.all_facts(conn, user_id=user_id, limit=50)),
            "data": {"memory": fact},
        }
    return {
        "summary": "记忆这块我只会三件事：记下来、列出来、忘掉。说一句「记住…」「你记得我什么」「忘掉…」就行。",
        "action": "memory_hint",
        "choices": [
            {"label": "你记得我什么", "value": "你记得我什么"},
            {"label": "记住我不喜欢电话推销", "value": "记住我不喜欢电话推销"},
        ],
        "data": {"memories": facts},
    }


_MEM_CHOICE_HINT: Final[dict[str, str]] = {
    "label": "记住我不喜欢电话推销",
    "value": "记住我不喜欢电话推销",
}


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
        # 动词前缀要认「建个/设个」这种口语缩写，否则「帮我建个…的提醒」会留下「建个」；
        # 句首的「我想/我要」也只是语气，不是提醒内容。
        r"^(帮我|请|麻烦|给我|我想|我要|设置|设个|设一个|建一个|建个|建条|建立|定一个|定个"
        r"|添加|加一个|新增|安排|记一下|记个|记得|提醒我|提醒一下|提醒)+",
        r"(提醒我|提醒一下|提醒|记得|别忘了|别忘|叫我|到点|到时候)+",
        r"(每(?:个)?月\s*\d+\s*[号日])",
        r"(每(?:周|星期)[一二三四五六日天])",
        r"(每天|每日)",
        # 时段词必须和钟点一起消掉，不然「明天早上 8 点喝水」会剩一个「早上」。
        r"((?:凌晨|早上|上午|中午|下午|傍晚|晚上|今晚|今早|明早|明晚)?\s*\d{1,2}\s*[点:：]\s*\d{0,2}\s*分?)",
        r"(明天|后天|大后天|今天|今晚|今早|明早|明晚|下周[一二三四五六日天]|\d{4}-\d{1,2}-\d{1,2})",
        r"(闹钟|铃声)",
    )
    for _ in range(4):
        before = title
        for pattern in patterns:
            title = re.sub(pattern, "", title)
        if title == before:
            break
    # 没带钟点的时段词（「提醒我明天早上喝水」）也只是时间信息，不是要做的事。
    title = re.sub(
        r"^(凌晨|早上|上午|中午|下午|傍晚|晚上|今晚|今早|明早|明晚)", "",
        title.strip(" ，。；;、:：-—"),
    )
    title = re.sub(r"[的了吧呀啊]$", "", title.strip(" ，。；;、:：-—"))
    return title.strip()[:40] or "你安排的事"


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
    # 不变量：提醒是「资金时间轴」上的一件事，必须能说出它盯的是哪笔钱。
    # 没有锚点（还款/扣费/预约/预算/储蓄/额度）就不建 —— 见 app/scope.py。
    anchor = scope.reminder_anchor(text)
    if not anchor:
        raise ValidationError(scope.REMINDER_BOUNDARY)
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
        "anchor": anchor,
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
        meta={"source": "assist", "reminder": True, "repeat": repeat, "anchor": anchor},
    )
    label = reminder_time.label(repeat, hour, minute, day, weekday)
    return {
        "ok": True,
        "plan": plan,
        "due_ms": int(due_ms),
        "due_text": reminder_time.time_text(due_ms),
        "repeat": repeat,
        "repeat_label": label,
        "anchor": anchor,
        "anchor_label": scope.anchor_label(anchor),
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
        "SELECT p.*, s.payload_json AS step_payload, s.due_ms AS step_due_ms,"
        " s.state AS step_state FROM plans p"
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
        # due/state 是步骤级的，plan_view 里没有；聊天里的「下次 X」就靠这两个字段。
        due_ms = int(row["step_due_ms"] or 0)
        view["due_ms"] = due_ms
        view["due_text"] = reminder_time.time_text(due_ms) if due_ms else ""
        view["state"] = str(row["step_state"] or "")
        view["anchor"] = str(payload.get("anchor") or "")
        view["anchor_label"] = scope.anchor_label(view["anchor"])
        view["repeat_label"] = reminder_time.label(
            str(view["repeat"]),
            int(payload.get("hour") or 9),
            int(payload.get("minute") or 0),
            int(payload["day"]) if payload.get("day") is not None else None,
            int(payload["weekday"]) if payload.get("weekday") is not None else None,
        )
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
                # 这条提醒盯的是哪笔钱（还款日 / 会员扣费 / 预约转账 / 预算 / 储蓄目标 / 额度），
                # 页面拿它当标签显示；老提醒没有这个字段就留空。
                "anchor": str(payload.get("anchor") or ""),
                "anchor_label": scope.anchor_label(str(payload.get("anchor") or "")),
            }
        )
    return items


def _stamp_text(ms: Any) -> str:
    """把毫秒时间戳写成「10-26 08:00」：同名计划/提醒只能靠时间与编号区分。"""
    try:
        value = int(ms or 0)
    except (TypeError, ValueError):
        return ""
    if value <= 0:
        return ""
    return time.strftime("%m-%d %H:%M", time.localtime(value / 1000))


def _reminder_cancel_choices(reminders: list[dict[str, Any]]) -> list[dict[str, str]]:
    """每条提醒一个「取消」chip，最多 8 个。

    同名的提醒（连着说几句「提醒我喝水」都建得出来）如果只给标题，几个按钮长得
    一模一样、点哪个全看后端挑了第一条——所以同名时改用计划号做 value，并把
    「下次什么时候」写进 label，让用户能分清点的是哪一条。
    """
    counts: dict[str, int] = {}
    for plan in reminders:
        title = str(plan["title"])
        counts[title] = counts.get(title, 0) + 1
    options: list[dict[str, str]] = []
    for plan in reminders:
        title = str(plan["title"])
        plan_id = str(plan.get("plan_id") or "")
        when = str(plan.get("due_text") or "")
        if counts.get(title, 0) > 1 and plan_id:
            label = f"取消「{title}」（{when}）" if when else f"取消「{title}」（{plan_id}）"
            options.append({"label": label, "value": f"取消提醒 {plan_id}"})
        else:
            options.append({"label": f"取消「{title}」", "value": f"取消提醒 {title}"})
        if len(options) >= 8:
            break
    return options


def _plan_cancel_choices(plans_: list[dict[str, Any]]) -> list[dict[str, str]]:
    """同名计划同样要能分辨：label 带日期与计划号，value 直接用计划号。"""
    counts: dict[str, int] = {}
    for plan in plans_:
        title = str(plan.get("title") or "")
        counts[title] = counts.get(title, 0) + 1
    options: list[dict[str, str]] = []
    for plan in plans_:
        choice = _plan_choice(plan)
        if choice is None:
            continue
        title = str(plan.get("title") or "")
        plan_id = str(plan.get("plan_id") or "")
        if counts.get(title, 0) > 1 and plan_id:
            when = _stamp_text(plan.get("updated_ms") or plan.get("created_ms"))
            tail = f"{when} · {plan_id}" if when else plan_id
            options.append({"label": f"结束「{title}」（{tail}）", "value": f"取消计划 {plan_id}"})
        else:
            options.append(choice)
        if len(options) >= 8:
            break
    return options


def _handle_reminder(conn, text: str, user_id: str) -> dict[str, Any]:
    clean = _clean(text)
    reminders = _active_reminders(conn, user_id)

    if (
        re.search(r"(取消|停掉|结束|删掉|去掉|关掉|不要).{0,10}提醒", clean)
        or re.search(r"提醒.{0,10}(取消|停掉|结束|删掉|去掉|关掉|不用了)", clean)
        or clean.startswith("取消这条提醒")
    ):
        # 动词可能在「提醒」前面（取消提醒 开会），也可能在后面（把提醒结束掉）；
        # 只认一种语序会把「结束掉」当成要新建一个叫「结束掉」的提醒。
        wanted = re.sub(r"^(请|帮我|麻烦|把|将)\s*", "", clean)
        wanted = re.sub(
            r"(取消|停掉|结束|删掉|去掉|关掉|不要|不用了|这条|这个|那个|提醒|一下|掉|吧|了|的)",
            "",
            wanted,
        ).strip(" 「」\"'，。！？,.!?")
        # 点 chip 发回来的可能带计划号（同名提醒只能靠编号分清），先按编号认
        by_id = [p for p in reminders if str(p.get("plan_id") or "") and str(p["plan_id"]) in clean]
        matches = by_id or [p for p in reminders if wanted and wanted in str(p["title"])]
        if not matches and len(reminders) == 1:
            matches = reminders
        if len(matches) > 1:
            return {
                "intent": "assist",
                "action": "reminder_cancel_choose",
                "summary": f"有 {len(matches)} 条提醒都对得上，要取消哪一条？",
                "choices": _reminder_cancel_choices(matches),
                "data": {"reminders": matches},
            }
        if not matches and reminders:
            return {
                "intent": "assist",
                "action": "reminder_cancel_choose",
                "summary": "你有不止一个提醒，要取消哪一个？",
                "choices": _reminder_cancel_choices(reminders),
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
        # 同名提醒（比如连着建了几条「喝水」）在列表里各占一行会显得像 bug：
        # 按标题归并，报「同名 N 条」并把最早那次当成「下次」。
        counts: dict[str, int] = {}
        soonest: dict[str, dict[str, Any]] = {}
        for item in reminders:
            title = str(item["title"])
            counts[title] = counts.get(title, 0) + 1
            current = soonest.get(title)
            if current is None or int(item.get("due_ms") or 0) < int(current.get("due_ms") or 0):
                soonest[title] = item
        lines = []
        for title, item in list(soonest.items())[:5]:
            extra = f"（同名 {counts[title]} 条）" if counts[title] > 1 else ""
            anchor = str(item.get("anchor_label") or "")
            lines.append(
                f"「{title}」{anchor + ' · ' if anchor else ''}下次 {item.get('due_text') or '—'}{extra}"
            )
        return {
            "intent": "assist",
            "action": "reminder_list",
            "summary": f"你有 {len(reminders)} 条提醒：" + "；".join(lines) + "。",
            # 列表本身也要给动作：只报「你有这些提醒」等于让用户自己再打一句。
            "choices": _reminder_cancel_choices(reminders),
            "data": {"reminders": reminders},
        }

    anchor = scope.reminder_anchor(clean)
    if not anchor:
        return {
            "intent": "assist",
            "action": "reminder_anchor_needed",
            "summary": scope.REMINDER_BOUNDARY,
            "choices": scope.anchor_choices(),
            "data": {"anchor": ""},
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
    anchor_text = created.get("anchor_label") or scope.anchor_label(str(created.get("anchor") or ""))
    anchor_part = f"（{anchor_text}）" if anchor_text else ""
    # 一次性提醒只有「09:00」这种时间标签，念出来像半句话；直接用具体日期。
    when_part = f"下一次 {when}" if repeat["repeat"] != "none" else when
    summary = f"已建好提醒{anchor_part}：{title}，{when_part}。"
    if repeat["repeat"] != "none":
        summary += f"重复规则：{label}，会一直重复到你取消。"
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



# ------------------------------------------------------------------ 计划的列出与结束


def _plan_line(plan: dict[str, Any]) -> str:
    steps = plan.get("steps") or []
    done = sum(1 for item in steps if str(item.get("state")) in ("done", "skipped"))
    status = PLAN_STATUS_LABELS.get(str(plan.get("status")), str(plan.get("status")))
    nxt = next((item for item in steps if str(item.get("state")) == "pending"), None)
    tail = ""
    if nxt is not None:
        tail = "，下一步 {} {}".format(
            str(nxt.get("title") or "待办"), str(nxt.get("due_text") or "")
        ).strip()
    return "「{}」（{}，{}/{} 步{}）".format(
        str(plan.get("title") or "计划"), status, done, len(steps), tail
    )


def _plan_choice(plan: dict[str, Any]) -> dict[str, str] | None:
    """提醒和储蓄目标各有自己的结束动词，别混进「取消计划」这条路径。"""
    title = str(plan.get("title") or "")
    if title.startswith("提醒："):
        return {"label": f"结束提醒「{title[3:]}」", "value": f"取消提醒 {title[3:]}"}
    if title.startswith("储蓄目标："):
        return None
    return {"label": f"结束「{title}」", "value": f"取消计划 {title}"}


def _real_plans_first(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """真正的计划排在提醒前面：提醒通常几十条，先占满 chip 就把「结束这份生日计划」挤没了。"""
    return sorted(
        items,
        key=lambda p: (str(p.get("title") or "").startswith("提醒："), -int(p.get("updated_ms") or 0)),
    )


def _handle_plan(conn, text: str, user_id: str) -> dict[str, Any]:
    clean = _clean(text)
    all_plans = plans.list_plans(conn, user_id=user_id, limit=20)
    live = [p for p in all_plans if str(p.get("status")) in ("active", "paused")]

    if _PLAN_CANCEL_RE.search(clean):
        key = re.sub(r"[\s，。！？,.!?、:：]", "", clean)
        for word in (
            "帮我", "请", "麻烦", "把", "那个", "这个", "计划", "取消", "停掉",
            "结束", "删掉", "放弃", "终止", "不想要", "不要了", "一下", "掉", "吧", "了", "的",
        ):
            key = key.replace(word, "")
        def norm(value: str) -> str:
            return re.sub(r"[\s，。！？,.!?、:：]", "", value)

        # 同名计划（「API 测试」建过好几份）只能靠计划号认：chip 的 value 会带编号
        by_id = [p for p in live if str(p.get("plan_id") or "") and str(p["plan_id"]) in clean]
        matches = by_id or [p for p in live if key and key in norm(str(p.get("title") or ""))]
        if not matches and len(live) == 1:
            matches = live
        if len(matches) > 1:
            return {
                "intent": "assist",
                "action": "plan_cancel_choose",
                "summary": f"有 {len(matches)} 个计划都对得上，要结束哪一个？结束只是停掉后面没做的事，账户里的钱一分没动。",
                "choices": _plan_cancel_choices(_real_plans_first(matches)),
                "data": {"plans": matches},
            }
        if not matches and live:
            return {
                "intent": "assist",
                "action": "plan_cancel_choose",
                "summary": "你有不止一个计划，要结束哪一个？结束只是停掉后面没做的事，账户里的钱一分没动。",
                "choices": _plan_cancel_choices(_real_plans_first(live)),
                "data": {"plans": live},
            }
        if not matches:
            return {
                "intent": "assist",
                "action": "plan_cancel_none",
                "summary": "现在没有进行中的计划，没什么可结束的。",
                "choices": [
                    {"label": "我有哪些提醒", "value": "我有哪些提醒"},
                    {"label": "我有哪些储蓄目标", "value": "我有哪些储蓄目标"},
                ],
                "data": {},
            }
        plan = matches[0]
        plans.update(conn, str(plan["plan_id"]), user_id=user_id, status="cancelled")
        return {
            "intent": "assist",
            "action": "plan_cancel",
            "summary": f"已结束计划「{plan['title']}」。计划里没做的事不会再提醒你，账户里的钱一分没动。",
            "choices": [{"label": "看看还剩下哪些计划", "value": "我的计划列表"}],
            "data": {"plan": plan},
        }

    if not all_plans:
        return {
            "intent": "assist",
            "action": "plan_list",
            "summary": "现在还没有任何计划。可以让我「提醒我明天上午 9 点开会」，或者「帮我建一个存钱目标」。",
            "choices": [
                {"label": "建一个提醒", "value": "提醒我明天上午 9 点开会"},
                {"label": "建一个储蓄目标", "value": "帮我建一个存钱目标"},
                {"label": "我有哪些提醒", "value": "我有哪些提醒"},
            ],
            "data": {"plans": []},
        }
    lines = [_plan_line(p) for p in all_plans[:5]]
    head = f"你有 {len(all_plans)} 个计划"
    if len(all_plans) > len(lines):
        head += f"，先列最近 {len(lines)} 个"
    choices = _plan_cancel_choices(_real_plans_first(live))
    if not choices:
        choices = [{"label": "建一个提醒", "value": "提醒我明天上午 9 点开会"}]
    return {
        "intent": "assist",
        "action": "plan_list",
        "summary": head + "：" + "；".join(lines) + "。",
        "choices": choices,
        "data": {"plans": all_plans},
    }


# ------------------------------------------------------------------ 疑似订阅


def _primary_account_id(conn) -> str:
    """演示环境默认那张储蓄卡；没有卡返回空串，由调用方给出诚实空态。"""
    row = conn.execute(
        "SELECT id FROM accounts"
        " ORDER BY CASE type WHEN 'debit' THEN 0 ELSE 1 END, id LIMIT 1"
    ).fetchone()
    return str(row["id"]) if row is not None else ""


def _sub_choices(merchant: str) -> list[dict[str, str]]:
    return [
        {"label": "不是我订的", "value": f"「{merchant}」不是我订的"},
        {"label": "确实是我订的", "value": f"「{merchant}」确实是我订的"},
        {"label": "看看别的疑似订阅", "value": "我有哪些疑似订阅"},
    ]


def _verdict_of(clean: str) -> str | None:
    if _SUB_NOT_RE.search(clean):
        return subscriptions.VERDICT_NOT_SUBSCRIPTION
    if _SUB_YES_RE.search(clean):
        return subscriptions.VERDICT_CONFIRMED
    return None


def _match_merchant(clean: str, names: list[str]) -> str:
    """从原话里认出商户名：长名字优先，避免「百度」吃掉「百度网盘」。"""
    hits = [name for name in names if name and name in clean]
    if not hits:
        return ""
    hits.sort(key=len, reverse=True)
    return hits[0]


def _handle_subscription(conn, text: str, user_id: str) -> dict[str, Any]:
    """逐笔核实一条疑似订阅，或在聊天里直接给它定性。

    和业务管理页的按钮同源：两个入口都只写 subscription_verdicts 这一行结论，
    不动钱、不建计划、不替用户退订。区别只是这里会把每一笔流水的原文念出来，
    让用户看得见「凭什么说它像订阅」。
    """
    clean = _clean(text)
    account_id = _primary_account_id(conn)
    if not account_id:
        return {
            "intent": "assist",
            "action": "subscription_check",
            "summary": "这个演示账户里还没有卡，没有流水可核对。",
            "choices": [{"label": "看看总体情况", "value": "看看我的总览"}],
            "data": {"candidates": []},
        }

    inferred = subscriptions.detect_recurring(conn, account_id, user_id=user_id)
    candidates = list(inferred.get("items") or [])
    names = [str(item.get("merchant") or "") for item in candidates]
    seen_names = set(names)
    for row in subscriptions.merchants_with_transactions(conn, account_id):
        name = str(row.get("merchant") or "")
        if name and name not in seen_names:
            names.append(name)
            seen_names.add(name)

    merchant = _match_merchant(clean, names)
    verdict = _verdict_of(clean)
    is_candidate = merchant in {str(item.get("merchant") or "") for item in candidates}

    # 只说了「不是我订的」而没带商户名：把上下文补上（只有一条候选时才敢补）。
    if verdict and not merchant and len(candidates) == 1:
        merchant = str(candidates[0].get("merchant") or "")
        is_candidate = True

    if verdict and not merchant:
        if not candidates:
            return {
                "intent": "assist",
                "action": "subscription_check",
                "summary": "账上没找到像订阅的周期性扣款，这句话我没记到任何一条上——你指的是哪个商户？",
                "choices": [
                    {"label": "看看这个月的支出", "value": "看看我这个月花了多少"},
                ],
                "data": {"candidates": []},
            }
        return {
            "intent": "assist",
            "action": "subscription_verdict_choose",
            "summary": "你有好几条疑似订阅，这句结论要记在哪一条上？",
            "choices": [
                {"label": f"「{item['merchant']}」", "value": f"「{item['merchant']}」" + ("不是我订的" if verdict == subscriptions.VERDICT_NOT_SUBSCRIPTION else "确实是我订的")}
                for item in candidates[:8]
            ],
            "data": {"candidates": candidates},
        }

    # 认不出商户：把候选列出来让用户点（每条候选都是一个商户，能点进去逐笔看）。
    if not merchant:
        if not candidates:
            return {
                "intent": "assist",
                "action": "subscription_check",
                "summary": "近 6 个月里我没找到符合「每月/每年、金额基本一致」的扣款。没有疑似订阅，也就没有要核实的对象——这不是我核实过了，是确实没有可核实的记录。",
                "choices": [
                    {"label": "看看这个月的支出", "value": "看看我这个月花了多少"},
                    {"label": "看看我的账单", "value": "帮我看看账单"},
                ],
                "data": {"candidates": []},
            }
        return {
            "intent": "assist",
            "action": "subscription_check",
            "summary": f"现在有 {len(candidates)} 条疑似订阅，要逐笔核对哪一条？点一条，我把每一笔的日期、金额、备注原文念给你听。",
            "choices": [
                {"label": f"核对「{item['merchant']}」（{item.get('occurrences')} 笔）", "value": f"帮我核实一下「{item['merchant']}」这几笔是不是订阅"}
                for item in candidates[:8]
            ],
            "data": {"candidates": candidates},
        }

    # 用户下了结论：只写结论这一行，其余什么都不动。
    if verdict:
        current = subscriptions.verdicts(conn, user_id).get(merchant)
        # 同一句话重复说：不再重写一遍结论（审计里也只有一条），但仍给下一步。
        if current is not None and current.get("verdict") == verdict:
            return {
                "intent": "assist",
                "action": "subscription_check",
                "summary": (
                    f"「{merchant}」你已经定过性了（{current.get('verdict_label')}），"
                    "这次没有改；同一结论重复说不会重复记账。要不要我把它最近几笔念给你听？"
                ),
                "choices": [
                    {"label": "逐笔看看这几笔", "value": f"帮我核实一下「{merchant}」这几笔是不是订阅"},
                    {"label": "看看别的疑似订阅", "value": "我有哪些疑似订阅"},
                ],
                "data": {"merchant": merchant, "verdict": current},
            }
        # 从没被推断成订阅、也没定过性的商户：不硬写一条结论进去。
        if current is None and not is_candidate:
            return {
                "intent": "assist",
                "action": "subscription_check",
                "summary": f"「{merchant}」并没有被我推断成疑似订阅（间隔或金额不规律），所以不用定性。要不要我把它最近几笔念给你听？",
                "choices": [
                    {"label": "逐笔看看这几笔", "value": f"帮我核实一下「{merchant}」这几笔是不是订阅"},
                    {"label": "看看这个月的支出", "value": "看看我这个月花了多少"},
                ],
                "data": {"merchant": merchant},
            }
        recorded = subscriptions.record_verdict(conn, user_id=user_id, merchant=merchant, verdict=verdict)
        label = subscriptions.VERDICT_LABELS.get(verdict, verdict)
        if verdict == subscriptions.VERDICT_CONFIRMED:
            extra = "这条已经从待办里拿掉了。我没有银行商户侧的接口，所以不会替你续费、也不会替你在商户那边退订——要不要续、怎么续，得你自己在商户那里办。"
        else:
            extra = "这条已经从待办里拿掉了，以后不再拿这几笔打扰你；点错了也可以在业务管理页点「重新当成疑似」收回。"
        return {
            "intent": "assist",
            "action": "subscription_verdict",
            "summary": f"已记下：「{merchant}」{label}。{extra}",
            "choices": [
                {"label": "逐笔看看这几笔", "value": f"帮我核实一下「{merchant}」这几笔是不是订阅"},
                {"label": "看看别的疑似订阅", "value": "我有哪些疑似订阅"},
            ],
            "data": {"merchant": merchant, "verdict": recorded},
        }

    # 逐笔核实：事实（每一笔）在前，推断（像不像订阅）在后。
    rows = subscriptions.merchant_transactions(conn, account_id, merchant)
    if not rows:
        return {
            "intent": "assist",
            "action": "subscription_check",
            "summary": f"近 6 个月账上没有「{merchant}」的支出记录，可能是更早以前的扣款，或者是别的渠道付的。",
            "choices": [{"label": "看看我的账单", "value": "帮我看看账单"}],
            "data": {"merchant": merchant, "transactions": []},
        }
    lines: list[str] = []
    for index, row in enumerate(rows, start=1):
        note = str(row.get("note") or "").strip()
        tail = f" · {note}" if note else ""
        lines.append(
            f"{index}) {row['time_text']} · {row['amount_yuan']} 元 · {row['category']}"
            f" · {row['channel_label']}{tail}"
        )
    candidate = next(
        (item for item in candidates if str(item.get("merchant") or "") == merchant), None
    )
    settled = subscriptions.verdicts(conn, user_id).get(merchant)
    head = f"「{merchant}」近 6 个月一共 {len(rows)} 笔，逐笔是这样："
    body = "；".join(lines) + "。"
    if candidate is not None:
        judge = (
            f"推断：{candidate.get('period_label')}、间隔 {candidate.get('interval_days_min')}—"
            f"{candidate.get('interval_days_max')} 天、把握度{candidate.get('confidence_label')}。"
            f"{candidate.get('uncertainty')}"
        )
    elif settled is not None:
        judge = (
            f"你之前已经给它定过性（{settled.get('verdict_label')}），所以它不再挂在待办里；"
            "这些流水本身还是能一笔笔看的。要反悔可以在业务管理页点「重新当成疑似」。"
        )
    else:
        judge = "推断：这几笔不满足「每月/每年、金额基本一致」，所以我没把它算成疑似订阅。"
    boundary = (
        "这些是账上流水的原文（分类和备注是记账时写的），不是商户订单。"
        "我没有银行官方接口，也拿不到商户侧订单，所以「是不是订阅」只能由你定："
    )
    return {
        "intent": "assist",
        "action": "subscription_check",
        "summary": f"{head}{body}{judge}{boundary}",
        "choices": _sub_choices(merchant),
        "data": {"merchant": merchant, "transactions": rows, "candidate": candidate},
    }


def handle(conn, text: str, *, session_id: str | None = None) -> dict[str, Any] | None:
    """识别并执行；没命中返回 None，调用方继续走模型解析。"""
    kind = detect(text)
    if kind is None:
        return None
    user_id = proactive.default_user_id(conn)
    if kind == "reminder":
        return _handle_reminder(conn, text, user_id)
    if kind == "plan":
        return _handle_plan(conn, text, user_id)
    if kind == "subscription":
        return _handle_subscription(conn, text, user_id)
    if kind == "memory":
        return _handle_memory(conn, text, user_id)
    return _handle_savings(conn, text, user_id)


__all__ = ["detect", "handle", "create_reminder", "active_reminders"]
