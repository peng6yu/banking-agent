"""订阅取消场景（t47）：意图分流 → 唯一匹配 → 黄灯确认 → 真取消。

四道防线，每一道都有用例钉住：
1. 意图层：只有用户原话里有取消动作词才走取消那一路——查订阅绝不会变成替用户退订；
2. 规划层：订阅名必须唯一匹配上，匹配不到或匹配到多条一律反问，写工具不会被调用；
3. 策略层：cancel_subscription 落在 R05_confirm_only 黄灯，用户点头才执行；
4. 工具层：require_token 把关，订阅状态真从 active 改成 cancelled。

每条用例都带反证：断言「该发生的发生了」之外，还要断言「不该发生的没发生」
（写工具没被调用、别的订阅没被动、审计链仍然可复算）。
"""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

import pytest

from tests import mfa
from app import audit, db
from app.clock import ValidationError
from app.llm import LLMResponse, ToolCall
from app.models import Dag
from app.planner import dag as planner_dag
from app.planner import executor, intent
from app.planner.intent_schema import CANCEL_SLOT
from scripts.seed import seed

SECRET = "a" * 64
# 种子里的 5 条订阅（scripts/seed.py SUB_SEEDS），第一条是爱奇艺会员
IQIYI = "爱奇艺会员"
IQIYI_SUB_ID = "sb_00000001"
# 订阅列表长什么样：list_subs 返回的 subs 项
SUBS = [
    {"sub_id": "sb_00000001", "merchant": IQIYI},
    {"sub_id": "sb_00000002", "merchant": "百度网盘"},
    {"sub_id": "sb_00000003", "merchant": "网易云音乐"},
]


