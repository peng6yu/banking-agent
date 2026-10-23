"""端到端：一句自然语言 → 意图 → 固定 DAG → 黄灯确认 → 两阶段扣款 → 审计链。

全程不经过 HTTP，直接驱动 app.planner 的 run/resume，验证的是
「意图解析 + 规划执行 + 工具层 + 资金 hold/commit + 审计哈希链」这条主干。
"""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

import pytest

from app import audit, db
from app.llm import LLMResponse, ToolCall
from app.models import Action
from app.planner import dag as planner_dag
from app.planner import executor, intent, runner
from app.security import token as token_mod
from app.tools import registry
from app.tools.base import ToolContext
from scripts.seed import seed

SECRET = "a" * 64
SESSION = "s_e2e"
AMOUNT_FEN = 50_000
SEED_BALANCE_FEN = 1_286_050
TEXT = "帮我给李静转 500 元"


class Scripted:
    """只会 emit_intent 的假模型。"""

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


def transfer_call() -> dict[str, Any]:
    """模型侧该给出来的结构化意图。"""
    return {"intent": "transfer", "payee_name": "李静", "amount_fen": AMOUNT_FEN, "missing": [], "confidence": 0.96}


def speak(text: str, *, approve: bool = True) -> list[tuple[str, dict[str, Any]]]:
    """从一句用户输入跑到收尾，返回整轮事件。"""
    return speak_as(text, transfer_call(), approve=approve)


def speak_as(
    text: str, call: dict[str, Any], *, approve: bool = True
) -> list[tuple[str, dict[str, Any]]]:
    """同 speak，但让用例自己决定模型给出的结构化意图（换收款人、制造歧义）。"""
    async def main() -> list[tuple[str, dict[str, Any]]]:
        parsed = await intent.parse(text, [], adapter=Scripted([call]))
        assert parsed.question is None
        built = planner_dag.build_or_single({"intent": parsed.intent, "slots": parsed.slots})
        events: list[tuple[str, dict[str, Any]]] = []

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

        task = asyncio.create_task(executor.run(SESSION, built, emit, confirm_timeout_ms=5_000))
        helper = asyncio.create_task(answerer(task))
        try:
            await asyncio.wait_for(task, timeout=20)
        finally:
            helper.cancel()
        return events

    return asyncio.run(main())


def names(events: list[tuple[str, dict[str, Any]]]) -> list[str]:
    return [name for name, _ in events]


def payload(events: list[tuple[str, dict[str, Any]]], name: str) -> list[dict[str, Any]]:
    return [data for event, data in events if event == name]


def balance() -> int:
    with db.connect() as conn:
        row = conn.execute("SELECT balance_fen FROM accounts WHERE type = 'debit' LIMIT 1").fetchone()
        return int(row["balance_fen"])


def agent_rows() -> list[dict[str, Any]]:
    """智能体动过的流水（只有它会产生 hold_id 非空的行）。"""
    with db.connect() as conn:
        return [
            dict(row)
            for row in conn.execute("SELECT * FROM transactions WHERE hold_id IS NOT NULL ORDER BY ts_ms")
        ]


def audit_types() -> list[str]:
    with db.connect() as conn:
        return [row["event_type"] for row in conn.execute("SELECT event_type FROM audit_log ORDER BY seq")]


def hold_statuses() -> list[str]:
    with db.connect() as conn:
        return [row["status"] for row in conn.execute("SELECT status FROM holds ORDER BY created_ms")]


def debit_account() -> tuple[str, str]:
    """(账户 id, 收款人 id)：真实种子库里 id 是随机生成的，这里按名字取。"""
    with db.connect() as conn:
        account = conn.execute("SELECT id FROM accounts WHERE type = 'debit' LIMIT 1").fetchone()
        payee = conn.execute("SELECT id FROM payees WHERE name = '李静' LIMIT 1").fetchone()
    return str(account["id"]), str(payee["id"])


def test_intent_and_dag_follow_the_fixed_transfer_template(bank: Path) -> None:
    parsed = asyncio.run(intent.parse(TEXT, [], adapter=Scripted([transfer_call()])))
    assert parsed.intent == "transfer"
    assert parsed.slots["payee_name"] == "李静"
    assert parsed.slots["amount_fen"] == AMOUNT_FEN
    assert parsed.missing == []

    built = planner_dag.build_or_single({"intent": parsed.intent, "slots": parsed.slots})
    assert [(node.id, node.tool) for node in built.nodes] == [
        ("n1", "resolve_payee"),
        ("n2", "query_balance"),
        ("n3", "transfer"),
    ]
    assert built.nodes[-1].depends_on == ["n2"]
    assert built.nodes[-1].params["amount_fen"] == AMOUNT_FEN


def test_yellow_light_asks_before_any_money_moves(bank: Path) -> None:
    events = speak(TEXT)
    confirm = payload(events, "confirm")
    assert len(confirm) == 1
    assert "李静" in confirm[0]["action_text"]
    assert "500" in confirm[0]["action_text"]
    assert confirm[0]["expires_in_s"] > 0

    trace = {data["node_id"]: data for data in payload(events, "trace")}
    assert trace["n1"]["level"] == "green"
    assert trace["n3"]["level"] == "yellow"

    # 确认之前不许出现 n3 的工具结果：钱是先问后动。
    first_transfer = next(i for i, (event, data) in enumerate(events) if event == "tool" and data["node_id"] == "n3")
    first_confirm = next(i for i, (event, _) in enumerate(events) if event == "confirm")
    assert first_confirm < first_transfer


