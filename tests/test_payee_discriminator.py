"""payee_card_tail 专属槽位：模型终于有个明确的地方放收款人尾号。

真机实测两次（MiniMax-M3.1-Flash-Preview）「转 500 元给张三尾号 4417」：
  第一次 slots = {"payee_name": "张三", "amount_fen": 50000, "payee_phone": "4417"}
  第二次 slots = {"payee_name": "张三", "amount_fen": 50000, "note": "尾号4417"}
两次都没填 card_tail（那个槽位原本写着「转账不需要」）。所以只认一条通道是打地鼠，
现在四条通道都认，且加了专属槽位让模型第一次就能放对地方。
"""

from __future__ import annotations

from typing import Any

from app.planner.dag import _payee_params
from app.planner.intent import SLOT_KEYS
from app.tools import queries
from tests.tool_fixtures import bank, ctx

__all__ = ["bank", "ctx"]


def _candidates(result: Any) -> list[dict[str, Any]]:
    return list(result.data.get("candidates") or [])


def test_dedicated_slot_exists_in_the_whitelist() -> None:
    """没有专属槽位，模型就只能往 note / payee_phone / card_tail 里乱塞。"""
    assert "payee_card_tail" in SLOT_KEYS


def test_dedicated_slot_is_used_first() -> None:
    params = _payee_params({"payee_name": "张三", "payee_card_tail": "4417"})
    assert params == {"name": "张三", "card_tail": "4417"}


def test_model_parking_the_tail_in_note_still_works() -> None:
    """第二次实测：模型把「尾号4417」整个塞进 note。"""
    params = _payee_params({"payee_name": "张三", "note": "尾号4417"})
    assert params.get("card_tail") == "4417"


def test_model_parking_the_tail_in_payee_phone_still_works() -> None:
    """第一次实测：模型把 4417 当手机号后四位塞进 payee_phone。"""
    params = _payee_params({"payee_name": "张三", "payee_phone": "4417"})
    assert params.get("card_tail") == "4417"


def test_model_parking_the_tail_in_card_tail_still_works() -> None:
    params = _payee_params({"payee_name": "张三", "card_tail": "4417"})
    assert params.get("card_tail") == "4417"


def test_note_without_the_keyword_is_not_treated_as_a_tail() -> None:
    """note 里随便一个 4 位数不能被当成尾号，否则就是猜人。"""
    params = _payee_params({"payee_name": "张三", "note": "买咖啡 1234 元"})
    assert "card_tail" not in params


def test_full_phone_number_is_passed_as_phone_not_tail() -> None:
    params = _payee_params({"payee_name": "李静", "payee_phone": "13900138001"})
    assert params == {"name": "李静", "phone": "13900138001"}


def test_tail_narrows_two_same_name_payees_to_one(ctx) -> None:
    """端到端效果：带尾号后只剩一个人，规划器就会往下走黄灯确认而不是反问。"""
    assert len(_candidates(queries.resolve_payee(ctx, {"name": "张三"}))) == 2
    narrowed = queries.resolve_payee(
        ctx, _payee_params({"payee_name": "张三", "payee_card_tail": "4417"})
    )
    rows = _candidates(narrowed)
    assert len(rows) == 1
    assert rows[0]["card_tail"] == "4417"


def test_wrong_tail_yields_nobody_instead_of_the_other_person(ctx) -> None:
    """用户点名的尾号对不上就一条都不留，不悄悄换成另一个同名的。"""
    result = queries.resolve_payee(ctx, {"name": "张三", "card_tail": "9999"})
    assert result.ok is True
    assert _candidates(result) == []