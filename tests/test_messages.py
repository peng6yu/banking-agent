"""助手主动提醒（消息）的测试：探测、频控、反馈与静默。

钉住四件事：
1. 只报有据可查的事：每条提醒的数字都能在 facts 里找到出处；
2. 不吵：每日封顶 3 条、同类 7 天内不重复、忽略 3 次自动静音；
3. 反馈幂等，重复点不会重复计数；
4. 静音是按「这一类洞察」记的，不是按某条消息记的。

边界（时间、封顶、静音）全部用固定时间点断言，不依赖跑测试的当天。
"""

from __future__ import annotations

import re

import pytest

from app import messages
from app.clock import NotFound, ValidationError
from tests.tool_fixtures import DEBIT_ID, FROZEN_MS, USER_ID, _tx, bank

__all__ = ["bank"]

NOW = FROZEN_MS
DAY = messages.DAY_MS
NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")


def _fake_message(
    conn,
    message_id: str,
    insight_id: str,
    *,
    dedupe_key: str = "",
    created_ms: int = NOW,
    status: str = "new",
) -> None:
    """直接落一条提醒：频控用例只关心「已经有多少条」，不关心探测器。"""
    conn.execute(
        "INSERT INTO messages (id, user_id, insight_id, dedupe_key, severity, title, body,"
        " facts_json, actions_json, status, created_ms, expires_ms)"
        " VALUES (?, ?, ?, ?, 'info', '占位提醒', '占位正文', '{}', '[]', ?, ?, ?)",
        (message_id, USER_ID, insight_id, dedupe_key or message_id, status, created_ms,
         created_ms + 30 * DAY),
    )


# ---------------------------------------------------------------- 探测器


def test_detect_reports_night_spend_and_subscription_hike(bank) -> None:
    """种子账本里既有凌晨大额，也有爱奇艺 25→35 的涨价，两条都该被想到。"""
    found = messages.detect_insights(bank, USER_ID, NOW)
    insight_ids = [item["insight_id"] for item in found]
    assert "I3_night_large" in insight_ids
    assert "I1_subscription_hike" in insight_ids
    assert len(insight_ids) == len(set(insight_ids)), "同一轮不该报两遍同类洞察"


def test_detect_returns_nothing_on_an_empty_ledger(bank) -> None:
    """没有任何消费时不许编一条提醒出来。"""
    bank.execute("DELETE FROM transactions")
    assert messages.detect_insights(bank, USER_ID, NOW) == []


def test_hike_ignores_a_small_increase(bank) -> None:
    """只涨 4%（25.00→26.00）不该触发涨价提醒：门槛写在同商户 30% 以上。"""
    bank.execute("UPDATE transactions SET amount_fen = 2600 WHERE id = 't_sub_c'")
    found = messages.detect_insights(bank, USER_ID, NOW)
    assert "I1_subscription_hike" not in [item["insight_id"] for item in found]


def test_night_ignores_a_small_late_night_charge(bank) -> None:
    """凌晨 120 元的小额不算「值得核实」：门槛是 500 元。"""
    bank.execute("UPDATE transactions SET amount_fen = 12000 WHERE id IN ('t_ovs_local', 't_ovs')")
    found = messages.detect_insights(bank, USER_ID, NOW)
    assert "I3_night_large" not in [item["insight_id"] for item in found]


# ---------------------------------------------------------------- 落库与频控


def test_generate_inserts_new_messages_with_short_ids_and_audit(bank) -> None:
    result = messages.generate(bank, USER_ID, now=NOW)
    assert result["created"], "种子里至少有两条该报的事"
    assert result["used"] == len(result["created"])
    rows = messages.list_messages(bank, USER_ID)
    assert rows and all(row["status"] == "new" for row in rows)
    assert all(row["id"].startswith("m") for row in rows), "提醒 id 要能贴进对话"
    events = [
        row["event_type"]
        for row in bank.execute("SELECT event_type FROM audit_log WHERE event_type LIKE 'message.%'")
    ]
    assert events.count("message.generated") == len(result["created"])


def test_generate_is_idempotent_within_a_week(bank) -> None:
    """一分钟内连点两次「现在想一想」：第二次不许再生成，也不许重复落库。"""
    first = messages.generate(bank, USER_ID, now=NOW)
    assert first["created"]
    second = messages.generate(bank, USER_ID, now=NOW)
    assert second["created"] == []
    reasons = {item["reason"] for item in second["skipped"]}
    assert reasons <= {"QUIET", "DUPLICATE"}
    assert len(messages.list_messages(bank, USER_ID)) == len(first["created"])


