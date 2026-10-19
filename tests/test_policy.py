"""三色裁决引擎的规则表与边界值测试。

对应文档：docs/05_安全层实现规格.md 5.1—5.2。
评审现场会考的那几个数字（999.99 / 1000.00 / 1000.01 / 900+200 / 900+100）都在这里。
"""

from __future__ import annotations

import ast
import pathlib

import pytest

from app.models import Action, Decision
from app.policy import RULES, PolicyContext, decide, explain
from app.policy import engine as engine_mod

TRANSFER_TOOL = "transfer"


def transfer_action(amount_fen: int | None, **kwargs: object) -> Action:
    return Action(
        tool=TRANSFER_TOOL,
        params={"payee_id": "p_zhangsan"},
        account_id="a_1",
        amount_fen=amount_fen,
        **kwargs,
    )


# ---------- docs/05 5.2 边界表：五个数字必须逐字对上 ----------

@pytest.mark.parametrize(
    ("amount_fen", "spent_fen", "level", "rule_id"),
    [
        (99_999, 0, "yellow", "R06_transfer_yellow"),      # 999.99 元
        (100_000, 0, "yellow", "R06_transfer_yellow"),     # 1000.00 元，线上仍算黄
        (100_001, 0, "red", "R08_transfer_red_single"),     # 1000.01 元越线
        (20_000, 90_000, "red", "R07_transfer_red_daily"),  # 今日 900 + 200 = 1100
        (10_000, 90_000, "yellow", "R06_transfer_yellow"),  # 今日 900 + 100 = 1000 正好在线上
    ],
)
def test_boundary_values(amount_fen: int, spent_fen: int, level: str, rule_id: str) -> None:
    decision = decide(transfer_action(amount_fen), PolicyContext(spent_today_fen=spent_fen))
    assert decision.level == level
    assert decision.rule_id == rule_id


def test_boundary_amounts_are_fen_only() -> None:
    """限额按分比：100_000 分就是 1000.00 元，不允许出现浮点。"""
    yellow = decide(transfer_action(100_000), PolicyContext())
    red = decide(transfer_action(100_001), PolicyContext())
    assert (yellow.level, red.level) == ("yellow", "red")
    assert yellow.limit_fen == 100_000 and red.limit_fen == 100_000


# ---------- 12 条规则逐条 ----------

def test_r01_readonly_green() -> None:
    decision = decide(Action(tool="query_balance", params={}, account_id="a_1"))
    assert (decision.level, decision.rule_id) == ("green", "R01_readonly")
    assert decision.required_factors == []


def test_r02_invalid_amount_red() -> None:
    decision = decide(transfer_action(0))
    assert (decision.level, decision.rule_id) == ("red", "R02_invalid_amount")
    assert decision.reason_text == "金额不正确，请重新输入"


def test_r02_missing_amount_red() -> None:
    decision = decide(transfer_action(None))
    assert decision.rule_id == "R02_invalid_amount"


def test_r03_unknown_tool_red() -> None:
    decision = decide(Action(tool="give_all_money_away", params={}, account_id="a_1"))
    assert (decision.level, decision.rule_id) == ("red", "R03_unknown_tool")
    assert decision.reason_text == "我还不会这个操作"


def test_r04_untrusted_amount_upgrades_to_two_factors() -> None:
    action = transfer_action(50_000, untrusted_fields=["params.note"])
    decision = decide(action, PolicyContext())
    assert (decision.level, decision.rule_id) == ("red", "R04_untrusted_amount")
    assert decision.required_factors == ["face", "sms"]


def test_r05_confirm_only_yellow() -> None:
    decision = decide(Action(tool="cancel_subscription", params={}, account_id="a_1"))
    assert (decision.level, decision.rule_id) == ("yellow", "R05_confirm_only")


def test_r06_yellow_has_no_extra_factor() -> None:
    decision = decide(transfer_action(50_000), PolicyContext())
    assert decision.required_factors == []


def test_r07_daily_red_has_two_factors() -> None:
    decision = decide(transfer_action(20_000), PolicyContext(spent_today_fen=90_000))
    assert decision.required_factors == ["face", "sms"]
    assert decision.spent_today_fen == 90_000


def test_r09_sensitive_red_has_three_factors() -> None:
    decision = decide(Action(tool="reset_password", params={}, account_id="a_1"))
    assert decision.rule_id == "R09_sensitive_red"
    assert decision.required_factors == ["face", "sms", "otp"]


def test_r10_invest_red_has_face_and_otp() -> None:
    decision = decide(Action(tool="buy_fund", params={}, account_id="a_1"))
    assert decision.rule_id == "R10_invest_red"
    assert decision.required_factors == ["face", "otp"]


def test_r11_default_deny_is_the_backstop() -> None:
    decision = decide(Action(tool="apply_virtual_card", params={}, account_id="a_1"))
    assert (decision.level, decision.rule_id) == ("red", "R11_default_deny")
    assert decision.reason_text == "这个操作我不确定是否安全，先不做"


