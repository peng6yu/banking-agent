"""澄清后续答：短回答接回上一轮原话（README 已知限制 3）。

收款人歧义闸门会以一句反问结束整轮；下一轮只回「尾号4417」时，如果直接解析，
解析器看不到金额与收款人，只能把问题再问一遍。app/planner/clarify.py 存的
上下文就是为这一步准备的。这里既测模块本身的匹配规则，也测它真的接进了
/api/chat 的解析链路（而不是只在单元测试里好看）。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from app import db
from app.api import routes
from app.llm import LLMResponse, ToolCall
from app.models import ChatRequest
from app.planner import clarify
from app.planner import executor
from scripts.seed import seed

SECRET = "a" * 64
CANDIDATES = [
    {"name": "张三", "card_tail": "4417", "bank": "招商银行"},
    {"name": "张三", "card_tail": "6620", "bank": "民生银行"},
]
ORIGINAL = "转 500 元给张三"


class Capturing:
    """把模型看到的最后一条用户消息留下来，其余照常回一个 transfer 意图。"""

    def __init__(self) -> None:
        self.seen: list[str] = []

    def chat(self, messages, **kwargs) -> LLMResponse:
        self.seen.append(str(messages[-1].get("content", "")))
        args = {"intent": "transfer", "payee_name": "张三", "amount_fen": 50_000, "missing": [], "confidence": 0.95}
        return LLMResponse(
            content="",
            reasoning="",
            tool_calls=[ToolCall(id="c1", name="emit_intent", arguments=json.dumps(args, ensure_ascii=False))],
            finish_reason="tool_calls",
            model="capturing",
        )


@pytest.fixture()
def memory():
    clarify._PENDING.clear()
    yield
    clarify._PENDING.clear()


@pytest.fixture()
def bank(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("CAPABILITY_SECRET", SECRET)
    monkeypatch.setattr(db, "DEFAULT_DB_PATH", tmp_path / "bank.db")
    conn = db.connect()
    db.init_db(conn)
    seed(conn)
    conn.close()
    executor.RUNS.clear()
    routes._history.clear()
    yield tmp_path / "bank.db"
    routes.set_adapter(None)
    executor.RUNS.clear()
    routes._history.clear()


def _remember(session_id: str = "s_clarify") -> None:
    clarify.remember(
        session_id,
        text=ORIGINAL,
        query="张三",
        candidates=CANDIDATES,
        question="有 2 位都叫「张三」：张三（尾号4417）、张三（尾号6620）。你要付给哪一位？",
    )


# ---------- 模块层 ----------


def test_tail_reply_rewrites_the_original_sentence(memory) -> None:
    _remember()
    assert clarify.augment("s_clarify", "尾号4417") == f"{ORIGINAL}（收款人卡尾号4417）"


def test_bare_four_digits_are_accepted(memory) -> None:
    _remember()
    assert clarify.augment("s_clarify", "6620") == f"{ORIGINAL}（收款人卡尾号6620）"


def test_tail_outside_the_candidates_is_not_accepted(memory) -> None:
    """候选里没有的尾号不接：宁可再问一遍，也不拿一个手滑的数字去付钱。"""
    _remember()
    assert clarify.augment("s_clarify", "尾号9999") is None
    assert clarify.peek("s_clarify") is not None, "没接上就不该清上下文"


def test_ordinal_picks_the_nth_candidate(memory) -> None:
    _remember()
    assert clarify.augment("s_clarify", "第二个") == f"{ORIGINAL}（收款人卡尾号6620）"


def test_ordinal_out_of_range_is_ignored(memory) -> None:
    _remember()
    assert clarify.augment("s_clarify", "第三个") is None


def test_plain_sentence_is_not_rewritten(memory) -> None:
    _remember()
    assert clarify.augment("s_clarify", "我想查余额") is None


def test_context_expires_after_ttl(memory, monkeypatch) -> None:
    _remember()
    pending = clarify.peek("s_clarify")
    assert pending is not None
    pending.created_ms -= clarify.CLARIFY_TTL_MS + 1
    assert clarify.augment("s_clarify", "尾号4417") is None


def test_forget_clears_the_context(memory) -> None:
    _remember()
    clarify.forget("s_clarify")
    assert clarify.augment("s_clarify", "尾号4417") is None


def test_no_context_means_no_rewrite(memory) -> None:
    assert clarify.augment("s_none", "尾号4417") is None


# ---------- API 层：真的接进解析链路 ----------


def test_chat_feeds_the_rewritten_sentence_to_the_model(bank: Path) -> None:
    adapter = Capturing()
    routes.set_adapter(adapter)
    _remember()
    asyncio.run(routes.chat(ChatRequest(text="尾号4417", session_id="s_clarify")))
    assert adapter.seen, "模型一次都没被调用"
    assert "转 500 元给张三" in adapter.seen[0]
    assert "尾号4417" in adapter.seen[0]


def test_chat_forgets_the_context_after_one_use(bank: Path) -> None:
    adapter = Capturing()
    routes.set_adapter(adapter)
    _remember()
    asyncio.run(routes.chat(ChatRequest(text="尾号4417", session_id="s_clarify")))
    asyncio.run(routes.chat(ChatRequest(text="尾号4417", session_id="s_clarify")))
    assert "转 500 元给张三" in adapter.seen[0]
    assert "转 500 元给张三" not in adapter.seen[1], "第二次不该再补全"


def test_chat_drops_the_context_when_the_user_changes_topic(bank: Path) -> None:
    adapter = Capturing()
    routes.set_adapter(adapter)
    _remember()
    asyncio.run(routes.chat(ChatRequest(text="帮我查下余额", session_id="s_clarify")))
    assert clarify.peek("s_clarify") is None