def test_confirmed_transfer_moves_money_and_extends_the_audit_chain(bank: Path) -> None:
    before = audit_types()
    events = speak(TEXT)

    assert names(events)[-1] == "done"
    assert payload(events, "done")[0]["balance_after_fen"] == SEED_BALANCE_FEN - AMOUNT_FEN
    assert balance() == SEED_BALANCE_FEN - AMOUNT_FEN

    rows = agent_rows()
    assert len(rows) == 1
    assert rows[0]["direction"] == "out"
    assert rows[0]["amount_fen"] == AMOUNT_FEN
    assert rows[0]["counterparty"] == "李静"

    added = audit_types()[len(before):]
    assert "confirm.approved" in added
    assert "transfer.initiated" in added
    assert "transfer.committed" in added
    # hold 已经是终态 committed：第二阶段是工具自己走完的，planner 没有再补一次。
    assert hold_statuses() == ["committed"]
    with db.connect() as conn:
        assert audit.verify_chain(conn) is True


def test_rejected_confirmation_moves_no_money(bank: Path) -> None:
    before = audit_types()
    events = speak(TEXT, approve=False)

    assert balance() == SEED_BALANCE_FEN
    assert agent_rows() == []
    transfer_tool = [data for data in payload(events, "tool") if data["node_id"] == "n3"]
    assert len(transfer_tool) == 1
    assert transfer_tool[0]["ok"] is False
    assert transfer_tool[0]["error_code"] == "USER_REJECTED"
    assert "confirm.rejected" in audit_types()[len(before):]
    with db.connect() as conn:
        assert audit.verify_chain(conn) is True


def test_transfer_tool_commits_inside_a_single_call(bank: Path) -> None:
    """直接调 transfer 工具：一次调用就 committed，planner 不再补第二次扣款。"""
    account_id, payee_id = debit_account()
    params = {"payee_id": payee_id, "amount_fen": AMOUNT_FEN, "note": "十月房租"}
    action = Action(tool="transfer", params=params, account_id=account_id, amount_fen=AMOUNT_FEN)
    token = token_mod.issue(action, secret=SECRET)
    with db.connect() as conn:
        ctx = ToolContext(conn=conn, session_id=SESSION, node_id="n3", account_id=account_id, secret=SECRET)
        result = registry.execute("transfer", params, token=token, ctx=ctx)
    assert result.ok is True
    assert result.data["status"] == "committed"
    assert result.data["balance_after_fen"] == SEED_BALANCE_FEN - AMOUNT_FEN
    assert hold_statuses() == ["committed"]
    assert balance() == SEED_BALANCE_FEN - AMOUNT_FEN
    assert len(agent_rows()) == 1


def test_planner_never_reaches_into_the_funds_module() -> None:
    """分层回归护栏：不可逆的 commit 只发生在工具层，planner 连 app.funds 都不 import。"""
    source = Path(runner.__file__).read_text(encoding="utf-8")
    assert "from app import funds" not in source
    assert "funds.commit" not in source


def test_events_never_leak_internals(bank: Path) -> None:
    events = speak(TEXT)
    blob = json.dumps(events, ensure_ascii=False)
    for needle in ("Traceback", "Exception", "sqlite3", ".py", "KeyError"):
        assert needle not in blob

def test_confirm_card_carries_what_a_normal_user_needs(bank: Path) -> None:
    """确认卡片的九个字段必须由后端下发，而不是前端拼猜测。

    普通用户要在这一屏决定「要不要给这个人这笔钱」，所以收款人、转出账户、
    手续费口径、权限等级、验证步骤缺一不可；没有可信数据时写明「不算」，不能编。
    """
    confirm = payload(speak(TEXT), "confirm")[0]

    source = confirm["source_account"]
    assert source["tail"] == "1234"
    assert source["balance_fen"] == SEED_BALANCE_FEN
    assert "不是扣款后" in source["note"]

    payee = confirm["payee"]
    assert payee["name"] == "李静"
    assert payee["bank"]
    assert payee["phone_masked"] is None or "****" in payee["phone_masked"]

    # 本演示环境没有资费表：宁可说不知道，也不能写 0 或猜一个数。
    assert confirm["fee"]["known"] is False
    assert confirm["fee"]["amount_fen"] is None
    assert "不计算手续费" in confirm["fee"]["text"]

    verification = confirm["verification"]
    assert verification["level"] == "yellow"
    assert verification["level_label"] == "中"
    assert isinstance(verification["steps"], list) and verification["steps"]
    for step in verification["steps"]:
        assert {"id", "label", "required"} <= set(step)

    assert confirm["tool_name"] and confirm["method"] and confirm["arrival"]
    # 立即执行也要有这个键，前端才能一律按「预约时间」渲染，而不是两种卡片。
    assert "scheduled_for" in confirm


def test_ambiguous_payee_returns_structured_candidates_and_picks_nothing(bank: Path) -> None:
    """两个「张三」时：给前端结构化候选，绝不替用户挑第一个。"""
    call = {
        "intent": "transfer",
        "payee_name": "张三",
        "amount_fen": AMOUNT_FEN,
        "missing": [],
        "confidence": 0.95,
    }
    events = speak_as("给张三转 500 元", call)

    done = payload(events, "done")[-1]
    clar = done["clarification"]
    assert clar["kind"] == "payee"
    assert clar["count"] == 2
    tails = [candidate["card_tail"] for candidate in clar["candidates"]]
    assert set(tails) == {"4417", "6620"}
    assert all(candidate["name"] == "张三" for candidate in clar["candidates"])
    assert all("phone_masked" in candidate for candidate in clar["candidates"])
    assert "4417" in clar["question"] and "6620" in clar["question"]
    assert clar["note"]

    # 一句「张三」不能变成任何一笔钱：n3 根本没跑，余额没动。
    assert not [data for data in payload(events, "tool") if data["node_id"] == "n3"]
    assert not payload(events, "confirm")
    assert balance() == SEED_BALANCE_FEN

