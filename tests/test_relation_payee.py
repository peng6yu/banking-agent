"""亲属称谓兜底：模型把「我妈」当关系词时，规则层把收款人补上（README 已知限制 4）。

2026-10-22 用真实 MiniMax 跑赛题原句「下周三我妈生日，帮我提前留出300块，
到时候给她订束花送过去」：模型把「我妈」当成关系词，payee_name 留空，
首轮反问「这份礼物是给谁过生日？」，用户补一句「我妈」才走通。数据层其实
早就支持这个称谓（scripts/seed.py 的「妈妈」alias 就是「我妈」），缺的是
解析层的一次兜底。现在按原话把称谓填进 payee_name；查不到人仍走正常反问，
不会猜错收款人。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from app import db
from app.llm import LLMResponse, ToolCall
from app.models import Action
from app.planner import executor
from app.planner import intent
from app.tools import base as tool_base
from app.tools import queries
from scripts.seed import seed

SECRET = "a" * 64
BIRTHDAY_TEXT = "下周三我妈生日，帮我提前留出300块，到时候给她订束花送过去"


class Scripted:
    def __init__(self, args: dict[str, Any]) -> None:
        self.args = args
        self.calls = 0

    def chat(self, messages, **kwargs) -> LLMResponse:
        self.calls += 1
        return LLMResponse(
            content="",
            reasoning="",
            tool_calls=[ToolCall(id="c1", name="emit_intent", arguments=json.dumps(self.args, ensure_ascii=False))],
            finish_reason="tool_calls",
            model="scripted",
        )


@pytest.fixture()
def bank(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("CAPABILITY_SECRET", SECRET)
    monkeypatch.setattr(db, "DEFAULT_DB_PATH", tmp_path / "bank.db")
    conn = db.connect()
    db.init_db(conn)
    seed(conn)
    conn.close()
    executor.RUNS.clear()
    return db.DEFAULT_DB_PATH


def parse(text: str, args: dict[str, Any]) -> intent.IntentResult:
    return asyncio.run(intent.parse(text, [], adapter=Scripted(args)))


def test_birthday_without_payee_slot_is_filled_from_the_sentence() -> None:
    parsed = parse(BIRTHDAY_TEXT, {"intent": "birthday_plan", "amount_fen": 30_000, "missing": [], "confidence": 0.9})
    assert parsed.slots.get("payee_name") == "我妈"
    assert "payee_name" not in parsed.missing
    assert parsed.question is None, f"不该再问一轮：{parsed.question}"


def test_transfer_with_relation_word_is_filled_too() -> None:
    parsed = parse("转 500 给我妈", {"intent": "transfer", "amount_fen": 50_000, "missing": [], "confidence": 0.9})
    assert parsed.slots.get("payee_name") == "我妈"


def test_model_supplied_payee_wins() -> None:
    parsed = parse(BIRTHDAY_TEXT, {"intent": "birthday_plan", "payee_name": "李静", "amount_fen": 30_000, "missing": [], "confidence": 0.9})
    assert parsed.slots.get("payee_name") == "李静"


def test_no_relation_word_still_asks() -> None:
    parsed = parse("帮我提前留出300块订束花", {"intent": "birthday_plan", "amount_fen": 30_000, "missing": [], "confidence": 0.9})
    assert "payee_name" not in parsed.slots
    assert parsed.question, "没有收款人线索时必须照旧反问"


def test_non_money_intent_is_not_touched() -> None:
    parsed = parse("帮我查下我妈的余额", {"intent": "query_balance", "missing": [], "confidence": 0.9})
    assert "payee_name" not in parsed.slots


@pytest.mark.parametrize(
    ("text", "expected"),
    [("我妈", "我妈"), ("老妈", "我妈"), ("妈妈", "妈妈"), ("我爸爸", "我爸"), ("老婆", "老婆")],
)
def test_relation_map(text: str, expected: str) -> None:
    assert intent._relation_payee(text) == expected


def test_seeded_alias_resolves_to_the_mother(bank: Path) -> None:
    """兜底填进去的「我妈」必须真能查到人：seed 里「妈妈」的 alias 就是「我妈」。"""
    conn = db.connect()
    try:
        ctx = tool_base.ToolContext(
            conn=conn,
            session_id="s_relation",
            node_id="n1",
            task_id="t_relation",
            account_id=tool_base.primary_account(conn)["id"],
            secret=SECRET,
        )
        result = queries.resolve_payee(ctx, {"name": "我妈"}, None)
    finally:
        conn.close()
    candidates = result.data.get("candidates") or []
    assert len(candidates) == 1, candidates
    assert candidates[0]["name"] == "妈妈"
    assert candidates[0]["card_tail"] == "7788"
