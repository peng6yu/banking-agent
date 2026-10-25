"""多进程租约测试的 worker：真起子进程去抢同一行预约。

为什么不用线程：SQLite 的单写者语义在**同一进程的多连接**和**多进程**下表现不同，
线程测试会共享 GIL 与解释器状态，跑不出「两个进程同时 tick」的真实现场。
这里每次都由父测试用 subprocess 起一个新解释器，数据库路径通过参数传进去。

起跑用文件闸门而不是墙钟：解释器启动 + 导入 app 的耗时在每个子进程上并不一样
（CI/负载高时能差几百毫秒），只对同一个墙钟毫秒会让慢的那个醒来时行已经被执行完，
它 SELECT 不到任何到期行、返回空列表——测试就会错误地判「没抢到租约的进程不够数」。
所以改成：每个 worker 先摸一个 ready 文件，然后轮询父进程写的 go 文件，
父进程等齐 count 个 ready 再写 go，四个进程几乎同一微秒进 SELECT。

用法（测试内部使用，不是命令行工具）：
    python tests/lease_worker.py <db_path> <mode:due|ticker> [start_at_ms]
环境变量（可选）：LEASE_READY_DIR（摸 ready 文件的目录）、LEASE_GO_FILE（父进程的起跑标记）。
输出：一行 JSON（execute_due 的结果列表，或 ticker.run_due_once 的结果字典）。
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

from app import db
from app.planner import ticker
from app.tools import scheduled


def _wait_until(target_ms: int) -> None:
    while True:
        now = int(time.time() * 1000)
        if now >= target_ms:
            return
        time.sleep(0.001)


def _mark_ready() -> None:
    """告诉父进程「我已经导入完、随时可以开跑」。"""
    ready_dir = os.environ.get("LEASE_READY_DIR")
    if ready_dir:
        Path(ready_dir, str(os.getpid())).write_text("", encoding="utf-8")


def _wait_for_go(fallback_ms: int) -> None:
    """等父进程写 go 文件；没有闸门时退回墙钟等待（单独手跑 worker 时用）。"""
    go_file = os.environ.get("LEASE_GO_FILE")
    if not go_file:
        _wait_until(fallback_ms)
        return
    deadline = time.time() + 120
    while not Path(go_file).exists():
        if time.time() > deadline:
            raise SystemExit("lease worker: 等不到父进程的 go 文件")
        time.sleep(0.0005)


def main(argv: list[str]) -> int:
    path, mode = argv[1], argv[2]
    fallback_ms = int(argv[3]) if len(argv) > 3 else 0
    db.DEFAULT_DB_PATH = type(db.DEFAULT_DB_PATH)(path)
    _mark_ready()
    _wait_for_go(fallback_ms)
    if mode == "ticker":
        out: object = ticker.run_due_once()
    else:
        conn = db.connect()
        try:
            out = scheduled.execute_due(conn)
        finally:
            conn.close()
    print(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
