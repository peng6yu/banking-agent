"""场景四卡片管理的意图与 DAG 测试（docs/07 §7.2 / §7.3）。

守住四条线：
1. 「冻结/挂失/调限额/申请虚拟卡/查卡」都能从 chat 走进 card_manage 意图；
2. 动作只从**用户原话**里读，不认模型填的 card_action——写操作必须用户自己说出口，
   模型顺手填一个 loss 就把一次查询变成挂失，这是这套系统最不该犯的错；
3. 要动某一张卡却没说是哪张时反问一次，绝不按主账户随便挑一张；
4. 卡片管理仍是固定单步模板，模型只填槽位、不定拓扑。
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from app.clock import ValidationError
from app.llm import LLMResponse, ToolCall
from app.planner import card_actions
from app.planner import dag as planner_dag


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


def _slots(text: str, **extra: Any) -> dict[str, Any]:
    slots: dict[str, Any] = dict(extra)
    return card_actions.card_action(slots, text)


@pytest.mark.parametrize(
    ("text", "action"),
    [
        ("把我尾号1234的卡冻结了", "freeze"),
        ("尾号1234的卡挂失了", "loss"),
        ("我卡丢了", "loss"),
        ("把尾号1234的卡解冻", "unfreeze"),
        ("给尾号1234的卡设个2000的限额", "set_limit"),
        ("申请一张虚拟卡", "apply"),
        ("我有几张卡", "query"),
        ("看看我的银行卡", "query"),
    ],
)
def test_action_is_read_from_the_users_own_words(text: str, action: str) -> None:
    assert _slots(text)["card_action"] == action


def test_unfreeze_is_not_swallowed_by_freeze() -> None:
    """「解冻」里含「冻」，顺序判错就会把解冻听成冻结。"""
    assert _slots("帮我解冻尾号1234的卡")["card_action"] == "unfreeze"
    assert _slots("帮我把尾号1234的卡冻了")["card_action"] == "freeze"


def test_model_supplied_action_is_overridden_by_the_users_words() -> None:
    """模型顺手填的 card_action 一律作废：写操作只能由用户自己说出口。

    这是本模块最重要的一条——用户在问「我有几张卡」，模型却填了 loss，
    跟过去就是把一次查询变成一次挂失。
    """
    slots = card_actions.card_action({"card_action": "loss"}, "我有几张卡")
    assert slots["card_action"] == "query"


def test_missing_tail_asks_back_instead_of_guessing() -> None:
    with pytest.raises(ValidationError) as caught:
        card_actions.card_params("freeze_card", {"card_action": "freeze"})
    assert "哪张卡" in str(caught.value)


def test_missing_limit_amount_asks_back() -> None:
    with pytest.raises(ValidationError) as caught:
        card_actions.card_params(
            "set_card_limit",
            {"card_action": "set_limit", "card_tail": "1234"},
        )
    assert "限额" in str(caught.value)


def test_query_and_apply_do_not_require_a_tail() -> None:
    assert card_actions.card_params("list_cards", {}) == {}
    assert card_actions.card_params("apply_virtual_card", {}) == {}


def test_each_action_maps_to_one_fixed_single_step_template() -> None:
    """六路各一步：卡片工具之间没有数据依赖，不需要多节点。"""
    assert card_actions.CARD_TEMPLATES["query"] == (("n1", "list_cards"),)
    assert card_actions.CARD_TEMPLATES["loss"] == (("n1", "report_card_loss"),)
    for template in card_actions.CARD_TEMPLATES.values():
        assert len(template) == 1


def test_dag_builds_the_freeze_node_with_the_tail_the_user_said() -> None:
    built = planner_dag.build({
        "intent": "card_manage",
        "slots": {"card_action": "freeze", "card_tail": "1234"},
    })
    assert [node.tool for node in built.nodes] == ["freeze_card"]
    assert built.nodes[0].params == {"card_tail": "1234"}


def test_dag_defaults_to_listing_cards() -> None:
    built = planner_dag.build({"intent": "card_manage", "slots": {}})
    assert [node.tool for node in built.nodes] == ["list_cards"]


def test_dag_set_limit_carries_both_tail_and_amount() -> None:
    built = planner_dag.build({
        "intent": "card_manage",
        "slots": {"card_action": "set_limit", "card_tail": "1234", "amount_fen": 200_000},
    })
    assert built.nodes[0].params == {"card_tail": "1234", "per_tx_limit_fen": 200_000}


@pytest.mark.parametrize(
    ("tool", "data", "expect"),
    [
        ("freeze_card", {"summary": "尾号 1234 的卡已冻结，现在不能再用它付款。"}, "尾号 1234 的卡已冻结"),
        ("report_card_loss", {"summary": "尾号 5678 的卡已挂失"}, "已挂失"),
        ("apply_virtual_card", {"summary": "已为你开出一张虚拟卡，尾号 9000。"}, "尾号 9000"),
        ("set_card_limit", {"summary": "尾号 1234 的单笔限额已设为 2,000.00 元。"}, "限额已设为"),
    ],
)
def test_tool_summary_is_human_not_the_internal_tool_name(
    tool: str, data: dict[str, Any], expect: str
) -> None:
    """SSE 的 tool 事件要说人话，不能把内部工具名念给用户听。

    历史缺陷：卡片五件套没登记到 events 的文案表，用户看到的是
    「freeze_card 执行完成」——和白灯卡念 payee_id 是同一类错。
    """
    from app.planner import events

    line = events.summarize(tool, data)
    assert expect in line
    assert tool not in line


def test_every_card_tool_is_registered_in_the_authoritative_table() -> None:
    """模板里出现的工具必须真的在册，否则执行时才炸（R11 才不会替你兜）。"""
    from app.tools import registry

    assert card_actions.CARD_TOOLS <= set(registry.TOOLS)
