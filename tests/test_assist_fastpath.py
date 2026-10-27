"""识别即执行快路（app/planner/assist.py）：储蓄目标与提醒的自然语言入口。

这一组守的是「用户在聊天里说一句话就能建成/结束提醒与储蓄」，而不是只会反问：
1. 提醒、储蓄各走各的分支，查询类（查余额）不抢真正的解析；
2. 建出来的提醒落库，页面 GET /api/plans 的 reminders 与聊天读到同一份数据；
3. 取消提醒改的是计划状态，不动钱；
4. 记存款只改 savings_goals.saved_fen，账户余额一分不动。
"""

from __future__ import annotations

import time

import pytest

import app.clock as clock
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
        ("提醒我明天上午 9 点会员续费", "reminder"),
        ("每周三提醒我房租到期", "reminder"),
        ("记得帮我提醒会员续费", "reminder"),
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
        # 有提醒词、但带收款人与金额：仍归转账链路，不能被提醒快路吞掉
        "提醒我给张三转账 100 元",
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
    created = assist.create_reminder(bank, text="提醒我明天上午 9 点会员续费", user_id=USER_ID)
    assert created["ok"] is True
    assert created["repeat"] == "none"
    assert created["due_ms"] > 0
    assert created["due_text"].endswith("09:00")

    items = assist.active_reminders(bank, USER_ID)
    assert [item["plan_id"] for item in items] == [created["plan"]["plan_id"]]
    assert items[0]["state"] == "pending"
    assert items[0]["title"] == "提醒我明天上午 9 点会员续费"


def test_repeating_reminder_computes_next_occurrence_and_label(bank) -> None:
    created = assist.create_reminder(
        bank, text="提醒我每天盯会员续费", user_id=USER_ID, repeat="daily", hour=8, minute=30
    )
    assert created["repeat"] == "daily"
    assert created["repeat_label"] == "每天 08:30"
    item = assist.active_reminders(bank, USER_ID)[0]
    assert item["repeat_label"] == "每天 08:30"
    assert item["due_text"].endswith("08:30")


def test_chat_creates_a_reminder_and_offers_the_next_step(bank) -> None:
    result = assist.handle(bank, "提醒我明天上午 9 点会员续费")
    assert result is not None
    assert result["intent"] == "assist"
    assert result["summary"].strip()
    # 反问/完成都要给可点的选项，前端才有东西可渲染
    assert all({"label", "value"} <= set(choice) for choice in result["choices"])
    assert len(assist.active_reminders(bank, USER_ID)) == 1


def test_cancel_by_matching_title_does_not_touch_money(bank) -> None:
    assist.handle(bank, "提醒我明天上午 9 点会员续费")
    items = assist.active_reminders(bank, USER_ID)
    assert len(items) == 1
    plan_id = items[0]["plan_id"]
    before = _balance(bank)
    result = assist.handle(bank, "取消提醒 提醒我明天上午 9 点会员续费")
    assert result["action"] == "reminder_cancel"
    assert assist.active_reminders(bank, USER_ID) == []
    assert _plan_status(bank, plan_id) == "cancelled"
    assert _balance(bank) == before


def test_cancel_with_several_reminders_asks_which_one(bank) -> None:
    assist.create_reminder(bank, text="提醒我明天上午 9 点会员续费", user_id=USER_ID)
    assist.create_reminder(bank, text="提醒我每天盯会员续费", user_id=USER_ID, repeat="daily")
    result = assist.handle(bank, "取消提醒")
    assert result["action"] == "reminder_cancel_choose"
    labels = [choice["label"] for choice in result["choices"]]
    # 两条标题不同，按钮就按标题给；点哪个取消哪个（不能取消错的那条）
    assert any("提醒我每天盯会员续费" in label for label in labels)
    pick = [c["value"] for c in result["choices"] if "提醒我每天盯会员续费" in c["label"]][0]
    done = assist.handle(bank, pick)
    assert done["action"] == "reminder_cancel"
    left = assist.active_reminders(bank, USER_ID)
    assert [item["title"] for item in left] == ["提醒我明天上午 9 点会员续费"]


def test_same_title_reminders_get_distinguishable_choices(bank) -> None:
    """连着说两句「提醒我会员续费」会建出两条同名提醒：按钮必须能分清点的是哪一条。"""
    assist.create_reminder(bank, text="提醒我会员续费", user_id=USER_ID, repeat="daily", hour=8, minute=0)
    assist.create_reminder(bank, text="提醒我会员续费", user_id=USER_ID, repeat="daily", hour=9, minute=0)
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
    assist.create_reminder(bank, text="提醒我每天盯会员续费", user_id=USER_ID, repeat="daily")
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


# --------------------------------------------------------------- 长期记忆快路