class Scripted:
    """按剧本回放模型回复：只回 emit_intent 的参数。"""

    def __init__(self, item: dict[str, Any]) -> None:
        self.item = item

    def chat(self, messages: list[dict[str, Any]], **kwargs: Any) -> LLMResponse:
        return LLMResponse(
            content="",
            reasoning="",
            tool_calls=[
                ToolCall(id="c1", name="emit_intent", arguments=json.dumps(self.item, ensure_ascii=False))
            ],
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


def parse(text: str, model_args: dict[str, Any]) -> intent.IntentResult:
    return asyncio.run(intent.parse(text, [], adapter=Scripted(model_args)))


def sub_dag(text: str, model_args: dict[str, Any]) -> Dag:
    """走生产那两步：先解析出意图与槽位，再按槽位建图（app/api/routes.py 就是这么走的）。"""
    parsed = parse(text, model_args)
    return planner_dag.build_or_single({"intent": parsed.intent, "slots": parsed.slots})


def drive(session_id: str, dag_obj: Dag, *, answers: list[dict[str, Any]] | None = None,
          confirm_timeout_ms: int = 3_000) -> list[tuple[str, dict[str, Any]]]:
    """跑一轮并替用户回答确认；只有 future 建好（执行器已挂起）才作答，避免竞态。"""
    events: list[tuple[str, dict[str, Any]]] = []

    async def emit(name: str, data: dict[str, Any]) -> None:
        events.append((name, data))

    async def answerer() -> None:
        handled: set[str] = set()
        used = 0
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            state = executor.get_run(session_id)
            if state is not None:
                for conf in list(state.confirmations.values()):
                    if conf.status != "pending" or conf.future is None or conf.id in handled:
                        continue
                    handled.add(conf.id)
                    if answers is None:
                        continue
                    reply = answers[min(used, len(answers) - 1)]
                    used += 1
                    approve = bool(reply.get("approve", True))
                    card_proofs = (
                        mfa.solve(session_id, conf.id, list(conf.required_factors))
                        if approve and conf.required_factors else []
                    )
                    await executor.resume(
                        session_id, conf.id, approve, factor=reply.get("factor"), proofs=card_proofs
                    )
            await asyncio.sleep(0.005)

    async def main() -> None:
        task = asyncio.create_task(
            executor.run(session_id, dag_obj, emit, confirm_timeout_ms=confirm_timeout_ms)
        )
        helper = asyncio.create_task(answerer())
        try:
            await asyncio.wait_for(task, timeout=20)
        finally:
            helper.cancel()

    asyncio.run(main())
    return events


def audit_payloads(conn: Any, event_type: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT payload_json FROM audit_log WHERE event_type = ? ORDER BY seq", (event_type,)
    ).fetchall()
    return [json.loads(row["payload_json"]) for row in rows]


def sub_statuses(conn: Any) -> dict[str, str]:
    rows = conn.execute("SELECT id, status FROM subs ORDER BY id").fetchall()
    return {row["id"]: row["status"] for row in rows}


def tool_runs(conn: Any, tool: str) -> list[dict[str, Any]]:
    return [item for item in audit_payloads(conn, "tool.executed") if item.get("tool") == tool]


# --------------------------------------------------------------- 1. 意图层分流


def test_only_a_cancel_request_keeps_the_subscription_name() -> None:
    """查订阅不能变成退订请求：模型顺手填了名字也不算数。"""
    cancel = parse(
        "帮我把爱奇艺会员的自动续费关掉",
        {"intent": "subscription", "sub_name": IQIYI, "missing": [], "confidence": 0.9},
    )
    assert cancel.slots["sub_name"] == IQIYI
    assert cancel.slots[CANCEL_SLOT] is True
    assert cancel.ok is True
    assert cancel.question is None

    query = parse(
        "看看我的订阅每月扣多少钱",
        {"intent": "subscription", "sub_name": IQIYI, "missing": [], "confidence": 0.9},
    )
    assert "sub_name" not in query.slots
    assert CANCEL_SLOT not in query.slots
    assert query.ok is True


def test_a_batch_cancel_asks_which_one_instead_of_guessing() -> None:
    """"把订阅都关了"没有点名到哪一条，只能问，不能替用户挑一条退掉。"""
    result = parse(
        "帮我把订阅都关了吧",
        {"intent": "subscription", "sub_name": "订阅", "missing": [], "confidence": 0.9},
    )
    assert result.ok is False
    assert result.question == "要取消哪一条订阅？请说订阅名称，比如「爱奇艺会员」。"
    assert "sub_name" not in result.slots
    assert result.slots[CANCEL_SLOT] is True


# --------------------------------------------------------------- 2. 规划层唯一匹配


def test_the_cancel_node_appears_only_on_the_cancel_route() -> None:
    query = sub_dag("看看我的订阅", {"intent": "subscription", "missing": [], "confidence": 0.9})
    assert [node.tool for node in query.nodes] == ["list_subs"]

    cancel = sub_dag(
        "帮我把爱奇艺会员的自动续费关掉",
        {"intent": "subscription", "sub_name": IQIYI, "missing": [], "confidence": 0.9},
    )
    assert [node.tool for node in cancel.nodes] == ["list_subs", "cancel_subscription"]
    assert cancel.nodes[1].depends_on == ["n1"]
    assert cancel.nodes[1].params["sub_id"].startswith("@n1.subs[merchant=")


def test_the_sub_id_comes_from_the_only_matching_subscription() -> None:
    cancel = sub_dag(
        "帮我把爱奇艺会员的自动续费关掉",
        {"intent": "subscription", "sub_name": IQIYI, "missing": [], "confidence": 0.9},
    )
    resolved = planner_dag.resolve_refs(cancel.nodes[1].params, {"n1": {"subs": SUBS}})
    assert resolved == {"sub_id": IQIYI_SUB_ID}


def test_an_unmatched_name_raises_a_question_instead_of_a_pick() -> None:
    """名单里没有的名字：抛出来的是一句反问，不是随便挑一条。"""
    cancel = sub_dag(
        "帮我把视频会员的自动续费关掉",
        {"intent": "subscription", "sub_name": "视频会员", "missing": [], "confidence": 0.9},
    )
    with pytest.raises(ValidationError) as excinfo:
        planner_dag.resolve_refs(cancel.nodes[1].params, {"n1": {"subs": SUBS}})
    assert "我没找到叫「视频会员」的订阅" in excinfo.value.user_text
    assert "要取消哪一条" in excinfo.value.user_text


def test_two_matching_subscriptions_ask_instead_of_guessing() -> None:
    """两条都叫「会员」：必须让用户点名，绝不替用户挑一条退掉。"""
    cancel = sub_dag(
        "帮我把视频会员关掉",
        {"intent": "subscription", "sub_name": "视频会员", "missing": [], "confidence": 0.9},
    )
    rows = [
        {"sub_id": "sb_00000001", "merchant": "爱奇艺视频会员"},
        {"sub_id": "sb_00000009", "merchant": "腾讯视频会员"},
    ]
    with pytest.raises(ValidationError) as excinfo:
        planner_dag.resolve_refs(cancel.nodes[1].params, {"n1": {"subs": rows}})
    assert "有 2 条订阅跟「视频会员」对得上" in excinfo.value.user_text
    assert "要取消哪一条" in excinfo.value.user_text


# --------------------------------------------------------------- 3+4. 确认与执行


def test_cancelling_goes_through_a_yellow_confirm_and_audits_it(bank: Path) -> None:
    dag_obj = sub_dag(
        "帮我把爱奇艺会员的自动续费关掉",
        {"intent": "subscription", "sub_name": IQIYI, "missing": [], "confidence": 0.9},
    )
    events = drive("s-sub-cancel", dag_obj, answers=[{"approve": True}])
    names = [name for name, _ in events]
    assert "confirm" in names, "退订是写操作，必须先弹黄灯确认"

    conn = db.connect()
    try:
        decisions = [
            item for item in audit_payloads(conn, "policy.decided")
            if item.get("tool") == "cancel_subscription"
        ]
        assert decisions, "退订这一步必须落 policy.decided 审计"
        assert decisions[-1]["level"] == "yellow"
        assert decisions[-1]["rule_id"] == "R05_confirm_only"
        assert audit_payloads(conn, "confirm.requested")
        # tool.executed 有两条：注册表记一条（tool/ok/writes），工具自己记一条状态变更
        executed = [item for item in tool_runs(conn, "cancel_subscription") if "sub_id" in item]
        assert executed and executed[-1]["sub_id"] == IQIYI_SUB_ID
        assert executed[-1]["state_change"] == "active->cancelled"
        statuses = sub_statuses(conn)
        assert statuses[IQIYI_SUB_ID] == "cancelled"
        # 反证：别的订阅一条都没动
        assert all(status == "active" for sid, status in statuses.items() if sid != IQIYI_SUB_ID)
        assert audit.verify_chain(conn) is True
    finally:
        conn.close()


def test_an_unmatched_name_never_reaches_the_write_tool(bank: Path) -> None:
    """名字对不上：写工具一次都不能被调用，订阅状态一条都不能变。"""
    dag_obj = sub_dag(
        "帮我把视频会员的自动续费关掉",
        {"intent": "subscription", "sub_name": "视频会员", "missing": [], "confidence": 0.9},
    )
    events = drive("s-sub-nomatch", dag_obj)
    conn = db.connect()
    try:
        # 反证：上游的列订阅确实跑过了——不是用例没走到生产路径
        assert tool_runs(conn, "list_subs"), "用例必须真的跑过生产链路"
        assert tool_runs(conn, "cancel_subscription") == []
        assert all(status == "active" for status in sub_statuses(conn).values())
        assert audit.verify_chain(conn) is True
    finally:
        conn.close()
    errors = [data for name, data in events if name == "error"]
    assert errors and "要取消哪一条" in errors[-1]["user_text"]


def test_a_cancel_without_a_name_never_reaches_the_write_tool(bank: Path) -> None:
    """就算请求绕过了意图层直接建图，名字为空也只能问，写工具不会被调用。"""
    dag_obj = planner_dag.build(
        {"intent": "subscription", "slots": {CANCEL_SLOT: True}}
    )
    events = drive("s-sub-noname", dag_obj)
    conn = db.connect()
    try:
        assert tool_runs(conn, "list_subs")
        assert tool_runs(conn, "cancel_subscription") == []
        assert all(status == "active" for status in sub_statuses(conn).values())
    finally:
        conn.close()
    errors = [data for name, data in events if name == "error"]
    assert errors and "要取消哪一条订阅" in errors[-1]["user_text"]
