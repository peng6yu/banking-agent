"""长期记忆：用户说过一次的事，下次对话不用再说一遍。

用户的要求（2026-10-25）：「不要每次对话都要建立一个新的会话，希望他像 hermes
一样越用越智能」。会话记忆解决「这一轮接得上」，这一组钉住的是「跨会话还记得」：

1. 写入/覆盖/遗忘的语义（同一个 key 只有一行，覆盖不是追加）；
2. 长度与 kind 的边界（脏数据当场拒，不静默截断）；
3. 提示词块带中文标签和安全口径（记忆只是默认值，动钱仍看本次原话）；
4. 重启（新连接读同一个库）之后记忆还在；
5. HTTP 三步往返、审计链可验证；
6. 抽取层 best-effort：模型报错、输入太短都不许抛异常打断聊天。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

import app.memory as memory
import app.memory_extract as memory_extract
from app import audit, clock, db
from app.api import routes
from app.clock import ValidationError
from app.llm import LLMError, LLMResponse, ToolCall
from app.main import app
from scripts.seed import seed

BASE_URL = "http://testserver"


async def _request(method: str, url: str, body: dict | None = None) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url=BASE_URL) as http:
        return await http.request(method, url, json=body)


def call(method: str, url: str, body: dict | None = None) -> httpx.Response:
    return asyncio.run(_request(method, url, body))


@pytest.fixture()
def bank(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """临时库 + 种子数据：走 HTTP 的用例要有 users 行，default_user_id 才取得到人。"""
    monkeypatch.setattr(db, "DEFAULT_DB_PATH", tmp_path / "bank.db")
    conn = db.connect()
    db.init_db(conn)
    seed(conn)
    conn.close()
    routes.set_adapter(None)
    yield tmp_path / "bank.db"
    routes.set_adapter(None)


def test_remember_and_all_facts_read_back(conn) -> None:
    """写完能读回来，字段齐（中文标签与时间文本给前端直接用）。"""
    fact = memory.remember(
        conn, user_id="u_mem", key="常去商户", value="乐刻健身", kind="fact", source="user"
    )
    assert fact["key"] == "常去商户"
    assert fact["kind"] == "fact"
    assert fact["kind_label"] == "事实"
    assert fact["source"] == "user"
    assert fact["updated_ms"] > 0

    facts = memory.all_facts(conn, user_id="u_mem")
    assert len(facts) == 1
    assert facts[0]["value"] == "乐刻健身"
    assert facts[0]["kind_label"] == "事实"
    # 时间文本是「YYYY-MM-DD HH:MM」，和账单/提醒一个口径
    assert facts[0]["updated_text"][4] == "-" and len(facts[0]["updated_text"]) == 16


def test_same_key_overwrites_instead_of_duplicating(conn) -> None:
    """同一件事说两遍只更新，不新增第二行——记忆要的是「现在的样子」，不是流水。"""
    memory.remember(conn, user_id="u_mem", key="称呼", value="老王")
    memory.remember(conn, user_id="u_mem", key="称呼", value="王哥", kind="preference")
    rows = conn.execute(
        "SELECT COUNT(*) AS n FROM agent_memory WHERE user_id = ?", ("u_mem",)
    ).fetchone()
    assert rows["n"] == 1
    facts = memory.all_facts(conn, user_id="u_mem")
    assert facts[0]["value"] == "王哥"
    assert facts[0]["kind"] == "preference"


def test_kind_must_be_one_of_three(conn) -> None:
    """kind 是闭集，写错立刻拒（不要等到库里 CHECK 报错，错误还得是中文）。"""
    with pytest.raises(ValidationError):
        memory.remember(conn, user_id="u_mem", key="天气", value="今天晴", kind="weather")
    with pytest.raises(ValidationError):
        memory.remember(conn, user_id="u_mem", key="", value="空键")
    with pytest.raises(ValidationError):
        memory.remember(conn, user_id="u_mem", key="空值", value="   ")


def test_key_and_value_limits(conn) -> None:
    """超长直接报错，不静默截断：静默截断会悄悄改变用户的意思。"""
    with pytest.raises(ValidationError):
        memory.remember(conn, user_id="u_mem", key="k" * (memory.KEY_MAX + 1), value="值")
    with pytest.raises(ValidationError):
        memory.remember(conn, user_id="u_mem", key="标签", value="v" * (memory.VALUE_MAX + 1))
    # 边界值本身是合法的
    ok = memory.remember(conn, user_id="u_mem", key="k" * memory.KEY_MAX, value="值")
    assert len(ok["key"]) == memory.KEY_MAX


def test_all_facts_sorted_newest_first(conn) -> None:
    """最近更新的在前；同刻按 key 排，顺序稳定（前端列表不会自己跳来跳去）。"""
    try:
        clock.freeze(1_700_000_000_000)
        memory.remember(conn, user_id="u_mem", key="甲", value="第一条")
        clock.freeze(1_700_000_100_000)
        memory.remember(conn, user_id="u_mem", key="丙", value="第三条")
        clock.freeze(1_700_000_200_000)
        memory.remember(conn, user_id="u_mem", key="乙", value="第二条")
    finally:
        clock.unfreeze()
    facts = memory.all_facts(conn, user_id="u_mem")
    assert [f["key"] for f in facts] == ["乙", "丙", "甲"]


def test_prompt_block_carries_labels_and_safety_line(conn) -> None:
    """提示词块：每条带中文标签，抬头带「可作默认值、金额仍以原话为准」的安全口径。"""
    memory.remember(conn, user_id="u_mem", key="记账习惯", value="每天记一笔", kind="preference")
    memory.remember(conn, user_id="u_mem", key="常去商户", value="乐刻健身")
    block = memory.prompt_block(conn, user_id="u_mem")
    assert memory.PROMPT_HEAD in block
    assert "作默认值" in block and "以本次原话为准" in block
    assert "- [偏好] 记账习惯：每天记一笔" in block
    assert "- [事实] 常去商户：乐刻健身" in block


def test_prompt_block_is_empty_without_memory(conn) -> None:
    """没有记忆就返回空串：调用方据此决定加不加这一段，别塞一个空标题进提示词。"""
    assert memory.prompt_block(conn, user_id="u_mem") == ""


def test_forget_is_idempotent(conn) -> None:
    """忘掉一条；本来就没有也算成功，只是 removed=False。"""
    memory.remember(conn, user_id="u_mem", key="口味", value="不吃辣")
    first = memory.forget(conn, user_id="u_mem", key="口味")
    second = memory.forget(conn, user_id="u_mem", key="口味")
    assert first == {"key": "口味", "removed": True}
    assert second == {"key": "口味", "removed": False}
    assert memory.all_facts(conn, user_id="u_mem") == []


def test_memory_survives_reconnect(tmp_path: Path) -> None:
    """「重启」= 关掉再开一个新连接读同一个库：记忆必须还在（这就是长期的意思）。"""
    path = tmp_path / "mem.db"
    conn = db.connect(path)
    db.init_db(conn)
    memory.remember(conn, user_id="u_mem", key="居住城市", value="杭州")
    conn.close()

    conn2 = db.connect(path)
    db.init_db(conn2)
    try:
        facts = memory.all_facts(conn2, user_id="u_mem")
        assert [f["value"] for f in facts] == ["杭州"]
    finally:
        conn2.close()


def test_http_roundtrip_remember_list_forget(bank: Path) -> None:
    """GET/POST/DELETE /api/memory 三步往返，用户能看、能补、能删。"""
    from urllib.parse import quote

    created = call("POST", "/api/memory", {"key": "记账习惯", "value": "每天记一笔", "kind": "preference"})
    assert created.status_code == 200, created.text
    body = created.json()
    assert body["ok"] is True
    assert body["fact"]["kind_label"] == "偏好"

    listed = call("GET", "/api/memory")
    assert listed.status_code == 200
    data = listed.json()
    assert data["count"] >= 1
    assert any(f["key"] == "记账习惯" for f in data["facts"])
    assert "记账习惯：每天记一笔" in data["prompt_block"]

    removed = call("DELETE", "/api/memory/" + quote("记账习惯"))
    assert removed.status_code == 200
    assert removed.json() == {"ok": True, "key": "记账习惯", "removed": True}

    again = call("GET", "/api/memory").json()
    assert all(f["key"] != "记账习惯" for f in again["facts"])


def test_writes_and_forgets_are_audited_and_chain_verifies(conn) -> None:
    """写和忘都落审计；审计链仍然自洽；审计里不存记忆全文（只存 40 字预览）。"""
    long_value = "这是一段很长的记忆内容" * 8
    memory.remember(conn, user_id="u_mem", key="长记忆", value=long_value)
    memory.forget(conn, user_id="u_mem", key="长记忆")

    rows = conn.execute(
        "SELECT event_type, payload_json FROM audit_log WHERE event_type LIKE 'memory.%' ORDER BY seq"
    ).fetchall()
    assert [row["event_type"] for row in rows] == ["memory.written", "memory.forgotten"]
    written = json.loads(rows[0]["payload_json"])
    assert written["user_id"] == "u_mem" and written["key"] == "长记忆"
    assert long_value not in rows[0]["payload_json"]
    assert audit.verify_chain(conn) is True


class _FixedAdapter:
    """假适配器：按预设返回一次响应，用来验抽取层的解析与失败降级。"""

    def __init__(self, response: Any = None, error: BaseException | None = None) -> None:
        self._response = response
        self._error = error

    def chat(self, messages: Any, tools: Any = None, **kwargs: Any) -> Any:
        if self._error is not None:
            raise self._error
        return self._response


def _tool_response(items: list[dict[str, Any]]) -> LLMResponse:
    return LLMResponse(
        tool_calls=[ToolCall(id="c1", name=memory_extract.TOOL_NAME, arguments=json.dumps({"items": items}))]
    )


def test_extract_writes_facts_but_never_raises(conn) -> None:
    """抽取层 best-effort：正常抽出事实；超长条目跳过；模型报错/输入太短都只返回 []。"""
    good = _FixedAdapter(
        _tool_response(
            [
                {"key": "常去商户", "value": "乐刻健身", "kind": "fact"},
                {"key": "超长", "value": "v" * (memory.VALUE_MAX + 5), "kind": "fact"},
            ]
        )
    )
    written = memory_extract.run(
        conn,
        user_id="u_mem",
        user_text="我每周三都去乐刻健身，帮我看看这个月的账单",
        assistant_text="好的，这个月一共 39 笔。",
        adapter=good,
    )
    assert [fact["key"] for fact in written] == ["常去商户"]
    assert [f["key"] for f in memory.all_facts(conn, user_id="u_mem")] == ["常去商户"]

    # 模型报错：吞掉，返回空列表，不打断本轮对话
    broken = _FixedAdapter(error=LLMError("模型挂了"))
    assert memory_extract.run(
        conn, user_id="u_mem", user_text="我以后想每月存两千块钱", assistant_text="好的", adapter=broken
    ) == []

    # 用户话太短：本地就跳过，连模型都不调
    assert memory_extract.run(
        conn, user_id="u_mem", user_text="嗯嗯", assistant_text="好的", adapter=broken
    ) == []

    # adapter=None 且构建适配器失败：同样只返回 []
    def _boom() -> Any:
        raise LLMError("构建失败")

    import app.memory_extract as extract_module

    original = extract_module.build_adapter
    extract_module.build_adapter = _boom
    try:
        assert memory_extract.run(
            conn, user_id="u_mem", user_text="我住在杭州，叫我老王", assistant_text="好的"
        ) == []
    finally:
        extract_module.build_adapter = original


def test_extract_merges_near_duplicate_values(conn) -> None:
    """同一件事被模型拆成两条近义说法时只留一条（用户不该自己去收拾重复记忆）。"""
    adapter = _FixedAdapter(
        _tool_response(
            [
                {"key": "记账习惯", "value": "记账喜欢按周看", "kind": "preference"},
                {"key": "记账周期偏好", "value": "记账喜欢按周查看", "kind": "preference"},
            ]
        )
    )
    written = memory_extract.run(
        conn,
        user_id="u_mem",
        user_text="我习惯用招商银行的卡，记账喜欢按周看",
        assistant_text="好的，记下了。",
        adapter=adapter,
    )
    facts = memory.all_facts(conn, user_id="u_mem")
    assert len(facts) == 1
    # 沿用先写的那条的键，内容是更完整的说法
    assert facts[0]["key"] == "记账习惯"
    assert facts[0]["value"] == "记账喜欢按周查看"
    assert [fact["key"] for fact in written] == ["记账习惯", "记账习惯"]

