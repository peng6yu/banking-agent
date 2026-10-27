"""疑似订阅定性：点「确实是我订的」之后，这条待办必须真的消失。

用户投诉（2026-10-25）：「点击是我定的这个待办不会消失啊」——原来的过滤只看
`not_subscription`，`confirmed` 仍然挂在「疑似订阅（从流水推断）」里，等于点
完按钮什么也没发生。这一组钉住四件事：

1. 两种定性都让这条从待办里消失（误报是处理完，确认也是处理完）；
2. 消失不是删除：定性留在 `subscription_verdicts` 里，管理页用 `settled` 列出来；
3. 定性可以撤回：撤掉之后重新按流水推断，该回来就回来；
4. 每一步都落审计，且不动钱、不建计划。

种子数据本身已经能推断出 3 条周期性扣款，所以这里的断言一律相对基线，不写死总数。
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest

from app import audit, subscriptions
from app.clock import ValidationError
from tests.tool_fixtures import DEBIT_ID, USER_ID, bank, ctx

__all__ = ["bank", "ctx"]

DAY_MS = 86_400_000
NOW = int(time.time() * 1000)
USER = USER_ID


def _uid(conn) -> str:
    from app.tools.base import primary_account

    return str(primary_account(conn)["user_id"])
MERCHANTS = ("测试商户甲", "测试商户乙")


def _monthly(conn, merchant: str, amounts: list[int], *, start_days_ago: int = 100) -> None:
    for index, amount in enumerate(amounts):
        conn.execute(
            "INSERT INTO transactions (id, account_id, direction, amount_fen, counterparty,"
            " category, note, ts_ms, balance_after_fen, channel, reversal_of)"
            " VALUES (?, ?, 'out', ?, ?, '订阅', '', ?, ?, 'app', NULL)",
            (
                f"t_{merchant}_{index}",
                DEBIT_ID,
                amount,
                merchant,
                NOW - (start_days_ago - index * 30) * DAY_MS,
                1_000_000,
            ),
        )


def _inferred(conn, user_id: str = USER) -> dict[str, Any]:
    return subscriptions.detect_recurring(conn, DEBIT_ID, as_of=NOW, user_id=user_id)


def _merchants(conn, user_id: str = USER) -> set[str]:
    return {str(item["merchant"]) for item in _inferred(conn, user_id)["items"]}


@pytest.fixture()
def subs(bank):
    _monthly(bank, MERCHANTS[0], [1200, 1200, 1200])
    _monthly(bank, MERCHANTS[1], [1500, 1500, 1500])
    assert set(MERCHANTS) <= _merchants(bank)
    return bank


def test_confirmed_verdict_removes_it_from_the_backlog(subs) -> None:
    before = _merchants(subs)
    subscriptions.record_verdict(
        subs, user_id=USER, merchant=MERCHANTS[0], verdict=subscriptions.VERDICT_CONFIRMED
    )
    after = _merchants(subs)
    # 用户的投诉点：点了「确实是我订的」之后，这条不能再挂在待办里
    assert MERCHANTS[0] not in after
    assert after == before - {MERCHANTS[0]}


def test_not_subscription_verdict_removes_it_from_the_backlog(subs) -> None:
    before = _merchants(subs)
    subscriptions.record_verdict(
        subs, user_id=USER, merchant=MERCHANTS[1], verdict=subscriptions.VERDICT_NOT_SUBSCRIPTION
    )
    after = _merchants(subs)
    assert MERCHANTS[1] not in after
    assert after == before - {MERCHANTS[1]}


def test_settling_everything_leaves_an_empty_backlog_but_keeps_the_verdicts(subs) -> None:
    for merchant in list(_merchants(subs)):
        subscriptions.record_verdict(
            subs, user_id=USER, merchant=merchant, verdict=subscriptions.VERDICT_CONFIRMED
        )
    assert _merchants(subs) == set()
    assert set(subscriptions.verdicts(subs, USER)) >= set(MERCHANTS)


def test_verdict_is_kept_and_can_be_cleared_to_come_back(subs) -> None:
    subscriptions.record_verdict(
        subs, user_id=USER, merchant=MERCHANTS[0], verdict=subscriptions.VERDICT_CONFIRMED
    )
    kept = subscriptions.verdicts(subs, USER)
    assert kept[MERCHANTS[0]]["verdict"] == "confirmed"
    assert kept[MERCHANTS[0]]["verdict_label"] == "确实是我订的"

    result = subscriptions.clear_verdict(subs, user_id=USER, merchant=MERCHANTS[0])
    assert result == {"merchant": MERCHANTS[0], "cleared": True}
    assert MERCHANTS[0] in _merchants(subs)

    cleared_again = subscriptions.clear_verdict(subs, user_id=USER, merchant=MERCHANTS[0])
    assert cleared_again["cleared"] is False


def test_verdict_moves_from_pending_to_settled_in_management_view(subs) -> None:
    from app.api import business

    uid = _uid(subs)
    before = business.management_view(subs)["subscriptions"]["inferred"]
    assert before["settled_count"] == 0
    assert before["dismissed_count"] == 0
    target = MERCHANTS[0]
    pending = {str(item["merchant"]): item for item in before["items"]}
    assert pending[target]["actions"][0]["label"] == "不是我订的"
    assert pending[target]["actions"][1]["label"] == "确实是我订的"
    assert pending[target]["actions"][2]["kind"] == "subscription_check"
    assert pending[target]["actions"][2]["label"] == "逐笔核对这几笔"
    # 这句话会被直接填进输入框：必须指名哪一家、几笔、多少钱，不能只说「这几笔」。
    prompt = pending[target]["actions"][2]["prompt"]
    assert target in prompt and str(pending[target]["occurrences"]) in prompt
    assert str(pending[target]["last_amount_yuan"]) in prompt

    subscriptions.record_verdict(
        subs, user_id=uid, merchant=target, verdict=subscriptions.VERDICT_CONFIRMED
    )
    after = business.management_view(subs)["subscriptions"]["inferred"]
    assert after["count"] == before["count"] - 1
    assert after["settled_count"] == 1
    assert after["confirmed_count"] == 1
    assert after["dismissed_count"] == 0
    assert target not in {str(item["merchant"]) for item in after["items"]}
    settled = {str(row["merchant"]): row for row in after["settled"]}[target]
    assert settled["verdict_label"] == "确实是我订的"
    assert settled["actions"] == [
        {
            "kind": "subscription_verdict_clear",
            "merchant": target,
            "label": "重新当成疑似",
            "style": "ghost",
        }
    ]


def test_bad_input_still_rejected(subs) -> None:
    with pytest.raises(ValidationError):
        subscriptions.record_verdict(subs, user_id=USER, merchant=MERCHANTS[0], verdict="不是这个")
    with pytest.raises(ValidationError):
        subscriptions.record_verdict(subs, user_id=USER, merchant="  ", verdict="confirmed")
    with pytest.raises(ValidationError):
        subscriptions.clear_verdict(subs, user_id=USER, merchant="")


def test_every_verdict_step_is_audited(subs) -> None:
    subscriptions.record_verdict(
        subs, user_id=USER, merchant=MERCHANTS[0], verdict=subscriptions.VERDICT_CONFIRMED
    )
    subscriptions.clear_verdict(subs, user_id=USER, merchant=MERCHANTS[0])
    rows = subs.execute(
        "SELECT event_type FROM audit_log"
        " WHERE event_type IN ('subscription.verdict', 'subscription.verdict_cleared')"
        " ORDER BY seq"
    ).fetchall()
    assert [str(row["event_type"]) for row in rows] == [
        "subscription.verdict",
        "subscription.verdict_cleared",
    ]
    audit.verify_chain(subs)


def _http(method: str, url: str, body: Any = None):
    import httpx

    from app.main import app

    async def go():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.request(method, url, json=body)

    return asyncio.run(go())


def test_api_verdict_then_clear_round_trip(subs, monkeypatch) -> None:
    # 接口自己开连接，必须把默认库指到这份临时账本上，否则读到的是仓库里的 data/bank.db
    from app import db as db_mod

    path = subs.execute("PRAGMA database_list").fetchone()[2]
    monkeypatch.setattr(db_mod, "DEFAULT_DB_PATH", __import__("pathlib").Path(path))
    inferred = _http("GET", "/api/business/management").json()["subscriptions"]["inferred"]
    baseline = inferred["count"]
    merchant = MERCHANTS[0]

    posted = _http("POST", "/api/subscriptions/verdict", {"merchant": merchant, "verdict": "confirmed"})
    assert posted.status_code == 200, posted.text
    assert posted.json()["verdict"]["verdict_label"] == "确实是我订的"

    after = _http("GET", "/api/business/management").json()["subscriptions"]["inferred"]
    assert after["count"] == baseline - 1
    assert [row["merchant"] for row in after["settled"]] == [merchant]

    cleared = _http("POST", "/api/subscriptions/verdict/clear", {"merchant": merchant})
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["cleared"] is True
    back = _http("GET", "/api/business/management").json()["subscriptions"]["inferred"]
    assert back["count"] == baseline
    assert back["settled"] == []