"""知识检索的单元测试。

对应文档：docs/11_知识层设计.md 第 4 节

检索口径是「结构化匹配 + 关键词 + 元数据筛选」，本文件守住：
1. 每个意图都能检索到规则，且 knowledge_ids 里第一条就是该意图；
2. 转账 / 理财 / 代扣三个场景都能同时取到术语与来源；
3. 意图不明、top_k 截断、未核实来源降级这些边界行为符合约定；
4. 工作流匹配依赖「意图允许的工具 ∩ 工作流节点」，不能把所有工作流都塞进来。
"""

from __future__ import annotations

from app.knowledge import get_base, retrieve
from app.planner.intent_schema import INTENT_NAMES

# 用户口语场景 → sources.yaml 的官方场景值（metadata.scenario_values）。
# 订阅在知识层归 mandate（代扣协议），理财归 wealth。
USER_SCENARIO = {
    "transfer": "transfer",
    "subscription": "mandate",
    "invest": "wealth",
    "split_bill": "transfer",
    "schedule_transfer": "transfer",
    "card_manage": "transfer",
    "query_balance": "transfer",
    "bill_analysis": "transfer",
    "reversal": "transfer",
    "birthday_plan": "transfer",
    "unknown": "global",
}


def test_retrieve_returns_intent_rule_first() -> None:
    """命中意图规则时，knowledge_ids 第一条必须是 intent:<意图名>。"""
    base = get_base()
    got = retrieve(base, intent="transfer", scenario="transfer")

    assert got.intent_rule is not None
    assert got.intent_rule.intent == "transfer"
    assert got.knowledge_ids[0] == "intent:transfer"
    assert "intent:transfer" in got.knowledge_ids


def test_every_intent_has_rule() -> None:
    """11 个意图逐个检索，都必须拿到规则，且不出现 KNOW_NO_INTENT_RULE。"""
    base = get_base()

    for intent in INTENT_NAMES:
        got = retrieve(base, intent=intent, scenario=USER_SCENARIO[intent], top_k=4)
        assert got.intent_rule is not None, f"{intent} 没有检索到规则"
        assert got.intent_rule.intent == intent
        codes = {w.code for w in got.warnings}
        assert "KNOW_NO_INTENT_RULE" not in codes, f"{intent} 被判成没有规则"


def test_unknown_intent_warns_instead_of_crashing() -> None:
    """意图名对不上时给 warning 而不是抛异常：知识层要能降级工作。"""
    base = get_base()
    got = retrieve(base, intent="transfer_money", scenario="transfer")

    assert got.intent_rule is None
    codes = [w.code for w in got.warnings]
    assert "KNOW_NO_INTENT_RULE" in codes
    assert all(w.ref != "transfer_money" or w.code == "KNOW_NO_INTENT_RULE" for w in got.warnings)


def test_three_business_scenarios_have_terms_and_sources() -> None:
    """转账 / 理财 / 代扣三个场景都要能取到术语，且术语来源可解析。"""
    base = get_base()

    for scenario in ("transfer", "wealth", "mandate"):
        got = retrieve(base, scenario=scenario, top_k=20)
        assert got.terms, f"{scenario} 场景没有检索到术语"
        assert got.sources, f"{scenario} 场景没有检索到来源"
        got_ids = {term.id for term in got.terms}
        assert got_ids, f"{scenario} 场景术语为空"

        for term in got.terms:
            assert scenario in term.scenarios, f"{term.id} 不属于 {scenario} 场景"
            assert term.source_ids, f"{term.id} 没有来源"
        for source in got.sources:
            assert source.source_id in base.sources


def test_expected_terms_hit_each_scenario() -> None:
    """三个场景各自的核心术语必须被检索到，防止场景值被误改。"""
    base = get_base()

    transfer_ids = {t.id for t in retrieve(base, scenario="transfer", top_k=30).terms}
    wealth_ids = {t.id for t in retrieve(base, scenario="wealth", top_k=30).terms}
    mandate_ids = {t.id for t in retrieve(base, scenario="mandate", top_k=30).terms}

    assert {"TERM-TRANSFER", "TERM-SCHEDULED-TRANSFER", "TERM-HOLD"} <= transfer_ids
    assert {"TERM-PURCHASE", "TERM-REDEEM", "TERM-RISK-TOLERANCE"} <= wealth_ids
    assert {"TERM-MANDATE", "TERM-CANCEL-MANDATE", "TERM-SUBSCRIPTION"} <= mandate_ids


