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
from app.policy.rules import KNOWN_TOOLS, PENDING_TOOLS
import app.policy.rules as rules_mod

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


def test_r01_readonly_green_for_list_products() -> None:
    """查理财列表是只读绿灯：R01 直通，不能被 R03 当成幻觉工具拦停。"""
    decision = decide(Action(tool="list_products", params={}, account_id="a_1"))
    assert (decision.level, decision.rule_id) == ("green", "R01_readonly")
    assert decision.reason_code == "READONLY"
    assert decision.rule_id != "R03_unknown_tool"
    assert decision.required_factors == []


def test_r01_readonly_green_for_estimate_risk() -> None:
    """风险测算同样只读：绿灯直通 R01，不落到 R03_unknown_tool。"""
    decision = decide(
        Action(
            tool="estimate_risk",
            params={"product_code": "F00001", "amount_fen": 100_000},
            account_id="a_1",
        )
    )
    assert (decision.level, decision.rule_id) == ("green", "R01_readonly")
    assert decision.reason_code == "READONLY"
    assert decision.rule_id != "R03_unknown_tool"
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


def test_r11_default_deny_is_the_backstop(monkeypatch) -> None:
    """R11 是最后一道兜底：登记了工具却没配规则的，一律红拒。

    PENDING_TOOLS 清空之后，R11 在现有工具集里确实走不到了——每个 KNOWN_TOOLS 成员
    都被前面某条规则接住，没登记的则先撞 R03。所以这条用例直接构造它真正防的那个场景：
    **有人往 KNOWN_TOOLS 里加了个工具，却忘了配规则**。那正是 R11 存在的意义，
    也是唯一能证明它不是死代码的方式（monkeypatch 补上登记，规则表照旧）。
    """
    monkeypatch.setattr(rules_mod, "KNOWN_TOOLS", KNOWN_TOOLS | {"future_tool"})
    decision = decide(Action(tool="future_tool", params={}, account_id="a_1"))
    assert (decision.level, decision.rule_id) == ("red", "R11_default_deny")
    assert decision.reason_text == "这个操作我不确定是否安全，先不做"


def test_unregistered_tool_hits_r03_before_r11() -> None:
    """完全没登记的工具先被 R03 接住——R11 不是第一道防线，说清楚免得误读。"""
    decision = decide(Action(tool="close_account", params={}, account_id="a_1"))
    assert decision.rule_id == "R03_unknown_tool"
    assert decision.level == "red"


def test_known_tools_no_longer_contain_pending_names() -> None:
    """销户、改卡密码都已实现，PENDING_TOOLS 清空；这些名字也不再落 R11。"""
    assert PENDING_TOOLS == frozenset()
    for name in ("close_card", "change_card_pin"):
        assert name in KNOWN_TOOLS
        decision = decide(Action(tool=name, params={"card_tail": "1234"}, account_id="a_1"))
        assert decision.rule_id != "R11_default_deny"


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


# ---------- 用户可见文案：不念内部 id，红灯不说「我不做」 ----------


def test_yellow_reason_text_names_the_payee_not_the_internal_id() -> None:
    """R06 的确认卡片要念收款人的名字，不是数据库主键。

    没有 payee_name 时 _payee_label 会回落到 payee_id（p_xxx），
    等于把 p_77c05e3a 这种内部 id 念给用户听。
    """
    decision = decide(
        Action(
            tool="transfer",
            params={"payee_id": "p_77c05e3a", "payee_name": "张三"},
            account_id="a_1",
            amount_fen=50_000,
        ),
        PolicyContext(),
    )
    assert decision.rule_id == "R06_transfer_yellow"
    assert "张三" in decision.reason_text
    assert "p_" not in decision.reason_text


