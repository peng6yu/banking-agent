"""真实渠道与模拟数据：把「演示模式」和「生产模式」的边界钉在代码里。

任务书要求两件事，这里各有一组断言：

1. 真实渠道未配置时，非演示模式的敏感操作必须 fail closed——
   关掉 FACTOR_DEMO_ECHO 后如果没有声明 FACTOR_PROVIDER，服务端拒绝发码，
   既不落挑战也不放行，接口回 503 而不是假装发过了。
2. 理财产品是比赛用的模拟数据，未知字段（期限/流动性/费率）必须是 null，
   并且明确标注 contest_simulation；没有做产品对比就写 supported=false。
"""

from __future__ import annotations

import asyncio
import sqlite3

import httpx
import pytest

from app import db, suitability
from app.api.business import management_view
from app.main import app
from app.security import factors
from scripts.seed import seed


@pytest.fixture()
def bank(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DEFAULT_DB_PATH", tmp_path / "bank.db")
    monkeypatch.delenv("API_TOKEN", raising=False)
    monkeypatch.delenv("API_TOKEN_FILE", raising=False)
    monkeypatch.setenv("API_AUTH_DISABLED", "1")
    conn = db.connect(tmp_path / "bank.db")
    db.init_db(conn)
    seed(conn)
    yield conn
    conn.close()


def _challenge_rows(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT COUNT(*) FROM factor_challenges").fetchone()[0])


def _pending_confirmation(conn: sqlite3.Connection, *, session_id: str = "s_ch") -> str:
    """手插一张 pending 确认 + 它的决策，只为让 /api/factor/challenge 有东西可查。"""
    from app.clock import now_ms
    from app.models import Action

    now = now_ms()
    conn.execute(
        "INSERT OR IGNORE INTO sessions (id, created_ms, last_ms, title, status)"
        " VALUES (?, ?, ?, 'challenge test', 'active')",
        (session_id, now, now),
    )
    action = Action(
        tool="transfer",
        params={"payee_id": "p_lij", "amount_fen": 200_000},
        account_id="a_3d7e60c2",
        amount_fen=200_000,
    )
    conn.execute(
        "INSERT INTO dag_runs (id, session_id, dag_json, status, created_ms, updated_ms)"
        " VALUES ('r_ch', ?, '{}', 'waiting', ?, ?)",
        (session_id, now, now),
    )
    conn.execute(
        "INSERT INTO dag_nodes (id, run_id, node_id, tool, params_json, state, decision_json, ts_ms)"
        " VALUES ('dn_ch', 'r_ch', 'n3', 'transfer', ?, 'waiting_factor', ?, ?)",
        (
            action.model_dump_json(),
            '{"level":"red","rule_id":"R08_transfer_red_single","reason_code":"TRANSFER_RED","required_factors":["face","sms"]}',
            now,
        ),
    )
    conn.execute(
        "INSERT INTO confirmations (id, session_id, run_id, node_id, action_json, status, created_ms, expires_ms)"
        " VALUES ('cf_ch', ?, 'r_ch', 'n3', ?, 'pending', ?, ?)",
        (session_id, action.model_dump_json(), now, now + 120_000),
    )
    return "cf_ch"


def _post(path: str, payload: dict) -> httpx.Response:
    async def go() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.post(path, json=payload)

    return asyncio.run(go())


def test_demo_mode_still_echoes_a_six_digit_code(bank, monkeypatch):
    monkeypatch.delenv(factors.ECHO_ENV, raising=False)
    monkeypatch.delenv(factors.PROVIDER_ENV, raising=False)
    issued = factors.issue(bank, session_id="s_ch", confirmation_id="cf_ch", factor="sms")
    assert issued["delivery"] == "demo_echo"
    assert issued["provider"] == "demo_stub"
    assert isinstance(issued["code"], str) and len(issued["code"]) == 6


def test_production_mode_without_a_channel_refuses_to_issue(bank, monkeypatch):
    """关掉演示回显又没声明渠道：不发码、不落挑战、不进审计。"""
    monkeypatch.setenv(factors.ECHO_ENV, "0")
    monkeypatch.delenv(factors.PROVIDER_ENV, raising=False)
    with pytest.raises(Exception) as caught:
        factors.issue(bank, session_id="s_ch", confirmation_id="cf_ch", factor="sms")
    assert "FACTOR_PROVIDER" in str(caught.value)
    assert _challenge_rows(bank) == 0
    kinds = [row["event_type"] for row in bank.execute("SELECT event_type FROM audit_log")]
    assert "factor.challenge_issued" not in kinds


def test_production_mode_with_a_declared_channel_sends_out_of_band(bank, monkeypatch):
    monkeypatch.setenv(factors.ECHO_ENV, "0")
    monkeypatch.setenv(factors.PROVIDER_ENV, "sms_gateway")
    issued = factors.issue(bank, session_id="s_ch", confirmation_id="cf_ch", factor="sms")
    assert issued["delivery"] == "out_of_band"
    assert issued["code"] is None
    assert issued["provider"] == "sms_gateway"
    assert "sms_gateway" in issued["note"]


def test_challenge_endpoint_fails_closed_without_a_real_channel(bank, monkeypatch):
    """接口层同样 fail closed：503 + 一句人话，不返回任何验证码。"""
    monkeypatch.setenv(factors.ECHO_ENV, "0")
    monkeypatch.delenv(factors.PROVIDER_ENV, raising=False)
    _pending_confirmation(bank)
    response = _post("/api/factor/challenge", {"session_id": "s_ch", "confirmation_id": "cf_ch"})
    assert response.status_code == 503, response.text
    payload = response.json()
    assert payload["code"] == "SYSTEM_BUSY"
    assert "渠道" in payload["user_text"]
    assert _challenge_rows(bank) == 0


def test_challenge_endpoint_hides_the_code_once_a_channel_is_declared(bank, monkeypatch):
    monkeypatch.setenv(factors.ECHO_ENV, "0")
    monkeypatch.setenv(factors.PROVIDER_ENV, "face_sdk")
    _pending_confirmation(bank)
    response = _post("/api/factor/challenge", {"session_id": "s_ch", "confirmation_id": "cf_ch"})
    assert response.status_code == 200, response.text
    payload = response.json()
    assert sorted(payload["required"]) == ["face", "sms"]
    assert payload["challenges"]
    for item in payload["challenges"]:
        assert item["code"] is None
        assert item["provider"] == "face_sdk"
        assert item["delivery"] == "out_of_band"


def test_investments_are_declared_simulation_and_unknown_fields_stay_empty(bank):
    """理财：模拟数据要自报家门，表里没有的字段必须是 null，而不是编一个数。"""
    suitability.save(bank, "u_9f2c41ab", {
        "q_horizon": "gt_3y",
        "q_experience": "equity",
        "q_loss": "buy_more",
        "q_goal": "aggressive",
        "q_share": "lt_10",
    })
    view = management_view(bank)
    invest = view["investments"]
    assert invest["simulation"]["data_source"] == "contest_simulation"
    assert invest["simulation"]["verified_source"] is False
    assert invest["comparison"]["supported"] is False
    assert "对比" in invest["comparison"]["note"]
    assert invest["products"]
    for product in invest["products"]:
        assert product["duration_days"] is None
        assert product["liquidity"] is None
        assert product["fee"] is None
        assert product["data_source"] == "contest_simulation"
        assert product["fee_text"]
