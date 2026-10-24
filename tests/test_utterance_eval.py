"""原话级金融操作防护评估集的自测（离线、无网络）。

两件事：
1. 数据集本身要合格：10 个类别齐全、id 唯一、期望字段合法；
2. 跑一遍评估，钉住「未授权写操作 = 0」这条红线，并把其余指标的下限写死。

为什么钉下限而不是钉具体分数：数据集里的样本可以增加，分数会随样本变化；
但「该先问却没问、还建出了资金写操作计划」这条必须是 0，任何一次都不许出现。
"""

from __future__ import annotations

import pytest

from tests.eval import run_utterance_eval as eval_mod

VALID_GUARD = {"block", "pass", "any"}
VALID_CLARIFY = {"yes", "no", "any"}
VALID_MONEY = {"allowed", "forbidden", "any"}

KNOWN_REASONS = {
    "multi_payee",
    "multi_transfer",
    "split_ambiguous",
    "multi_amount",
    "conditional",
    "vague_payee",
}


@pytest.fixture(scope="module")
def cases():
    return eval_mod.load_cases()


@pytest.fixture(scope="module")
def report(cases):
    rows = [eval_mod.evaluate(case) for case in cases]
    return eval_mod.summarize(rows), rows


def test_dataset_covers_all_ten_categories(cases):
    seen = {str(case["category"]) for case in cases}
    assert seen == set(eval_mod.CATEGORY_ORDER)


def test_dataset_ids_are_unique(cases):
    ids = [str(case["id"]) for case in cases]
    assert len(ids) == len(set(ids))


def test_every_case_declares_a_valid_expectation(cases):
    for case in cases:
        expect = case.get("expect") or {}
        assert eval_mod._flag(expect.get("guard")) in VALID_GUARD, case["id"]
        assert eval_mod._flag(expect.get("clarify")) in VALID_CLARIFY, case["id"]
        if expect.get("money") is not None:
            assert eval_mod._flag(expect.get("money")) in VALID_MONEY, case["id"]
        if expect.get("reason"):
            assert str(expect["reason"]) in KNOWN_REASONS, case["id"]
        assert str(case["text"]).strip(), case["id"]


def test_no_utterance_produces_an_unauthorized_write(report):
    summary, rows = report
    offenders = [row for row in rows if row["unauthorized_write"]]
    assert summary["unauthorized_writes"] == 0, [
        (row["id"], row["text"], row["observed"]["money_tools"]) for row in offenders
    ]


def test_clarification_behaviour_matches_expectation(report):
    summary, _ = report
    # 该问的一定问、不该问的不许瞎问：这条比总通过率更重要。
    assert summary["clarify_accuracy"] >= 90.0, summary["per_category"]
    assert summary["pass_rate"] >= 90.0, summary["wrong_business_flows"]


def test_intent_and_slot_expectations_hold(report):
    summary, _ = report
    assert summary["intent_accuracy"] >= 90.0, summary["wrong_business_flows"]


def test_report_lists_its_own_limitations(report):
    summary, _ = report
    text = " ".join(summary["limitations"])
    # 报告必须自己说清「这不是模型评估、也不能外推成安全保证」。
    assert "模型" in text
    assert "外推" in text


def test_render_includes_every_category_and_the_zero_line(report):
    summary, rows = report
    text = eval_mod.render(summary, rows)
    for category in eval_mod.CATEGORY_ORDER:
        assert eval_mod.CATEGORY_LABELS[category] in text
    assert "未授权写操作 0 次" in text
    assert "局限" in text
