"""确认卡必须真的落进 confirmations 表。

历史问题：这张表只有 UPDATE 没有 INSERT —— 表恒空、`/api/session` 永远列不出卡片，
而 `resolve_confirmation` 的 UPDATE 打在零行上、等于空操作，整条链路看着「有实现」，
实际一次都没写进去过。本文件钉住三件事：

1) 建卡就落库、状态 pending；
2) 批准/拒绝都写回 resolved_ms；
3) 超时与策略拦停这两条**不走 resolve_confirmation** 的路径同样落库（否则卡片永远
   停在 pending，`/api/session` 会一直当它还在等用户点）。

反证方式：删掉 state.new_confirmation 里的 INSERT，第 1、2 条立刻红；
把 executor._timeout 里的 mark_confirmation 改回 `conf.status = "expired"`，第 3 条立刻红。
"""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

import pytest

from app import db
from app.llm import LLMResponse, ToolCall
from app.planner import dag as planner_dag
from app.planner import executor, intent
from scripts.seed import seed

SECRET = "b" * 64
SESSION = "s_persist"
AMOUNT_FEN = 50_000


class Scripted:
    """只会 emit_intent 的假模型。"""

    def __init__(self, item: dict[str, Any]) -> None:
        self.item = item

    def chat(self, messages: list[dict[str, Any]], **kwargs: Any) -> LLMResponse:
        return LLMResponse(
            content="",
            reasoning="",
            tool_calls=[ToolCall(id="c1", name="emit_intent", arguments=json.dumps(self.item, ensure_ascii=False))],
            finish_reason="tool_calls",
            model="scripted",
        )


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


def transfer_call() -> dict[str, Any]:
    return {"intent": "transfer", "payee_name": "李静", "amount_fen": AMOUNT_FEN, "missing": [], "confidence": 0.95}


def rows() -> list[dict[str, Any]]:
    with db.connect() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM confirmations ORDER BY created_ms")]


def drive(*, approve: bool | None, timeout_ms: int = 5_000) -> tuple[list[str], list[dict[str, Any]]]:
    """跑一轮转账；approve=None 表示谁都不点（走超时）。

    返回 (事件名列表, 每次 emit confirm 帧那一刻从库里读到的卡片)。
    """

    async def main() -> tuple[list[str], list[dict[str, Any]]]:
        parsed = await intent.parse("帮我给李静转 500 元", [], adapter=Scripted(transfer_call()))
        built = planner_dag.build_or_single({"intent": parsed.intent, "slots": parsed.slots})
        seen: list[str] = []
        snapshot: list[dict[str, Any]] = []

        async def emit(name: str, data: dict[str, Any]) -> None:
            seen.append(name)
            if name == "confirm":
                snapshot.append(rows()[0] if rows() else {})

        async def answerer(task: asyncio.Task[None]) -> None:
            handled: set[str] = set()
            deadline = time.monotonic() + 20
            while approve is not None and time.monotonic() < deadline and not task.done():
                for conf in list((executor.get_run(SESSION) or type("X", (), {"confirmations": {}})()).confirmations.values()):
                    if conf.status != "pending" or conf.id in handled:
                        continue
                    handled.add(conf.id)
                    await executor.resume(SESSION, conf.id, approve)
                await asyncio.sleep(0.005)

        task = asyncio.create_task(executor.run(SESSION, built, emit, confirm_timeout_ms=timeout_ms))
        helper = asyncio.create_task(answerer(task))
        try:
            await asyncio.wait_for(task, timeout=30)
        finally:
            helper.cancel()
        return seen, snapshot

    return asyncio.run(main())


def test_card_is_persisted_while_waiting(bank: Path) -> None:
    """emit confirm 的那一刻，库里就该有一张 pending 卡（用户此时确实在等）。"""
    _, snapshot = drive(approve=True)
    assert snapshot, "没有等到 confirm 帧"
    card = snapshot[0]
    assert card["status"] == "pending"
    assert card["session_id"] == SESSION
    assert card["node_id"] == "n3"
    assert json.loads(card["action_json"])["tool"] == "transfer"


def test_approved_card_records_resolution(bank: Path) -> None:
    drive(approve=True)
    cards = rows()
    assert len(cards) == 1
    assert cards[0]["status"] == "approved"
    assert cards[0]["resolved_ms"] is not None


def test_rejected_card_records_resolution(bank: Path) -> None:
    drive(approve=False)
    cards = rows()
    assert len(cards) == 1
    assert cards[0]["status"] == "rejected"
    assert cards[0]["resolved_ms"] is not None


def test_timeout_marks_row_expired(bank: Path) -> None:
    """超时不经过 resolve_confirmation，但也必须落库成 expired。"""
    events, _ = drive(approve=None, timeout_ms=150)
    assert "error" in events
    cards = rows()
    assert len(cards) == 1
    assert cards[0]["status"] == "expired", "超时只改了内存状态，库里还停在 pending"
    assert cards[0]["resolved_ms"] is not None
