"""定时转账的到点执行：后台节拍与 /api/scheduled/run 都必须真的把钱划出去。

本文件盯的是一条真实存在过的缺口：execute_due 实现完成、也有单测，
但全仓没有任何生产调用点——用户预约成功之后那笔钱永远不会动，
演示现场看起来就是「定时转账没做完」。

这和 breaker.record_denial 只在单测里被调用是同一类病，
所以这里断言的不是函数返回值，而是「真实入口（HTTP 端点）打进来之后余额确实变了」。

t11 又盯住两条定时转账特有的不变量：

1. 大额预约在**创建期**就要走红灯（人脸+短信），不能只点一下确认就排上；
   因子没过，那笔预约不能落库。
2. 创建期验过因子的预约，到点执行时凭这条记录放行当日额度线——
   否则「预约一笔 2000 元」会在到点那天被额度线拦死，用户以为产品坏了。
3. 当日额度不够不是失败：这笔让出今天、顺延到下一个自然日，状态仍是 scheduled，
   审计写 scheduled.deferred；顺延有上限，超过上限才明确失败。

反证：
- 把 schedule_transfer 放回 CONFIRM_ONLY_TOOLS（不判金额）→ 第 1 条变红；
- 把到点执行撞额度时的顺延分支去掉 → 第 3 条变红。
"""

from __future__ import annotations

import asyncio
import datetime
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from tests import mfa
from app import db
from app import funds as funds_mod
from app.api import routes
from app.main import app
from app.models import Action, Dag
from app.planner import dag as planner_dag
from app.planner import executor, ticker
from app.security import token as token_mod
from app.security.token import TokenMismatch
from app.tools import scheduled
from app.tools.base import ToolContext
from scripts.seed import DEBIT_ACCOUNT_ID, PAYEE_LIJING, USER_ID, seed

SECRET = "a" * 64
BASE_URL = "http://testserver"
AMOUNT_FEN = 50_000
BIG_AMOUNT_FEN = 200_000  # 2000 元 -> 超过单笔上限 1000 元 -> R08 红灯
SEED_BALANCE_FEN = 1_286_050
DUE_ID = "sch_due01"
CONFIRM_TIMEOUT_MS = 300


@pytest.fixture()
def bank(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("CAPABILITY_SECRET", SECRET)
    monkeypatch.setattr(db, "DEFAULT_DB_PATH", tmp_path / "bank.db")
    conn = db.connect()
    db.init_db(conn)
    seed(conn)
    conn.close()
    executor.RUNS.clear()
    routes._history.clear()
    yield tmp_path / "bank.db"
    executor.RUNS.clear()
    routes._history.clear()


def _conn() -> Any:
    return db.connect()


def _insert(
    conn: Any,
    due_ms: int,
    amount_fen: int = AMOUNT_FEN,
    *,
    factor_verified_ms: int = 0,
    sched_id: str = DUE_ID,
) -> str:
    """直接写一行预约；不经过工具层，避开头寸问题。"""
    conn.execute(
        "INSERT INTO scheduled_transfers (id, user_id, account_id, payee_id, amount_fen, note,"
        " due_ms, recurrence, status, created_ms, idempotency_key, factor_verified_ms)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'scheduled', ?, ?, ?)",
        (sched_id, USER_ID, DEBIT_ACCOUNT_ID, PAYEE_LIJING, amount_fen, "",
         due_ms, "once", ticker_now(), "k_" + sched_id, factor_verified_ms),
    )
    return sched_id


def ticker_now() -> int:
    from app import clock

    return clock.now_ms()


def _balance() -> int:
    conn = _conn()
    try:
        row = conn.execute("SELECT balance_fen FROM accounts WHERE id = ?", (DEBIT_ACCOUNT_ID,)).fetchone()
        return int(row["balance_fen"])
    finally:
        conn.close()


def _status() -> str:
    conn = _conn()
    try:
        row = conn.execute("SELECT status FROM scheduled_transfers WHERE id = ?", (DUE_ID,)).fetchone()
        return str(row["status"])
    finally:
        conn.close()


def _row(sched_id: str | None = None) -> Any:
    """取预约行；不给 id 就取唯一的那一行（工具层生成的 id 是随机的）。"""
    conn = _conn()
    try:
        if sched_id is None:
            return conn.execute(
                "SELECT id, status, amount_fen, due_ms, defer_count, factor_verified_ms"
                " FROM scheduled_transfers ORDER BY created_ms LIMIT 1"
            ).fetchone()
        return conn.execute(
            "SELECT id, status, amount_fen, due_ms, defer_count, factor_verified_ms"
            " FROM scheduled_transfers WHERE id = ?",
            (sched_id,),
        ).fetchone()
    finally:
        conn.close()


def _count_scheduled() -> int:
    conn = _conn()
    try:
        return int(conn.execute("SELECT COUNT(*) AS n FROM scheduled_transfers").fetchone()["n"])
    finally:
        conn.close()


def _held_holds() -> int:
    conn = _conn()
    try:
        return int(
            conn.execute("SELECT COUNT(*) AS n FROM holds WHERE status = 'held'").fetchone()["n"]
        )
    finally:
        conn.close()


def _audit_events(event_type: str) -> list[dict[str, Any]]:
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT payload_json FROM audit_log WHERE event_type = ?", (event_type,)
        ).fetchall()
        return [json.loads(row["payload_json"]) for row in rows]
    finally:
        conn.close()


