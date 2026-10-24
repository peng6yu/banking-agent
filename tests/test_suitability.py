"""投资者适当性的单元测试（阶段 6 P0-6）。

审计发现：改动前 app/ 里根本没有"适当性"三个字，买基金只看金额，
界面却可以说"已做风险适配"。这一组把问卷本身、画像落库、以及
"没测评 / 等级不够就不许申购"三件事钉死。

刻意不测的东西：真实监管口径的问卷设计、双录、冷静期。这些是演示简化版，
模块 docstring 和回执里都照实写了 disclaimer，测不了就不假装有。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import httpx
import pytest

from app import db, suitability
from app.main import app
from app.planner import executor
from app.tools import registry
from app.tools.base import ToolContext
from scripts.seed import DEBIT_ACCOUNT_ID, seed
from tests.tool_fixtures import DEBIT_BALANCE, USER_ID, _balance, _token, bank, ctx

__all__ = ["bank", "ctx"]

MONEY_CODE = "F00001"    # R1
EQUITY_CODE = "F00004"   # R4，起购 500 元
BASE_URL = "http://testserver"


def _answers(**over: str) -> dict[str, str]:
    """一套合法的 5 题答案，按需覆盖其中几题。"""
    base = {
        "q_horizon": "1y_3y",
        "q_experience": "mixed",
        "q_loss": "hold",
        "q_goal": "growth",
        "q_share": "10_30",
    }
    base.update(over)
    return base


# ---------------------------------------------------------------- 问卷本身


@pytest.mark.parametrize(
    ("score_answers", "level"),
    [
        ({"q_horizon": "lt_6m", "q_experience": "deposit_only", "q_loss": "sell_all",
          "q_goal": "preserve", "q_share": "gt_50"}, "C1"),
        ({"q_horizon": "6m_1y", "q_experience": "money_bond", "q_loss": "sell_part",
          "q_goal": "steady", "q_share": "30_50"}, "C2"),
        (_answers(), "C3"),
        ({"q_horizon": "gt_3y", "q_experience": "equity", "q_loss": "hold",
          "q_goal": "growth", "q_share": "10_30"}, "C4"),
        ({"q_horizon": "gt_3y", "q_experience": "equity", "q_loss": "buy_more",
          "q_goal": "aggressive", "q_share": "lt_10"}, "C5"),
    ],
)
def test_score_maps_to_the_documented_bands(score_answers: dict[str, str], level: str) -> None:
    """0/4/8/12/14 五档边界：每一档都要落在 LEVEL_BANDS 写的地方。"""
    result = suitability.assess(score_answers, now=0)
    assert result["level"] == level
    band = next(item for item in suitability.LEVEL_BANDS if item[1] == level)
    assert band[0] <= result["score"]
    assert result["capacity"] == band[3]
    assert result["capacity_label"] == f"最高可买 R{band[3]}"


def test_the_middle_answers_land_on_balance_型() -> None:
    """1y_3y + mixed + hold + growth + 10_30 = 10 分，C3 平衡型、最高 R3。"""
    result = suitability.assess(_answers(), now=0)
    assert result["score"] == 10
    assert result["level"] == "C3"
    assert result["level_label"] == "平衡型"
    assert result["capacity"] == 3
    assert result["horizon_years"] == 3.0
    assert result["loss_tolerance"] == "能接受阶段性回撤"
    assert "不是监管口径" in result["disclaimer"]


def test_missing_answers_are_refused_with_a_counted_message() -> None:
    """少答一题不许"按已答的算"：报错并把还缺几题说清楚。"""
    partial = _answers()
    partial.pop("q_loss")
    with pytest.raises(Exception) as excinfo:
        suitability.normalize(partial)
    err = excinfo.value
    assert "还差 1 题" in str(err)


def test_unknown_question_or_option_is_refused() -> None:
    """模型编出来的题号或选项一律拒，不允许静默忽略。"""
    with pytest.raises(Exception):
        suitability.normalize({**_answers(), "q_favorite_color": "blue"})
    with pytest.raises(Exception):
        suitability.normalize(_answers(q_loss="risk_all_in"))


# ---------------------------------------------------------------- 落库与读取


def test_save_then_load_round_trips_and_audits(ctx: ToolContext) -> None:
    """保存 -> 读回 -> 审计里有 suitability.assessed；再存一次是更新不是新增。"""
    before = [
        str(row["event_type"])
        for row in ctx.conn.execute("SELECT event_type FROM audit_log ORDER BY seq")
    ]
    first = suitability.save(ctx.conn, USER_ID, _answers())
    assert first["level"] == "C3"
    loaded = suitability.load(ctx.conn, USER_ID)
    assert loaded is not None
    assert loaded["level"] == "C3"
    assert loaded["answers"]["q_goal"] == "growth"
    rows = ctx.conn.execute(
        "SELECT COUNT(*) AS n FROM investor_profiles WHERE user_id = ?", (USER_ID,)
    ).fetchone()
    assert int(rows["n"]) == 1
    created = int(loaded["created_ms"])

    second = suitability.save(ctx.conn, USER_ID, _answers(q_goal="aggressive"))
    assert second["score"] > first["score"]
    again = suitability.load(ctx.conn, USER_ID)
    assert again is not None
    assert int(again["created_ms"]) == created
    assert again["answers"]["q_goal"] == "aggressive"
    kinds = [
        str(row["event_type"])
        for row in ctx.conn.execute("SELECT event_type FROM audit_log ORDER BY seq")
    ]
    # bank fixture 已经写过一次画像，这里只数本用例新增的两条
    assert kinds.count("suitability.assessed") == before.count("suitability.assessed") + 2


# ---------------------------------------------------------------- 闸门


def test_gate_refuses_when_there_is_no_profile(ctx: ToolContext) -> None:
    ctx.conn.execute("DELETE FROM investor_profiles")
    product = ctx.conn.execute("SELECT * FROM fund_products WHERE code = ?", (MONEY_CODE,)).fetchone()
    assert product is not None
    gate = suitability.check(ctx.conn, USER_ID, product)
    assert gate["ok"] is False
    assert gate["reason"] == "SUITABILITY_NOT_ASSESSED"
    assert gate["next"] == "assess_suitability"
    assert gate["profile"] is None


def test_gate_refuses_a_product_above_the_profile_capacity(ctx: ToolContext) -> None:
    suitability.save(ctx.conn, USER_ID, {
        "q_horizon": "lt_6m", "q_experience": "deposit_only", "q_loss": "sell_all",
        "q_goal": "preserve", "q_share": "gt_50",
    })
    product = ctx.conn.execute("SELECT * FROM fund_products WHERE code = ?", (EQUITY_CODE,)).fetchone()
    assert product is not None
    gate = suitability.check(ctx.conn, USER_ID, product)
    assert gate["ok"] is False
    assert gate["reason"] == "PRODUCT_RISK_TOO_HIGH"
    assert "R4" in gate["user_text"] and "C1" in gate["user_text"]
    assert gate["next"] == "list_products"


def test_gate_passes_when_the_profile_is_high_enough(ctx: ToolContext) -> None:
    product = ctx.conn.execute("SELECT * FROM fund_products WHERE code = ?", (EQUITY_CODE,)).fetchone()
    assert product is not None
    gate = suitability.check(ctx.conn, USER_ID, product)
    assert gate["ok"] is True
    assert not gate["reason"]


def test_buy_fund_is_refused_without_a_profile_and_moves_no_money(ctx: ToolContext) -> None:
    """没有画像：申购直接拒，余额、流水、份额都不许动。"""
    ctx.conn.execute("DELETE FROM investor_profiles")
    before_tx = int(ctx.conn.execute("SELECT COUNT(*) AS n FROM transactions").fetchone()["n"])
    params: dict[str, Any] = {"product_code": MONEY_CODE, "amount_fen": 20_000}
    result = registry.execute("buy_fund", params, token=_token("buy_fund", params), ctx=ctx)
    assert result.ok is False
    assert result.error_code == "SUITABILITY_NOT_ASSESSED"
    assert _balance(ctx.conn) == DEBIT_BALANCE
    assert int(ctx.conn.execute("SELECT COUNT(*) AS n FROM transactions").fetchone()["n"]) == before_tx
    kinds = [
        str(row["event_type"])
        for row in ctx.conn.execute("SELECT event_type FROM audit_log ORDER BY seq")
    ]
    assert "suitability.checked" in kinds


# ---------------------------------------------------------------- HTTP 接口


@pytest.fixture()
def seeded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(db, "DEFAULT_DB_PATH", tmp_path / "bank.db")
    conn = db.connect()
    db.init_db(conn)
    seed(conn)
    conn.close()
    executor.RUNS.clear()
    yield tmp_path / "bank.db"
    executor.RUNS.clear()


def _call(method: str, url: str, payload: dict[str, Any] | None = None) -> httpx.Response:
    async def run() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url=BASE_URL) as http:
            return await http.request(method, url, json=payload)

    return asyncio.run(run())


def test_http_get_exposes_the_questions_when_nothing_is_assessed(seeded: Path) -> None:
    """没测评时前端要能直接渲染题目，所以 GET 必须把 5 道题和选项一起给。"""
    res = _call("GET", "/api/investment/suitability")
    assert res.status_code == 200
    data = res.json()
    assert data["assessed"] is False
    assert data["profile"] is None
    assert {q["id"] for q in data["questions"]} == {
        "q_horizon", "q_experience", "q_loss", "q_goal", "q_share",
    }
    assert all(q["options"] for q in data["questions"])
    assert "不是监管口径" in data["disclaimer"]


def test_http_post_saves_the_profile_and_get_reads_it_back(seeded: Path) -> None:
    res = _call("POST", "/api/investment/suitability", {"answers": _answers()})
    assert res.status_code == 200
    profile = res.json()["profile"]
    assert profile["level"] == "C3"
    assert profile["capacity"] == 3
    again = _call("GET", "/api/investment/suitability").json()
    assert again["assessed"] is True
    assert again["profile"]["level"] == "C3"


def test_http_post_rejects_an_incomplete_questionnaire(seeded: Path) -> None:
    res = _call("POST", "/api/investment/suitability", {"answers": {"q_goal": "growth"}})
    assert res.status_code == 400
    assert "题" in res.json()["user_text"]


def test_the_seeded_demo_user_starts_without_a_profile(seeded: Path) -> None:
    """种子库不带画像：演示"没测评就买不了"这条路径要能复现。"""
    conn = db.connect()
    try:
        rows = conn.execute("SELECT COUNT(*) AS n FROM investor_profiles").fetchone()
    finally:
        conn.close()
    assert int(rows["n"]) == 0
    # DEBIT_ACCOUNT_ID 只是确认种子库确实是那份演示数据
    assert DEBIT_ACCOUNT_ID.startswith("a_")
