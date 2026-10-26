"""手动记一笔：用户自己在账单页补一笔流水。

用户的要求（2026-10-25）：要有订单手动录入入口——刷卡/现金/别的银行的账，
本行流水里没有，用户要能自己补上，并且补完能在账单里看到。
这一组钉住：

1. 补录只写本行流水 + 演示余额，渠道固定 manual，响应永远带 manual=true 与边界说明；
2. 支出扣余额、收入加余额，balance_after_fen 与库里的余额一致；
3. 同一天重复提交同一笔是幂等的（不重复扣钱），换一天才是一笔新的；
4. 钱不够要 409 拒绝，且一分不动；
5. 入参挡住 0 元/超长商户名/多余字段；
6. 账单统计要算进这笔，审计要落 transaction.manual 且链仍然完整。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from app import audit, db
from app.api import routes
from app.clock import now_ms
from app.use_cases import transactions as transaction_cases
from app.main import app
from scripts.seed import seed

BASE_URL = "http://testserver"
DAY_MS = 86_400_000


@pytest.fixture()
def bank(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(db, "DEFAULT_DB_PATH", tmp_path / "bank.db")
    conn = db.connect()
    db.init_db(conn)
    seed(conn)
    conn.close()
    routes._history.clear()
    routes.set_adapter(None)
    yield tmp_path / "bank.db"
    routes._history.clear()
    routes.set_adapter(None)


def call(method: str, url: str, body: dict | None = None) -> httpx.Response:
    async def run() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url=BASE_URL) as http:
            return await http.request(method, url, json=body)

    return asyncio.run(run())


def _debit() -> dict:
    conn = db.connect()
    try:
        row = conn.execute(
            "SELECT * FROM accounts WHERE type = 'debit' ORDER BY id LIMIT 1"
        ).fetchone()
        return dict(row)
    finally:
        conn.close()


def _balance() -> int:
    return int(_debit()["balance_fen"])


def _tx(transaction_id: str) -> dict | None:
    conn = db.connect()
    try:
        row = conn.execute("SELECT * FROM transactions WHERE id = ?", (transaction_id,)).fetchone()
        return dict(row) if row is not None else None
    finally:
        conn.close()


def _manual_rows() -> list[dict]:
    conn = db.connect()
    try:
        return [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM transactions WHERE channel = 'manual' ORDER BY ts_ms DESC"
            ).fetchall()
        ]
    finally:
        conn.close()


def _audit_rows(event: str) -> list[dict]:
    conn = db.connect()
    try:
        return [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM audit_log WHERE event_type = ? ORDER BY seq", (event,)
            ).fetchall()
        ]
    finally:
        conn.close()


def _body(**overrides) -> dict:
    body = {
        "merchant": "楼下便利店",
        "amount_fen": 12345,
        "direction": "out",
        "category": "餐饮",
        "note": "现金买的",
    }
    body.update(overrides)
    return body


def test_out_records_a_manual_row_and_debits_the_balance(bank: Path) -> None:
    before = _balance()
    body = call("POST", "/api/transactions/manual", _body()).json()
    assert body["ok"] is True and body["manual"] is True
    assert body["notice"] == transaction_cases.MANUAL_NOTICE
    tx = body["transaction"]
    assert tx["manual"] is True and tx["channel"] == "manual"
    assert tx["amount_fen"] == 12345 and tx["amount_yuan"] == "123.45"
    assert tx["merchant"] == "楼下便利店" and tx["counterparty"] == "楼下便利店"
    assert body["balance_after_fen"] == before - 12345
    assert _balance() == before - 12345
    row = _tx(tx["transaction_id"])
    assert row is not None and int(row["balance_after_fen"]) == before - 12345


def test_in_records_a_manual_row_and_credits_the_balance(bank: Path) -> None:
    before = _balance()
    body = call("POST", "/api/transactions/manual", _body(direction="in", amount_fen=50000)).json()
    assert body["transaction"]["direction"] == "in"
    assert body["balance_after_fen"] == before + 50000
    assert _balance() == before + 50000


def test_same_day_duplicate_is_idempotent(bank: Path) -> None:
    before = _balance()
    first = call("POST", "/api/transactions/manual", _body()).json()
    second = call("POST", "/api/transactions/manual", _body()).json()
    assert second["idempotent"] is True
    assert second["transaction"]["transaction_id"] == first["transaction"]["transaction_id"]
    assert _balance() == before - 12345
    assert len(_manual_rows()) == 1


def test_a_note_change_is_a_new_entry(bank: Path) -> None:
    call("POST", "/api/transactions/manual", _body())
    call("POST", "/api/transactions/manual", _body(note="换了个备注"))
    assert len(_manual_rows()) == 2


def test_insufficient_funds_is_rejected_without_moving_money(bank: Path) -> None:
    before = _balance()
    resp = call("POST", "/api/transactions/manual", _body(amount_fen=before + 1))
    assert resp.status_code == 409
    assert resp.json()["code"] == "INSUFFICIENT_FUNDS"
    assert _balance() == before
    assert _manual_rows() == []


def test_income_has_no_balance_ceiling(bank: Path) -> None:
    before = _balance()
    body = call("POST", "/api/transactions/manual", _body(direction="in", amount_fen=before * 3)).json()
    assert body["balance_after_fen"] == before * 4


@pytest.mark.parametrize(
    "bad",
    [
        {"amount_fen": 0},
        {"amount_fen": -100},
        {"merchant": ""},
        {"merchant": "商" * 41},
        {"category": ""},
        {"direction": "sideways"},
        {"note": "备" * 81},
    ],
)
def test_bad_payloads_are_400(bank: Path, bad: dict) -> None:
    resp = call("POST", "/api/transactions/manual", _body(**bad))
    assert resp.status_code == 400
    assert resp.json()["code"] == "VALIDATION"
    assert _manual_rows() == []


def test_unknown_field_is_rejected(bank: Path) -> None:
    resp = call("POST", "/api/transactions/manual", _body(account_id="a_hacker"))
    assert resp.status_code == 400
    assert resp.json()["code"] == "VALIDATION"


def test_defaults_fill_category_and_note(bank: Path) -> None:
    body = call(
        "POST",
        "/api/transactions/manual",
        {"merchant": "没有分类的店", "amount_fen": 1000},
    ).json()
    assert body["transaction"]["category"] == "其他"
    assert body["transaction"]["note"] == ""


def test_manual_entry_counts_in_the_bill(bank: Path) -> None:
    before = call("GET", "/api/business/bill?period=month").json()
    call("POST", "/api/transactions/manual", _body(amount_fen=12345, category="餐饮"))
    after = call("GET", "/api/business/bill?period=month").json()
    assert after["total_out_fen"] == before["total_out_fen"] + 12345
    assert after["out_count"] == before["out_count"] + 1


def test_audit_records_the_manual_entry_and_chain_stays_valid(bank: Path) -> None:
    body = call("POST", "/api/transactions/manual", _body()).json()
    rows = _audit_rows("transaction.manual")
    assert len(rows) == 1
    payload = json.loads(rows[0]["payload_json"])
    assert payload["tool"] == "manual_transaction"
    assert payload["transaction_id"] == body["transaction"]["transaction_id"]
    assert payload["amount_fen"] == 12345 and payload["direction"] == "out"
    assert payload["balance_after_fen"] == body["balance_after_fen"]
    assert rows[0]["seq"] == body["audit_seq"]
    conn = db.connect()
    try:
        assert audit.verify_chain(conn) is True
    finally:
        conn.close()


def test_yesterday_entry_is_not_deduplicated(bank: Path) -> None:
    """同一笔如果落在不同自然日，就该是两笔（幂等只看当天）。"""
    before = _balance()
    call("POST", "/api/transactions/manual", _body())
    # 直接把上一笔挪到一天前，再提交同样的内容：应当是新的一笔
    conn = db.connect()
    try:
        conn.execute(
            "UPDATE transactions SET ts_ms = ? WHERE channel = 'manual'",
            (now_ms() - DAY_MS,),
        )
        conn.commit()
    finally:
        conn.close()
    body = call("POST", "/api/transactions/manual", _body()).json()
    assert body["idempotent"] is False
    assert _balance() == before - 12345 * 2
    assert len(_manual_rows()) == 2
