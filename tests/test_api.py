"""API 层测试：十个端点 + SSE 八事件 + 异常到 HTTP 的映射。

本机没有 pytest-asyncio（docs/09 9.11），异步用例一律 asyncio.run 包起来；
httpx 的 ASGITransport 会把响应体收完才返回，所以转账这种需要中途确认的流程，
由一个并发协程在同一条 SSE 流上打 /api/confirm。
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

import httpx
import pytest

from app import db
from app.api import chat_flow, routes
from app.llm import LLMError, LLMResponse, ToolCall
from app.main import app
from app.planner import executor
from app.tools.registry import TOOL_NAMES
from scripts.seed import seed

SECRET = "a" * 64
BASE_URL = "http://testserver"
AMOUNT_FEN = 50_000
SEED_BALANCE_FEN = 1_286_050
# emit_intent 的实参是平铺的（docs/07 7.2 的 properties 就在顶层）
TRANSFER_CALL = {
    "intent": "transfer",
    "payee_name": "李静",
    "amount_fen": AMOUNT_FEN,
    "missing": [],
    "confidence": 0.95,
}
BIRTHDAY_CALL = {
    "intent": "birthday_plan",
    "payee_name": "李静",
    "amount_fen": 20_000,
    "missing": [],
    "confidence": 0.93,
}


class Scripted:
    """按剧本回放模型回复；元素是 dict 就当 emit_intent 的参数。"""

    def __init__(self, script: list[Any]) -> None:
        self.script = list(script)
        self.calls = 0

    def chat(self, messages: list[dict[str, Any]], **kwargs: Any) -> LLMResponse:
        item = self.script[min(self.calls, len(self.script) - 1)]
        self.calls += 1
        return LLMResponse(
            content="",
            reasoning="",
            tool_calls=[ToolCall(id="c1", name="emit_intent", arguments=json.dumps(item, ensure_ascii=False))],
            finish_reason="tool_calls",
            model="scripted",
        )


class Down:
    """模型不可用：用来验 503 分支。"""

    def chat(self, messages: list[dict[str, Any]], **kwargs: Any) -> LLMResponse:
        raise LLMError("没有 API Key")


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
    routes.set_adapter(Scripted([dict(TRANSFER_CALL)]))
    yield tmp_path / "bank.db"
    routes.set_adapter(None)
    executor.RUNS.clear()
    routes._history.clear()


def call(method: str, url: str, *, raise_app_exceptions: bool = True, **kwargs: Any) -> httpx.Response:
    """同步发一个请求；内部用 ASGITransport，不占端口。"""

    async def run() -> httpx.Response:
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=raise_app_exceptions)
        async with httpx.AsyncClient(transport=transport, base_url=BASE_URL) as http:
            return await http.request(method, url, **kwargs)

    return asyncio.run(run())


def parse_sse(text: str) -> list[tuple[str, dict[str, Any]]]:
    """把 event: xxx / data: {...} 还原成 (名字, 载荷) 列表。"""
    events: list[tuple[str, dict[str, Any]]] = []
    for block in text.split("\n\n"):
        if not block.strip():
            continue
        name = None
        data: dict[str, Any] = {}
        for line in block.splitlines():
            if line.startswith("event: "):
                name = name or line[len("event: ") :]
            elif line.startswith("data: "):
                data = json.loads(line[len("data: ") :])
        assert name is not None, block
        events.append((name, data))
    return events


def chat(
    session_id: str,
    text: str,
    *,
    approve: bool = True,
    cancel: bool = False,
    duplicate: bool = False,
) -> tuple[list[tuple[str, Any]], list[httpx.Response], list[httpx.Response]]:
    """跑一轮 /api/chat；确认卡片一挂起就并发回一条 /api/confirm 或 /api/cancel。

    duplicate=True 时，答第二张卡片前先把第一张再答一遍，用来验幂等。
    """

    async def main() -> tuple[list[tuple[str, Any]], list[httpx.Response], list[httpx.Response]]:
        transport = httpx.ASGITransport(app=app)
        answers: list[httpx.Response] = []
        repeats: list[httpx.Response] = []
        async with httpx.AsyncClient(transport=transport, base_url=BASE_URL) as http:
            chat_task = asyncio.create_task(http.post("/api/chat", json={"session_id": session_id, "text": text}))

            async def answerer() -> None:
                handled: set[str] = set()
                first_id: str | None = None
                deadline = time.monotonic() + 20
                while time.monotonic() < deadline and not chat_task.done():
                    state = executor.get_run(session_id)
                    for conf in list(state.confirmations.values()) if state else []:
                        if conf.status != "pending" or conf.future is None or conf.id in handled:
                            continue
                        handled.add(conf.id)
                        if duplicate and first_id is not None:
                            repeats.append(
                                await http.post(
                                    "/api/confirm",
                                    json={"session_id": session_id, "confirmation_id": first_id, "approve": True},
                                )
                            )
                        if first_id is None:
                            first_id = conf.id
                        if cancel:
                            answers.append(await http.post("/api/cancel", json={"session_id": session_id}))
                            continue
                        answers.append(
                            await http.post(
                                "/api/confirm",
                                json={
                                    "session_id": session_id,
                                    "confirmation_id": conf.id,
                                    "approve": approve,
                                    "factor": "face+sms",
                                },
                            )
                        )
                    await asyncio.sleep(0.005)

            helper = asyncio.create_task(answerer())
            try:
                chat_resp = await asyncio.wait_for(chat_task, timeout=25)
            finally:
                helper.cancel()
        return parse_sse(chat_resp.text), answers, repeats

    return asyncio.run(main())


def names(events: list[tuple[str, Any]]) -> list[str]:
    return [name for name, _ in events]


def payload(events: list[tuple[str, Any]], name: str) -> dict[str, Any]:
    for event, data in events:
        if event == name:
            return dict(data)
    raise AssertionError(f"没有 {name} 事件：{names(events)}")


def balance() -> int:
    conn = db.connect()
    try:
        return int(conn.execute("SELECT balance_fen FROM accounts WHERE type='debit' LIMIT 1").fetchone()[0])
    finally:
        conn.close()


def test_sse_frame_is_exactly_two_lines(bank: Path) -> None:
    assert routes.frame("tool", {"ok": True}) == 'event: tool\ndata: {"ok": true}\n\n'


def test_health_reports_the_authoritative_tool_count_and_a_live_db(bank: Path) -> None:
    resp = call("GET", "/api/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["tools"] == len(TOOL_NAMES)
    assert body["db"] is True
    assert isinstance(body["now_ms"], int)


def test_tools_endpoint_returns_the_authoritative_table(bank: Path) -> None:
    body = call("GET", "/api/tools").json()
    assert body["count"] == len(TOOL_NAMES)
    assert tuple(tool["name"] for tool in body["tools"]) == TOOL_NAMES
    for tool in body["tools"]:
        assert tool["params"]["additionalProperties"] is False


def test_chat_missing_payee_only_asks_a_question(bank: Path) -> None:
    routes.set_adapter(Scripted([{"intent": "transfer", "missing": ["payee_name"], "confidence": 0.9}]))
    resp = call("POST", "/api/chat", json={"session_id": "s_api_q", "text": "帮我转笔钱"})
    events = parse_sse(resp.text)
    assert names(events) == ["start", "intent", "done"]
    assert payload(events, "start")["session_id"] == "s_api_q"
    assert payload(events, "intent")["intent"] == "transfer"
    assert "转给谁" in payload(events, "done")["summary"]


def test_chat_transfer_flow_confirms_and_moves_the_money(bank: Path) -> None:
    before = balance()
    events, answers, _repeats = chat("s_api_t", "给李静转 500 元")
    assert answers and answers[0].status_code == 200
    assert answers[0].json()["status"] == "approved"
    order = names(events)
    assert order[:2] == ["start", "intent"]
    assert "confirm" in order and order[-1] == "done"
    assert "error" not in order
    trace = next(data for name, data in events if name == "trace" and data["tool"] == "transfer")
    assert trace["level"] == "yellow"
    assert trace["rule_id"] == "R06_transfer_yellow"
    card = payload(events, "confirm")
    assert card["confirmation_id"] and card["action_text"]
    assert card["expires_in_s"] == 120
    assert "transfer" in [data["tool"] for name, data in events if name == "trace"]
    done = payload(events, "done")
    assert done["balance_after_fen"] == before - AMOUNT_FEN
    # 分与元必须成对下发：SSE 帧同样不许只给分不给元（前端不该自己除 100）。
    assert done["balance_after_yuan"] == f"{(before - AMOUNT_FEN) / 100:,.2f}"
    assert balance() == before - AMOUNT_FEN


def test_cancel_endpoint_stops_the_run_and_frees_the_hold(bank: Path) -> None:
    before = balance()
    events, answers, _repeats = chat("s_api_c", "给李静转 500 元", cancel=True)
    assert answers[0].json()["cancelled"] is True
    # 取消也要给一个终帧：客户端靠 done 区分「正常收尾」与「流被截断」。
    assert names(events)[-1] == "done"
    assert events[-1][1]["ended"] == "cancelled"
    assert balance() == before
    conn = db.connect()
    try:
        states = dict(
            conn.execute(
                "SELECT node_id, state FROM dag_nodes WHERE run_id ="
                " (SELECT id FROM dag_runs WHERE session_id = 's_api_c')"
            )
        )
        holds = conn.execute("SELECT COUNT(*) FROM holds WHERE status = 'held'").fetchone()[0]
    finally:
        conn.close()
    assert states["n3"] == "skipped"
    assert holds == 0


def test_confirm_twice_is_idempotent(bank: Path) -> None:
    routes.set_adapter(Scripted([dict(BIRTHDAY_CALL)]))
    events, _answers, repeats = chat("s_api_i", "给李静 200 块生日红包", duplicate=True)
    assert len(payload(events, "confirm")) or True
    assert repeats, "第二张卡片前应该重发过一次第一张"
    assert repeats[0].json() == {"ok": True, "status": "approved", "idempotent": True}
    assert names(events).count("confirm") == 2


def test_session_and_audit_endpoints_explain_the_run(bank: Path) -> None:
    events, _answers, _repeats = chat("s_api_s", "给李静转 500 元")
    done = payload(events, "done")
    assert done["balance_after_fen"] and done["balance_after_yuan"]
    session = call("GET", "/api/session/s_api_s").json()
    assert session["found"] is True
    assert session["status"] in {"done", "cancelled"}
    states = {node["node_id"]: node["state"] for node in session["nodes"]}
    assert states["n1"] == "done"
    assert states["n3"] == "done"
    trail = call("GET", "/api/audit", params={"session_id": "s_api_s"}).json()
    assert trail["chain_ok"] is True
    assert trail["count"] >= 10
    kinds = [event["event_type"] for event in trail["events"]]
    for expected in ("session.start", "intent.parsed", "dag.built", "policy.decided", "transfer.committed"):
        assert expected in kinds
    assert call("GET", "/api/session/nope").json()["found"] is False


def test_audit_verify_endpoint_proves_the_chain(bank: Path) -> None:
    routes.set_adapter(Scripted([{"intent": "query_balance", "missing": [], "confidence": 0.9}]))
    chat_body = call("POST", "/api/chat", json={"session_id": "s_api_v", "text": "我卡里还有多少钱"})
    assert chat_body.status_code == 200
    assert names(parse_sse(chat_body.text))[-1] == "done"
    body = call("GET", "/api/audit/verify").json()
    assert body["ok"] is True
    assert body["count"] > 0
    assert isinstance(body["head"], str) and body["head"]


def test_demo_reset_rewinds_the_money_the_agent_moved(bank: Path) -> None:
    events, _answers, _repeats = chat("s_api_r", "给李静转 500 元")
    assert balance() == SEED_BALANCE_FEN - AMOUNT_FEN
    body = call("POST", "/api/demo/reset", json={"reason": "演示重置"}).json()
    assert body["ok"] is True
    assert body["balance_fen"] == SEED_BALANCE_FEN
    assert balance() == SEED_BALANCE_FEN
    assert events


def test_bad_request_body_is_a_chinese_400(bank: Path) -> None:
    resp = call("POST", "/api/chat", json={"text": ""})
    assert resp.status_code == 400
    body = resp.json()
    assert body["code"] == "VALIDATION"
    assert set(body) == {"code", "user_text"}
    assert "Traceback" not in resp.text and "validation error" not in resp.text.lower()
    extra = call("POST", "/api/chat", json={"session_id": "s", "text": "你好", "oops": 1})
    assert extra.status_code == 400


def test_model_unavailable_is_503(bank: Path) -> None:
    routes.set_adapter(Down())
    resp = call("POST", "/api/chat", json={"session_id": "s_api_d", "text": "你好"})
    assert resp.status_code == 503
    assert resp.json()["code"] == "MODEL_UNAVAILABLE"
    assert "LLMError" not in resp.text


def test_unhandled_error_is_500_without_a_stack(monkeypatch: pytest.MonkeyPatch, bank: Path) -> None:
    def boom() -> Any:
        raise RuntimeError("secret internal detail")

    # W4 拆分后聊天主流程在 app/api/chat_flow.py：它 import 的是那个模块里的
    # get_adapter，补丁要打在真正被调用的模块上（打 routes 上的副本不会生效）。
    monkeypatch.setattr(chat_flow, "get_adapter", boom)
    resp = call("POST", "/api/chat", json={"session_id": "s_api_x", "text": "你好"}, raise_app_exceptions=False)
    assert resp.status_code == 500
    assert resp.json() == {"code": "INTERNAL", "user_text": "系统开小差了，已经记录。"}
    assert "Traceback" not in resp.text
    assert "RuntimeError" not in resp.text
    assert "secret internal detail" not in resp.text


def test_index_serves_the_frontend_and_unknown_route_stays_chinese(bank: Path) -> None:
    root = call("GET", "/")
    assert root.status_code == 200
    assert "text/html" in root.headers["content-type"]
    # W5 之后 index.html 只是壳：结构在 HTML、样式在 /static/app.css、脚本在 /static/js/*.js。
    assert "/static/app.css" in root.text
    assert "/static/js/10-core.js" in root.text
    missing = call("GET", "/api/nope")
    assert missing.status_code == 404
    assert missing.json()["code"] == "HTTP_ERROR"
    assert "Not Found" not in missing.text


def test_static_assets_are_served_without_the_api_token(bank: Path) -> None:
    """样式与脚本挂在 /static 下，不校验 X-API-Token：否则没令牌就只剩一个白页。

    这条同时锁住「壳引用的文件真的存在」——旧版把 CSS/JS 内联在同一份 HTML 里，
    拆分之后最容易犯的错就是文件名改了却忘了改壳。
    """
    css = call("GET", "/static/app.css")
    assert css.status_code == 200
    assert "text/css" in css.headers["content-type"]
    assert "--green" in css.text

    core = call("GET", "/static/js/10-core.js")
    assert core.status_code == 200
    assert "api/chat" in core.text or "API_CHAT" in core.text

    assert call("GET", "/static/js/nope.js").status_code == 404