def call(url: str) -> httpx.Response:
    """同步打端点；ASGITransport 不占端口。"""

    async def run() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url=BASE_URL) as http:
            return await http.post(url)

    return asyncio.run(run())


def schedule_dag(amount_fen: int, due_ms: int) -> Dag:
    """走真实的意图层：intent -> DAG -> 节点参数。"""
    return planner_dag.build(
        {
            "intent": "schedule_transfer",
            "slots": {"payee_name": "李静", "amount_fen": amount_fen, "due_ms": due_ms},
        }
    )


def drive(
    session_id: str,
    dag_obj: Dag,
    *,
    factor: str | None = None,
    confirm_timeout_ms: int = CONFIRM_TIMEOUT_MS,
) -> list[tuple[str, dict[str, Any]]]:
    """跑一轮对话，收集事件流。factor 给了就在收到因子卡时立刻回答。"""
    events: list[tuple[str, dict[str, Any]]] = []

    async def emit(name: str, data: dict[str, Any]) -> None:
        events.append((name, dict(data)))
        if name in ("confirm", "factor"):
            cid = str(data["confirmation_id"])
            # factor 没给 = 这个用例故意不回答；这时也不能偷偷把证明补上，
            # 否则「因子没过就不能落库」这类反证就永远测不出来。
            await executor.resume(
                session_id, cid, True, factor=factor,
                proofs=mfa.solve(session_id, cid) if factor else [],
            )

    asyncio.run(
        executor.run(session_id, dag_obj, emit, confirm_timeout_ms=confirm_timeout_ms)
    )
    return events


def test_endpoint_moves_the_money(bank: Path) -> None:
    """端点打进来要真的扣钱，且第二次不能再扣一次。"""
    assert _balance() == SEED_BALANCE_FEN
    conn = _conn()
    with db.tx(conn):
        _insert(conn, ticker_now() - 1_000)
    conn.close()

    first = call("/api/scheduled/run")
    assert first.status_code == 200
    assert first.json()["executed"] == 1
    assert _balance() == SEED_BALANCE_FEN - AMOUNT_FEN
    assert _status() == "executed"

    second = call("/api/scheduled/run")
    assert second.json()["executed"] == 0
    assert _balance() == SEED_BALANCE_FEN - AMOUNT_FEN


def test_future_row_is_left_alone(bank: Path) -> None:
    """没到点的预约一个字都不能动。"""
    conn = _conn()
    with db.tx(conn):
        _insert(conn, ticker_now() + 86_400_000)
    conn.close()

    assert ticker.run_due_once() == []
    assert _balance() == SEED_BALANCE_FEN
    assert _status() == "scheduled"