def test_daily_cap_stops_at_three(bank) -> None:
    """今天已经有 3 条，探测器再发现新东西也不许落库。"""
    for index in range(messages.MAX_PER_DAY):
        _fake_message(bank, f"m_cap{index}", f"I_test{index}")
    result = messages.generate(bank, USER_ID, now=NOW)
    assert result["created"] == []
    assert {item["reason"] for item in result["skipped"]} == {"DAILY_CAP"}
    assert len(messages.list_messages(bank, USER_ID)) == messages.MAX_PER_DAY


def test_yesterdays_messages_do_not_count_toward_today(bank) -> None:
    """封顶按自然日算：昨天说了三条，不占今天的额度。"""
    for index in range(messages.MAX_PER_DAY):
        _fake_message(bank, f"m_old{index}", f"I_old{index}", created_ms=NOW - DAY)
    assert messages.daily_usage(bank, USER_ID, now=NOW)["used"] == 0
    assert messages.generate(bank, USER_ID, now=NOW)["created"]


# ---------------------------------------------------------------- 反馈与静音


def test_adopt_flips_status_and_records_feedback(bank) -> None:
    created = messages.generate(bank, USER_ID, now=NOW)["created"][0]["message_id"]
    row = messages.feedback(bank, USER_ID, created, "adopted")
    assert row["status"] == "adopted"
    assert messages.counts(bank, USER_ID)["adopted"] == 1
    feedback_rows = [
        item["action"]
        for item in bank.execute("SELECT action FROM message_feedback WHERE message_id = ?", (created,))
    ]
    assert feedback_rows == ["adopted"]


def test_feedback_is_idempotent(bank) -> None:
    """同一个按钮连点两下：状态不变、反馈只留一条、静音计数不翻倍。"""
    created = messages.generate(bank, USER_ID, now=NOW)["created"][0]["message_id"]
    messages.feedback(bank, USER_ID, created, "adopted")
    again = messages.feedback(bank, USER_ID, created, "adopted")
    assert again["status"] == "adopted"
    count = bank.execute(
        "SELECT COUNT(*) FROM message_feedback WHERE message_id = ?", (created,)
    ).fetchone()[0]
    assert count == 1


def test_dismiss_three_times_mutes_the_whole_insight(bank) -> None:
    """连续忽略 3 次，这一类以后都不再出现——不是只跳过这一条。"""
    for index in range(3):
        _fake_message(bank, f"m_ignore{index}", "I3_night_large")
    for index in range(3):
        messages.feedback(bank, USER_ID, f"m_ignore{index}", "dismissed")
    assert "I3_night_large" in messages.muted_insights(bank, USER_ID, now=NOW)
    result = messages.generate(bank, USER_ID, now=NOW)
    assert "I3_night_large" not in [item["insight_id"] for item in result["created"]]


def test_mute_applies_immediately(bank) -> None:
    """点「这类别再提醒」一次就静音，不用等三次。"""
    created = messages.generate(bank, USER_ID, now=NOW)["created"][0]
    messages.feedback(bank, USER_ID, created["message_id"], "muted")
    assert created["insight_id"] in messages.muted_insights(bank, USER_ID, now=NOW)


def test_unknown_action_is_rejected(bank) -> None:
    created = messages.generate(bank, USER_ID, now=NOW)["created"][0]["message_id"]
    with pytest.raises(ValidationError):
        messages.feedback(bank, USER_ID, created, "like")


def test_unknown_message_raises_not_found(bank) -> None:
    with pytest.raises(NotFound):
        messages.get_message(bank, USER_ID, "m_nope")


# ---------------------------------------------------------------- 文案白名单


def test_every_number_in_a_message_comes_from_its_facts(bank) -> None:
    """语言层白名单：正文里出现的每个数字都必须能在 facts 里找到，不许模型凭感觉写。"""
    for item in messages.detect_insights(bank, USER_ID, NOW):
        allowed = {
            str(value).replace(",", "") for value in item["facts"].values()
            if isinstance(value, (int, float, str))
        }
        for text in (item["title"], item["body"]):
            for token in NUMBER.findall(text):
                assert token.replace(",", "") in allowed, f"{item['insight_id']} 的文案里出现了 facts 之外的数字 {token}"
