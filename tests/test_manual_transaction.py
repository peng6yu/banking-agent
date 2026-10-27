"""手动记一笔：用户自己在账单页补一笔流水。

用户的要求（2026-10-25）：要有订单手动录入入口——刷卡/现金/别的银行的账，
本行流水里没有，用户要能自己补上，并且补完能在账单里看到。
这一组钉住：

1. 补录只写本行流水 + 演示余额，渠道固定 manual，响应永远带 manual=true 与边界说明；
2. 支出扣余额、收入加余额，balance_after_fen 与库里的余额一致；
3. 同一次提交（同一个 client_nonce）重试是幂等的，不带编号的两次提交是两笔；
   同一个编号配不同内容要 400，不能悄悄记成两笔不同的账；
4. 可以补最近 90 天内的账并保留真实发生时间；未来与 90 天前都拒绝；
5. 可以选信用卡：刷卡只动这张卡的可用额度，不碰储蓄卡；还款不能超过已用额度；
6. 钱不够要 409 拒绝，且一分不动；入参挡住 0 元/超长商户名/多余字段；
7. 补录完立刻回报这笔命中的账单规则，账单统计要算进这笔，
   审计要落 transaction.manual 且链仍然完整。
"""

from __future__ import annotations

import asyncio
import json
import time
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