def test_yellow_reason_text_falls_back_to_the_other_party() -> None:
    """没有展示名时说「对方」：不抛异常，也不把内部 id 念出来。"""
    decision = decide(
        Action(
            tool="transfer",
            params={"merchant": "flower_shop"},
            account_id="a_1",
            amount_fen=50_000,
        ),
        PolicyContext(),
    )
    assert decision.rule_id == "R06_transfer_yellow"
    assert "对方" in decision.reason_text
    assert "p_" not in decision.reason_text


def test_red_single_text_asks_for_verification_not_refusal() -> None:
    """R08 是可验证的红：face + sms 验过就放行，文案不能停在「我不能直接做」。"""
    decision = decide(transfer_action(100_001), PolicyContext())
    assert decision.rule_id == "R08_transfer_red_single"
    assert "人脸" in decision.reason_text
    assert "短信" in decision.reason_text
    assert "不能直接做" not in decision.reason_text


def test_red_daily_text_asks_for_verification_not_refusal() -> None:
    """R07 与 R08 同理：日累计越线也是双因子验过就放行，不能说「我先不做」。"""
    decision = decide(transfer_action(20_000), PolicyContext(spent_today_fen=90_000))
    assert decision.rule_id == "R07_transfer_red_daily"
    assert "人脸" in decision.reason_text
    assert "短信" in decision.reason_text
    assert "我先不做" not in decision.reason_text


# ---------- 预约转账不吃日线：规则空隙的回归（评审 F1 / t18） ----------
# 背景：schedule_transfer 的钱到出账那天才走，所以创建期只判单笔上限，日线归执行日。
# R07 已按设计豁免它，但 R06 原来带 not _over_daily —— 今日已花 900 再排 500 时
# R06 否掉、R07 豁免、R08 只判单笔也不管，最后掉进 R11_default_deny 判红且不带因子，
# 用户被无理由拒绝。下面三条钉住修好后的行为，也钉住没有连带放宽单笔上限。

def schedule_action(amount_fen: int | None, **kwargs: object) -> Action:
    return Action(
        tool="schedule_transfer",
        params={"payee_id": "p_zhangsan", "due_ms": 1_800_000_000_000},
        account_id="a_1",
        amount_fen=amount_fen,
        **kwargs,
    )


def test_scheduled_transfer_stays_yellow_when_today_is_nearly_used_up() -> None:
    """今日已花 900 再排一笔 500：应为黄灯确认，不是 R11 兜底拒绝。

    这条是 F1 的正身。修前实际拿到 (red, R11_default_deny, required_factors=[])，
    执行器据此走 _blocked 直接终止，用户看到「这个操作我不确定是否安全」。
    """
    decision = decide(schedule_action(50_000), PolicyContext(spent_today_fen=90_000))
    assert decision.level == "yellow"
    assert decision.rule_id == "R06_transfer_yellow"
    assert not decision.required_factors


def test_scheduled_transfer_daily_exemption_does_not_widen_single_limit() -> None:
    """同一场景下金额翻到 2000：仍必须是 R08 单笔红灯，且照旧带 face + sms。

    这条防的是「为了让上面那条变黄，顺手把日线豁免写成了整条规则的豁免」。
    """
    decision = decide(schedule_action(200_000), PolicyContext(spent_today_fen=90_000))
    assert decision.level == "red"
    assert decision.rule_id == "R08_transfer_red_single"
    assert set(decision.required_factors) == {"face", "sms"}


def test_daily_exemption_is_scoped_to_scheduled_transfer_only() -> None:
    """豁免只对预约生效：同一笔 500 元走「当面转」在今天已花 900 时仍是 R07 红灯。

    这是把豁免范围钉住——将来若有人把 _DAILY_RULE_EXEMPT_TOOLS 加宽，这条会先红。
    """
    decision = decide(transfer_action(50_000), PolicyContext(spent_today_fen=90_000))
    assert decision.level == "red"
    assert decision.rule_id == "R07_transfer_red_daily"
    assert set(decision.required_factors) == {"face", "sms"}
