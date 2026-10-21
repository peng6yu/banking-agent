"""生日送礼场景（birthday_plan）的意图与 DAG 测试（docs/07 §7.2 / §7.3）。

守住四条线：
1. 意图枚举的每个取值都有说明——模型不认生日安排就只会回 unknown 再反问（场景 6 走不通）；
2. 生日三个槽位（purpose/gift/date_hint）必须在 schema 与白名单里同时出现，
   缺一处那个槽位就是死代码（merchant、card_tail 就这样丢过一次）；
3. 缺槽位时反问有话说（MISSING_LABELS 里查得到），一次只问一个；
4. birthday_plan 仍是固定四节点模板，模型只填槽位、不定拓扑。
"""
from __future__ import annotations

import ast
import asyncio
import json
from pathlib import Path
from typing import Any

from app.llm import LLMResponse, ToolCall
from app.planner import dag as planner_dag
from app.planner import intent

DOC_07 = Path(__file__).resolve().parents[1] / "docs" / "07_规划器_API与前端规格.md"
BIRTHDAY_SLOTS = ("purpose", "gift", "date_hint")


class Scripted:
    """按剧本回放模型回复；元素是 dict 就当 emit_intent 的参数。"""

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
                ToolCall(
                    id="c1",
                    name="emit_intent",
                    arguments=json.dumps(item, ensure_ascii=False),
                )
            ],
            finish_reason="tool_calls",
            model="scripted",
        )


def parse(text: str, adapter: Any) -> intent.IntentResult:
    return asyncio.run(intent.parse(text, [], adapter=adapter))


def _schema_block() -> str:
    """docs/07 §7.2 里 emit_intent 工具 schema 的那个代码块。"""
    text = DOC_07.read_text(encoding="utf-8")
    head = text.index("### `emit_intent` 工具 schema")
    start = text.index("```python", head) + len("```python")
    end = text.index("```", start)
    return text[start:end].strip()


def test_every_intent_value_is_explained_in_the_schema() -> None:
    """intent 的 description 要逐个点名每个取值。

    历史缺陷：intent 只有 enum 没有说明，真实模型把「下周三我妈生日，提前留出300块
    给她订束花」判成 unknown，场景 6 的四节点 DAG 根本建不起来。
    """
    props = intent.emit_intent_schema()["parameters"]["properties"]
    description = props["intent"]["description"]
    for name in intent.INTENT_NAMES:
        assert name in description, f"intent 说明里没有 {name}"
    assert "birthday_plan" in description
    assert "预留" in description and "生日" in description


def test_birthday_slots_are_declared_in_schema_and_whitelist() -> None:
    """生日三个槽位必须同时在 SLOT_KEYS 与 schema 里，且都带说明。"""
    props = intent.emit_intent_schema()["parameters"]["properties"]
    for slot in BIRTHDAY_SLOTS:
        assert slot in intent.SLOT_KEYS, f"白名单漏了 {slot}"
        assert slot in props, f"schema 漏了 {slot}"
        assert props[slot]["description"], f"{slot} 没有说明，模型填不对"


def test_missing_labels_cover_the_birthday_slots() -> None:
    """缺槽位要反问，但得问得出话：MISSING_LABELS 里必须有这三个槽位。"""
    for slot in BIRTHDAY_SLOTS:
        assert slot in intent.MISSING_LABELS, f"缺 {slot} 时拼不出反问"
        assert intent.MISSING_LABELS[slot].strip()


def test_birthday_request_keeps_its_slots() -> None:
    """模型给的生日槽位不能被白名单丢掉。"""
    result = parse(
        "下周三我妈生日，帮我提前留出300块，到时候给她订束花送过去",
        Scripted([{
            "intent": "birthday_plan",
            "payee_name": "李静",
            "amount_fen": 30_000,
            "purpose": "生日礼物",
            "gift": "花束",
            "date_hint": "下周三",
            "missing": [],
            "confidence": 0.9,
        }]),
    )
    assert result.intent == "birthday_plan"
    assert result.slots["purpose"] == "生日礼物"
    assert result.slots["gift"] == "花束"
    assert result.slots["date_hint"] == "下周三"
    assert result.slots["amount_fen"] == 30_000
    assert result.question is None
    assert result.ok is True


def test_a_missing_birthday_slot_asks_one_question() -> None:
    """缺 gift 就问 gift，一次只问一个，槽位不丢。"""
    result = parse(
        "下周三我妈生日，提前留出300块",
        Scripted([{
            "intent": "birthday_plan",
            "payee_name": "李静",
            "amount_fen": 30_000,
            "purpose": "生日礼物",
            "date_hint": "下周三",
            "missing": ["gift"],
            "confidence": 0.9,
        }]),
    )
    assert result.ok is False
    assert result.missing == ["gift"]
    assert result.question == intent.MISSING_LABELS["gift"]


def test_birthday_dag_is_the_fixed_four_node_template() -> None:
    """四节点固定模板：resolve_payee → query_balance → reserve_funds → pay_merchant。"""
    built = planner_dag.build({
        "intent": "birthday_plan",
        "slots": {
            "payee_name": "李静",
            "amount_fen": 30_000,
            "purpose": "生日礼物",
            "gift": "花束",
            "date_hint": "下周三",
        },
    })
    assert [node.tool for node in built.nodes] == [
        "resolve_payee", "query_balance", "reserve_funds", "pay_merchant",
    ]
    reserve = built.nodes[2]
    merchant = built.nodes[3]
    assert reserve.params["amount_fen"] == 30_000
    assert reserve.params["purpose"] == "生日礼物"
    assert merchant.params["merchant"] == "flower_shop"
    # gift 槽位要真落到 pay_merchant 的 item 上：原来读的是不在白名单里的 item，等于永远拿不到。
    assert merchant.params["item"] == "花束"


def test_doc07_schema_block_matches_the_code() -> None:
    """docs/07 §7.2 的 schema 代码块必须与 emit_intent_schema() 逐字一致。

    文档是评审现场唯一能对着看的东西；它和代码一漂，评委按文档验收就会判我们说谎。
    """
    assert ast.literal_eval(_schema_block()) == intent.emit_intent_schema()

def test_a_hallucinated_missing_slot_is_normalized_not_asked_back() -> None:
    """模型报了白名单里没有的槽位名（真模型实测报的是 birthday_date）→ 归一，不反问。

    反问一个我们自己都填不了的槽位没有意义：用户答了也进不了任何工具参数，
    反而把一次本来能跑完的请求卡在一句反问上。
    """
    result = parse(
        "帮我订一束花送李静当生日礼物，先留出300块",
        Scripted([{
            "intent": "birthday_plan",
            "payee_name": "李静",
            "amount_fen": 30_000,
            "purpose": "生日礼物",
            "gift": "花束",
            "date_hint": "生日当天",
            "missing": ["birthday_date"],
            "confidence": 0.86,
        }]),
    )
    assert result.missing == []
    assert result.question is None
    assert result.ok is True


def test_an_unknown_missing_key_is_dropped() -> None:
    """归一不出来的键一律丢掉：missing 里不该出现我们不认识的槽位名。"""
    result = parse(
        "帮我订一束花送李静当生日礼物，先留出300块",
        Scripted([{
            "intent": "birthday_plan",
            "payee_name": "李静",
            "amount_fen": 30_000,
            "missing": ["whatever_the_model_made_up"],
            "confidence": 0.86,
        }]),
    )
    assert result.missing == []
    assert result.ok is True
