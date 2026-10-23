"""能力令牌七步校验测试。

对应文档：docs/05_安全层实现规格.md 5.3。
七步每一步都要有独立失败用例，评审现场逐条问。
"""

from __future__ import annotations

import base64
import json

import pytest

from app import audit, clock
from app.clock import MissingCapability, TokenExpired, TokenMismatch
from app.models import Action
from app.security import token as tk

SECRET = "f" * 64
OLD_SECRET = "e" * 64
TTL_MS = 120_000


@pytest.fixture(autouse=True)
def frozen_clock():
    clock.freeze(1_760_000_000_000)
    try:
        yield
    finally:
        clock.unfreeze()


def action(amount_fen: int = 50_000, **kwargs: object) -> Action:
    return Action(
        tool="transfer",
        params={"payee_id": "p_zhangsan"},
        account_id="a_1",
        amount_fen=amount_fen,
        **kwargs,
    )


def issue(target: Action | None = None, *, secret: str = SECRET, ttl_ms: int = TTL_MS) -> str:
    return tk.issue(target or action(), secret=secret, ttl_ms=ttl_ms)


def test_happy_path_passes_and_returns_payload(conn) -> None:
    target = action()
    token = issue(target)
    payload = tk.verify(conn, token, target, secret=SECRET)
    assert payload.tool == "transfer"
    assert payload.account_id == "a_1"
    assert payload.amount_fen == 50_000
    assert payload.params_hash == tk.params_hash(target.params)


def test_step1_broken_structure_rejected(conn) -> None:
    target = action()
    for broken in ("", "no-dot", "a.b.c", ".onlysig", "bodyonly."):
        with pytest.raises(TokenMismatch):
            tk.verify(conn, broken, target, secret=SECRET)


def test_step1_non_base64_body_rejected(conn) -> None:
    with pytest.raises(TokenMismatch):
        tk.verify(conn, "!!!not-base64!!!.deadbeef", action(), secret=SECRET)


def test_step1_missing_payload_fields_rejected(conn) -> None:
    body = tk._b64encode(json.dumps({"v": 1, "tool": "transfer"}).encode("utf-8"))
    forged = f"{body}.{tk.sign(body, SECRET)}"
    with pytest.raises(TokenMismatch):
        tk.verify(conn, forged, action(), secret=SECRET)


def test_step2_signature_mismatch_rejected(conn) -> None:
    token = issue()
    body, sig = token.split(".")
    flipped = ("0" if sig[0] != "0" else "1") + sig[1:]
    with pytest.raises(TokenMismatch) as excinfo:
        tk.verify(conn, f"{body}.{flipped}", action(), secret=SECRET)
    assert excinfo.value.fields.get("reason") == "SIGNATURE_MISMATCH"


def test_step2_old_secret_rejected(conn) -> None:
    with pytest.raises(TokenMismatch):
        tk.verify(conn, issue(secret=OLD_SECRET), action(), secret=SECRET)


def test_step3_expired_token_rejected(conn) -> None:
    token = issue(ttl_ms=TTL_MS)
    clock.freeze(clock.now_ms() + TTL_MS + 1)
    with pytest.raises(TokenExpired):
        tk.verify(conn, token, action(), secret=SECRET)


def test_step3_expiry_boundary_is_not_expired_yet(conn) -> None:
    token = issue(ttl_ms=TTL_MS)
    clock.freeze(clock.now_ms() + TTL_MS - 1)
    assert tk.verify(conn, token, action(), secret=SECRET).exp_ms > 0


