"""多进程租约：两个真进程同时 tick 时，同一笔预约只能扣一次钱。

单机测试（tests/test_scheduled_lease.py）已经证明「抢不到租约就跳过」，但那是**同一进程的
两个连接**。这一组补的是任务书里点名的那条边界——「不得把已接入调度器等同于多进程调度
可靠性已验证」：父测试用 subprocess 起多个新解释器，各自打开同一个 SQLite 文件，等到同一个
墙钟毫秒再一起调用 execute_due / ticker.run_due_once，然后核对：

- 每一行只被一个 worker 执行（其余 worker 收到 CLAIMED_BY_ANOTHER_WORKER）；
- 流水只多一笔，余额只少一次；
- ticker 这条路（会自己 db.connect）同样成立，不是只有直调工具才安全。

反证：把 _claim 的 UPDATE … WHERE 改成「先 SELECT status 再执行」，本文件三条都会变红。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from app import clock, db
from scripts.seed import DEBIT_ACCOUNT_ID, PAYEE_LIJING, USER_ID, seed

SECRET = "0123456789abcdef" * 4
START_BALANCE_FEN = 1_286_050
# 100 元/笔：刻意压在当日额度（1000 元）以内，让这一组只测租约，
# 不把「额度不够 → 顺延」混进来。额度顺延有 tests/test_scheduled_lease.py 专测。
AMOUNT_FEN = 10_000
ROOT = Path(__file__).resolve().parents[1]
WORKER = Path(__file__).resolve().with_name("lease_worker.py")


@pytest.fixture()
def bank(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("CAPABILITY_SECRET", SECRET)
    monkeypatch.setattr(db, "DEFAULT_DB_PATH", tmp_path / "bank.db")
    conn = db.connect()
    db.init_db(conn)
    seed(conn)
    conn.close()
    return db.DEFAULT_DB_PATH


def _insert(
    path: Path,
    sched_id: str,
    *,
    amount_fen: int = AMOUNT_FEN,
    due_ms: int | None = None,
) -> None:
    now = clock.now_ms()
    conn = db.connect(path)
    try:
        conn.execute(
            "INSERT INTO scheduled_transfers (id, user_id, account_id, payee_id, amount_fen, note,"
            " due_ms, recurrence, status, created_ms, executed_ms, factor_verified_ms, defer_count,"
            " idempotency_key, claim_token, claimed_ms)"
            " VALUES (?,?,?,?,?,'',?,'once','scheduled',?,0,0,0,?,'',0)",
            (sched_id, USER_ID, DEBIT_ACCOUNT_ID, PAYEE_LIJING, amount_fen,
             now - 1_000 if due_ms is None else due_ms, now, f"idem-{sched_id}"),
        )
    finally:
        conn.close()


def _run_workers(path: Path, mode: str, count: int) -> list[Any]:
    """同一起跑线起 count 个进程，返回每个进程打印的 JSON。

    起跑用文件闸门（见 tests/lease_worker.py 的说明）：先等每个子进程都摸到 ready，
    父进程再写 go，让所有人在几乎同一微秒进 execute_due 的 SELECT；只对墙钟毫秒的话，
    解释器启动慢的那个醒来时行已经被执行完，它压根查不到到期行，返回空列表。
    """
    gate = path.parent / f"lease-gate-{os.getpid()}-{time.monotonic_ns()}"
    gate.mkdir()
    go_file = gate / "go"
    env = dict(os.environ)
    env["CAPABILITY_SECRET"] = SECRET
    env["PYTHONPATH"] = str(ROOT)
    env["LEASE_READY_DIR"] = str(gate)
    env["LEASE_GO_FILE"] = str(go_file)
    procs = [
        subprocess.Popen(
            [sys.executable, str(WORKER), str(path), mode, "0"],
            cwd=str(ROOT),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for _ in range(count)
    ]
    deadline = time.time() + 60
    while True:
        ready = [p for p in gate.iterdir() if p.name != "go"]
        if len(ready) >= count:
            break
        assert time.time() < deadline, f"只有 {len(ready)}/{count} 个 worker 就绪：{[p.poll() for p in procs]}"
        time.sleep(0.002)
    go_file.write_text("", encoding="utf-8")
    outs: list[Any] = []
    for proc in procs:
        out, err = proc.communicate(timeout=60)
        assert proc.returncode == 0, f"worker 失败：{err[-800:]}"
        outs.append(json.loads(out.strip().splitlines()[-1]))
    return outs


def _executed(outs: list[Any]) -> list[dict[str, Any]]:
    """把各 worker 的结果摊平，只留真正执行成功的条目。"""
    flat: list[dict[str, Any]] = []
    for out in outs:
        items = out if isinstance(out, list) else out.get("items", [])
        flat.extend(item for item in items if item.get("status") == "executed")
    return flat


def _tx_count(path: Path) -> int:
    conn = db.connect(path)
    try:
        return int(conn.execute("SELECT COUNT(*) AS n FROM transactions").fetchone()["n"])
    finally:
        conn.close()


def _balance(path: Path) -> int:
    conn = db.connect(path)
    try:
        row = conn.execute("SELECT balance_fen FROM accounts WHERE id = ?", (DEBIT_ACCOUNT_ID,)).fetchone()
        return int(row["balance_fen"])
    finally:
        conn.close()


def test_four_processes_racing_the_same_due_transfer_charge_once(bank: Path) -> None:
    """4 个进程抢同一行：钱只出一次。

    这里**不**断言「3 个进程都收到 CLAIMED_BY_ANOTHER_WORKER」：到得稍晚的那个进程
    执行 SELECT 时行可能已被改成 executed，它压根查不到到期行，返回空列表——
    这同样是安全的结果，把它算失败只会让用例在慢机器/高负载下随机变红。
    真正的不变量是：只有一个 executed、流水只多一笔、余额只少一次，
    并且任何看到过这一行的进程，结论只能是「我执行」或「被租约挡下」。
    """
    _insert(bank, "sched_race")
    before = _tx_count(bank)
    outs = _run_workers(bank, "due", 4)

    flat = [
        item
        for out in outs
        for item in (out if isinstance(out, list) else out.get("items", []))
    ]
    executed = [item for item in flat if item.get("status") == "executed"]
    skipped = [item for item in flat if item.get("status") == "skipped"]
    assert len(executed) == 1, f"只能有一笔真正执行，实际 {len(executed)}：{outs}"
    assert executed[0]["scheduled_id"] == "sched_race"
    assert all(item.get("status") in {"executed", "skipped"} for item in flat), flat
    assert all(item.get("reason") == "CLAIMED_BY_ANOTHER_WORKER" for item in skipped), skipped
    assert {item.get("scheduled_id") for item in flat} == {"sched_race"}
    assert _tx_count(bank) == before + 1
    assert _balance(bank) == START_BALANCE_FEN - AMOUNT_FEN


def test_two_processes_running_the_ticker_charge_once(bank: Path) -> None:
    """走 ticker 那条真实路径：它自己开连接，不经过测试传进去的 conn。"""
    _insert(bank, "sched_tick")
    before = _tx_count(bank)
    outs = _run_workers(bank, "ticker", 2)

    executed = _executed(outs)
    assert len(executed) == 1, f"ticker 并发下也只能扣一次：{outs}"
    assert _tx_count(bank) == before + 1
    assert _balance(bank) == START_BALANCE_FEN - AMOUNT_FEN


def test_three_processes_racing_five_rows_execute_each_exactly_once(bank: Path) -> None:
    ids = [f"sched_batch_{i}" for i in range(5)]
    for sched_id in ids:
        _insert(bank, sched_id)
    before = _tx_count(bank)
    outs = _run_workers(bank, "due", 3)

    executed = _executed(outs)
    assert len(executed) == 5, f"五行各执行一次，实际 {len(executed)}：{outs}"
    assert {item["scheduled_id"] for item in executed} == set(ids)
    assert len({item["scheduled_id"] for item in executed}) == 5, "同一行不能被执行两次"
    assert _tx_count(bank) == before + 5
    assert _balance(bank) == START_BALANCE_FEN - 5 * AMOUNT_FEN
