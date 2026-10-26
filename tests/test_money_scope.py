"""业务边界（app/scope.py）：这个助手只处理钱的事。

为什么单独一组：
1. 曾经「提醒我喝水」也能建出一条提醒——提醒必须挂在资金时间点上才有意义；
2. 长期记忆曾经连「我住杭州」都记——记忆只收与钱有关的偏好与事实；
3. 「讲个笑话」这种请求不该被当成「没听懂」再反问一遍，要说明边界并给资金动作。

这组用纯函数 + 快路 + HTTP 三层各钉一次，防止有人把边界只写在提示词里。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest

import app.memory as memory
import app.memory_extract as memory_extract
import app.planner.assist as assist
import app.planner.intent as intent
import app.proactive as proactive
import app.scope as scope
from app import db
from app.main import app
from app.planner import executor
from scripts.seed import seed

BASE_URL = "http://testserver"
USER_ID = "u_9f2c41ab"


@pytest.fixture()
def bank(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(db, "DEFAULT_DB_PATH", tmp_path / "bank.db")
    conn = db.connect()
    db.init_db(conn)
    seed(conn)
    executor.RUNS.clear()
    yield conn
    executor.RUNS.clear()
    conn.close()


def _request(method: str, url: str, body: dict | None = None) -> httpx.Response:
    async def run() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url=BASE_URL) as http:
            return await http.request(method, url, json=body)

    return asyncio.run(run())


def _count(conn, table: str) -> int:
    return int(conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"])


# --------------------------------------------------------------- 纯函数


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("查一下我储蓄卡尾号1234的余额", "money"),
        ("帮我看看这个月账单", "money"),
        ("提醒我每月 25 号还信用卡", "money"),
        ("我有哪些理财", "money"),
        ("明天天气怎么样", "off"),
        ("讲个笑话", "off"),
        ("提醒我明天 9 点喝水", "unknown"),
        ("会员续费", "money"),
    ],
)
def test_classify_separates_money_from_off_topic(text: str, expected: str) -> None:
    assert scope.classify(text) == expected


@pytest.mark.parametrize(
    ("text", "anchor"),
    [
        ("提醒我每月 25 号还信用卡", "repayment"),
        ("会员到期前提醒我", "deduction"),
        ("提醒我预约转账那天别忘了", "scheduled"),
        ("提醒我看这个月预算", "budget"),
        ("提醒我存够一万", "savings"),
        ("提醒我额度快满了", "limit"),
        ("提醒我明天 9 点喝水", ""),
    ],
)
def test_reminder_anchor_requires_a_money_timeline(text: str, anchor: str) -> None:
    assert scope.reminder_anchor(text) == anchor


def test_anchor_choices_are_clickable_and_cover_every_anchor() -> None:
    choices = scope.anchor_choices()
    assert len(choices) == len(scope.REMINDER_ANCHORS)
    assert all(item["label"] and item["value"] for item in choices)


def test_out_of_scope_reply_only_answers_off_topic() -> None:
    assert scope.OUT_OF_SCOPE_REPLY in scope.out_of_scope_reply("讲个笑话")
    assert scope.out_of_scope_reply("查一下余额") == ""
    assert scope.out_of_scope_reply("提醒我喝水") == ""


def test_is_money_text_keeps_memory_honest() -> None:
    assert scope.is_money_text("记账喜欢按周看")
    assert scope.is_money_text("我常用招商银行那张卡")
    assert not scope.is_money_text("我叫老王")
    assert not scope.is_money_text("我住杭州")


# --------------------------------------------------------------- 提醒快路


def test_reminder_without_money_anchor_creates_nothing(bank) -> None:
    before_plans = _count(bank, "plans")
    out = assist.handle(bank, "提醒我明天 9 点喝水")

    assert out is not None
    assert out["action"] == "reminder_anchor_needed"
    assert out["summary"] == scope.REMINDER_BOUNDARY
    assert [item["label"] for item in out["choices"]] == [
        item["label"] for item in scope.anchor_choices()
    ]
    assert _count(bank, "plans") == before_plans


def test_reminder_with_anchor_is_created_and_labelled(bank) -> None:
    out = assist.handle(bank, "提醒我每月 25 号会员续费")

    assert out is not None and out["action"] == "reminder_create"
    assert out["data"]["anchor"] == "deduction"
    row = bank.execute("SELECT id, meta_json FROM plans ORDER BY rowid DESC LIMIT 1").fetchone()
    assert '"anchor": "deduction"' in row["meta_json"]
    step = bank.execute("SELECT payload_json FROM plan_steps WHERE plan_id = ?", (row["id"],)).fetchone()
    assert '"anchor": "deduction"' in step["payload_json"]


def test_form_endpoint_rejects_reminder_without_anchor(bank) -> None:
    before_plans = _count(bank, "plans")
    res = _request("POST", "/api/reminders", {"text": "提醒我喝水", "repeat": "daily", "hour": 8})

    assert res.status_code == 400
    body = res.json()
    text = " ".join(str(value) for value in body.values())
    assert scope.REMINDER_BOUNDARY in text
    assert _count(bank, "plans") == before_plans  # 一条都没多


# --------------------------------------------------------------- 长期记忆


def test_memory_fast_path_refuses_off_topic_memory(bank) -> None:
    user_id = proactive.default_user_id(bank)
    out = assist.handle(bank, "记住我喜欢喝咖啡")

    assert out is not None and out["action"] == "memory_out_of_scope"
    assert out["summary"] == scope.MEMORY_BOUNDARY
    assert memory.all_facts(bank, user_id=user_id) == []


def test_memory_extract_drops_off_topic_items(bank) -> None:
    user_id = proactive.default_user_id(bank)
    written = memory_extract._write_items(
        bank,
        user_id,
        [
            {"key": "住址", "value": "我住杭州"},
            {"key": "记账习惯", "value": "记账喜欢按周看", "kind": "preference"},
        ],
    )

    assert [fact["key"] for fact in written] == ["记账习惯"]
    assert [fact["key"] for fact in memory.all_facts(bank, user_id=user_id)] == ["记账习惯"]


# --------------------------------------------------------------- 提示词与接口


def test_system_prompt_states_the_money_only_boundary() -> None:
    assert "你只处理与钱有关的事" in intent.SYSTEM_PROMPT
    assert "与钱无关的请求" in intent.SYSTEM_PROMPT


def test_off_topic_chat_gets_the_boundary_instead_of_a_new_object(bank) -> None:
    before_plans = _count(bank, "plans")
    before_runs = _count(bank, "dag_runs")

    async def run() -> tuple[bool, str]:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url=BASE_URL) as http:
            async with http.stream("POST", "/api/chat", json={"text": "讲个笑话"}) as res:
                out_of_scope = False
                summary = ""
                async for line in res.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    import json as _json

                    payload = _json.loads(line[5:].strip())
                    if payload.get("out_of_scope"):
                        out_of_scope = True
                    if payload.get("summary"):
                        summary = str(payload["summary"])
                return out_of_scope, summary

    out_of_scope, summary = asyncio.run(run())

    assert out_of_scope is True
    assert summary == scope.OUT_OF_SCOPE_REPLY
    assert _count(bank, "plans") == before_plans
    assert _count(bank, "dag_runs") == before_runs


# ------------------------------------------------- 边界给的选项必须真能用


def test_every_anchor_choice_really_builds_a_reminder(bank) -> None:
    """chip 上写什么，点下去就得真能建成；「点了没反应」的选项等于没有选项。"""
    before = _count(bank, "plans")
    choices = scope.anchor_choices()
    assert len(choices) == 6
    for choice in choices:
        text = choice["value"]
        assert assist.detect(text) == "reminder", text
        out = assist.handle(bank, text)
        assert out["action"] == "reminder_create", (text, out)
        assert str(out["data"].get("anchor") or ""), text
    assert _count(bank, "plans") == before + len(choices)
    # 页面列表也要带锚点，否则卡片上只剩「每月」，看不出这条提醒盯的是哪笔钱。
    listed = assist.active_reminders(bank, USER_ID)
    assert len(listed) == len(choices)
    assert all(str(item.get("anchor_label") or "") for item in listed)


@pytest.mark.parametrize(
    "text",
    [
        "提醒我给张三转账 100 元",
        "提醒我给张三转 100",
        "提醒我明天把房租转给房东",
        "提醒我生日那天给妈妈转 500 元",
    ],
)
def test_reminder_words_do_not_swallow_real_transfers(text: str) -> None:
    """带金额、或「给某人转」的仍然是转账链路，不能被提醒快路吞掉。"""
    assert assist.detect(text) != "reminder"
