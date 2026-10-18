"""app/models.py 单测：全部 extra=forbid，字段默认值不能是共享对象。"""

from __future__ import annotations

import pytest
from pydantic import ValidationError as PydanticValidationError

from app.models import Action, ChatRequest, ConfirmRequest, DAGNode, Dag, Decision, ToolResult

_MODELS = (Action, Decision, DAGNode, Dag, ToolResult, ChatRequest, ConfirmRequest)


@pytest.mark.parametrize("model", _MODELS, ids=lambda model: model.__name__)
def test_all_models_forbid_extra(model: type) -> None:
    """多塞一个字段必须报错，防止前端偷偷混进额外参数。"""
    kwargs = _minimal_kwargs(model)
    assert model(**kwargs).model_dump() is not None
    with pytest.raises(PydanticValidationError):
        model(**kwargs, __unexpected__=1)


@pytest.mark.parametrize("model", _MODELS, ids=lambda model: model.__name__)
def test_all_models_have_type_annotations(model: type) -> None:
    for name, field in model.model_fields.items():
        assert field.annotation is not None, f"{model.__name__}.{name} 缺类型注解"


def _minimal_kwargs(model: type) -> dict[str, object]:
    samples: dict[type, dict[str, object]] = {
        Action: {"tool": "transfer", "params": {}, "account_id": "a_1"},
        Decision: {"level": "green", "rule_id": "R000", "reason_code": "OK",
                   "reason_text": "正常"},
        DAGNode: {"id": "n1", "tool": "transfer", "params": {}},
        Dag: {"id": "run1", "nodes": []},
        ToolResult: {"ok": True},
        ChatRequest: {"text": "转 500 给张三"},
        ConfirmRequest: {"session_id": "s_1", "confirmation_id": "cf_1", "approve": True},
    }
    return samples[model]


def test_defaults_are_not_shared() -> None:
    """default_factory 漏写会导致多个实例共用同一个 list，这是经典事故。"""
    first = Action(tool="t", params={}, account_id="a_1")
    second = Action(tool="t", params={}, account_id="a_1")
    first.untrusted_fields.append("note")
    assert second.untrusted_fields == []


def test_chat_request_text_length() -> None:
    with pytest.raises(PydanticValidationError):
        ChatRequest(text="")
    with pytest.raises(PydanticValidationError):
        ChatRequest(text="转" * 501)
    assert ChatRequest(text="转 500 给张三").text == "转 500 给张三"


def test_tool_result_failure_shape() -> None:
    result = ToolResult(ok=False, error_code="INSUFFICIENT_FUNDS", error_text="余额不足")
    assert result.ok is False
    assert result.data == {}
    assert result.error_code == "INSUFFICIENT_FUNDS"