@pytest.mark.parametrize(
    "text",
    ["记住我不喜欢银行电话推销", "记一下我常用招行那张卡", "你记得我什么", "忘掉记忆「不喜欢银行电话推销」"],
)
def test_memory_phrases_route_to_the_memory_fast_path(text: str) -> None:
    assert assist.detect(text) == "memory"


def test_remember_list_and_forget_only_touch_the_memory_table(bank) -> None:
    """说一句就记住、问一句就列出、说忘掉就删——三步都不许动账户余额。"""
    import app.memory as memory

    before = _balance(bank)

    made = assist.handle(bank, "记住我不喜欢银行电话推销")
    assert made is not None and made["action"] == "memory_remember"
    facts = memory.all_facts(bank, user_id=proactive.default_user_id(bank))
    assert [fact["key"] for fact in facts] == ["不喜欢银行电话推销"]
    assert facts[0]["kind"] == "preference"

    listed = assist.handle(bank, "你记得我什么")
    assert listed is not None and listed["action"] == "memory_list"
    assert "不喜欢银行电话推销" in listed["summary"]
    assert listed["data"]["memories"]

    forgot = assist.handle(bank, "忘掉记忆「不喜欢银行电话推销」")
    assert forgot is not None and forgot["action"] == "memory_forget"
    assert memory.all_facts(bank, user_id=proactive.default_user_id(bank)) == []

    assert _balance(bank) == before


def test_forget_unknown_keeps_everything(bank) -> None:
    import app.memory as memory

    assist.handle(bank, "记住我周末不看理财")
    out = assist.handle(bank, "忘掉记忆「不存在的偏好」")

    assert out is not None and out["action"] == "memory_forget_none"
    assert [fact["key"] for fact in memory.all_facts(bank, user_id=proactive.default_user_id(bank))] == [
        "周末不看理财"
    ]


def test_reminder_still_wins_over_memory_markers(bank) -> None:
    """「提醒我…」里就算带「记」字，也仍然是提醒，不能被记忆快路抢走。"""
    out = assist.handle(bank, "提醒我明天上午 9 点会员续费")

    assert out is not None and out["action"] == "reminder_create"


# --------------------------------------------------------------- 账单异常核实
def test_detect_sends_a_bill_question_to_the_bill_path() -> None:
    """账单页「让 AI 助手核这一笔」的原话：以前「核实 + 那笔」被抢进订阅核对分支。"""
    assert assist.detect("帮我核实一下凌晨那笔境外消费，是不是我自己刷的。") == "bill"
    assert assist.detect("帮我核实一下「乐刻健身」这几笔是不是订阅") == "subscription"


def _night_tx(conn, tx_id: str, amount_fen: int, merchant: str, hour: int) -> None:
    """今天的某点整一笔支出：账单三条规则是读时算的，造数就能命中。"""
    parts = time.localtime(clock.now_ms() / 1000)
    ts_ms = int(
        time.mktime((parts.tm_year, parts.tm_mon, parts.tm_mday, hour, 0, 0, 0, -1, -1)) * 1000
    )
    account_id = str(
        conn.execute("SELECT id FROM accounts WHERE type='debit'").fetchone()["id"]
    )
    conn.execute(
        "INSERT INTO transactions"
        " (id, account_id, direction, amount_fen, counterparty, category, note, ts_ms,"
        " balance_after_fen, channel) VALUES (?, ?, 'out', ?, ?, '购物', '', ?, 0, 'manual')",
        (tx_id, account_id, amount_fen, merchant, ts_ms),
    )


def test_bill_check_answers_with_the_one_transaction_it_found(bank) -> None:
    bank.execute("DELETE FROM transactions")
    _night_tx(bank, "t_night", 288_000, "Apple Store 香港", 3)
    bank.commit()
    out = assist.handle(bank, "帮我核实一下凌晨那笔境外消费，是不是我自己刷的。")
    assert out["action"] == "bill_check"
    text = out["summary"]
    assert "疑似订阅" not in text, "账单异常不能答成订阅清单"
    assert "R-BILL-02" in text and "流水号 t_night" in text
    assert "不是欺诈认定" in text
    assert any("冻结" in choice["label"] for choice in out["choices"])
    assert [item["rule_id"] for item in out["data"]["anomalies"]] == ["R-BILL-02"]


def test_bill_check_is_honest_when_nothing_hit(bank) -> None:
    bank.execute("DELETE FROM transactions")
    bank.commit()
    out = assist.handle(bank, "帮我核实一下凌晨那笔境外消费")
    assert out["action"] == "bill_check"
    assert "没有命中账单规则" in out["summary"]
    assert out["data"]["anomalies"] == []


def test_bill_check_pivots_to_the_action_once_the_user_says_it_was_not_them(bank) -> None:
    bank.execute("DELETE FROM transactions")
    _night_tx(bank, "t_night", 288_000, "Apple Store 香港", 3)
    bank.commit()
    out = assist.handle(bank, "这笔不是我刷的，帮我处理")
    text = out["summary"]
    assert "临时冻结" in text and "争议交易" in text and "流水号 t_night" in text
    assert any("冻结" in choice["value"] for choice in out["choices"])

