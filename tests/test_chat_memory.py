"""会话记忆：关掉页面、重启服务之后，会话还要接得上。

用户的要求（2026-10-25）：会话要有记忆，不该问一句就变成一个全新会话。
以前历史只放在进程内存里（routes._history），重启就丢，前端也从不回显。
这一组钉住：

1. 一轮一轮落库、按时间正序取回，标题取自第一句用户消息；
2. /api/chat 真的把用户与助手两轮都记下来，并且能从历史接口读回来；
3. 服务「重启」（新连接读同一个库）之后历史还在；
4. 只留最近 KEEP_TURNS 轮，超出的剪掉；
5. 记忆只存对话原文，不参与任何权限判断——这里只验存取，不验放行。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest

import app.chat_history as chat_history
from app import db
from app.api import routes
from app.clock import now_ms
from app.main import app
from scripts.seed import seed

BASE_URL = "http://testserver"


async def _request(method: str, url: str, body: dict | None = None) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url=BASE_URL) as http:
        return await http.request(method, url, json=body)


def call(method: str, url: str, body: dict | None = None) -> httpx.Response:
    return asyncio.run(_request(method, url, body))


@pytest.fixture()
def bank(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(db, "DEFAULT_DB_PATH", tmp_path / "bank.db")
    conn = db.connect()
    db.init_db(conn)
    seed(conn)
    conn.close()
    routes._history.clear()
    routes.set_adapter(None)
    yield tmp_path / "bank.db"
    routes._history.clear()
    routes.set_adapter(None)


def test_append_and_recent_keep_order_and_title(conn) -> None:
    chat_history.append(conn, "s_mem", "user", "我想看看这个月的账单")
    chat_history.append(conn, "s_mem", "assistant", "这个月一共 39 笔，花得最多的是购物。")
    turns = chat_history.recent(conn, "s_mem")
    assert [turn["role"] for turn in turns] == ["user", "assistant"]
    assert turns[0]["content"] == "我想看看这个月的账单"
    assert turns[0]["seq"] == 1 and turns[1]["seq"] == 2
    assert chat_history.title(conn, "s_mem") == "我想看看这个月的账单"


def test_title_only_comes_from_the_first_user_turn(conn) -> None:
    chat_history.append(conn, "s_mem2", "user", "第一句")
    chat_history.append(conn, "s_mem2", "user", "第二句")
    assert chat_history.title(conn, "s_mem2") == "第一句"


def test_latest_finds_the_most_recently_used_session(conn) -> None:
    chat_history.append(conn, "s_old", "user", "旧会话")
    chat_history.append(conn, "s_new", "user", "新会话")
    latest = chat_history.latest(conn)
    assert latest["session_id"] == "s_new"
    assert [turn["content"] for turn in latest["turns"]] == ["新会话"]
    assert latest["title"] == "新会话"


def test_latest_on_empty_library_is_honest(conn) -> None:
    assert chat_history.latest(conn) == {
        "session_id": "",
        "turns": [],
        "last_ms": 0,
        "last_text": None,
        "title": "",
        "total": 0,
    }


def test_history_survives_a_restart(tmp_path: Path) -> None:
    path = tmp_path / "restart.db"
    first = db.connect(path)
    db.init_db(first)
    chat_history.append(first, "s_restart", "user", "重启前说的话")
    chat_history.append(first, "s_restart", "assistant", "重启前的回答")
    first.close()

    second = db.connect(path)
    try:
        turns = chat_history.recent(second, "s_restart")
        assert [turn["content"] for turn in turns] == ["重启前说的话", "重启前的回答"]
    finally:
        second.close()


def test_clear_forgets_turns(conn) -> None:
    chat_history.append(conn, "s_clear", "user", "忘掉我")
    out = chat_history.clear(conn, "s_clear")
    assert out == {"session_id": "s_clear", "removed": 1}
    assert chat_history.recent(conn, "s_clear") == []
    # 记忆模块从不代建 sessions 行：那是执行器的职责，否则 session.start 审计会丢
    assert conn.execute("SELECT COUNT(*) FROM sessions WHERE id = 's_clear'").fetchone()[0] == 0


def test_only_the_last_keep_turns_are_kept(conn) -> None:
    total = chat_history.KEEP_TURNS + 5
    for index in range(total):
        chat_history.append(conn, "s_long", "user", f"第 {index} 句", ts_ms=now_ms() + index)
    turns = chat_history.recent(conn, "s_long", limit=total + 10)
    assert len(turns) == chat_history.KEEP_TURNS
    assert turns[0]["content"] == "第 5 句"
    assert turns[-1]["content"] == f"第 {total - 1} 句"


def test_rejects_empty_session_and_unknown_role(conn) -> None:
    with pytest.raises(ValueError):
        chat_history.append(conn, "", "user", "hi")
    with pytest.raises(ValueError):
        chat_history.append(conn, "s_bad", "tool", "hi")
    with pytest.raises(ValueError):
        chat_history.clear(conn, "  ")


def test_overlong_content_is_truncated(conn) -> None:
    chat_history.append(conn, "s_big", "user", "长" * (chat_history.MAX_CONTENT + 50))
    turns = chat_history.recent(conn, "s_big")
    assert len(turns[0]["content"]) == chat_history.MAX_CONTENT


def test_history_endpoint_reads_back_what_was_stored(bank: Path) -> None:
    conn = db.connect()
    try:
        chat_history.append(conn, "s_api_hist", "user", "浏览器关掉之后再打开")
        chat_history.append(conn, "s_api_hist", "assistant", "我还记得。")
    finally:
        conn.close()

    body = call("GET", "/api/chat/history?session_id=s_api_hist").json()
    assert body["ok"] is True
    assert body["found"] is True
    assert body["title"] == "浏览器关掉之后再打开"
    assert [turn["role"] for turn in body["turns"]] == ["user", "assistant"]

    empty = call("GET", "/api/chat/history?session_id=s_never").json()
    assert empty["ok"] is True and empty["found"] is False
    assert empty["turns"] == []
    assert call("GET", "/api/chat/history").json()["found"] is False


def test_latest_endpoint_returns_the_session_to_resume(bank: Path) -> None:
    conn = db.connect()
    try:
        chat_history.append(conn, "s_api_last", "user", "上次问的是账单")
        chat_history.append(conn, "s_api_last", "assistant", "你上个月花了 15,847 元。")
    finally:
        conn.close()

    body = call("GET", "/api/chat/latest").json()
    assert body["ok"] is True
    assert body["session_id"] == "s_api_last"
    assert [turn["content"] for turn in body["turns"]][0] == "上次问的是账单"


def test_chat_endpoint_persists_user_and_assistant_turns(bank: Path) -> None:
    resp = call("POST", "/api/chat", {"session_id": "s_api_persist", "text": "我有哪些提醒"})
    assert resp.status_code == 200
    assert "done" in resp.text

    # 内存里也有，但这里故意读库，验证「重启后也接得上」
    conn = db.connect()
    try:
        turns = chat_history.recent(conn, "s_api_persist")
    finally:
        conn.close()
    assert len(turns) >= 2
    assert turns[0]["role"] == "user" and turns[0]["content"] == "我有哪些提醒"
    assert turns[-1]["role"] == "assistant" and turns[-1]["content"].strip()

class _Clueless:
    """模型一律回 unknown：用来钉住「没听懂时也要带着上一句一起问」。"""

    def chat(self, messages: list[dict[str, Any]], **kwargs: Any) -> Any:
        from app.llm import LLMResponse, ToolCall

        return LLMResponse(
            content="",
            reasoning="",
            tool_calls=[
                ToolCall(
                    id="c1",
                    name="emit_intent",
                    arguments=json.dumps(
                        {"intent": "unknown", "confidence": 1.0, "missing": []}, ensure_ascii=False
                    ),
                )
            ],
            finish_reason="tool_calls",
            model="scripted",
        )


def test_chat_without_session_id_continues_the_latest_session(bank: Path) -> None:
    """没带会话号时接上最近一条会话：换个标签页、丢一次 sessionStorage 都不该开新会话。"""
    call("POST", "/api/chat", {"session_id": "s_continue", "text": "我有哪些提醒"})

    resp = call("POST", "/api/chat", {"text": "我有哪些提醒"})

    assert resp.status_code == 200
    assert '"session_id": "s_continue"' in resp.text


def test_chat_without_any_session_creates_one(bank: Path) -> None:
    """一条历史都没有时才新开：新会话号必须真的出现在 start 帧里。"""
    resp = call("POST", "/api/chat", {"text": "查一下我的余额"})

    assert resp.status_code == 200
    assert '"session_id": "s_' in resp.text


def test_unknown_intent_quotes_the_previous_sentence(bank: Path) -> None:
    """没听懂时把上一句念回来——空口问「你想干什么」等于每一句都从零开始。"""
    routes.set_adapter(_Clueless())
    call("POST", "/api/chat", {"session_id": "s_ctx", "text": "帮我看看这个月的账单"})

    resp = call("POST", "/api/chat", {"session_id": "s_ctx", "text": "那个呢"})

    assert "帮我看看这个月的账单" in resp.text

