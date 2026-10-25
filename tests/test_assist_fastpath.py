"""识别即执行快路（app/planner/assist.py）：储蓄目标与提醒的自然语言入口。

这一组守的是「用户在聊天里说一句话就能建成/结束提醒与储蓄」，而不是只会反问：
1. 提醒、储蓄各走各的分支，查询类（查余额）不抢真正的解析；
2. 建出来的提醒落库，页面 GET /api/plans 的 reminders 与聊天读到同一份数据；
3. 取消提醒改的是计划状态，不动钱；
4. 记存款只改 savings_goals.saved_fen，账户余额一分不动。
"""

from __future__ import annotations

import pytest

import app.planner.assist as assist
import app.proactive as proactive
from app import db

USER_ID = "u_9f2c41ab"


@pytest.fixture()
def bank(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DEFAULT_DB_PATH", tmp_path / "bank.db")
    conn = db.connect()
    db.init_db(conn)
    from scripts import seed

    seed.seed(conn)
    yield conn
    conn.close()


def _balance(conn) -> int:
    return int(
        conn.execute("SELECT balance_fen FROM accounts WHERE type='debit'").fetchone()["balance_fen"]
    )


def _plan_status(conn, plan_id: str) -> str:
    row = conn.execute("SELECT status FROM plans WHERE id = ?", (plan_id,)).fetchone()
    return str(row["status"]) if row else ""


# --------------------------------------------------------------- 意图判定


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("提醒我明天上午 9 点开会", "reminder"),
        ("每周三提醒我交房租", "reminder"),
        ("记得帮我提醒忘带卡", "reminder"),
        ("我存了 500 元", "savings"),
        ("我想攒钱买电脑", "savings"),
    ],
)
def test_detect_routes_to_the_right_fast_path(text: str, expected: str) -> None:
    assert assist.detect(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "查一下余额",
        "",  # 空串不能误判成任何一条快路
        "提醒我还信用卡还款",  # 带「还款」的提醒要留给钱相关流程，不能当纯提醒
        # 「储蓄卡」是借记卡，不是储蓄目标：查余额/账单绝不能被储蓄快路吃掉
        "查一下我储蓄卡尾号1234的余额",
        "我的储蓄卡余额是多少",
        "帮我看看信用卡账单",
        "看看我这个月花了多少",
        "转账 100 元给张三",
    ],
)
def test_detect_leaves_money_and_plain_questions_alone(text: str) -> None:
    assert assist.detect(text) is None


def test_handle_returns_none_when_nothing_matches(bank) -> None:
    assert assist.handle(bank, "查一下余额") is None


# ----------------------------------------------------------------- 提醒


def test_create_once_reminder_lands_in_the_database(bank) -> None:
    created = assist.create_reminder(bank, text="提醒我明天上午 9 点开会", user_id=USER_ID)
    assert created["ok"] is True
    assert created["repeat"] == "none"
    assert created["due_ms"] > 0
    assert created["due_text"].endswith("09:00")

    items = assist.active_reminders(bank, USER_ID)
    assert [item["plan_id"] for item in items] == [created["plan"]["plan_id"]]
    assert items[0]["state"] == "pending"
    assert items[0]["title"] == "提醒我明天上午 9 点开会"


def test_repeating_reminder_computes_next_occurrence_and_label(bank) -> None:
    created = assist.create_reminder(
        bank, text="提醒我每天喝水", user_id=USER_ID, repeat="daily", hour=8, minute=30
    )
    assert created["repeat"] == "daily"
    assert created["repeat_label"] == "每天 08:30"
    item = assist.active_reminders(bank, USER_ID)[0]
    assert item["repeat_label"] == "每天 08:30"
    assert item["due_text"].endswith("08:30")


def test_chat_creates_a_reminder_and_offers_the_next_step(bank) -> None:
    result = assist.handle(bank, "提醒我明天上午 9 点开会")
    assert result is not None
    assert result["intent"] == "assist"
    assert result["summary"].strip()
    # 反问/完成都要给可点的选项，前端才有东西可渲染
    assert all({"label", "value"} <= set(choice) for choice in result["choices"])
    assert len(assist.active_reminders(bank, USER_ID)) == 1


def test_cancel_by_matching_title_does_not_touch_money(bank) -> None:
    assist.handle(bank, "提醒我明天上午 9 点开会")
    items = assist.active_reminders(bank, USER_ID)
    assert len(items) == 1
    plan_id = items[0]["plan_id"]
    before = _balance(bank)
    result = assist.handle(bank, "取消提醒 提醒我明天上午 9 点开会")
    assert result["action"] == "reminder_cancel"
    assert assist.active_reminders(bank, USER_ID) == []
    assert _plan_status(bank, plan_id) == "cancelled"
    assert _balance(bank) == before


