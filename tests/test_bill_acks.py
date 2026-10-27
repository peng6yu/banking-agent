"""账单异常的「我已核实」：核实一次，这一组以后就不再打扰。

为什么值得单独测：规则命中只是线索，但以前用户看完之后没有任何办法让它消失——
同一条会永远挂在首页收件箱里，久而久之整张卡就没人看了。核实必须真的有效果，
又必须只做「免打扰」这一件事：不写钱、不改流水、不认欺诈，而且同组后来添了新
的一笔，那笔没被看过就得重新出现，不能被旧的核实记录掩盖。
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from app import clock, db
from app.main import app
from scripts.seed import DEBIT_ACCOUNT_ID, seed

BASE_URL = "http://testserver"
FROZEN_MS = int(datetime(2026, 10, 23, 12, 0).timestamp() * 1000)
MERCHANT = "云音乐会员"


def call(method: str, url: str, **kwargs: Any) -> httpx.Response:
    async def run() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url=BASE_URL) as http:
            return await http.request(method, url, **kwargs)

    return asyncio.run(run())


def _tx(
    conn: Any,
    tx_id: str,
    when: datetime,
    amount_fen: int,
    merchant: str = MERCHANT,
    *,
    account_id: str = DEBIT_ACCOUNT_ID,
) -> None:
    """造一笔订阅扣款：上月 100 元、本月 150 元，正好命中「涨价 30%」。"""
    conn.execute(
        "INSERT INTO transactions (id, account_id, direction, amount_fen, counterparty,"
        " category, note, ts_ms, balance_after_fen, channel)"
        " VALUES (?, ?, 'out', ?, ?, '订阅', '', ?, 0, 'app')",
        (tx_id, account_id, amount_fen, merchant, int(when.timestamp() * 1000)),
    )


@pytest.fixture()
def bank(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    """干净的种子库 + 冻结时钟 + 两笔确定性样本（不依赖演示数据的具体日期）。"""
    monkeypatch.setattr(db, "DEFAULT_DB_PATH", tmp_path / "bank.db")
    clock.freeze(FROZEN_MS)
    conn = db.connect()
    db.init_db(conn)
    seed(conn)
    _tx(conn, "t_ack_prev", datetime(2026, 9, 15, 21, 0), 10_000)
    _tx(conn, "t_ack_cur", datetime(2026, 10, 10, 21, 0), 15_000)
    conn.close()
    yield str(tmp_path / "bank.db")
    clock.unfreeze()


def _hike(items: list[dict[str, Any]], tx_id: str = "t_ack_prev") -> dict[str, Any] | None:
    """只认自己造的那一组：种子里也有一条爱奇艺涨价，别拿错组。"""
    for item in items:
        if str(item.get("kind")) != "subscription_hike":
            continue
        if tx_id in [str(tx) for tx in (item.get("evidence") or {}).get("tx_ids") or []]:
            return item
    return None


def _bill_anomalies() -> list[dict[str, Any]]:
    return list(call("GET", "/api/business/bill").json()["anomalies"]["items"])


def test_acking_a_group_takes_it_off_the_bill_and_the_ledger(bank: str) -> None:
    """核实之后：账单页不再列它、明细不再标红，流水本身一行都不少。"""
    hike = _hike(_bill_anomalies())
    assert hike is not None and hike["evidence"]["tx_ids"]
    rows_before = len(call("GET", "/api/transactions?limit=100").json()["items"])
    marked_before = call("GET", "/api/transactions?limit=100").json()["anomaly_count"]

    response = call(
        "POST",
        "/api/bill/anomalies/ack",
        json={
            "kind": "subscription_hike",
            "account_id": DEBIT_ACCOUNT_ID,
            "tx_ids": hike["evidence"]["tx_ids"],
            "note": "我自己订的，知道涨价",
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True and body["created"] == 2

    assert _hike(_bill_anomalies()) is None
    ledger = call("GET", "/api/transactions?limit=100").json()
    assert ledger["anomaly_count"] < marked_before
    assert len(ledger["items"]) == rows_before  # 只免打扰，不动流水

    conn = db.connect()
    try:
        acked = conn.execute("SELECT COUNT(*) AS n FROM anomaly_acks").fetchone()["n"]
        events = conn.execute(
            "SELECT COUNT(*) AS n FROM audit_log WHERE event_type = 'bill.anomaly_acked'"
        ).fetchone()["n"]
    finally:
        conn.close()
    assert acked == 2 and events == 1


def test_the_same_group_comes_back_when_a_new_row_joins_it(bank: str) -> None:
    """同组后来又多了一笔：那笔用户没看过，整组就该重新出现，不能被旧记录盖住。"""
    hike = _hike(_bill_anomalies())
    assert hike is not None
    # 先只核实「看到的这两笔」的其中一笔，整组不该消失。
    call(
        "POST",
        "/api/bill/anomalies/ack",
        json={"kind": "subscription_hike", "tx_ids": [hike["evidence"]["tx_ids"][0]]},
    )
    assert _hike(_bill_anomalies()) is not None

    # 两笔都核实掉之后才消失。
    call(
        "POST",
        "/api/bill/anomalies/ack",
        json={"kind": "subscription_hike", "tx_ids": hike["evidence"]["tx_ids"]},
    )
    assert _hike(_bill_anomalies()) is None

    # 本月再来一笔更贵的扣款：又成了没看过的事实，异常自己回来。
    conn = db.connect()
    try:
        _tx(conn, "t_ack_new", datetime(2026, 10, 20, 21, 0), 18_000)
        conn.commit()
    finally:
        conn.close()
    again = _hike(_bill_anomalies())
    assert again is not None and "t_ack_new" in again["evidence"]["tx_ids"]


def test_acking_twice_is_idempotent_but_still_recorded(bank: str) -> None:
    """重复核实：不新增记录（幂等），审计仍记一条——「又看了一遍」也是事实。"""
    hike = _hike(_bill_anomalies())
    assert hike is not None
    payload = {"kind": "subscription_hike", "tx_ids": hike["evidence"]["tx_ids"]}
    assert call("POST", "/api/bill/anomalies/ack", json=payload).json()["created"] == 2
    second = call("POST", "/api/bill/anomalies/ack", json=payload)
    assert second.status_code == 200
    assert second.json()["created"] == 0
    assert second.json()["acked_count"] == 2


def test_a_wrong_kind_or_someone_elses_row_is_refused(bank: str) -> None:
    """认不出的规则、别的卡上的流水，都得给中文 400，而不是默默记一条。"""
    hike = _hike(_bill_anomalies())
    assert hike is not None
    unknown = call(
        "POST",
        "/api/bill/anomalies/ack",
        json={"kind": "no_such_rule", "tx_ids": hike["evidence"]["tx_ids"]},
    )
    assert unknown.status_code == 400
    assert unknown.json()["code"] == "VALIDATION"

    conn = db.connect()
    try:
        other = conn.execute(
            "SELECT id FROM accounts WHERE id != ? LIMIT 1", (DEBIT_ACCOUNT_ID,)
        ).fetchone()["id"]
        _tx(conn, "t_ack_other", datetime(2026, 10, 11, 21, 0), 9_900, account_id=str(other))
        conn.commit()
    finally:
        conn.close()
    foreign = call(
        "POST",
        "/api/bill/anomalies/ack",
        json={"kind": "subscription_hike", "tx_ids": ["t_ack_other"]},
    )
    assert foreign.status_code == 400
    assert "不在这张卡上" in foreign.json()["user_text"]
    # 被拒的两次都不能留下痕迹。
    assert _hike(_bill_anomalies()) is not None