def test_huge_interval_is_not_mistaken_for_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """只有 0 表示关闭，负数与非法值都退回默认，不能把节拍关掉。"""
    monkeypatch.setenv(ticker.TICK_ENV, "0")
    assert ticker.tick_interval_ms() == ticker.TICK_OFF
    monkeypatch.setenv(ticker.TICK_ENV, "-5")
    assert ticker.tick_interval_ms() == 0
    monkeypatch.setenv(ticker.TICK_ENV, "abc")
    assert ticker.tick_interval_ms() == ticker.DEFAULT_TICK_MS


def test_start_returns_none_when_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """关掉时不起后台任务，免得测试进程里留一坨永远 sleep 的任务。"""
    monkeypatch.setenv(ticker.TICK_ENV, "0")

    async def run() -> Any:
        return ticker.start()

    assert asyncio.run(run()) is None


# ---------- t11：预约这条链路自己的不变量 ----------


def test_big_schedule_demands_factor_and_is_not_stored_without_it(bank: Path) -> None:
    """2000 元的预约必须走红灯（人脸+短信）；因子没过就不能落库。

    反证：把 schedule_transfer 放回 CONFIRM_ONLY_TOOLS（只黄灯确认），
    这里就会看到 confirm 而不是 factor。
    """
    dag_obj = schedule_dag(BIG_AMOUNT_FEN, ticker_now() + 86_400_000)
    events = drive("s_sched_big", dag_obj)

    names = [name for name, _ in events]
    assert "factor" in names, f"大额预约必须问因子，实际事件流：{names}"
    assert "confirm" not in names, f"大额预约不能只问一句确认：{names}"

    card = next(data for name, data in events if name == "factor")
    assert card["level"] == "red"
    assert sorted(card["required"]) == ["face", "sms"]

    # 因子没过 -> 预约不落库
    assert _count_scheduled() == 0
    assert _balance() == SEED_BALANCE_FEN


def test_factor_approved_schedule_executes_and_moves_the_money(bank: Path) -> None:
    """因子验过的预约落库时记着这件事；到点执行凭它放行当日额度线。

    2000 元 > 1000 元的当日额度：没这条记录的话，钱在到点那天会被额度线拦死。
    """
    dag_obj = schedule_dag(BIG_AMOUNT_FEN, ticker_now() + 86_400_000)
    events = drive("s_sched_big_ok", dag_obj, factor="face+sms")

    assert any(name == "factor" for name, _ in events)
    assert _count_scheduled() == 1
    row = _row()
    assert row is not None
    assert str(row["status"]) == "scheduled"
    assert int(row["amount_fen"]) == BIG_AMOUNT_FEN
    assert int(row["factor_verified_ms"]) > 0, "因子验过了却没记下来"

    # 把它挪到已到点，然后走真实的执行入口
    sched_id = str(row["id"])
    conn = _conn()
    with db.tx(conn):
        conn.execute(
            "UPDATE scheduled_transfers SET due_ms = ? WHERE id = ?",
            (ticker_now() - 1_000, sched_id),
        )
    conn.close()

    resp = call("/api/scheduled/run")
    assert resp.status_code == 200
    assert resp.json()["executed"] == 1
    assert _balance() == SEED_BALANCE_FEN - BIG_AMOUNT_FEN
    assert str(_row(sched_id)["status"]) == "executed"