def test_cancel_with_several_reminders_asks_which_one(bank) -> None:
    assist.create_reminder(bank, text="提醒我明天上午 9 点开会", user_id=USER_ID)
    assist.create_reminder(bank, text="提醒我每天喝水", user_id=USER_ID, repeat="daily")
    result = assist.handle(bank, "取消提醒")
    assert result["action"] == "reminder_cancel_choose"
    labels = [choice["label"] for choice in result["choices"]]
    # 两条标题不同，按钮就按标题给；点哪个取消哪个（不能取消错的那条）
    assert any("提醒我每天喝水" in label for label in labels)
    pick = [c["value"] for c in result["choices"] if "提醒我每天喝水" in c["label"]][0]
    done = assist.handle(bank, pick)
    assert done["action"] == "reminder_cancel"
    left = assist.active_reminders(bank, USER_ID)
    assert [item["title"] for item in left] == ["提醒我明天上午 9 点开会"]


def test_same_title_reminders_get_distinguishable_choices(bank) -> None:
    """连着说两句「提醒我喝水」会建出两条同名提醒：按钮必须能分清点的是哪一条。"""
    assist.create_reminder(bank, text="提醒我喝水", user_id=USER_ID, repeat="daily", hour=8, minute=0)
    assist.create_reminder(bank, text="提醒我喝水", user_id=USER_ID, repeat="daily", hour=9, minute=0)
    result = assist.handle(bank, "我有哪些提醒")
    assert result["action"] == "reminder_list"
    values = [choice["value"] for choice in result["choices"]]
    assert len(values) == 2
    assert len(set(values)) == 2
    # 两条都带「下次什么时候」，否则两个按钮长得一模一样
    assert all("（" in choice["label"] for choice in result["choices"])
    before = {item["plan_id"] for item in assist.active_reminders(bank, USER_ID)}
    done = assist.handle(bank, values[0])
    assert done["action"] == "reminder_cancel"
    after = {item["plan_id"] for item in assist.active_reminders(bank, USER_ID)}
    assert len(after) == 1 and after < before


def test_list_reminders_reports_next_due(bank) -> None:
    assist.create_reminder(bank, text="提醒我每天喝水", user_id=USER_ID, repeat="daily")
    result = assist.handle(bank, "我有哪些提醒")
    assert result["action"] == "reminder_list"
    assert "下次" in result["summary"]


# ----------------------------------------------------------------- 储蓄


def _goal(bank, *, target_fen: int = 600_000, saved_fen: int = 0) -> str:
    goal_id = "sg_test01"
    stamp = 1_792_000_000_000
    bank.execute(
        "INSERT INTO savings_goals (id, user_id, name, target_fen, target_date, saved_fen,"
        " status, created_ms, updated_ms, last_monitored_ms)"
        " VALUES (?, ?, '半年存 6000 元', ?, '2027-04-30', ?, 'active', ?, ?, 0)",
        (goal_id, USER_ID, target_fen, saved_fen, stamp, stamp),
    )
    return goal_id


def test_record_savings_updates_goal_only_and_never_moves_money(bank) -> None:
    goal_id = _goal(bank)
    before = _balance(bank)
    result = assist.handle(bank, "记录存了 200 元")
    assert result["action"] == "savings_record"
    row = bank.execute("SELECT saved_fen FROM savings_goals WHERE id = ?", (goal_id,)).fetchone()
    assert int(row["saved_fen"]) == 20_000
    assert _balance(bank) == before


def test_record_savings_without_goal_does_not_fake_one(bank) -> None:
    before = _balance(bank)
    result = assist.handle(bank, "记录存了 200 元")
    assert result["action"] == "savings_record_none"
    # 没有目标就不能记账——更不能顺手建一个目标糊弄过去
    assert bank.execute("SELECT COUNT(*) AS n FROM savings_goals").fetchone()["n"] == 0
    assert _balance(bank) == before


def test_cancel_savings_goal_keeps_the_money(bank) -> None:
    goal_id = _goal(bank)
    before = _balance(bank)
    result = assist.handle(bank, "取消储蓄计划 半年存 6000 元")
    assert result["action"] == "savings_cancel"
    row = bank.execute("SELECT status FROM savings_goals WHERE id = ?", (goal_id,)).fetchone()
    assert str(row["status"]) == "cancelled"
    assert _balance(bank) == before
    assert proactive.default_user_id(bank) == USER_ID


def test_same_title_plans_get_distinguishable_choices(bank) -> None:
    """同名计划（「API 测试」建过好几份）不能只靠标题结束：chip 要带编号，点了只结束那一份。"""
    import time

    from app import plans as plans_mod

    stamp = int(time.time() * 1000)
    ids = []
    for offset in (0, 60_000):
        plan = plans_mod.create(
            bank,
            user_id=USER_ID,
            title="API 测试",
            steps=[{"kind": "reminder", "title": "确认余额够生日预算", "due_ms": stamp + 3_600_000 + offset}],
        )
        ids.append(plan["plan_id"])

    listed = assist.handle(bank, "我的计划列表")
    assert listed["action"] == "plan_list"
    choices = [c for c in listed["choices"] if c["label"].startswith("结束「API 测试」")]
    assert len(choices) == 2
    assert len({c["value"] for c in choices}) == 2
    assert all("（" in c["label"] for c in choices)

    done = assist.handle(bank, choices[0]["value"])
    assert done["action"] == "plan_cancel"
    statuses = {
        row["id"]: row["status"]
        for row in bank.execute("SELECT id, status FROM plans WHERE id IN (?, ?)", tuple(ids))
    }
    assert sorted(statuses.values()) == ["active", "cancelled"]
