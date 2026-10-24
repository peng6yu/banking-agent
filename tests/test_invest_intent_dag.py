"""理财意图到 DAG 再到策略红灯的贯通测试（docs/07 §7.2 / §7.3）。

守住四条线：
1. 理财按 action 分三路：买=buy_fund、赎回=redeem_fund、查=list_products；
2. 槽位名 product_code 与工具参数同名，零映射；
3. 申购/赎回是写操作，必须走 R10 红灯 + 人脸与动态口令双因子；
4. 认不出 action 或缺槽位时退到只读的产品清单，绝不带着半截参数动钱。
"""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

import pytest

from tests import mfa
from app import audit, db, suitability
from app.llm import LLMResponse, ToolCall
from app.models import Action
from app.planner import dag as planner_dag
from app.planner import executor, intent
from app.policy.engine import decide
from app.policy.rules import KNOWN_TOOLS, PENDING_TOOLS, PolicyContext
import app.policy.rules as rules_mod
from app.tools import registry
from scripts.seed import seed

SECRET = "a" * 64
MIXED_CODE = "F00003"   # 均衡配置混合A：R3，起购 100 元
BOND_CODE = "F00002"    # 稳盈债券A：R2，起购 100 元
BUY_AMOUNT_FEN = 100_000
REDEEM_AMOUNT_FEN = 20_000
SEED_BALANCE_FEN = 1_286_050
SEED_SHARES_MILLI = 405_910   # F00003 的存量份额


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
            tool_calls=[
                ToolCall(id="c1", name="emit_intent", arguments=json.dumps(item, ensure_ascii=False))
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
    # 这些用例测的是"因子/令牌是否真的挡住了申购"，不是适当性。
    # 给测试用户一份 C5 画像，让适当性闸门放行，闸门本身在 tests/test_suitability.py 里测。
    suitability.save(conn, "u_9f2c41ab", {
        "q_horizon": "gt_3y", "q_experience": "equity", "q_loss": "buy_more",
        "q_goal": "aggressive", "q_share": "lt_10",
    })
    conn.close()
    executor.RUNS.clear()
    return db.DEFAULT_DB_PATH


def parse(text: str, adapter: Any) -> intent.IntentResult:
    return asyncio.run(intent.parse(text, [], adapter=adapter))


def drive(session_id: str, dag_obj: Any, *, answers: list[dict[str, Any]] | None = None,
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


def names(events: list[tuple[str, dict[str, Any]]]) -> list[str]:
    return [name for name, _ in events]


def payload(events: list[tuple[str, dict[str, Any]]], name: str) -> dict[str, Any]:
    return next(data for event, data in events if event == name)


def balance(conn: Any) -> int:
    return int(
        conn.execute("SELECT balance_fen FROM accounts WHERE type='debit' LIMIT 1").fetchone()[0]
    )


def count(conn: Any, table: str) -> int:
    return int(conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()[0])


def shares(conn: Any, code: str) -> int:
    row = conn.execute(
        "SELECT shares_milli FROM fund_holdings WHERE product_code = ?", (code,)
    ).fetchone()
    return int(row["shares_milli"]) if row is not None else 0


# ---------------------------------------------------------------- 7.2 意图与槽位


def test_intent_schema_exposes_invest_and_its_three_actions() -> None:
    """意图枚举里有 invest，动作枚举里有买/赎回/查三档。"""
    props = intent.emit_intent_schema()["parameters"]["properties"]
    assert "invest" in props["intent"]["enum"]
    assert props["action"]["enum"] == ["buy", "redeem", "query"]


def test_buy_request_from_the_model_becomes_a_buy_fund_node() -> None:
    """「帮我买 1000 块均衡配置混合A」→ invest/buy → buy_fund 节点。"""
    result = parse(
        "帮我买 1000 块均衡配置混合A",
        Scripted([{
            "intent": "invest",
            "action": "buy",
            "product_code": MIXED_CODE,
            "amount_fen": BUY_AMOUNT_FEN,
            "missing": [],
            "confidence": 0.9,
        }]),
    )
    assert result.intent == "invest"
    assert result.slots["action"] == "buy"
    assert result.slots["product_code"] == MIXED_CODE
    assert result.slots["amount_fen"] == BUY_AMOUNT_FEN
    assert result.question is None
    built = planner_dag.build({"intent": result.intent, "slots": result.slots})
    assert [node.tool for node in built.nodes] == ["buy_fund"]
    assert built.nodes[0].params == {"product_code": MIXED_CODE, "amount_fen": BUY_AMOUNT_FEN}


def test_redeem_request_becomes_a_redeem_fund_node() -> None:
    """「赎回我的基金」→ invest/redeem → redeem_fund 节点。"""
    result = parse(
        "赎回我的基金",
        Scripted([{
            "intent": "invest",
            "action": "redeem",
            "product_code": BOND_CODE,
            "amount_fen": REDEEM_AMOUNT_FEN,
            "missing": [],
            "confidence": 0.9,
        }]),
    )
    assert result.intent == "invest"
    assert result.slots["action"] == "redeem"
    built = planner_dag.build({"intent": result.intent, "slots": result.slots})
    assert [node.tool for node in built.nodes] == ["redeem_fund"]
    assert built.nodes[0].params["product_code"] == BOND_CODE


def test_product_list_question_becomes_a_readonly_list_products_node() -> None:
    """「我有哪些基金产品」→ invest/query → 只读的 list_products 节点。"""
    result = parse(
        "我有哪些基金产品",
        Scripted([{"intent": "invest", "action": "query", "missing": [], "confidence": 0.9}]),
    )
    assert result.intent == "invest"
    assert result.slots["action"] == "query"
    built = planner_dag.build({"intent": result.intent, "slots": result.slots})
    assert [node.tool for node in built.nodes] == ["list_products"]
    assert built.nodes[0].params == {}
    assert "list_products" in registry.READONLY_TOOL_NAMES


# ---------------------------------------------------------------- 策略层登记


def test_placeholder_tools_are_all_implemented_now() -> None:
    """所有占位工具都已实现，PENDING_TOOLS 清空。

    这条从「至少留两个占位当 R11 活证据」改过来，是因为四件占位（销户、改卡密码、
    虚拟卡申请、调限额）真的都做完了。R11 的可达性不依赖 PENDING_TOOLS，
    而依赖「模型编出一个没登记的工具名」，见 test_r11_default_deny_is_the_backstop。
    """
    assert PENDING_TOOLS == frozenset()
    for name in ("buy_fund", "redeem_fund", "close_card", "change_card_pin"):
        assert name in KNOWN_TOOLS, name
        assert name in registry.TOOL_NAMES, name


def test_default_deny_still_has_a_reachable_path(monkeypatch) -> None:
    """R11 不是死代码：登记了工具却忘了配规则时，它是最后一道红拒。

    不拿现成的工具名做证据，因为现有工具全都被前面某条规则接住了；
    直接构造「加了登记但漏配规则」这个真实失误场景。
    """
    monkeypatch.setattr(rules_mod, "KNOWN_TOOLS", KNOWN_TOOLS | {"future_tool"})
    decision = decide(Action(tool="future_tool", params={}, account_id="a_3d7e60c2"))
    assert decision.rule_id == "R11_default_deny"
    assert decision.level == "red"


# ---------------------------------------------------------------- 执行器贯通


def test_buy_fund_run_asks_for_face_and_otp_then_buys(bank: Path) -> None:
    """写操作必须红灯 + 双因子；因子齐了才真的申购，余额只经资金层扣。"""
    dag_obj = planner_dag.build({
        "intent": "invest",
        "slots": {"action": "buy", "product_code": MIXED_CODE, "amount_fen": BUY_AMOUNT_FEN},
    })
    events = drive("s_buy", dag_obj, answers=[{"approve": True, "factor": "face+otp"}])
    trace = payload(events, "trace")
    assert trace["level"] == "red"
    assert trace["rule_id"] == "R10_invest_red"
    factor = payload(events, "factor")
    assert sorted(factor["required"]) == ["face", "otp"]
    assert names(events)[-1] == "done"
    conn = db.connect()
    assert balance(conn) == SEED_BALANCE_FEN - BUY_AMOUNT_FEN
    assert shares(conn, MIXED_CODE) > SEED_SHARES_MILLI
    assert audit.verify_chain(conn) is True
    conn.close()


def test_buy_fund_without_the_factor_never_moves_money(bank: Path) -> None:
    """双因子没给齐就不许申购：超时作废，余额一分不动。"""
    dag_obj = planner_dag.build({
        "intent": "invest",
        "slots": {"action": "buy", "product_code": MIXED_CODE, "amount_fen": BUY_AMOUNT_FEN},
    })
    events = drive("s_buy_nofactor", dag_obj, answers=None, confirm_timeout_ms=1)
    assert "factor" in names(events)
    assert payload(events, "error")["code"] == "CONFIRM_TIMEOUT"
    conn = db.connect()
    assert balance(conn) == SEED_BALANCE_FEN
    assert shares(conn, MIXED_CODE) == SEED_SHARES_MILLI
    conn.close()


def test_product_list_run_is_green_and_moves_no_money(bank: Path) -> None:
    """查产品是只读绿灯：不弹确认，不动钱。"""
    dag_obj = planner_dag.build({"intent": "invest", "slots": {"action": "query"}})
    events = drive("s_query", dag_obj, answers=None)
    assert "factor" not in names(events)
    assert "confirm" not in names(events)
    assert payload(events, "trace")["level"] == "green"
    assert names(events)[-1] == "done"
    conn = db.connect()
    assert balance(conn) == SEED_BALANCE_FEN
    conn.close()


def test_missing_product_code_falls_back_to_the_readonly_product_list(bank: Path) -> None:
    """缺槽位不退成带着半截参数的申购：退到只读的产品清单，钱一分不动。"""
    fallback = planner_dag.build_or_single({
        "intent": "invest",
        "slots": {"action": "buy", "amount_fen": BUY_AMOUNT_FEN},
    })
    assert [node.tool for node in fallback.nodes] == ["list_products"]
    events = drive("s_missing", fallback, answers=None)
    assert names(events)[-1] == "done"
    conn = db.connect()
    assert balance(conn) == SEED_BALANCE_FEN
    assert shares(conn, MIXED_CODE) == SEED_SHARES_MILLI
    conn.close()


def test_an_unknown_action_goes_to_the_readonly_product_list() -> None:
    """认不出的 action 不许猜成申购：走只读那一路。"""
    fallback = planner_dag.build_or_single({
        "intent": "invest",
        "slots": {"action": "whatever", "product_code": MIXED_CODE, "amount_fen": BUY_AMOUNT_FEN},
    })
    assert [node.tool for node in fallback.nodes] == ["list_products"]

