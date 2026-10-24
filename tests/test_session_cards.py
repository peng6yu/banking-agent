"""刷新页面后，服务端必须能把「同一张确认卡」重建出来。

任务书要求：SSE 中断或页面刷新后，业务状态要能恢复；点确认 != 成功，也不能因为刷新
就在看不见金额和收款人的情况下再签一次字。老实现 /api/session 只回确认号，前端只能画
一个「还有一个没处理完的确认 / 确认号 cf_xxx」的提示框——那不是恢复，是把举证责任推给用户。

这里钉三条：
1. 挂着的卡能重建出金额、收款人、转出账户、权限等级、验证步骤（字段与 SSE 首推是同一套）；
2. 老数据里没存理由时照样能重建（不许因为少一个字段就整张卡作废）；
3. action_json 坏掉时宁可 card=None，也不编一张看起来能签的卡。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from app import db
from app.planner import dag as planner_dag
from app.planner import executor
from scripts.seed import seed

SECRET = "c" * 64


@pytest.fixture()
def bank(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("CAPABILITY_SECRET", SECRET)
    monkeypatch.setattr(db, "DEFAULT_DB_PATH", tmp_path / "bank.db")
    conn = db.connect()
    db.init_db(conn)
    seed(conn)
    conn.close()
    executor.RUNS.clear()
    return db.DEFAULT_DB_PATH


def _transfer_dag(amount_fen: int) -> Any:
    return planner_dag.build({"intent": "transfer", "slots": {"payee_name": "李静", "amount_fen": amount_fen}})


async def _open_pending(session_id: str, amount_fen: int = 30_000) -> tuple[str, asyncio.Task[None]]:
    """跑一张黄灯图，停在「卡已弹出、等人确认」这一刻。"""
    seen: dict[str, str] = {}
    ready = asyncio.Event()

    async def emit(name: str, data: dict[str, Any]) -> None:
        if name == "confirm":
            seen["cid"] = str(data["confirmation_id"])
            ready.set()

    task = asyncio.create_task(
        executor.run(session_id, _transfer_dag(amount_fen), emit, confirm_timeout_ms=15_000)
    )
    await asyncio.wait_for(ready.wait(), timeout=10)
    return seen["cid"], task


async def _get_session(session_id: str) -> Any:
    import httpx

    from app.main import app

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http:
        return await http.get("/api/session/" + session_id)


def _with_session(session_id: str) -> tuple[dict[str, Any], str]:
    """跑完一次挂起 -> 读 /api/session -> 关掉后台任务，返回 (payload, confirmation_id)。"""

    async def run() -> tuple[dict[str, Any], str]:
        cid, task = await _open_pending(session_id)
        try:
            res = await _get_session(session_id)
            assert res.status_code == 200, res.text
            return res.json(), cid
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    return asyncio.run(run())


def test_session_rebuilds_the_pending_card_for_a_refresh(bank: Path) -> None:
    payload, cid = _with_session("s_refresh_card")
    cards = payload["confirmations"]
    assert len(cards) == 1
    item = cards[0]
    assert item["confirmation_id"] == cid
    assert item["status"] == "pending"
    assert item["expired"] is False

    card = item["card"]
    assert card is not None, "刷新后重建不出卡片：用户会在看不见金额和收款人的情况下被要求再签一次字"
    assert card["amount_yuan"] == "300.00"
    assert card["payee"]["name"] == "李静"
    assert card["payee"]["card_tail"] == "8821"
    assert card["source_account"]["tail"] == "1234"
    assert card["source_account"]["balance_fen"] == 1_286_050
    assert card["verification"]["level"] == "yellow"
    assert card["verification"]["steps"], "验证步骤不能是空的：那就是没有告诉用户要做什么"
    assert card["fee"]["known"] is False
    assert card["status"] == "pending"
    assert card["expires_in_s"] > 0
    assert "李静" in card["reason_text"]


def test_old_rows_without_reason_still_rebuild(bank: Path) -> None:
    """老数据只存了 level/rule_id/required_factors —— 少一个字段不等于整张卡不能签。"""
    payload, cid = _with_session("s_refresh_legacy")
    conn = db.connect()
    try:
        row = conn.execute("SELECT run_id, node_id FROM confirmations WHERE id = ?", (cid,)).fetchone()
        legacy = {"level": "yellow", "rule_id": "R06_transfer_yellow", "required_factors": []}
        with db.tx(conn):
            conn.execute(
                "UPDATE dag_nodes SET decision_json = ? WHERE run_id = ? AND node_id = ?",
                (json.dumps(legacy), row["run_id"], row["node_id"]),
            )
        res = asyncio.run(_get_session("s_refresh_legacy"))
    finally:
        conn.close()
    card = res.json()["confirmations"][0]["card"]
    assert card is not None
    assert card["amount_yuan"] == "300.00"
    assert card["reason_text"] == ""


def test_broken_action_json_yields_no_card(bank: Path) -> None:
    """解析不出动作时只回确认号：绝不编一张看起来能签的卡。"""
    payload, cid = _with_session("s_refresh_broken")
    conn = db.connect()
    try:
        with db.tx(conn):
            conn.execute("UPDATE confirmations SET action_json = ? WHERE id = ?", ("not-json", cid))
        res = asyncio.run(_get_session("s_refresh_broken"))
    finally:
        conn.close()
    item = res.json()["confirmations"][0]
    assert item["confirmation_id"] == cid
    assert item["card"] is None