def _credit() -> dict:
    conn = db.connect()
    try:
        row = conn.execute(
            "SELECT * FROM accounts WHERE type = 'credit' ORDER BY id LIMIT 1"
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


def test_the_same_submission_nonce_is_idempotent(bank: Path) -> None:
    """同一次提交重试（网络抖一下又点一次）只记一笔，钱只扣一次。"""
    before = _balance()
    body = _body(client_nonce="nc_same_retry")
    first = call("POST", "/api/transactions/manual", body).json()
    second = call("POST", "/api/transactions/manual", body).json()
    assert second["idempotent"] is True
    assert second["transaction"]["transaction_id"] == first["transaction"]["transaction_id"]
    assert _balance() == before - 12345
    assert len(_manual_rows()) == 1


def test_two_identical_entries_without_a_nonce_are_two_rows(bank: Path) -> None:
    """一天里两杯同价咖啡：没有编号就是两次独立提交，必须都能记上。"""
    before = _balance()
    call("POST", "/api/transactions/manual", _body())
    call("POST", "/api/transactions/manual", _body())
    assert _balance() == before - 12345 * 2
    assert len(_manual_rows()) == 2


def test_reusing_a_nonce_for_different_content_is_refused(bank: Path) -> None:
    """同一个编号对应两笔不同的账，说明客户端把编号用错了：400 且一笔不加。"""
    call("POST", "/api/transactions/manual", _body(client_nonce="nc_conflict"))
    before = _balance()
    resp = call("POST", "/api/transactions/manual", _body(client_nonce="nc_conflict", amount_fen=999))
    assert resp.status_code == 400
    assert resp.json()["code"] == "VALIDATION"
    assert _balance() == before
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
    """多出来的字段一律拒收：补录表单只认它声明过的字段。"""
    resp = call("POST", "/api/transactions/manual", _body(merchant_id="x"))
    assert resp.status_code == 400
    assert resp.json()["code"] == "VALIDATION"


def test_an_unknown_account_is_refused(bank: Path) -> None:
    """账户编号拼错时不能悄悄记到主卡上——那是记错账，不是 400 能比的坑。"""
    resp = call("POST", "/api/transactions/manual", _body(account_id="a_hacker"))
    assert resp.status_code == 400
    assert resp.json()["code"] == "VALIDATION"
    assert "账户" in resp.json()["user_text"]
    assert _manual_rows() == []


def test_a_backdated_entry_keeps_the_time_it_happened(bank: Path) -> None:
    """补录历史账单要落在它发生的那个时刻，否则趋势/凌晨/订阅窗口全会错位。"""
    happened = now_ms() - 5 * DAY_MS
    tx = call("POST", "/api/transactions/manual", _body(ts_ms=happened)).json()["transaction"]
    assert tx["ts_ms"] == happened
    row = _tx(tx["transaction_id"])
    assert row is not None and int(row["ts_ms"]) == happened


def test_a_future_time_is_refused(bank: Path) -> None:
    resp = call("POST", "/api/transactions/manual", _body(ts_ms=now_ms() + DAY_MS))
    assert resp.status_code == 400
    assert resp.json()["code"] == "VALIDATION"
    assert _manual_rows() == []


def test_a_time_older_than_ninety_days_is_refused(bank: Path) -> None:
    resp = call("POST", "/api/transactions/manual", _body(ts_ms=now_ms() - 91 * DAY_MS))
    assert resp.status_code == 400
    assert resp.json()["code"] == "VALIDATION"
    assert _manual_rows() == []


def test_a_credit_card_spend_only_moves_that_cards_credit(bank: Path) -> None:
    """信用卡刷卡是「可用额度变少」，不是从储蓄卡扣钱；两张卡的余额都不该混。"""
    card = _credit()
    debit_before = _balance()
    body = call(
        "POST", "/api/transactions/manual",
        _body(account_id=card["id"], amount_fen=20000, category="购物"),
    ).json()
    assert body["manual"] is True
    assert body["notice"] == transaction_cases.CREDIT_NOTICE
    assert body["account"]["type"] == "credit"
    assert body["balance_after_fen"] == int(card["balance_fen"]) - 20000
    assert _balance() == debit_before
    conn = db.connect()
    try:
        after = conn.execute("SELECT balance_fen FROM accounts WHERE id = ?", (card["id"],)).fetchone()
    finally:
        conn.close()
    assert int(after["balance_fen"]) == int(card["balance_fen"]) - 20000


def test_a_credit_card_repayment_cannot_exceed_what_was_used(bank: Path) -> None:
    """还款最多把已用额度还平；多还的那部分没有地方放，直接 400。"""
    card = _credit()
    used = int(card["credit_limit_fen"]) - int(card["balance_fen"])
    resp = call(
        "POST", "/api/transactions/manual",
        _body(account_id=card["id"], direction="in", amount_fen=used + 1),
    )
    assert resp.status_code == 400
    assert resp.json()["code"] == "VALIDATION"
    conn = db.connect()
    try:
        after = conn.execute("SELECT balance_fen FROM accounts WHERE id = ?", (card["id"],)).fetchone()
    finally:
        conn.close()
    assert int(after["balance_fen"]) == int(card["balance_fen"])


def test_a_backdated_night_overseas_entry_reports_the_rule_it_hits(bank: Path) -> None:
    """补录完立刻告诉用户「这笔命中了哪条规则」——检测是读时算的，不用等下次刷新。"""
    parts = time.localtime((now_ms() - DAY_MS) / 1000)
    happened = int(time.mktime((
        parts.tm_year, parts.tm_mon, parts.tm_mday, 3, 0, 0, 0, 0, -1,
    )) * 1000)
    body = call(
        "POST", "/api/transactions/manual",
        _body(merchant="Apple Store 香港", amount_fen=288000, category="购物", ts_ms=happened),
    ).json()
    kinds = [hit["kind"] for hit in body["hits"]]
    assert "odd_hour_overseas" in kinds
    assert body["hit_count"] == len(body["hits"])
    assert "命中" in body["hits_note"]
    hit = body["hits"][kinds.index("odd_hour_overseas")]
    assert hit["amount_fen"] == 288000 and hit["amount_yuan"] == "2,880.00"
    assert hit["title"] and hit["detail"]


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
    assert after["spend_out_fen"] == before["spend_out_fen"] + 12345
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


def test_the_same_amount_at_a_different_time_is_a_new_entry(bank: Path) -> None:
    """同样一杯咖啡，今天一笔、三天前补一笔，是两笔真实消费，不能互相吞掉。"""
    before = _balance()
    call("POST", "/api/transactions/manual", _body(ts_ms=now_ms() - 3 * DAY_MS))
    body = call("POST", "/api/transactions/manual", _body()).json()
    assert body["idempotent"] is False
    assert _balance() == before - 12345 * 2
    assert len(_manual_rows()) == 2