def test_small_schedule_defers_when_the_day_is_over_the_limit(bank: Path) -> None:
    """当日额度不够不是失败：顺延到下一个自然日，状态仍是 scheduled。

    反证：把到点执行撞额度时的顺延分支去掉，这里就会看到 failed。
    """
    old_due = ticker_now() - 1_000
    conn = _conn()
    with db.tx(conn):
        # 今天已经花满了 1000 元的额度
        conn.execute(
            "INSERT INTO daily_limits (account_id, day, spent_fen, tx_count) VALUES (?, ?, ?, 1)",
            (DEBIT_ACCOUNT_ID, funds_mod.day_of(ticker_now()), 100_000),
        )
        _insert(conn, old_due, amount_fen=30_000)
    conn.close()

    resp = call("/api/scheduled/run")
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert items and items[0]["status"] == "scheduled", "额度不够不该算执行成功"

    row = _row()
    assert str(row["status"]) == "scheduled", f"撞额度不该判 failed：{row['status']}"
    assert int(row["defer_count"]) == 1

    # 顺延到下一个自然日的同一时刻
    local = datetime.datetime.fromtimestamp(old_due / 1000)
    expect = int(
        datetime.datetime.combine(
            local.date() + datetime.timedelta(days=1), local.time()
        ).timestamp()
        * 1000
    )
    assert int(row["due_ms"]) == expect
    assert int(row["due_ms"]) > ticker_now()

    # 审计要留痕：原 due_ms、新 due_ms、原因
    events = _audit_events("scheduled.deferred")
    assert len(events) == 1
    assert int(events[0]["original_due_ms"]) == old_due
    assert int(events[0]["due_ms"]) == expect
    assert events[0]["reason"] == "DAILY_LIMIT_EXCEEDED"

    # 钱没动，hold 也不能白占着
    assert _balance() == SEED_BALANCE_FEN
    assert _held_holds() == 0


def test_old_database_gets_new_columns_without_rebuild(bank: Path) -> None:
    """老库（答辩现场那份 data/bank.db）不能要求重建。

    把新列删掉模拟老库，代码第一次碰到它时自己补列。
    """
    conn = _conn()
    with db.tx(conn):
        conn.execute("ALTER TABLE scheduled_transfers DROP COLUMN factor_verified_ms")
        conn.execute("ALTER TABLE scheduled_transfers DROP COLUMN defer_count")
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(scheduled_transfers)").fetchall()}
    conn.close()
    assert "factor_verified_ms" not in cols
    assert "defer_count" not in cols

    # 老库上的预约照样能排、能执行
    conn = _conn()
    ctx = ToolContext(
        conn=conn, session_id="s_old_db", node_id="n2",
        account_id=DEBIT_ACCOUNT_ID, secret=SECRET,
    )
    params = {
        "payee_id": PAYEE_LIJING,
        "amount_fen": 30_000,
        "due_ms": ticker_now() + 86_400_000,
    }
    tok = token_mod.issue(
        Action(
            tool="schedule_transfer",
            params=params,
            account_id=DEBIT_ACCOUNT_ID,
            amount_fen=30_000,
        ),
        secret=SECRET,
    )
    result = scheduled.schedule_transfer(ctx, params, tok)
    assert result.ok, result.error_text
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(scheduled_transfers)").fetchall()}
    conn.close()
    assert "factor_verified_ms" in cols
    assert "defer_count" in cols
    assert _count_scheduled() == 1


def test_schedule_needs_a_real_token(bank: Path) -> None:
    """排期必须验令牌：拿个对不上参数的令牌排不了期（原来只判非空）。"""
    conn = _conn()
    ctx = ToolContext(
        conn=conn, session_id="s_fake_tok", node_id="n2",
        account_id=DEBIT_ACCOUNT_ID, secret=SECRET,
    )
    params = {
        "payee_id": PAYEE_LIJING,
        "amount_fen": 30_000,
        "due_ms": ticker_now() + 86_400_000,
    }
    forged = token_mod.issue(
        Action(
            tool="schedule_transfer",
            params={**params, "amount_fen": 999_999},
            account_id=DEBIT_ACCOUNT_ID,
            amount_fen=999_999,
        ),
        secret=SECRET,
    )
    with pytest.raises(TokenMismatch):
        scheduled.schedule_transfer(ctx, params, forged)
    conn.close()
    assert _count_scheduled() == 0
