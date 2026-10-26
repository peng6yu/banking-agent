"""缺槽位时给「可点选项」：问句对齐、快捷候选、上下文示例（第 5 层，只读工具元数据）。"""

from __future__ import annotations

import logging
import sqlite3
from typing import Any, Callable, Final

import app.proactive as proactive

from app import audit, clock, db
from app.planner import assist, executor, intent, payment_guard, recover, ticker
from app.tools.base import primary_account

log = logging.getLogger(__name__)

#: 往候选列表里塞一条（去重、最多 8 条由调用方兜）。别名只为让下面的小函数短一点。
_Add = Callable[[str, str], None]


_SLOT_QUESTION_HINTS: Final[tuple[tuple[str, tuple[str, ...]], ...]] = (
    ("payee_names", ("哪几个人", "参与", "和谁", "谁一起")),
    ("payee_name", ("给谁", "转给", "收款", "谁过生日", "送给谁", "哪个人", "谁来")),
    ("amount_fen", ("多少", "金额", "预算", "几块")),
    ("gift", ("送什么", "送啥", "礼物", "送点")),
    ("product_code", ("哪只", "哪个基金", "哪个产品", "产品代码", "哪款")),
    ("card_tail", ("哪张卡", "尾号", "哪张")),
    ("subscription_name", ("哪个订阅", "哪个商户", "哪家")),
    ("sub_name", ("哪个订阅", "哪个商户", "哪家")),
    ("merchant", ("哪个商户", "在哪", "哪家")),
    ("date_hint", ("哪天", "几号", "什么时候", "日期")),
    ("due_ms", ("什么时候", "哪天", "几号", "时间")),
    ("txn_ref", ("哪一笔", "哪条记录", "交易号")),
)


_QUICK_CHOICES: Final[tuple[tuple[str, str], ...]] = (
    ("查一下我的余额", "查一下我的余额"),
    ("看看我这个月的账单", "看看我这个月的账单"),
    ("我有哪些理财产品", "我有哪些理财产品"),
    ("我有哪些订阅", "我有哪些订阅"),
    ("我有哪些提醒", "我有哪些提醒"),
    ("我的计划列表", "我的计划列表"),
    ("我有哪些储蓄目标", "我有哪些储蓄目标"),
    ("我有哪些卡", "我有哪些卡"),
)


_INTENT_EXAMPLES: Final[dict[str, str]] = {
    "query_balance": "查一下我的余额",
    "transfer": "转 100 给李静",
    "bill_analysis": "看看我这个月的账单",
    "subscription": "我有哪些订阅",
    "birthday_plan": "帮我建个生日计划",
    "invest": "我有哪些理财产品",
    "card_manage": "我有哪些卡",
    "schedule_transfer": "下周一转 100 给李静",
    "split_bill": "我们三个人 AA 这顿饭",
    "reversal": "冲正刚才那笔转账",
}


def _focused_slots(parsed: intent.IntentResult) -> list[str]:
    """这一轮反问到底在问哪个槽位：先按问句原文精确对上标签，再按关键词兜底。"""
    missing = [str(item) for item in (parsed.missing or [])]
    if not missing:
        return []
    question = str(parsed.question or "").strip()
    if question:
        labels = dict(intent.MISSING_LABELS)
        labels.update(intent.INTENT_MISSING_LABELS.get(str(parsed.intent), {}))
        for slot in missing:
            if labels.get(slot) == question:
                return [slot]
        for slot, words in _SLOT_QUESTION_HINTS:
            if slot in missing and any(word in question for word in words):
                return [slot]
    return missing[:1]


def _broad_choices(parsed: intent.IntentResult) -> list[dict[str, str]]:
    """没听懂 / 置信度不够时的选项：一条照着说的例子 + 常用动作。"""
    example = _INTENT_EXAMPLES.get(str(parsed.intent))
    choices: list[dict[str, str]] = []
    if example:
        choices.append({"label": f"这样说：{example}", "value": example})
    for label, value in _QUICK_CHOICES:
        if value != example:
            choices.append({"label": label, "value": value})
    return choices[:8]


#: 每个槽位给什么候选：一个小函数一类，查不到的槽位就不进表（退回纯文字反问）。
#: 拆开的理由：以前这里是一条 100 多行的 if/elif，加一个槽位就要滚到底确认没漏。
def _amount_choices(add: _Add) -> None:
    for amount in ("100", "500", "1000", "2000"):
        add(f"{amount} 元", f"{amount} 元")


def _payees_choices(conn: sqlite3.Connection, add: _Add) -> None:
    rows = conn.execute("SELECT name, bank, card_tail FROM payees ORDER BY id LIMIT 8").fetchall()
    for row in rows:
        tail = str(row["card_tail"] or "")
        add(f"{row['name']}（{row['bank']} 尾号 {tail}）", str(row["name"]))


def _payee_names_choices(conn: sqlite3.Connection, add: _Add) -> None:
    rows = conn.execute("SELECT name FROM payees ORDER BY id LIMIT 8").fetchall()
    for row in rows:
        add(str(row["name"]), str(row["name"]))


