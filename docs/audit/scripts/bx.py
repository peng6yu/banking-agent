"""体检用的小客户端：SSE 聊天 + 确认 + 挑战。

前置：
  cd banking-agent
  CONFIRM_TIMEOUT_S=120 SCHEDULED_TICK_MS=0 FACTOR_DEMO_ECHO=1 \
    .venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8123

环境变量：
  BANK_AUDIT_BASE   默认 http://127.0.0.1:8123
  BANK_AUDIT_TOKEN  显式令牌；不设则依次读 data/api_token、../../data/api_token
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path

import httpx

BASE = os.environ.get("BANK_AUDIT_BASE", "http://127.0.0.1:8123")


def _token() -> str:
    tok = os.environ.get("BANK_AUDIT_TOKEN", "").strip()
    if tok:
        return tok
    here = Path.cwd()
    for base in [here, *here.parents]:
        cand = base / "data" / "api_token"
        if cand.exists():
            return cand.read_text(encoding="utf-8").strip()
    raise SystemExit("找不到 API token：设 BANK_AUDIT_TOKEN 或在 banking-agent/ 下运行")


TOKEN = _token()
HEADERS = {"X-API-Token": TOKEN}


def client(timeout: float = 180.0) -> httpx.Client:
    return httpx.Client(base_url=BASE, headers=HEADERS, timeout=timeout)


def chat(c, text, sid, on_event=None, verbose=True):
    """跑一轮对话；on_event(name, data) 在读取线程里被调用（可发确认）。返回全部事件。"""
    evs = []

    def reader():
        with c.stream("POST", "/api/chat", json={"text": text, "session_id": sid}) as r:
            name = None
            for line in r.iter_lines():
                if line.startswith("event: "):
                    name = line[7:]
                elif line.startswith("data: "):
                    data = json.loads(line[6:])
                    evs.append((name, data))
                    if verbose:
                        print(f"   [{name}] {json.dumps(data, ensure_ascii=False)[:420]}")
                    if on_event:
                        on_event(name, data)

    t = threading.Thread(target=reader)
    t.start()
    return evs, t


def first(evs, name):
    for n, d in evs:
        if n == name:
            return d
    return None


def confirm(c, sid, cid, approve=True, proofs=None):
    body = {"session_id": sid, "confirmation_id": cid, "approve": approve}
    if proofs is not None:
        body["proofs"] = proofs
    r = c.post("/api/confirm", json=body)
    try:
        payload = r.json()
    except Exception:
        payload = {"raw": r.text[:300]}
    return r.status_code, payload


def balance(c):
    d = c.get("/api/business/overview").json()
    return d["account"]["balance_fen"], d["account"]["balance_yuan"]