def test_keyword_match_finds_term() -> None:
    """关键词按 term / definition / aliases 匹配，口语别名也要能命中。"""
    base = get_base()

    got = retrieve(base, keywords=("自动续费",), top_k=5)
    assert any(term.id == "TERM-MANDATE" for term in got.terms)

    got2 = retrieve(base, keywords=("卡丢了",), top_k=5)
    assert any(term.id == "TERM-CARD-LOSS" for term in got2.terms)


def test_top_k_caps_terms() -> None:
    """top_k 只截断术语，不截断意图规则本身。"""
    base = get_base()

    got = retrieve(base, intent="transfer", scenario="transfer", top_k=2)
    assert len(got.terms) == 2
    assert got.intent_rule is not None


def test_workflows_matched_by_allowed_tools() -> None:
    """工作流匹配靠「意图允许的工具 ∩ 工作流节点」，转账必须带出两阶段流程。"""
    base = get_base()
    got = retrieve(base, intent="transfer", scenario="transfer")

    wf_ids = {wf.id for wf in got.workflows}
    assert "wf_transfer_two_phase" in wf_ids

    for wf in got.workflows:
        assert set(wf.nodes) & set(got.intent_rule.allowed_tools), f"{wf.id} 与转账意图无关却被带出"


def test_redeem_then_transfer_workflow_reachable_from_invest() -> None:
    """理财意图要能带出「赎回→到账→转账」依赖，且顺序约束可读。"""
    base = get_base()
    got = retrieve(base, intent="invest", scenario="wealth")

    wf_ids = [wf.id for wf in got.workflows]
    assert "wf_wealth_redeem_then_transfer" in wf_ids

    redeem = next(wf for wf in got.workflows if wf.id == "wf_wealth_redeem_then_transfer")
    joined = " | ".join(redeem.transitions)
    assert "redeem_fund -> query_balance" in joined
    assert "query_balance -> transfer" in joined
    assert any("未到账" in rule for rule in redeem.rules)


def test_sources_collected_from_rules_terms_workflows() -> None:
    """来源要同时来自意图规则、术语与工作流，不能只取其中一类。"""
    base = get_base()
    got = retrieve(base, intent="subscription", scenario="mandate", top_k=20)

    source_ids = set(got.source_ids())
    assert set(got.intent_rule.source_ids) <= source_ids
    assert any(sid in source_ids for term in got.terms for sid in term.source_ids)

    # 解约依赖的「银行侧撤销后商户仍可能计费」这条，来自代扣撤销来源。
    assert "BANK-ICBC-MANDATE-CANCEL" in source_ids


def test_unverified_source_flag_for_degradation() -> None:
    """引用到未核实来源时 has_unverified_source 必须为真，调用方据此降级。"""
    base = get_base()

    reversal = retrieve(base, intent="reversal", scenario="transfer")
    assert reversal.has_unverified_source
    assert "GAP-TRANSFER-RETURN-CN" in reversal.source_ids()

    balance = retrieve(base, intent="query_balance")
    assert balance.intent_rule is not None
    assert set(balance.intent_rule.source_ids) <= set(balance.source_ids())


def test_retrieve_keeps_warning_from_base() -> None:
    """加载期的来源告警要透传到检索结果，不能在中途被吞掉。"""
    base = get_base()
    got = retrieve(base, intent="transfer", scenario="transfer")

    base_codes = {w.code for w in base.warnings}
    got_codes = {w.code for w in got.warnings}
    assert base_codes <= got_codes


def test_retrieve_without_any_argument_is_safe() -> None:
    """什么都不给时返回空结果而不是报错，方便调用方按需拼接参数。"""
    base = get_base()
    got = retrieve(base)

    assert got.intent_rule is None
    assert got.terms == ()
    assert got.knowledge_ids == ()
    assert all(w.code != "KNOW_NO_INTENT_RULE" for w in got.warnings)