def test_step3_manually_extended_expiry_still_fails_signature(conn) -> None:
    """手工把 exp_ms 改大签名就对不上，第 2 步就该拦下。"""
    token = issue()
    body = token.split(".")[0]
    payload = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    payload["exp_ms"] = payload["exp_ms"] + 10 ** 9
    new_body = tk._b64encode(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8"))
    with pytest.raises(TokenMismatch):
        tk.verify(conn, f"{new_body}.{tk.sign(body, SECRET)}", action(), secret=SECRET)


def test_step4_nonce_replay_rejected(conn) -> None:
    token = issue()
    tk.verify(conn, token, action(), secret=SECRET)
    with pytest.raises(TokenMismatch) as excinfo:
        tk.verify(conn, token, action(), secret=SECRET)
    assert excinfo.value.fields.get("reason") == "NONCE_REPLAY"


def test_step4_nonce_row_is_written(conn) -> None:
    token = issue()
    payload = tk.verify(conn, token, action(), secret=SECRET)
    row = conn.execute("SELECT * FROM token_nonces WHERE nonce = ?", (payload.nonce,)).fetchone()
    assert row is not None
    assert row["expires_ms"] == payload.exp_ms


def test_step5_tool_mismatch_rejected(conn) -> None:
    other = Action(tool="query_balance", params={}, account_id="a_1")
    token = tk.issue(other, secret=SECRET)
    with pytest.raises(TokenMismatch) as excinfo:
        tk.verify(conn, token, action(), secret=SECRET)
    assert excinfo.value.fields.get("reason") == "TOOL_MISMATCH"


def test_step6_payee_swap_rejected(conn) -> None:
    token = issue(Action(tool="transfer", params={"payee_id": "p_lisi"}, account_id="a_1", amount_fen=50_000))
    with pytest.raises(TokenMismatch) as excinfo:
        tk.verify(conn, token, action(), secret=SECRET)
    assert excinfo.value.fields.get("reason") == "PARAMS_MISMATCH"


def test_step7_amount_changed_by_one_fen_rejected(conn) -> None:
    token = issue()
    with pytest.raises(TokenMismatch) as excinfo:
        tk.verify(conn, token, action(50_001), secret=SECRET)
    assert excinfo.value.fields.get("reason") == "AMOUNT_MISMATCH"


def test_step7_account_mismatch_rejected(conn) -> None:
    token = issue()
    other_account = Action(tool="transfer", params={"payee_id": "p_zhangsan"}, account_id="a_2", amount_fen=50_000)
    with pytest.raises(TokenMismatch) as excinfo:
        tk.verify(conn, token, other_account, secret=SECRET)
    assert excinfo.value.fields.get("reason") == "ACCOUNT_MISMATCH"


def test_nonce_is_consumed_even_when_step7_fails(conn) -> None:
    """第 4 步先消耗 nonce：校验失败的令牌也不能被再拿来试。"""
    token = issue()
    with pytest.raises(TokenMismatch):
        tk.verify(conn, token, action(50_001), secret=SECRET)
    with pytest.raises(TokenMismatch) as excinfo:
        tk.verify(conn, token, action(50_001), secret=SECRET)
    assert excinfo.value.fields.get("reason") == "NONCE_REPLAY"


def test_every_rejection_writes_token_rejected_audit(conn) -> None:
    tk.verify(conn, issue(), action(), secret=SECRET)
    with pytest.raises(TokenMismatch):
        tk.verify(conn, issue(), action(50_001), secret=SECRET)
    rows = conn.execute("SELECT payload_json FROM audit_log WHERE event_type = 'token.rejected'").fetchall()
    assert rows
    assert "AMOUNT_MISMATCH" in rows[-1]["payload_json"]


def test_issue_audited_writes_token_issued(conn) -> None:
    tk.issue_audited(conn, action(), secret=SECRET, session_id="s_1", task_id="t_1")
    row = conn.execute("SELECT payload_json FROM audit_log WHERE event_type = 'token.issued'").fetchone()
    assert row is not None
    payload = json.loads(row["payload_json"])
    assert payload["ttl"] == TTL_MS
    assert "params_hash" in payload
    assert audit.verify_chain(conn)


def test_missing_secret_fails_closed(monkeypatch) -> None:
    """显式关掉（设成空串）与显式传错都必须 fail closed。

    变量**未设置**不在此列：那种情况会读/生成 data/token_secret（见
    tests/test_capability_secret.py），这里断言的是「有人明确动了这个配置」的两条路。
    """
    monkeypatch.setenv(tk.SECRET_ENV, "")
    with pytest.raises(MissingCapability):
        tk.load_secret()
    with pytest.raises(MissingCapability):
        tk.load_secret("not-64-hex")


def test_short_secret_rejected() -> None:
    with pytest.raises(MissingCapability):
        tk.load_secret("abc123")


def test_require_token_records_rejection(conn) -> None:
    with pytest.raises(MissingCapability):
        tk.require_token(None, conn, session_id="s_1")
    row = conn.execute("SELECT payload_json FROM audit_log WHERE event_type = 'token.rejected'").fetchone()
    assert "TOKEN_MISSING" in row["payload_json"]


def test_params_hash_is_key_order_independent() -> None:
    assert tk.params_hash({"a": 1, "b": 2}) == tk.params_hash({"b": 2, "a": 1})
    assert tk.params_hash({"a": 1}) != tk.params_hash({"a": 2})


def test_decode_reads_payload_without_consuming_nonce(conn) -> None:
    token = issue()
    assert tk.decode(token, secret=SECRET).amount_fen == 50_000
    assert conn.execute("SELECT COUNT(*) AS c FROM token_nonces").fetchone()["c"] == 0
