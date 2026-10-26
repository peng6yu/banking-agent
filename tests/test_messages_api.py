"""助手主动提醒的 HTTP 面测试：列表、刷新、反馈、缺字段与错误映射。

走真实 HTTP 端点（httpx ASGITransport），断言的是评审现场能看到的行为：
刷新幂等、封顶生效、反馈幂等、非法动作给中文 400、不存在的提醒给中文 404。
"""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime
from typing import Any

import httpx
import pytest

from app import clock, db
from app.main import app
from scripts.seed import seed

BASE_URL = "http://testserver"
FROZEN_MS = int(datetime(2026, 10, 23, 12, 0).timestamp() * 1000)


def call(method: str, url: str, **kwargs: Any) -> httpx.Response:
    async def run() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url=BASE_URL) as http:
            return await http.request(method, url, **kwargs)

    return asyncio.run(run())


def _tx(conn, tx_id: str, when: datetime, amount_fen: int, merchant: str,
        category: str = "订阅") -> None:
    """造一笔支出：两条洞察分别需要「本月比上月贵」和「本月凌晨大额」。"""
    account = conn.execute("SELECT id FROM accounts WHERE type = 'debit' LIMIT 1").fetchone()["id"]
    conn.execute(
        "INSERT INTO transactions (id, account_id, direction, amount_fen, counterparty,"
        " category, note, ts_ms, balance_after_fen, channel)"
        " VALUES (?, ?, 'out', ?, ?, ?, '', ?, 0, 'app')",
        (tx_id, account, amount_fen, merchant, category, int(when.timestamp() * 1000)),
    )


@pytest.fixture()
def bank(tmp_path, monkeypatch: pytest.MonkeyPatch) -> str:
    """干净的演示库 + 冻结时钟 + 两笔确定性样本。

    不依赖种子流水的具体日期（演示数据的月份是相对生成的，跨月会漂）：
    涨价用「上月 100 元、本月 150 元」的云音乐会员，凌晨大额自己造一笔。
    """
    monkeypatch.setattr(db, "DEFAULT_DB_PATH", tmp_path / "bank.db")
    clock.freeze(FROZEN_MS)
    conn = db.connect()
    db.init_db(conn)
    seed(conn)
    _tx(conn, "t_msg_hike_prev", datetime(2026, 9, 15, 21, 0), 10_000, "云音乐会员")
    _tx(conn, "t_msg_hike_cur", datetime(2026, 10, 10, 21, 0), 15_000, "云音乐会员")
    _tx(conn, "t_msg_night", datetime(2026, 10, 20, 2, 14), 288_000, "Apple Store 香港", "购物")
    conn.close()
    yield str(tmp_path / "bank.db")
    clock.unfreeze()


@pytest.fixture()
def user_id(bank) -> str:
    conn = db.connect()
    try:
        return str(conn.execute("SELECT id FROM users LIMIT 1").fetchone()["id"])
    finally:
        conn.close()


def test_empty_list_explains_why_it_is_empty(bank) -> None:
    """还没生成过时不报错：列表给空态文案 + 今日额度，别让页面白屏。"""
    response = call("GET", "/api/messages")
    assert response.status_code == 200
    body = response.json()
    assert body["items"] == []
    assert body["daily"] == {"used": 0, "cap": 3}
    assert body["counts"]["total"] == 0
    assert "最多说三条" in body["empty_reason"]


def test_refresh_generates_and_second_call_is_idempotent(bank) -> None:
    first = call("POST", "/api/messages/refresh").json()
    assert first["created"], "种子账本里至少该想到一条"
    assert first["messages"]["items"], "刷新后列表里要能立刻看到"
    assert len(first["created"]) <= 3
    second = call("POST", "/api/messages/refresh").json()
    assert second["created"] == [], "连点两次不许重复生成"
    assert len(second["messages"]["items"]) == len(first["messages"]["items"])
    assert all(item["reason"] in {"QUIET", "DUPLICATE", "DAILY_CAP"} for item in second["skipped"])


def test_items_carry_facts_actions_and_three_feedback_buttons(bank) -> None:
    call("POST", "/api/messages/refresh")
    item = call("GET", "/api/messages?limit=1").json()["items"][0]
    assert item["message_id"].startswith("m")
    assert item["status"] == "new" and item["status_label"] == "待你看"
    assert item["facts"], "每条提醒都要能摊开依据"
    assert item["actions"], "提醒后面要能直接点下一步"
    assert [entry["action"] for entry in item["feedback_actions"]] == ["adopted", "dismissed", "muted"]
    assert item["created_text"], "时间要给可读文本，不要裸毫秒"


def test_feedback_marks_adopted_and_is_idempotent(bank) -> None:
    call("POST", "/api/messages/refresh")
    message_id = call("GET", "/api/messages").json()["items"][0]["message_id"]
    first = call("POST", f"/api/messages/{message_id}/feedback", json={"action": "adopted"})
    assert first.status_code == 200
    assert first.json()["message"]["status"] == "adopted"
    assert first.json()["messages"]["counts"]["adopted"] == 1
    again = call("POST", f"/api/messages/{message_id}/feedback", json={"action": "adopted"})
    assert again.status_code == 200
    assert again.json()["messages"]["counts"]["adopted"] == 1, "重复采纳不算两次"


def test_invalid_feedback_action_is_a_chinese_400(bank) -> None:
    call("POST", "/api/messages/refresh")
    message_id = call("GET", "/api/messages").json()["items"][0]["message_id"]
    response = call("POST", f"/api/messages/{message_id}/feedback", json={"action": "like"})
    assert response.status_code == 400
    body = response.json()
    assert body["code"] == "VALIDATION"
    assert "不认识的反馈动作" in body["user_text"]


def test_missing_message_is_a_chinese_404(bank) -> None:
    response = call("POST", "/api/messages/m_nope/feedback", json={"action": "adopted"})
    assert response.status_code == 404
    body = response.json()
    assert body["code"] == "NOT_FOUND"
    assert "这条提醒不存在" in body["user_text"]
    assert "Traceback" not in response.text, "错误响应不许带堆栈"


def test_insights_catalog_explains_each_rule(bank) -> None:
    """洞察目录：用户问「你凭什么想到这个」时，页面能摊开依据与数据来源。"""
    body = call("GET", "/api/messages/insights").json()
    assert body["count"] == len(body["insights"]) == 3
    for insight in body["insights"]:
        assert insight["why"], "每类洞察都要有一句人话的判定条件"
        assert insight["source"], "每类洞察都要写清看的是哪张表"
        assert insight["id"] in {"I1_subscription_hike", "I2_spending_pace", "I3_night_large"}


def test_limit_is_clamped_to_a_readable_page(bank) -> None:
    # FastAPI 的入参校验在这里被统一翻成 400 + 中文（不是 422 + 英文字段名）
    for url in ("/api/messages?limit=0", "/api/messages?limit=99"):
        response = call("GET", url)
        assert response.status_code == 400
        assert response.json()["code"] == "VALIDATION"
