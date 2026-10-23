"""用户可见文案：反问里不能出现内部标识（意图枚举名、槽位名）。

t49 修的两处缺陷：
1. 低置信度反问原本念的是内部枚举名——「我没太确定你是要「query_balance」这件事」；
2. 缺槽位反问只看槽位不分意图，生日送礼也问「请问转给谁？」。

这里用几条断言钉住修好的行为：低置信度念中文意图说法、缺槽位按意图区分、
未知意图仍走 UNKNOWN_QUESTION 原文。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from app.llm import LLMResponse, ToolCall
from app.planner import intent


class Scripted:
    """按剧本回放模型回复（和 tests/test_planner.py 同一个套路）。"""

    def __init__(self, script: list[Any]) -> None:
        self.script = list(script)
        self.calls = 0

    def chat(self, messages: list[dict[str, Any]], **kwargs: Any) -> LLMResponse:
        item = self.script[min(self.calls, len(self.script) - 1)]
        self.calls += 1
        return LLMResponse(
            content="",
            reasoning="",
            tool_calls=[
                ToolCall(id="c1", name="emit_intent", arguments=json.dumps(item, ensure_ascii=False))
            ],
            finish_reason="tool_calls",
            model="scripted",
        )


def parse(text: str, adapter: Any) -> intent.IntentResult:
    """intent.parse 是协程，测试里用 asyncio.run 包一层（同 tests/test_planner.py）。"""
    return asyncio.run(intent.parse(text, [], adapter=adapter))


@pytest.mark.parametrize("intent_name", [n for n in intent.INTENT_NAMES if n != "unknown"])
def test_low_confidence_question_speaks_chinese_not_enum_name(intent_name: str) -> None:
    """低置信度反问：念中文意图说法，不出现 snake_case 内部名。"""
    result = parse(
        "帮我看看",
        Scripted(
            [
                {
                    "intent": intent_name,
                    "payee_name": "李静",
                    "amount_fen": 50000,
                    # split_bill 要名单、schedule_transfer 要日子：不补齐的话
                    # 反问会先被「缺槽位」分支接住，测不到低置信度这条路径
                    "payee_names": ["张三", "李静"],
                    "due_hint": "下周三",
                    "missing": [],
                    "confidence": 0.1,
                }
            ]
        ),
    )
    assert result.ok is False
    assert result.question is not None
    assert "_" not in result.question
    assert intent_name not in result.question
    assert intent.INTENT_LABELS[intent_name] in result.question
    assert "确认" in result.question


def test_unmapped_intent_name_is_not_spoken_to_user() -> None:
    """模型给了个我们没登记的意图名，也不能把它念给用户。"""
    result = parse("帮我看看", Scripted([{"intent": "foo_bar", "missing": [], "confidence": 0.1}]))
    assert result.question is not None
    assert "foo_bar" not in result.question
    assert "_" not in result.question


def test_birthday_missing_payee_asks_who_the_birthday_is_for() -> None:
    """生日送礼缺收款人：问「给谁过生日」，不能问得像在办转账。

    注意原句里**不能**出现亲属称谓：出现了就会被 app/planner/intent.py 的
    _relation_payee 兜底填成收款人，这一轮根本不会反问（已知限制 4 的修复）。
    """
    result = parse(
        "下周三生日，帮我提前留出300块，到时候订束花送过去",
        Scripted(
            [
                {
                    "intent": "birthday_plan",
                    "amount_fen": 30000,
                    "missing": ["payee_name"],
                    "confidence": 0.9,
                }
            ]
        ),
    )
    assert result.ok is False
    assert result.question is not None
    assert "转给谁" not in result.question
    assert "给谁过生日" in result.question


def test_transfer_missing_payee_still_asks_who_to_transfer_to() -> None:
    """转账缺收款人：仍然是「请问转给谁？」（原口径不变）。"""
    result = parse(
        "转500元",
        Scripted([{"intent": "transfer", "amount_fen": 50000, "missing": ["payee_name"], "confidence": 0.9}]),
    )
    assert result.ok is False
    assert result.question == "请问转给谁？"


def test_unknown_intent_still_uses_unknown_question() -> None:
    """未知意图仍然走 UNKNOWN_QUESTION 原文，别被改坏。"""
    result = parse("你好", Scripted([{"intent": "unknown", "missing": [], "confidence": 0.9}]))
    assert result.question == intent.UNKNOWN_QUESTION
    assert result.ok is False


def test_missing_slot_fallback_never_speaks_slot_identifier() -> None:
    """兜底反问也不能把内部槽位名念给用户。"""
    result = parse("帮我看看", Scripted([{"intent": "query_balance", "missing": ["merchant"], "confidence": 0.9}]))
    assert result.question is not None
    assert "merchant" not in result.question
    assert "_" not in result.question
