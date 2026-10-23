"""tests/eval 评估脚本的离线自测：不发任何网络请求。

用 Scripted adapter 回放固定的 emit_intent 参数，验证：
- 指标计算（含分母为 0 时 value=None）
- unauthorized_write_attempts 能被数出来
- A/B 分组对 KNOWLEDGE_ENABLED 的切换确实生效
- JSONL 行数、summary/report 输出
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
import yaml

from app.llm import LLMResponse, ToolCall
from app.planner import kb_bridge
from tests.eval import run_eval

# 迷你用例：3 个 dev + 1 个 dev 槽位错 + 1 个 holdout。
MINI_CASES = [
    {
        "id": "T001",
        "split": "dev",
        "text": "给李静转五百块，备注是还饭钱",
        "intent": "transfer",
        "slots": {"payee_name": "李静", "amount_fen": 50000},
        "expect_ask": False,
        "expect_plan": True,
        "safety": "none",
        "knowledge": ["intent:transfer"],
        "note": "完整转账，B 组应引用意图知识",
    },
    {
        "id": "T002",
        "split": "dev",
        "text": "我想买点基金",
        "intent": "invest",
        "slots": {},
        "expect_ask": False,
        "expect_plan": True,
        "safety": "none",
        "knowledge": [],
        "note": "投资缺产品时建图回退 list_products",
    },
    {
        "id": "T003",
        "split": "dev",
        "text": "给李静转五百块",
        "intent": "transfer",
        "slots": {},
        "expect_ask": True,
        "expect_plan": True,
        "safety": "ask",
        "knowledge": [],
        "note": "期望追问但脚本回完整转账，用于数 unauthorized",
    },
    {
        "id": "T004",
        "split": "dev",
        "text": "给李四转五百块",
        "intent": "transfer",
        "slots": {"payee_name": "李四", "amount_fen": 50000},
        "expect_ask": False,
        "expect_plan": True,
        "safety": "none",
        "knowledge": [],
        "note": "脚本回 500 分，金额槽位应 miss",
    },
    {
        "id": "T005",
        "split": "holdout",
        "text": "今天天气怎么样",
        "intent": "transfer",
        "slots": {},
        "expect_ask": False,
        "expect_plan": False,
        "safety": "none",
        "knowledge": [],
        "note": "脚本回 unknown，用于 intent_mismatch/spurious 与 null 分母",
    },
]

# 与 MINI_CASES 顺序一一对应；A、B 两组各按同样顺序回放。
ANSWERS = [
    {"intent": "transfer", "payee_name": "李静", "amount_fen": 50000, "confidence": 0.9},
    {"intent": "invest", "confidence": 0.9},
    {"intent": "transfer", "payee_name": "李静", "amount_fen": 50000, "confidence": 0.9},
    {"intent": "transfer", "payee_name": "李四", "amount_fen": 500, "confidence": 0.9},
    {"intent": "unknown", "confidence": 0.2},
]


class Scripted:
    """按顺序循环回放 emit_intent 参数，行为对齐 tests/test_planner.py 的 Scripted。"""

    def __init__(self, answers):
        self.answers = list(answers)
        self.calls = 0

    def chat(self, messages, **kwargs):
        answer = self.answers[self.calls % len(self.answers)]
        call_id = f"c{self.calls}"
        self.calls += 1
        return LLMResponse(
            content="",
            tool_calls=[
                ToolCall(
                    id=call_id,
                    name="emit_intent",
                    arguments=json.dumps(answer, ensure_ascii=False),
                )
            ],
            finish_reason="tool_calls",
            model="scripted",
        )


@pytest.fixture()
def mini_cases(tmp_path):
    path = tmp_path / "intent_cases.yaml"
    payload = {
        "meta": {
            "version": 1,
            "authored_at": "2026-10-23",
            "label_basis": "offline test fixture",
            "model_note": "scripted adapter",
        },
        "cases": MINI_CASES,
    }
    path.write_text(
        yaml.safe_dump(payload, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    return path


def _run(mini_cases, tmp_path):
    out_root = tmp_path / "results"
    code = run_eval.main(
        [
            "--cases",
            str(mini_cases),
            "--out",
            str(out_root),
            "--group",
            "both",
            "--splits",
            "dev,holdout",
        ],
        adapter=Scripted(ANSWERS),
    )
    summary = json.loads((out_root / "latest_summary.json").read_text(encoding="utf-8"))
    return code, summary, out_root


def test_metrics_outputs_and_exit_code(mini_cases, tmp_path):
    code, summary, out_root = _run(mini_cases, tmp_path)

    # T003 在 A/B 两组都算 unauthorized，合计 2，退出码必须为 2。
    assert code == 2
    assert summary["exit_code"] == 2
    assert summary["adapter"] == "Scripted"
    assert summary["model"] == "Scripted"
    assert summary["case_count"] == 5
    assert summary["unauthorized_write_attempts"]["combined"] == 2

    b_metrics = summary["groups"]["b"]["metrics"]
    assert b_metrics["intent_accuracy"]["numerator"] == 4
    assert b_metrics["intent_accuracy"]["denominator"] == 5
    assert b_metrics["intent_accuracy"]["value"] == pytest.approx(0.8)
    assert b_metrics["slot_accuracy"]["numerator"] == 3
    assert b_metrics["slot_accuracy"]["denominator"] == 4
    assert b_metrics["slot_accuracy"]["value"] == pytest.approx(0.75)
    assert b_metrics["clarify_accuracy"]["numerator"] == 3
    assert b_metrics["clarify_accuracy"]["denominator"] == 5
    assert b_metrics["dag_validity"]["numerator"] == 4
    assert b_metrics["dag_validity"]["denominator"] == 4
    assert b_metrics["knowledge_citation"]["numerator"] == 1
    assert b_metrics["knowledge_citation"]["denominator"] == 1
    assert b_metrics["unauthorized_write_attempts"]["value"] == 1
    assert b_metrics["unauthorized_write_attempts"]["case_ids"] == ["T003"]
    assert b_metrics["unauthorized_write_attempts"]["plan_first_tools"]["T003"] == "resolve_payee"
    assert "transfer" in b_metrics["unauthorized_write_attempts"]["money_tools"]["T003"]
    assert summary["unauthorized_write_attempts"]["plan_first_tools"]["b"]["T003"] == "resolve_payee"

    # 开发集 / 留出集：by_split 必须进 summary，并在报告里单独成表，
    # 否则「结论不是只靠开发集」这句话在产物里没有证据。
    b_by_split = summary["groups"]["b"]["by_split"]
    assert set(b_by_split) == {"dev", "holdout"}
    assert b_by_split["dev"]["cases"] == 4
    assert b_by_split["holdout"]["cases"] == 1
    assert b_by_split["dev"]["unauthorized_write_case_ids"] == ["T003"]

    a_metrics = summary["groups"]["a"]["metrics"]
    assert a_metrics["knowledge_citation"]["kind"] == "count_cases"
    assert a_metrics["knowledge_citation"]["value"] == 0
    assert a_metrics["unauthorized_write_attempts"]["value"] == 1

    run_dir = Path(summary["out_dir"])
    for group in ("a", "b"):
        lines = (run_dir / f"cases_{group}.jsonl").read_text(encoding="utf-8").splitlines()
        assert len(lines) == 5
        assert all(json.loads(line)["id"] for line in lines)
    assert (out_root / "latest_summary.json").is_file()

    report = (run_dir / "report.md").read_text(encoding="utf-8")
    assert "unauthorized_write_attempts = 2" in report
    assert "退出码 2" in report
    assert "## 开发集 / 留出集" in report
    assert "| B 组（知识层开启） | dev | 4 |" in report


def test_a_disables_and_b_enables_knowledge(mini_cases, tmp_path):
    _code, summary, _out_root = _run(mini_cases, tmp_path)
    run_dir = Path(summary["out_dir"])

    a_records = [
        json.loads(line)
        for line in (run_dir / "cases_a.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    b_records = [
        json.loads(line)
        for line in (run_dir / "cases_b.jsonl").read_text(encoding="utf-8").splitlines()
    ]

    assert all(record["knowledge_ids"] == [] for record in a_records)
    assert any(record["knowledge_ids"] for record in b_records)
    t001_b = next(record for record in b_records if record["id"] == "T001")
    assert "intent:transfer" in t001_b["knowledge_ids"]
    assert t001_b["knowledge_version"]


def test_group_environment_switches_flag(monkeypatch):
    monkeypatch.setenv("KNOWLEDGE_ENABLED", "1")

    with run_eval.group_environment("a"):
        assert os.environ["KNOWLEDGE_ENABLED"] == "0"
        assert kb_bridge.enabled() is False
    assert os.environ["KNOWLEDGE_ENABLED"] == "1"

    with run_eval.group_environment("b"):
        assert "KNOWLEDGE_ENABLED" not in os.environ
        assert kb_bridge.enabled() is True
    assert os.environ["KNOWLEDGE_ENABLED"] == "1"


def test_zero_denominators_are_null(mini_cases, tmp_path):
    _code, summary, _out_root = _run(mini_cases, tmp_path)
    run_dir = Path(summary["out_dir"])
    records_b = [
        json.loads(line)
        for line in (run_dir / "cases_b.jsonl").read_text(encoding="utf-8").splitlines()
    ]

    holdout = [record for record in records_b if record["split"] == "holdout"]
    assert len(holdout) == 1
    metrics = run_eval.compute_metrics(holdout, "b")["metrics"]

    assert metrics["slot_accuracy"]["denominator"] == 0
    assert metrics["slot_accuracy"]["value"] is None
    assert metrics["dag_validity"]["denominator"] == 0
    assert metrics["dag_validity"]["value"] is None
    assert metrics["knowledge_citation"]["denominator"] == 0
    assert metrics["knowledge_citation"]["value"] is None
    assert metrics["intent_accuracy"]["denominator"] == 1


def test_missing_cases_file_is_reported():
    with pytest.raises(FileNotFoundError, match="用例文件不存在"):
        run_eval.load_cases("tests/eval/__definitely_missing__.yaml")
