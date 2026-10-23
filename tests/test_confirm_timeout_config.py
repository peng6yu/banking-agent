"""确认窗口可配置：CONFIRM_TIMEOUT_S 环境变量（README 已知限制 5）。

原来 120 秒是写死的常量：命令行/IM 客户端要另开一个请求去 POST 确认，
窗口短了来不及、长了又挡着演示节奏。现在默认仍是 120 秒，可用
CONFIRM_TIMEOUT_S（10—3600 秒）覆盖；非法值不 raise，回落默认值并打警告——
这是一个运行期调参项，为拼错的变量名把确认链路变成 500 不划算。
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

import pytest

from app import db
from app.models import Dag
from app.planner import dag as planner_dag
from app.planner import executor
from app.planner import state as planner_state
from scripts.seed import seed

SECRET = "a" * 64
ENV = planner_state.CONFIRM_TIMEOUT_ENV


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ENV, raising=False)


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


def test_default_window_is_120_seconds() -> None:
    assert planner_state.confirm_timeout_ms() == 120_000
    assert planner_state.confirm_timeout_ms() == planner_state.CONFIRM_TIMEOUT_MS


def test_env_override_in_seconds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV, "30")
    assert planner_state.confirm_timeout_ms() == 30_000


def test_boundaries_are_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV, str(planner_state.MIN_CONFIRM_TIMEOUT_S))
    assert planner_state.confirm_timeout_ms() == planner_state.MIN_CONFIRM_TIMEOUT_S * 1000
    monkeypatch.setenv(ENV, str(planner_state.MAX_CONFIRM_TIMEOUT_S))
    assert planner_state.confirm_timeout_ms() == planner_state.MAX_CONFIRM_TIMEOUT_S * 1000


@pytest.mark.parametrize("raw", ["abc", "0", "-5", "5", str(planner_state.MAX_CONFIRM_TIMEOUT_S + 1)])
def test_invalid_values_fall_back_to_default(monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
    monkeypatch.setenv(ENV, raw)
    assert planner_state.confirm_timeout_ms() == planner_state.CONFIRM_TIMEOUT_MS


def test_confirm_event_reports_the_effective_window(bank: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """SSE 的 expires_in_s 与卡片 expires_ms 都要跟着环境变量走，不能一个改一个不改。"""
    monkeypatch.setenv(ENV, "45")
    events = _drive_and_answer("s_timeout_env")
    confirm = next(data for name, data in events if name == "confirm")
    assert confirm["expires_in_s"] == 45
    created: list[Any] = []
    # 卡片本身的有效期也要是 45 秒
    conn = db.connect()
    try:
        row = conn.execute("SELECT expires_ms, created_ms FROM confirmations").fetchone()
        created.append(row)
    finally:
        conn.close()
    assert created and created[0] is not None
    assert created[0]["expires_ms"] - created[0]["created_ms"] == 45_000


def _drive_and_answer(session_id: str) -> list[tuple[str, dict[str, Any]]]:
    events: list[tuple[str, dict[str, Any]]] = []
    dag_obj: Dag = planner_dag.build(
        {"intent": "transfer", "slots": {"payee_name": "李静", "amount_fen": 50_000}}
    )

    async def emit(name: str, data: dict[str, Any]) -> None:
        events.append((name, data))

    async def answerer() -> None:
        handled: set[str] = set()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            state = executor.get_run(session_id)
            if state is not None:
                for conf in list(state.confirmations.values()):
                    if conf.status == "pending" and conf.future is not None and conf.id not in handled:
                        handled.add(conf.id)
                        await executor.resume(session_id, conf.id, True)
                        return
            await asyncio.sleep(0.01)

    async def main() -> None:
        task = asyncio.create_task(answerer())
        await executor.run(session_id, dag_obj, emit)
        await task

    asyncio.run(main())
    return events
