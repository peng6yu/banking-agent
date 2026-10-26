"""只读业务视图的接口测试：五个 GET + 鉴权 + 不许编数据的口径。

为什么单独一组：六个区域的界面（财务总览 / 账单分析 / 业务管理 / 计划与提醒 /
执行记录）全部读这五个只读接口，它们把「后端真实有什么」摊给前端。所以
这一组要钉住三件事：
1. 结构稳定（前端照着渲染，字段缺一个就是空白卡片）；
2. 口径诚实（异常只叫「发现异常」、适当性明确没做、订阅只说银行侧）；
3. 空列表不等于「暂不可用」——空状态由各区块自己渲染，不能被推成 unavailable。
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

import httpx
import pytest

from app import db
from app.main import app
from app.planner import executor
from scripts.seed import (
    CREDIT_ACCOUNT_ID,
    CREDIT_LIMIT_FEN,
    DEBIT_ACCOUNT_ID,
    seed,
)

BASE_URL = "http://testserver"
SEED_BALANCE_FEN = 1_286_050
OCT_OUT_FEN = 1_234_100
OCT_OUT_COUNT = 33


@pytest.fixture()
def bank(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """种一份真种子库，并把 DEFAULT_DB_PATH 指过去。"""

    monkeypatch.setattr(db, "DEFAULT_DB_PATH", tmp_path / "bank.db")
    conn = db.connect()
    db.init_db(conn)
    seed(conn)
    conn.close()
    executor.RUNS.clear()
    yield tmp_path / "bank.db"
    executor.RUNS.clear()


def get(url: str) -> httpx.Response:
    """同步发 GET；ASGITransport 不占端口。"""

    async def run() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url=BASE_URL) as http:
            return await http.get(url)

    return asyncio.run(run())


# ---------------------------------------------------------------- 财务总览


def test_overview_balance_and_month_totals_come_from_the_database(bank: Path) -> None:
    res = get("/api/business/overview")
    assert res.status_code == 200
    data = res.json()

    account = data["account"]
    assert account["account_id"] == DEBIT_ACCOUNT_ID
    assert account["balance_fen"] == SEED_BALANCE_FEN
    assert account["balance_yuan"] == "12,860.50"
    # 信用账户的可用额度是账单口径，接口不许瞎推算
    credit = [a for a in data["accounts"] if a["account_id"] == CREDIT_ACCOUNT_ID][0]
    assert credit["credit_limit_fen"] == CREDIT_LIMIT_FEN
    assert credit["available_fen"] is None
    assert "账单" in credit["available_note"]

    month = data["this_month"]
    # 种子数据按「当前时刻」往前铺，所以月度合计会随运行日期变化；
    # 这里直接拿接口给的窗口回库里对账，而不是写死某一天的快照。
    window = month["window"]
    conn = db.connect()
    try:
        row = conn.execute(
            "SELECT COALESCE(SUM(amount_fen), 0) AS total, COUNT(*) AS n FROM transactions"
            " WHERE account_id = ? AND direction = 'out' AND ts_ms >= ? AND ts_ms < ?",
            (account["account_id"], window["start_ms"], window["end_ms"]),
        ).fetchone()
    finally:
        conn.close()
    assert month["total_out_fen"] == int(row["total"])
    assert month["out_count"] == int(row["n"])
    for cat in month["by_category"]:
        # 分类只保证金额是分，不保证有 yuan 字段——前端按 amount_fen 格式化
        assert isinstance(cat["amount_fen"], int)
        assert 0 <= cat["pct"] <= 100


def test_overview_trend_is_six_points_and_last_month_is_partial(bank: Path) -> None:
    trend = get("/api/business/overview").json()["trend"]
    assert len(trend) == 6
    assert trend[-1]["partial"] is True
    assert all("net_positive" in point for point in trend)


def test_month_whose_spending_exceeds_income_shows_a_minus_sign(bank: Path) -> None:
    """亏损月份的「结余」必须带负号。

    `net_yuan` 曾经是 `fen_to_yuan_str(abs(net))`：趋势表把 -1,311.00 写成 1,311.00，
    和账单页红色的 -1,311.00 并排出现——同一屏两个结余，用户不知道该信哪个。
    """

    conn = db.connect()
    try:
        conn.execute(
            "INSERT INTO transactions (id, account_id, direction, amount_fen, counterparty,"
            " category, note, ts_ms, balance_after_fen, channel, reversal_of)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("t_neg_overspend", DEBIT_ACCOUNT_ID, "out", 90_000_000, "一笔超支", "购物", "",
             int(time.time() * 1000), 1_000_000, "app", None),
        )
        conn.commit()
    finally:
        conn.close()

    last = get("/api/business/overview").json()["trend"][-1]
    assert last["net_fen"] < 0
    assert last["net_positive"] is False
    assert last["net_yuan"].startswith("-"), last["net_yuan"]


def test_overview_never_sells_empty_lists_as_unavailable(bank: Path) -> None:
    """空列表是正常业务状态，不能塞进「暂不可用」——否则横幅会自相矛盾。"""

    data = get("/api/business/overview").json()
    unavailable = {item["section"] for item in data["unavailable"]}
    assert "savings_goals" not in unavailable
    assert "scheduled_tasks" not in unavailable
    assert "spending_by_category" not in unavailable
    assert "空列表" in data["unavailable_note"]
    for item in data["unavailable"]:
        assert item["reason"].strip()


def test_overview_quick_actions_are_prompts_not_shortcuts(bank: Path) -> None:
    """快捷入口只填空输入框，真正的执行仍然要过权限判定。"""

    actions = get("/api/business/overview").json()["quick_actions"]
    assert len(actions) == 5
    for action in actions:
        assert action["prompt"].strip()
        assert "tool" not in action and "auto" not in action


# ---------------------------------------------------------------- 账单分析


def test_bill_anomaly_is_a_hint_not_a_fraud_verdict(bank: Path) -> None:
    data = get("/api/business/bill?period=month").json()
    block = data["anomalies"]
    assert block["state"] == "发现异常（待核实）"
    assert "不是欺诈认定" in block["disclaimer"]
    assert block["count"] == len(block["items"]) >= 1

    for item in block["items"]:
        assert item["status"] == "发现异常"
        assert item["rule"]["id"].startswith("R-BILL-")
        assert item["possible_false_positive"].strip()
        assert item["actions"], "每条异常都要给出用户能做的下一步"


def test_bill_anomaly_evidence_points_at_real_transactions(bank: Path) -> None:
    data = get("/api/business/bill?period=month").json()
    for item in data["anomalies"]["items"]:
        ids = item["evidence"]["tx_ids"]
        assert ids, "没有流水号就没有证据"
        assert all(isinstance(tx_id, str) and tx_id.startswith("t_") for tx_id in ids)
        seen = {row["transaction_id"] for row in item["evidence"]["transactions"]}
        assert set(ids) == seen


def test_bill_period_year_is_supported(bank: Path) -> None:
    data = get("/api/business/bill?period=year").json()
    assert data["period"] == "year"
    assert data["range"]["start_ms"] < data["range"]["end_ms"]


# ---------------------------------------------------------------- 业务管理


def test_management_reports_the_suitability_gate_honestly(bank: Path) -> None:
    """适当性闸门是真做了：接口给出画像状态、题目数、等级带，并说清没测评会怎样。

    种子库不带画像，所以这里必须如实是 assessed=False；测过之后的读法在
    tests/test_suitability.py 里覆盖。重点是：不再有一句"我们没做适当性"的自认缺陷，
    而是一个真的会拦住申购的闸门 + 一句"这是简化问卷，不是监管口径"的边界。
    """
    data = get("/api/business/management").json()
    invest = data["investments"]
    assert invest["suitability_assessed"] is False
    gate = invest["suitability"]
    assert gate["assessed"] is False
    assert len(gate["questions"]) == 5
    assert {band["level"] for band in gate["level_bands"]} == {"C1", "C2", "C3", "C4", "C5"}
    assert "不是监管口径" in gate["disclaimer"]
    note = invest["suitability_note"]
    assert "风险测评" in note and "拒绝" in note
    assert invest["products"], "产品列表来自本地种子"
    assert invest["data_source"].strip()


def test_management_subscription_boundary_says_merchant_untouched(bank: Path) -> None:
    data = get("/api/business/management").json()
    subs = data["subscriptions"]
    assert subs["count"] >= 1
    assert "商户会员" in subs["boundary"]
    for item in subs["items"]:
        assert "商户" in item["boundary"]
    notes = " ".join(data["capabilities"]["subscription"]["notes"])
    assert "只能停止银行侧代扣" in notes


def test_management_reversal_is_labelled_as_simulated_accounting(bank: Path) -> None:
    notes = " ".join(get("/api/business/management").json()["capabilities"]["reversal"]["notes"])
    assert "冲正" in notes
    assert "模拟" in notes


# ---------------------------------------------------------------- 计划与提醒


def test_plans_separate_saved_scheduled_running_done(bank: Path) -> None:
    data = get("/api/business/plans").json()
    legend = {item["state"]: item["label"] for item in data["state_legend"]}
    assert legend == {
        "saved": "计划已保存",
        "scheduled": "任务已安排",
        "running": "任务正在执行",
        "done": "任务已完成",
        "failed": "已失败/已取消",
    }
    # 没有持久化任务时，必须给出空状态原因，不能留白
    assert data["scheduled_tasks"]["empty_reason"].strip()


def test_plans_reports_the_real_scheduler_not_a_promise(bank: Path) -> None:
    scheduler = get("/api/business/plans").json()["scheduler"]
    assert scheduler["kind"] == "in_process_ticker"
    assert isinstance(scheduler["enabled"], bool)
    assert isinstance(scheduler["running"], bool)
    assert "手动" in scheduler["monitoring_scope"]


# ---------------------------------------------------------------- 执行记录


def test_executions_expose_task_state_labels_and_audit_chain(bank: Path) -> None:
    """状态要全：DAG 节点真的会出现 pending / skipped，缺了就在表里露英文。"""

    data = get("/api/business/executions").json()
    labels = data["state_labels"]
    assert set(labels) == {
        "draft",
        "pending",
        "need_info",
        "waiting_confirm",
        "waiting_factor",
        "running",
        "done",
        "success",
        "failed",
        "cancelled",
        "skipped",
        "manual",
    }
    # 前端表格直接显示 label：任何一个英文 key 漏翻，用户看到的就是 "pending"
    assert all(text and text != key for key, text in labels.items())
    audit = data["audit"]
    assert audit["chain_ok"] is True
    # append_only 是一句人话解释，不是布尔：界面直接把它展示给用户
    assert "触发器" in audit["append_only"]


# ---------------------------------------------------------------- 鉴权


def test_business_views_require_the_api_token(
    bank: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """只读视图也不能裸奔：读得到余额和流水，同样是敏感数据。"""

    monkeypatch.delenv("API_AUTH_DISABLED", raising=False)
    monkeypatch.setenv("API_TOKEN", "s3cret-token")
    for url in (
        "/api/business/overview",
        "/api/business/bill?period=month",
        "/api/business/management",
        "/api/business/plans",
        "/api/business/executions",
    ):
        assert get(url).status_code == 401, url

    async def with_token() -> Any:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url=BASE_URL) as http:
            return await http.get(
                "/api/business/overview",
                headers={"Authorization": "Bearer s3cret-token"},
            )

    assert asyncio.run(with_token()).status_code == 200