def test_r12_other_account_red() -> None:
    """R12：代查他人账户红灯，主体不对就不弹确认框。"""
    decision = decide(
        Action(tool="query_balance", params={"account_owner": "other"}, account_id="a_1")
    )
    assert (decision.level, decision.rule_id) == ("red", "R12_other_account")
    assert decision.reason_code == "OTHER_ACCOUNT"
    assert decision.required_factors == []


def test_r12_is_not_a_dead_rule() -> None:
    """R12 排在 R01 前面，所以只读查询的目标是别人时它真的能被命中。"""
    ids = [rule.rule_id for rule in RULES]
    assert ids.index("R12_other_account") < ids.index("R01_readonly")
    assert decide(
        Action(tool="query_transactions", params={"account_owner": "other"}, account_id="a_1")
    ).rule_id == "R12_other_account"
    assert decide(
        Action(tool="query_balance", params={"account_owner": "self"}, account_id="a_1")
    ).rule_id == "R01_readonly"


def test_twelve_rules_in_table_order() -> None:
    assert [rule.rule_id for rule in RULES] == [
        "R12_other_account",
        "R01_readonly",
        "R02_invalid_amount",
        "R03_unknown_tool",
        "R04_untrusted_amount",
        "R05_confirm_only",
        "R06_transfer_yellow",
        "R07_transfer_red_daily",
        "R08_transfer_red_single",
        "R09_sensitive_red",
        "R10_invest_red",
        "R11_default_deny",
    ]


def test_rules_stop_at_first_hit() -> None:
    """短路求值：未知工具先被 R03 拦下，不会再走到 R11。"""
    decision = decide(Action(tool="nope", params={}, account_id="a_1", amount_fen=999_999_99))
    assert decision.rule_id == "R03_unknown_tool"


# ---------- 纯函数与确定性 ----------

def _engine_source() -> str:
    return pathlib.Path(engine_mod.__file__).read_text(encoding="utf-8")


def _engine_tree() -> ast.Module:
    return ast.parse(_engine_source())


def test_engine_does_not_import_llm() -> None:
    for node in ast.walk(_engine_tree()):
        if isinstance(node, ast.Import):
            assert all("llm" not in alias.name for alias in node.names)
        if isinstance(node, ast.ImportFrom):
            assert "llm" not in (node.module or "")


def test_engine_does_not_read_environment() -> None:
    for node in ast.walk(_engine_tree()):
        if isinstance(node, ast.Attribute):
            assert node.attr != "environ"
        if isinstance(node, ast.Name):
            assert node.id != "os"


def test_engine_does_not_touch_database() -> None:
    for node in ast.walk(_engine_tree()):
        if isinstance(node, ast.Import):
            assert all(alias.name not in {"sqlite3"} for alias in node.names)
        if isinstance(node, ast.ImportFrom):
            assert (node.module or "") not in {"sqlite3", "app.db"}


def test_engine_never_calls_the_clock() -> None:
    for node in ast.walk(_engine_tree()):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            assert node.func.id not in {"now_ms", "now_s", "time", "datetime"}


def test_engine_has_no_bare_except_and_no_print() -> None:
    source = _engine_source()
    assert "except:" not in source
    assert "print(" not in source


def test_decide_is_deterministic() -> None:
    action = transfer_action(50_000)
    ctx = PolicyContext(spent_today_fen=1_000)
    first = decide(action, ctx)
    second = decide(action, ctx)
    assert first == second
    assert decide(action) == decide(action, PolicyContext())


def test_decide_does_not_mutate_context() -> None:
    ctx = PolicyContext(spent_today_fen=90_000)
    decide(transfer_action(20_000), ctx)
    assert ctx.spent_today_fen == 90_000
    assert ctx.limit_fen == 100_000


def test_decide_never_writes_database(conn) -> None:
    before = conn.execute("SELECT COUNT(*) AS c FROM audit_log").fetchone()["c"]
    decide(transfer_action(50_000), PolicyContext())
    after = conn.execute("SELECT COUNT(*) AS c FROM audit_log").fetchone()["c"]
    assert before == after


# ---------- explain 三行 ----------

def test_explain_three_lines_for_yellow() -> None:
    action = transfer_action(50_000)
    lines = explain(decide(action, PolicyContext()), action).splitlines()
    assert len(lines) == 3
    assert lines[0] == "🟡 待确认 · R06_transfer_yellow"
    assert "500.00" in lines[1]
    assert "1000" in lines[2] or "1,000.00" in lines[2]
    assert "依据" in lines[2]


def test_explain_without_action_still_three_lines() -> None:
    decision = decide(Action(tool="query_balance", params={}, account_id="a_1"))
    lines = explain(decision).splitlines()
    assert len(lines) == 3
    assert lines[0].endswith("R01_readonly")


def test_every_decision_carries_rule_id_and_text() -> None:
    for action in (
        transfer_action(50_000),
        transfer_action(200_000),
        Action(tool="query_balance", params={}, account_id="a_1"),
        Action(tool="whatever", params={}, account_id="a_1"),
    ):
        decision: Decision = decide(action)
        assert decision.rule_id
        assert decision.reason_text
