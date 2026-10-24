"""AA 拆分的收款人消歧 + 工具层复校验。

这一组用例只证明一件事：**任何一位参与人没能唯一确定，钱就一分不动**。
它同时是 split_plan 从「默认取 candidates[0]」改成「候选必须恰好一个」的回归网。
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

import pytest

from app import db
from app.clock import ValidationError
from app.planner import dag as planner_dag
from app.planner import executor
from app.planner.split_plan import build_split, parse_participant
from app.tools import split as split_tools
from scripts.seed import seed
from tests.tool_fixtures import PAYEE_LIJING, _token, bank, ctx

__all__ = ["bank", "ctx"]

SECRET = "b" * 64
SESSION = "s_split_aa"
ZHANG_A = "p_77c05e3a"  # 张三 / 招商 4417 / 同事A
ZHANG_B = "p_9b3f16d2"  # 张三 / 民生 6620 / 同事B
LIJING = "p_1d6f92b0"
WANG = "p_4a2e7c58"
SEED_BALANCE_FEN = 1_286_050
TOTAL = 40_000


@pytest.fixture()
def bank_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("CAPABILITY_SECRET", SECRET)
    monkeypatch.setattr(db, "DEFAULT_DB_PATH", tmp_path / "bank.db")
    conn = db.connect()
    db.init_db(conn)
    seed(conn)
    conn.close()
    executor.RUNS.clear()
    yield db.DEFAULT_DB_PATH
    executor.RUNS.clear()


def _balance() -> int:
    conn = db.connect()
    try:
        row = conn.execute("SELECT balance_fen FROM accounts WHERE id = 'a_3d7e60c2'").fetchone()
        return int(row["balance_fen"])
    finally:
        conn.close()


def _holds() -> int:
    conn = db.connect()
    try:
        return int(conn.execute("SELECT COUNT(*) AS n FROM holds").fetchone()["n"])
    finally:
        conn.close()


def _run(
    slots: dict[str, Any], *, approve: bool = True, text: str = "这顿饭 400，我和张三、李静 AA"
) -> tuple[Any, list[tuple[str, dict[str, Any]]]]:
    """按给好的槽位建图并跑完，返回 (dag, 事件表)。"""
    built = planner_dag.build_or_single({"intent": "split_bill", "slots": slots})
    events: list[tuple[str, dict[str, Any]]] = []

    async def main() -> list[tuple[str, dict[str, Any]]]:
        async def emit(name: str, data: dict[str, Any]) -> None:
            events.append((name, data))

        async def answerer(task: asyncio.Task[None]) -> None:
            handled: set[str] = set()
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline and not task.done():
                state = executor.get_run(SESSION)
                if state is not None:
                    for conf in list(state.confirmations.values()):
                        if conf.status != "pending" or conf.future is None or conf.id in handled:
                            continue
                        handled.add(conf.id)
                        await executor.resume(SESSION, conf.id, approve)
                await asyncio.sleep(0.005)

        task = asyncio.create_task(
            executor.run(
                SESSION,
                built,
                emit,
                confirm_timeout_ms=4_000,
                user_text=text,
            )
        )
        helper = asyncio.create_task(answerer(task))
        try:
            await asyncio.wait_for(task, timeout=20)
        finally:
            helper.cancel()
        return events

    return built, asyncio.run(main())


def _payloads(events: list[tuple[str, dict[str, Any]]], name: str) -> list[dict[str, Any]]:
    return [data for name_, data in events if name_ == name]


# --------------------------------------------------------------------------- 建图


def test_split_references_require_exactly_one_candidate() -> None:
    built = build_split({"payee_names": ["张三", "李静"], "amount_fen": TOTAL})
    refs = built.nodes[-1].params["payee_ids"]
    assert all(".candidates.unique." in ref for ref in refs)
    assert not any(".candidates.0" in ref for ref in refs)


def test_unique_reference_refuses_two_candidates() -> None:
    results = {"n1": {"candidates": [{"payee_id": ZHANG_A}, {"payee_id": ZHANG_B}]}}
    with pytest.raises(ValidationError) as exc:
        planner_dag.resolve_refs({"payee_id": "@n1.candidates.unique.payee_id"}, results)
    assert exc.value.fields.get("reason") == "DAG_PAYEE_AMBIGUOUS"


def test_unique_reference_accepts_one_candidate() -> None:
    results = {"n1": {"candidates": [{"payee_id": LIJING}]}}
    resolved = planner_dag.resolve_refs({"payee_id": "@n1.candidates.unique.payee_id"}, results)
    assert resolved["payee_id"] == LIJING


def test_duplicate_participants_are_refused_at_build_time() -> None:
    with pytest.raises(ValidationError) as exc:
        build_split({"payee_names": ["张三", "张三"], "amount_fen": TOTAL})
    assert exc.value.fields.get("reason") == "DAG_SPLIT_DUPLICATE"


def test_same_name_with_different_tails_is_not_a_duplicate() -> None:
    built = build_split({"payee_names": ["张三 尾号4417", "张三 尾号6620"], "amount_fen": TOTAL})
    tails = [node.params.get("card_tail") for node in built.nodes if node.tool == "resolve_payee"]
    assert tails == ["4417", "6620"]


def test_parse_participant_forms() -> None:
    assert parse_participant("张三 尾号4417") == ("张三", "4417")
    assert parse_participant("张三（4417）") == ("张三", "4417")
    assert parse_participant("张三 4417") == ("张三", "4417")
    # 名字里带数字但不带标记的，不能被当成卡尾号拆开
    assert parse_participant("张三4417") == ("张三4417", None)


# --------------------------------------------------------------------- 端到端闸门


def test_ambiguous_participant_blocks_the_whole_split(bank_db: Path) -> None:
    _built, events = _run({"payee_names": ["张三", "李静"], "amount_fen": TOTAL})
    # 没有确认卡，也没有任何资金写
    assert _payloads(events, "confirm") == []
    assert _balance() == SEED_BALANCE_FEN
    assert _holds() == 0
    done = _payloads(events, "done")
    assert done, "歧义时必须停下来给用户一个明确交代"
    clarification = done[-1]["clarification"]
    assert clarification["accepts"] == ["restate_with_tail"]
    assert clarification["count"] == 2
    assert "4417" in done[-1]["question"] and "6620" in done[-1]["question"]
    assert "AA 拆分第 1 位参与人" in done[-1]["question"]
    summaries = " ".join(str(item.get("summary") or "") for item in _payloads(events, "tool"))
    assert "已经 AA 转出" not in summaries


def test_zero_candidates_blocks_and_asks_for_a_full_name(bank_db: Path) -> None:
    _built, events = _run({"payee_names": ["王五不存在", "李静"], "amount_fen": TOTAL})
    assert _payloads(events, "confirm") == []
    assert _balance() == SEED_BALANCE_FEN
    assert _holds() == 0
    question = _payloads(events, "done")[-1]["question"]
    assert "没找到" in question
    assert "AA 不会执行" in question or "不会执行" in question


def test_partial_ambiguity_never_reaches_the_split_node(bank_db: Path) -> None:
    # 第一位唯一（李静），第二位有歧义（张三）：前面的节点成功也不能继续
    _built, events = _run({"payee_names": ["李静", "张三"], "amount_fen": TOTAL})
    assert _payloads(events, "confirm") == []
    assert _balance() == SEED_BALANCE_FEN
    assert _holds() == 0
    assert "第 2 位参与人" in _payloads(events, "done")[-1]["question"]


def test_disambiguated_by_tail_executes(bank_db: Path) -> None:
    _built, events = _run({"payee_names": ["张三 尾号4417", "李静"], "amount_fen": TOTAL})
    assert _payloads(events, "confirm"), "唯一候选时应正常给出确认卡"
    summary = str(_payloads(events, "done")[-1].get("summary") or "")
    assert "已经 AA 转出 400.00 元" in summary
    assert _balance() == SEED_BALANCE_FEN - TOTAL
    conn = db.connect()
    try:
        rows = conn.execute("SELECT counterparty, amount_fen FROM transactions ORDER BY id DESC LIMIT 2").fetchall()
    finally:
        conn.close()
    paid = {str(row["counterparty"]): int(row["amount_fen"]) for row in rows}
    assert paid == {"张三": 20_000, "李静": 20_000}


def test_indivisible_total_keeps_every_fen(bank_db: Path) -> None:
    _built, events = _run(
        {"payee_names": ["张三 尾号4417", "李静", "王强"], "amount_fen": 10_000}
    )
    summary = str(_payloads(events, "done")[-1].get("summary") or "")
    assert "已经 AA 转出 100.00 元" in summary
    assert _balance() == SEED_BALANCE_FEN - 10_000
    conn = db.connect()
    try:
        rows = conn.execute(
            "SELECT counterparty, amount_fen FROM transactions WHERE hold_id IS NOT NULL ORDER BY ts_ms DESC, id DESC LIMIT 3"
        ).fetchall()
    finally:
        conn.close()
    # 零头给前一位：3334 / 3333 / 3333，一分不丢也一分不多
    paid = {str(row["counterparty"]): int(row["amount_fen"]) for row in rows}
    assert paid == {"张三": 3_334, "李静": 3_333, "王强": 3_333}
    assert sum(paid.values()) == 10_000


# ------------------------------------------------------------------- 工具层复校验


def test_empty_payee_id_is_refused(ctx: Any) -> None:
    params = {"payee_ids": [PAYEE_LIJING, ""], "amount_fen": TOTAL}
    result = split_tools.split_transfer(ctx, params, _token("split_transfer", params, TOTAL))
    assert result.ok is False
    assert result.error_code == "SPLIT_BAD_PAYEES"


def test_per_person_amount_must_match_the_total(ctx: Any) -> None:
    params = {"payee_ids": [PAYEE_LIJING, "p_zhang_a", "p_zhang_b"], "amount_fen": 10_000, "per_person_amount_fen": 3_333}
    result = split_tools.split_transfer(ctx, params, _token("split_transfer", params, 10_000))
    assert result.ok is False
    assert result.error_code == "SPLIT_PER_PERSON_MISMATCH"


def test_per_person_amount_is_accepted_when_it_divides_evenly(ctx: Any) -> None:
    params = {"payee_ids": [PAYEE_LIJING, "p_zhang_a"], "amount_fen": TOTAL, "per_person_amount_fen": 20_000}
    result = split_tools.split_transfer(ctx, params, _token("split_transfer", params, TOTAL))
    assert result.ok is True
    assert result.data["total_amount_fen"] == TOTAL
    assert result.data["per_person_amount_fen"] == 20_000
    assert result.data["indivisible"] is False