def _card_choices(conn: sqlite3.Connection, user_id: str, add: _Add) -> None:
    rows = conn.execute(
        "SELECT c.tail, c.kind, c.status FROM cards c"
        " JOIN accounts a ON a.id = c.account_id WHERE a.user_id = ?"
        " ORDER BY CASE c.status WHEN 'active' THEN 0 ELSE 1 END, c.id",
        (user_id,),
    ).fetchall()
    kind_label = {"physical": "实体卡", "virtual": "虚拟卡"}
    for row in rows:
        tail = str(row["tail"])
        add(f"尾号 {tail}（{kind_label.get(str(row['kind']), str(row['kind']))}）", f"尾号{tail}")


def _product_choices(conn: sqlite3.Connection, user_id: str, add: _Add) -> None:
    rows = conn.execute(
        "SELECT p.name, p.code FROM fund_holdings h"
        " JOIN fund_products p ON p.code = h.product_code WHERE h.user_id = ?",
        (user_id,),
    ).fetchall()
    if not rows:
        rows = conn.execute(
            "SELECT name, code FROM fund_products ORDER BY risk_level, code LIMIT 6"
        ).fetchall()
    for row in rows:
        add(str(row["name"]), str(row["name"]))


def _gift_choices(add: _Add) -> None:
    # 生日送礼的 item 是自由文本；这里给几个常见答法，省得用户想措辞。
    for label, value in (
        ("鲜花和蛋糕", "鲜花和蛋糕"),
        ("只要鲜花", "鲜花"),
        ("只要蛋糕", "蛋糕"),
        ("一份小礼物", "小礼物"),
    ):
        add(label, value)


def _action_choices(parsed: intent.IntentResult, add: _Add) -> None:
    if str(parsed.intent) == "invest":
        for label, value in (("申购", "申购"), ("赎回", "赎回")):
            add(label, value)
    elif str(parsed.intent) == "card_manage":
        for label, value in (
            ("冻结这张卡", "冻结卡片"),
            ("解冻这张卡", "解冻卡片"),
            ("挂失这张卡", "挂失卡片"),
            ("调限额", "调整限额"),
        ):
            add(label, value)


def _subscription_choices(conn: sqlite3.Connection, account_id: str, add: _Add) -> None:
    rows = conn.execute(
        "SELECT merchant FROM subs WHERE account_id = ? AND status = 'active'"
        " ORDER BY merchant LIMIT 8",
        (account_id,),
    ).fetchall()
    for row in rows:
        add(str(row["merchant"]), f"停掉 {row['merchant']} 的订阅")


def _due_choices(add: _Add) -> None:
    for label, value in (
        ("明天 09:00", "明天 09:00"),
        ("下周一 09:00", "下周一 09:00"),
        ("下个月 1 号 09:00", "下个月 1 号 09:00"),
    ):
        add(label, value)


def _month_choices(add: _Add) -> None:
    for label, value in (("这个月", "这个月"), ("上个月", "上个月"), ("最近三个月", "最近三个月")):
        add(label, value)


def _choices_for(parsed: intent.IntentResult) -> list[dict[str, str]]:
    """反问时带上可点的选项：能查数据库的就不让用户手打。"""
    slots = _focused_slots(parsed)
    if not slots:
        return _broad_choices(parsed)
    choices: list[dict[str, str]] = []
    seen: set[str] = set()

    def add(label: str, value: str) -> None:
        value = str(value).strip()
        if value and value not in seen:
            seen.add(value)
            choices.append({"label": label, "value": value})

    try:
        with db.open_conn() as conn:
            user_id = proactive.default_user_id(conn)
            account_id = str(primary_account(conn)["id"])
            for slot in slots:
                _fill_slot(conn, slot, parsed, user_id, account_id, add)
    except Exception:  # noqa: BLE001 - 选项只是锦上添花，查不到就退回纯文字反问
        log.warning("api.chat.choices_failed", exc_info=True)
    return choices[:8]


def _fill_slot(
    conn: sqlite3.Connection,
    slot: str,
    parsed: intent.IntentResult,
    user_id: str,
    account_id: str,
    add: _Add,
) -> None:
    """把一个槽位的候选填进去；同一个槽位有多种叫法（别名）时在这里归一。"""
    if slot == "amount_fen":
        _amount_choices(add)
    elif slot == "payee_name":
        _payees_choices(conn, add)
    elif slot == "payee_names":
        _payee_names_choices(conn, add)
    elif slot == "card_tail":
        _card_choices(conn, user_id, add)
    elif slot == "product_code":
        _product_choices(conn, user_id, add)
    elif slot == "gift":
        _gift_choices(add)
    elif slot == "action":
        _action_choices(parsed, add)
    elif slot in ("subscription_name", "sub_name"):
        _subscription_choices(conn, account_id, add)
    elif slot in ("due_ms", "date_hint", "due_hint"):
        _due_choices(add)
    elif slot in ("month", "period"):
        _month_choices(add)
