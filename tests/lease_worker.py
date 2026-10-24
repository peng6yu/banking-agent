"""多进程租约测试的 worker：真起子进程去抢同一行预约。

为什么不用线程：SQLite 的单写者语义在**同一进程的多连接**和**多进程**下表现不同，
线程测试会共享 GIL 与解释器状态，跑不出「两个进程同时 tick」的真实现场。
这里每次都由父测试用 subprocess 起一个新解释器，数据库路径与开跑时刻都通过参数传进去，
所有 worker 等到同一个墙钟毫秒再一起动手，最大化争抢。

用法（测试内部使用，不是命令行工具）：
    python tests/lease_worker.py <db_path> <mode:due|ticker> <start_at_ms>
输出：一行 JSON（execute_due 的结果列表，或 ticker.run_due_once 的结果字典）。
"""

from __future__ import annotations

import json
import sys
import time

from app import db
from app.planner import ticker
from app.tools import scheduled


def _wait_until(target_ms: int) -> None:
    while True:
        now = int(time.time() * 1000)
        if now >= target_ms:
            return
        time.sleep(0.001)


def main(argv: list[str]) -> int:
    path, mode, start_at = argv[1], argv[2], int(argv[3])
    db.DEFAULT_DB_PATH = type(db.DEFAULT_DB_PATH)(path)
    _wait_until(start_at)
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