def test_the_card_hands_the_assistant_the_exact_transaction(bank) -> None:
    """同一晚两笔凌晨境外：卡片上那句必须点到哪一笔就是哪一笔。

    以前固定模板只有「凌晨那笔」，服务端只能猜第一笔；现在按钮发出的原话带流水号。
    """
    import app.views.bill as bill_view_module

    bank.execute("DELETE FROM transactions")
    _night_tx(bank, "t_apple", 288_000, "Apple Store 香港", 2)
    _night_tx(bank, "t_lv", 468_000, "Louis Vuitton 香港", 4)
    bank.commit()
    parts = time.localtime(clock.now_ms() / 1000)
    data = bill_view_module.bill_view(
        bank, period="month", year=parts.tm_year, month=parts.tm_mon
    )
    prompts = {
        item["evidence"]["tx_ids"][0]: [
            action["prompt"] for action in item["actions"] if action["kind"] == "prompt"
        ][0]
        for item in data["anomalies"]["items"]
        if item["kind"] == "odd_hour_overseas"
    }
    assert set(prompts) == {"t_apple", "t_lv"}, data["anomalies"]["items"]
    assert "t_apple" in prompts["t_apple"] and "t_apple" not in prompts["t_lv"]

    out = assist.handle(bank, prompts["t_lv"])
    text = out["summary"]
    assert "t_lv" in text and "t_apple" not in text
    assert "Louis Vuitton 香港" in text and "4,680.00" in text


def _hike_txs(bank) -> None:
    """上月 25.00 + 本月 35.00 的同一家订阅：命中 R-BILL-03（涨价 40%）。"""
    parts = time.localtime(clock.now_ms() / 1000)
    year, month = parts.tm_year, parts.tm_mon
    previous = (year, month - 1) if month > 1 else (year - 1, 12)
    account_id = str(
        bank.execute("SELECT id FROM accounts WHERE type='debit'").fetchone()["id"]
    )
    rows = (("t_prev", previous, 2_500), ("t_cur", (year, month), 3_500))
    for tx_id, (row_year, row_month), amount_fen in rows:
        ts_ms = int(
            time.mktime((row_year, row_month, 5, 10, 0, 0, 0, -1, -1)) * 1000
        )
        bank.execute(
            "INSERT INTO transactions"
            " (id, account_id, direction, amount_fen, counterparty, category, note, ts_ms,"
            " balance_after_fen, channel) VALUES (?, ?, 'out', ?, '爱奇艺会员', '订阅', '', ?, 0, 'manual')",
            (tx_id, account_id, amount_fen, ts_ms),
        )


def test_a_scene_question_never_borrows_another_kind_of_anomaly(bank) -> None:
    """问凌晨境外，账上只有订阅涨价：必须说这一类没命中，不能拿涨价冒充当目标。"""
    bank.execute("DELETE FROM transactions")
    _hike_txs(bank)
    bank.commit()
    out = assist.handle(bank, "帮我核实一下凌晨那笔境外消费，是不是我自己刷的。")
    assert out["action"] == "bill_check"
    text = out["summary"]
    assert "没有命中「凌晨、境外」" in text
    assert "我按这句话在账上找到的是这一笔" not in text
    assert out["data"]["anomalies"] == [], "没找到就是没找到，不能冒充"
    assert out["data"]["other_anomalies"], "别的异常要列出来供点选"
    assert any("核对「爱奇艺会员」" in choice["label"] for choice in out["choices"])


def test_a_hike_answer_talks_about_this_months_charge_not_the_difference(bank) -> None:
    """涨价异常记的是差额（10 元），答复要说这个月扣了 35 元、上月 25 元。"""
    bank.execute("DELETE FROM transactions")
    _hike_txs(bank)
    bank.commit()
    out = assist.handle(bank, "爱奇艺会员这个月涨价了，帮我核实一下")
    text = out["summary"]
    assert "35.00 元（上月 25.00 元，涨了 10.00 元）" in text
    assert "R-BILL-03" in text
    assert "业务管理" in text, "订阅涨价该指向停代扣"
    assert "争议交易" not in text, "订阅涨价不是盗刷，别教人报争议交易"

def test_a_hike_is_never_handled_as_a_stolen_card(bank) -> None:
    """账上只有订阅涨价时，用户说「不是我刷的」不能教他报争议交易。"""
    bank.execute("DELETE FROM transactions")
    _hike_txs(bank)
    bank.commit()
    out = assist.handle(bank, "这笔不是我刷的，帮我处理")
    text = out["summary"]
    assert "争议交易" not in text
    assert "没有命中「被盗刷（不是你刷的）」" in text
    assert "核对「爱奇艺会员」" in "".join(choice["label"] for choice in out["choices"])
