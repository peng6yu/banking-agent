"""术语规范化的单元测试。

对应文档：docs/11_知识层设计.md 第 4.3 节

normalize 只做字面替换，不做语义推断。本文件守住：
1. 口语说法能映射回标准术语，且长别名优先（「关掉代扣」不能被「代扣」先吃掉）；
2. 标准术语本身不会被自己的别名二次替换；
3. 命中带歧义说明的术语时，ambiguous 会显式列出来；
4. terms_in_text 与 scenarios_of 能给下游提供「命中了哪些术语、涉及哪些场景」。
"""

from __future__ import annotations

from app.knowledge import get_base, normalize, scenarios_of, terms_in_text


def test_colloquial_transfer_maps_to_standard_term() -> None:
    """「打钱 / 划钱」这类说法要归到标准术语「转账」。"""
    base = get_base()

    for text in ("我想打钱给他", "麻烦帮我划钱过去", "把这笔转给对方"):
        got = normalize(base, text)
        assert "TERM-TRANSFER" in got.term_ids, f"{text} 没有命中转账术语"
        assert "转账" in got.text


def test_balance_colloquial_maps_to_standard_term() -> None:
    """「余额」这类说法要归到「账户余额」。"""
    base = get_base()
    got = normalize(base, "帮我查一下余额")

    assert got.term_ids == ("TERM-BALANCE",)
    assert got.text == "帮我查一下账户余额"
    assert ("余额", "TERM-BALANCE") in got.matches

    # 更口语的整句同样要命中（正文可能因别名是术语前缀而重复，以 term_ids 为准）。
    spoken = normalize(base, "卡里还有多少钱")
    assert "TERM-BALANCE" in spoken.term_ids
    assert ("卡里还有多少钱", "TERM-BALANCE") in spoken.matches


def test_long_alias_wins_over_short_alias() -> None:
    """长别名优先：「关掉代扣」应命中「解约」，而不是先被「代扣」替换成代扣协议。"""
    base = get_base()
    got = normalize(base, "我要关掉代扣")

    assert "TERM-CANCEL-MANDATE" in got.term_ids
    assert "代扣协议" not in got.text
    assert got.text.endswith("解约")


def test_card_loss_phrase_maps_to_loss_term() -> None:
    """「卡丢了」要归到「挂失」，且不能被别的卡片术语抢走。"""
    base = get_base()
    got = normalize(base, "我卡丢了怎么办")

    assert got.term_ids == ("TERM-CARD-LOSS",)
    assert "挂失" in got.text


def test_unsubscribe_phrase_maps_to_cancel_mandate() -> None:
    """「退订」要归到「解约」；服务侧的「订阅」是另一条术语。"""
    base = get_base()
    got = normalize(base, "帮我退订这个订阅服务")

    assert "TERM-CANCEL-MANDATE" in got.term_ids
    assert "解约" in got.text
    assert "TERM-SUBSCRIPTION" in got.term_ids


def test_continuous_monthly_maps_to_mandate() -> None:
    """「连续包月 / 自动续费」属于银行侧代扣协议。"""
    base = get_base()
    got = normalize(base, "这个自动续费怎么关")

    assert "TERM-MANDATE" in got.term_ids
    assert "代扣协议" in got.text


def test_purchase_and_redeem_phrases() -> None:
    """「买入」归申购，「取出来」归赎回（歧义会在 ambiguous 里提示）。"""
    base = get_base()

    buy = normalize(base, "我想买入一点理财")
    assert "TERM-PURCHASE" in buy.term_ids
    assert "申购" in buy.text

    redeem = normalize(base, "帮我把基金取出来")
    assert "TERM-REDEEM" in redeem.term_ids
    assert "赎回" in redeem.text


def test_risk_tolerance_phrase() -> None:
    """「能承受多大风险」要归到「风险承受能力」。"""
    base = get_base()
    got = normalize(base, "我怎么知道我能承受多大风险")

    assert "TERM-RISK-TOLERANCE" in got.term_ids
    assert "风险承受能力" in got.text


def test_standard_term_is_not_rewritten_by_itself() -> None:
    """alias == term 的说法不参与替换，标准术语原样保留。"""
    base = get_base()
    for word in ("转账", "挂失", "申购", "赎回", "账单"):
        got = normalize(base, word)
        assert got.text == word, f"标准术语「{word}」被改写成「{got.text}」"
        assert got.term_ids == ()


def test_known_limitation_alias_prefix_duplicates_text() -> None:
    """已知限制：别名是术语前缀时，正文会被重复替换，调用方应以 term_ids 为准。

    「账户余额」里含别名「余额」，于是被替换成「账户账户余额」。
    这是 normalize 只做字面替换的代价；docs/11 第 7 节记录了它，
    这里锁住「term_ids 仍然正确」这个下游真正依赖的行为。
    """
    base = get_base()
    got = normalize(base, "账户余额")

    assert "TERM-BALANCE" in got.term_ids
    assert ("余额", "TERM-BALANCE") in got.matches


def test_matches_records_alias_and_term_id() -> None:
    """matches 要保留「原文里的说法 → 术语 id」的对应关系，方便审计回指。"""
    base = get_base()
    got = normalize(base, "每月预算不够花了")

    assert ("每月预算", "TERM-BUDGET") in got.matches
    assert "TERM-BUDGET" in got.term_ids
    assert "预算" in got.text


def test_ambiguous_term_is_flagged() -> None:
    """命中带歧义说明的术语时，ambiguous 要列出术语 id。"""
    base = get_base()
    got = normalize(base, "我要把理财取出来")

    assert "TERM-REDEEM" in got.term_ids
    assert "TERM-REDEEM" in got.ambiguous
    assert base.terms["TERM-REDEEM"].ambiguity


def test_empty_text_is_safe() -> None:
    """空文本直接返回，不报错。"""
    base = get_base()
    got = normalize(base, "")

    assert got.text == ""
    assert got.term_ids == ()
    assert got.matches == ()


def test_terms_in_text_ranks_hits() -> None:
    """terms_in_text 返回术语对象，且多次命中的术语排在前面。"""
    base = get_base()
    hits = terms_in_text(base, "打钱打钱，我要划钱，顺便问下余额")

    assert hits
    assert hits[0].id == "TERM-TRANSFER"
    assert all(term.id in base.terms for term in hits)


def test_scenarios_of_maps_terms_to_scenarios() -> None:
    """terms_of 的术语 id 能映射到官方场景值，供检索按场景筛选。"""
    base = get_base()
    got = normalize(base, "我想打钱，但是卡里余额不够")

    scenes = scenarios_of(base, got.term_ids)
    assert "transfer" in scenes
    assert set(scenes) <= {"transfer", "wealth", "mandate", "cross_scene", "global"}

    # 未知术语 id 不应导致异常。
    assert scenarios_of(base, ("TERM-NOT-EXIST",)) == ()
