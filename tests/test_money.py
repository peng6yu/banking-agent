"""app/money.py 单测：金额换算 + 中文口语解析。"""

from __future__ import annotations

import pytest

from app.clock import ValidationError
from app.money import fen_to_yuan_str, parse_amount, yuan_to_fen


@pytest.mark.parametrize(
    ("text", "expect_fen"),
    [
        ("500", 50_000),
        ("五百", 50_000),
        ("500块", 50_000),
        ("500元", 50_000),
        ("五百五", 55_000),
        ("一千二", 120_000),
        ("500.5", 50_050),
        ("五百五毛", 50_050),
        ("两块五", 250),
        ("两万三千五百", 2_350_000),
        ("1234.56元", 123_456),
    ],
)
def test_parse_amount_normal_cases(text: str, expect_fen: int) -> None:
    assert parse_amount(text) == expect_fen


@pytest.mark.parametrize("text", ["全部", "所有钱"])
def test_parse_amount_all_money(text: str) -> None:
    """「全部」这类全额指令要能识别出来，交给上层转成查询全部流水。"""
    assert parse_amount(text) == -1


@pytest.mark.parametrize("text", ["", "   ", "abc", "不知道多少"])
def test_parse_amount_illegal_returns_minus_one(text: str) -> None:
    """非法输入默认返回 -1（上层据此反问），而不是抛异常打断会话。"""
    assert parse_amount(text) == -1


def test_parse_amount_strict_raises() -> None:
    with pytest.raises(ValidationError) as excinfo:
        parse_amount("嗯嗯", strict=True)
    assert excinfo.value.user_text == "没听懂金额"


def test_yuan_to_fen() -> None:
    assert yuan_to_fen(1000.0) == 100_000
    assert yuan_to_fen("1000.01") == 100_001
    assert yuan_to_fen(12) == 1_200


@pytest.mark.parametrize(
    ("value", "message"),
    [
        (0, "金额必须大于 0"),
        (-5, "金额必须为正"),
        (1_000_000.005, "精度超过分"),
        (2_000_000, "单笔超过 100 万元"),
    ],
)
def test_yuan_to_fen_rejects(value: float, message: str) -> None:
    with pytest.raises(ValidationError) as excinfo:
        yuan_to_fen(value)
    assert excinfo.value.user_text == message


def test_fen_to_yuan_str() -> None:
    assert fen_to_yuan_str(1_286_050) == "12,860.50"
    assert fen_to_yuan_str(100) == "1.00"
    assert fen_to_yuan_str(5) == "0.05"
    assert fen_to_yuan_str(-550) == "-5.50"
