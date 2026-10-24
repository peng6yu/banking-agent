#!/usr/bin/env python3.12
"""量一次「一句话到确认卡」的端到端耗时（脚本桩，无网络）。

为什么单独一个脚本：README / 演示方案里的交互效率数字需要可复跑，不能只写「很快」。
口径：MOCK_MODE=script（只替换 LLM 那一层，策略/DAG/令牌/资金/审计全是生产代码），
TestClient 直连 ASGI，不含浏览器渲染与网络；真模型耗时另见 README 的历史实测。

用法：
    PYTHONPATH=. .venv/bin/python scripts/measure_latency.py
"""
from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path

os.environ.setdefault("MOCK_MODE", "script")
os.environ.setdefault("CAPABILITY_SECRET", "0" * 64)
os.environ.setdefault("API_AUTH_DISABLED", "1")
os.environ.setdefault("CONFIRM_TIMEOUT_S", "30")

_TMP = Path(tempfile.mkdtemp(prefix="banking-latency-"))

from fastapi.testclient import TestClient  # noqa: E402

from app import db  # noqa: E402

db.DEFAULT_DB_PATH = _TMP / "bank.db"

from app.main import app  # noqa: E402
from scripts.seed import seed  # noqa: E402


def _frames(resp, stop_at: tuple[str, ...] = ()) -> list[tuple[str | None, dict]]:
    out: list[tuple[str | None, dict]] = []
    name: str | None = None
    for line in resp.iter_lines():
        if isinstance(line, bytes):
            line = line.decode()
        if line.startswith("event:"):
            name = line[6:].strip()
        elif line.startswith("data:"):
            try:
                out.append((name, json.loads(line[5:].strip())))
            except Exception:
                continue
            if name in stop_at:
                break
    return out


def main() -> int:
    result: dict[str, object] = {}
    with TestClient(app) as client:
        conn = db.connect()
        db.init_db(conn)
        seed(conn)
        conn.close()
        sid = "s_latency"

        t0 = time.perf_counter()
        with client.stream("POST", "/api/chat", json={"session_id": sid, "text": "查一下余额"}) as r:
            frames = _frames(r)
        result["green_ms"] = round((time.perf_counter() - t0) * 1000, 1)
        result["green_events"] = [n for n, _ in frames]

        t0 = time.perf_counter()
        conf = None
        with client.stream("POST", "/api/chat", json={"session_id": sid, "text": "给李静转 100 元"}) as r:
            frames = _frames(r, stop_at=("confirm", "factor"))
        result["to_confirm_ms"] = round((time.perf_counter() - t0) * 1000, 1)
        for name, data in frames:
            if name == "confirm":
                conf = data.get("confirmation_id") or (data.get("card") or {}).get("confirmation_id")

        if conf:
            t0 = time.perf_counter()
            resp = client.post(
                "/api/confirm",
                json={"session_id": sid, "confirmation_id": conf, "approve": True},
            )
            result["confirm_ms"] = round((time.perf_counter() - t0) * 1000, 1)
            result["confirm_status"] = resp.status_code
        else:
            result["confirm_ms"] = None
            result["confirm_status"] = "no_confirm_frame"

    result["mode"] = "MOCK_MODE=script / TestClient / 无网络 / 不含浏览器渲染"
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
