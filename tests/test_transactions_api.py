"""流水明细接口：全量对账单、合计口径、过滤与分页。

为什么值得单独测：这张卡「到底花了多少」以前只有账单页的统计口径，用户想看
具体是哪些笔、按关键词找一笔，界面上根本没有入口（「相关流水」只列命中规则的
证据流水）。这个接口把「账」摊开，口径必须和账单页一模一样，否则同一屏又出现
两个支出数——那是我们已经修过一次的毛病。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest

from app import db
from app.main import app
from scripts.seed import DEBIT_ACCOUNT_ID, seed

BASE_URL = "http://testserver"


@pytest.fixture()
def bank(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """种一份真种子库，并把 DEFAULT_DB_PATH 指过去。"""

    monkeypatch.setattr(db, "DEFAULT_DB_PATH", tmp_path / "bank.db")
    conn = db.connect()
    db.init_db(conn)
    seed(conn)
    conn.close()
    yield tmp_path / "bank.db"


def get(url: str) -> httpx.Response:
    async def run() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url=BASE_URL) as http:
            return await http.get(url)

    return asyncio.run(run())


def test_the_ledger_lists_more_than_the_rule_hits(bank: Path) -> None:
    """全量流水不是「异常证据」：条数应该明显多于账单页的命中流水。"""
    data = get("/api/transactions?limit=100").json()
    assert data["ok"] is True
    assert data["total"] >= data["count"] > 0
    assert data["account"]["account_id"] == DEBIT_ACCOUNT_ID
    first = data["items"][0]
    assert first["amount_yuan"] and first["ts_text"] and first["counterparty"]


def test_ledger_totals_match_the_bill_page(bank: Path) -> None:
    """主口径必须与账单页同一个数：否则同一张卡出现两个「本月支出」。"""
    bill = get("/api/business/bill").json()
    ledger = get("/api/transactions?limit=100").json()
    assert ledger["out_fen"] == bill["spend_out_fen"]
    assert ledger["in_fen"] == bill["spend_in_fen"]
    assert ledger["net_fen"] == bill["net_fen"]
    # 账户互转/理财单列，不混进支出
    assert ledger["internal_out_fen"] >= 0
    assert ledger["out_fen"] + ledger["internal_out_fen"] >= ledger["out_fen"]


def test_filtering_by_direction_keeps_only_that_side(bank: Path) -> None:
    data = get("/api/transactions?direction=in&limit=50").json()
    assert data["total"] > 0
    assert all(item["direction"] == "in" for item in data["items"])
    assert data["out_fen"] == 0


def test_keyword_searches_counterparty_and_note(bank: Path) -> None:
    data = get("/api/transactions?q=%E7%88%B1%E5%A5%87%E8%89%BA&limit=50").json()
    assert data["total"] > 0
    for item in data["items"]:
        assert "爱奇艺" in item["counterparty"] or "爱奇艺" in item["note"]


def test_paging_reports_the_real_total_and_pages_do_not_overlap(bank: Path) -> None:
    first = get("/api/transactions?limit=3&offset=0").json()
    second = get("/api/transactions?limit=3&offset=3").json()
    assert first["count"] == 3 and first["has_more"] is True
    assert first["total"] == second["total"]
    assert {i["transaction_id"] for i in first["items"]} & {i["transaction_id"] for i in second["items"]} == set()
    whole = get("/api/transactions?limit=100").json()
    assert second["total"] == whole["total"]


def test_a_year_is_more_than_a_month(bank: Path) -> None:
    month = get("/api/transactions").json()
    year = get("/api/transactions?period=year&limit=1").json()
    assert year["total"] >= month["total"]
    assert year["range"]["label"].endswith("年全年")


def test_another_card_has_its_own_ledger(bank: Path) -> None:
    credit = get("/api/transactions?account_id=a_b41a8d35").json()
    debit = get("/api/transactions").json()
    assert credit["account"]["type"] == "credit"
    assert credit["account"]["account_id"] != debit["account"]["account_id"]


def test_a_flagged_row_says_which_rule_it_hit(bank: Path) -> None:
    """命中规则的笔要能在明细里就地解释，且与账单页是同一条规则。"""
    data = get("/api/transactions?limit=100").json()
    assert data["anomaly_count"] >= 1
    flagged = [item for item in data["items"] if item["anomaly"]]
    assert flagged, "种子里爱奇艺涨价应该命中订阅规则"
    hit = flagged[0]["anomaly"]
    assert hit["rule_id"].startswith("R-BILL-")
    assert hit["title"] and hit["detail"] and hit["confidence_label"]
    assert hit["tx_count"] >= 1 and hit["amount_yuan"]
    bill = get("/api/business/bill").json()
    rules = {item["rule"]["id"] for item in bill["anomalies"]["items"]}
    assert hit["rule_id"] in rules, "明细里的规则必须与账单页同源"
    assert "不是欺诈认定" in data["anomaly_note"]
    # 页面上只出现中文把握度：confidence 是给前端分组的枚举，label 才是人读的。
    assert hit["confidence_label"] in {"高", "中", "低"}
    bill_label = bill["anomalies"]["items"][0]["rule"]["confidence_label"]
    assert bill_label in {"高", "中", "低"}
    assert not any("a" <= ch.lower() <= "z" for ch in bill_label)


def test_a_plain_row_carries_no_flag(bank: Path) -> None:
    """没有命中的笔不能跟着标红，否则整张表天天飘红。"""
    data = get("/api/transactions?limit=100").json()
    plain = [item for item in data["items"] if item["anomaly"] is None]
    assert plain, "正常流水不应该被标红"
    assert data["anomaly_count"] < data["count"]


def test_flagged_only_shows_the_rows_with_a_rule(bank: Path) -> None:
    """勾「只看命中账单规则的」= 列表只剩有问题的那几笔，合计也跟着收窄。"""
    whole = get("/api/transactions?limit=100").json()
    plain = get("/api/transactions?flagged=0&limit=100").json()
    assert plain["filters"]["flagged"] is False
    assert plain["total"] == whole["total"]
    only = get("/api/transactions?flagged=1&limit=100").json()
    assert only["filters"]["flagged"] is True
    assert only["total"] == only["count"] == len(only["items"])
    assert only["total"] == whole["anomaly_count"] >= 1
    assert all(item["anomaly"] for item in only["items"])


def test_flagged_with_nothing_hit_is_an_honest_empty_page(bank: Path) -> None:
    """一个都没命中时，空列表要配 0 合计，不能把全量合计糊上去。"""
    data = get(
        "/api/transactions?flagged=1&direction=in&q=%E7%88%B1%E5%A5%87%E8%89%BA&limit=50"
    ).json()
    assert data["total"] == data["count"] == 0
    assert data["items"] == []
    assert data["out_fen"] == 0 and data["in_fen"] == 0 and data["net_fen"] == 0
    assert data["has_more"] is False


def test_unknown_account_is_a_400_with_a_human_sentence(bank: Path) -> None:
    res = get("/api/transactions?account_id=a_nope")
    assert res.status_code == 400
    body = res.json()
    assert body["code"] == "VALIDATION"
    assert "账户" in body["user_text"]


def test_bad_direction_and_bad_month_are_refused(bank: Path) -> None:
    assert get("/api/transactions?direction=sideways").status_code == 400
    assert get("/api/transactions?month=13").status_code == 400
