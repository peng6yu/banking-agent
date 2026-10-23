"""知识层加载与结构校验的单元测试。

对应文档：docs/11_知识层设计.md 第 3 节

这个文件守住阶段 3 的四条底线：
1. 5 个 YAML 都能被 load() 读进来，结构性错误必须 fail closed；
2. 内容性告警里，来源悬空、术语悬空、关系端点错、别名冲突这四类必须为零；
3. 意图规则与规划器真实必填槽位一致，allowed_tools 与工作流节点只能是真工具；
4. 34 条来源 id 全部可解析，知识条目引用的来源不许悬空。
"""

from __future__ import annotations

from app.knowledge import get_base, load, reset_cache
from app.knowledge.loader import FILE_MAP, KNOWLEDGE_DIR
from app.knowledge.models import RULE_LEVELS, SOURCE_STATUSES
from app.planner.card_actions import CARD_ACTION_SLOT
from app.planner.intent_schema import INTENT_NAMES, SLOT_KEYS
from app.tools.tool_specs import TOOL_SPECS

# 真工具名集合：knowledge 里的 allowed_tools 与 workflows.nodes 只能取这个集合里的名字。
TOOL_NAMES = frozenset(spec.name for spec in TOOL_SPECS)

# 四类内容性告警属于「知识层自身坏了」，本项目要求为零。
BLOCKING_WARNING_CODES = frozenset(
    {
        "KNOW_UNKNOWN_SOURCE",
        "KNOW_UNKNOWN_TERM_REF",
        "KNOW_BAD_RELATION_ENDPOINT",
        "KNOW_ALIAS_CONFLICT",
    }
)

# 来源核验状态带来的告警是来源登记的固有属性，不是知识层错误，允许存在。
ALLOWED_SOURCE_WARNING_CODES = frozenset(
    {"KNOW_SOURCE_UNVERIFIED", "KNOW_SOURCE_SUPERSEDED", "KNOW_SOURCE_STALE"}
)

# 任务书要求的 12 条标准术语：term 字段必须逐字等于这些值。
STANDARD_TERMS = (
    "转账",
    "预约转账",
    "代扣协议",
    "解约",
    "申购",
    "赎回",
    "账单",
    "账户余额",
    "挂失",
    "储蓄目标",
    "预算",
    "风险承受能力",
)

# 任务书硬要求必须存在的工作流 id。
REQUIRED_WORKFLOW_IDS = frozenset(
    {
        "wf_transfer_two_phase",
        "wf_wealth_redeem_then_transfer",
        "wf_mandate_cancel",
        "wf_card_loss",
        "wf_scheduled_transfer",
        "wf_split_bill",
    }
)


def test_every_yaml_file_exists_and_parses() -> None:
    """5 个知识文件都存在，且 load() 能把它们全部解析出来。"""
    for key, filename in FILE_MAP.items():
        assert (KNOWLEDGE_DIR / filename).is_file(), f"{key} 对应的 {filename} 不存在"

    reset_cache()
    base = load(KNOWLEDGE_DIR)

    assert base.version, "版本号不能为空：审计要靠它回指当时用的是哪一版知识"
    assert len(base.version) == 16
    assert base.terms and base.entities and base.relations
    assert base.intents and base.workflows and base.sources


def test_counts_within_task_ranges() -> None:
    """条目数量落在任务书要求的区间里。"""
    base = get_base()

    assert 32 <= len(base.terms) <= 45
    assert 20 <= len(base.entities) <= 25
    assert 18 <= len(base.relations) <= 22
    assert len(base.intents) == len(INTENT_NAMES)
    assert 8 <= len(base.workflows) <= 12
    assert len(base.sources) == 34


def test_no_blocking_content_warnings() -> None:
    """来源悬空、术语悬空、关系端点错、别名冲突必须为零。"""
    base = get_base()

    blocking = [w for w in base.warnings if w.code in BLOCKING_WARNING_CODES]
    assert blocking == [], f"出现阻断性告警：{[(w.code, w.detail) for w in blocking]}"

    unknown = [w for w in base.warnings if w.code not in (BLOCKING_WARNING_CODES | ALLOWED_SOURCE_WARNING_CODES)]
    assert unknown == [], f"出现未预期的告警类型：{[(w.code, w.detail) for w in unknown]}"


def test_every_term_is_complete() -> None:
    """每条术语都要有 id、term、定义与来源，且 scenarios 落在官方取值里。"""
    base = get_base()
    scenarios = {"transfer", "wealth", "mandate", "cross_scene", "global"}

    for term in base.terms.values():
        assert term.id.startswith("TERM-"), f"{term.id} 的 id 前缀不符合约定"
        assert term.term.strip(), f"{term.id} 缺 term"
        assert term.definition.strip(), f"{term.id} 缺定义"
        assert term.source_ids, f"{term.id} 没有来源"
        assert set(term.scenarios) <= scenarios, f"{term.id} 的场景不合法：{term.scenarios}"
        for ref in term.related:
            assert ref in base.terms, f"{term.id} 关联了不存在的术语 {ref}"
        for sid in term.source_ids:
            assert sid in base.sources, f"{term.id} 引用了不存在的来源 {sid}"


def test_standard_terms_present_verbatim() -> None:
    """任务书点名的 12 条术语必须逐字存在，且别名覆盖口语说法。"""
    base = get_base()
    by_term = {term.term: term for term in base.terms.values()}

    for word in STANDARD_TERMS:
        assert word in by_term, f"缺少标准术语「{word}」"
        assert by_term[word].aliases, f"标准术语「{word}」没有别名"

    assert set(by_term).issuperset(STANDARD_TERMS)
    # 抽查几条高风险口语说法，防止别名被后来改动时悄悄丢掉。
    assert "打钱" in by_term["转账"].aliases
    assert "自动续费" in by_term["代扣协议"].aliases
    assert "卡丢了" in by_term["挂失"].aliases
    assert "卡里还有多少钱" in by_term["账户余额"].aliases


def test_aliases_are_unique_across_terms() -> None:
    """同一个说法只能指向一条术语（含 term 本身），否则 normalize 会随机替换。"""
    base = get_base()
    owner: dict[str, str] = {}

    for term in base.terms.values():
        for word in (*term.aliases, term.term):
            assert word not in owner, f"「{word}」同时属于 {owner.get(word)} 与 {term.id}"
            owner[word] = term.id


def test_entities_and_relation_endpoints() -> None:
    """实体字段齐全，关系的 domain/range 必须指向已定义实体。"""
    base = get_base()

    for entity in base.entities.values():
        assert entity.label.strip(), f"{entity.id} 缺 label"
        assert entity.type.strip(), f"{entity.id} 缺 type"
        assert entity.definition.strip(), f"{entity.id} 缺定义"
        assert entity.source_ids, f"{entity.id} 没有来源"
        for sid in entity.source_ids:
            assert sid in base.sources, f"{entity.id} 引用了不存在的来源 {sid}"

    for rel in base.relations.values():
        assert rel.label.strip() and rel.kind.strip(), f"{rel.id} 缺 label 或 kind"
        assert rel.domain in base.entities, f"{rel.id} 的 domain 悬空：{rel.domain}"
        assert rel.range in base.entities, f"{rel.id} 的 range 悬空：{rel.range}"
        for sid in rel.source_ids:
            assert sid in base.sources, f"{rel.id} 引用了不存在的来源 {sid}"


def test_intent_rules_cover_all_intents() -> None:
    """意图规则必须与规划器的意图枚举一一对应，不多不少。"""
    base = get_base()

    assert set(base.intents) == set(INTENT_NAMES)
    for rule in base.intents.values():
        # 只读意图可以没有必填槽位，但必须写清「什么时候该追问」。
        assert rule.ask_when, f"{rule.intent} 没有写追问条件"
        assert rule.forbidden_ops, f"{rule.intent} 没有写禁止操作"
        assert rule.source_ids, f"{rule.intent} 没有来源"
        for sid in rule.source_ids:
            assert sid in base.sources, f"{rule.intent} 引用了不存在的来源 {sid}"


def test_intent_slots_match_planner_contract() -> None:
    """required_slots 与 app/planner/intent.py:_missing 的必填槽位一致，且只用真实槽位名。"""
    base = get_base()

    for rule in base.intents.values():
        for slot in (*rule.required_slots, *rule.optional_slots):
            # card_action 是 card_actions.py 的内部动作槽位（由模型给出的 action 归一而来），
            # 不在 intent_schema.SLOT_KEYS 里，但确实是规划器读取的真实槽位名。
            allowed = set(SLOT_KEYS) | {CARD_ACTION_SLOT}
            assert slot in allowed, f"{rule.intent} 用了不存在的槽位 {slot}"

    assert set(base.intents["transfer"].required_slots) == {"payee_name", "amount_fen"}
    assert set(base.intents["birthday_plan"].required_slots) == {"payee_name", "amount_fen"}
    assert set(base.intents["schedule_transfer"].required_slots) == {
        "payee_name",
        "amount_fen",
        "due_ms",
    }
    assert set(base.intents["split_bill"].required_slots) == {"payee_names", "amount_fen"}
    # invest 的产品代码不是硬必填：与 _missing() 的既有回退行为对齐——
    # 缺 product_code 时规划器回退到只读 list_products，不凭猜测下单。
    invest_rule = base.intents["invest"]
    assert "product_code" not in invest_rule.required_slots
    assert "product_code" in invest_rule.optional_slots

    # card_manage 的必填槽位是条件性的（card_action 决定 card_tail / amount_fen），写在 ask_when 里，
    # 不写成必填；card_action 是 app/planner/card_actions.py 的内部槽位名，不在 SLOT_KEYS 中。
    card_rule = base.intents["card_manage"]
    assert card_rule.required_slots == ()
    assert {"card_action", "card_tail", "amount_fen"} <= set(card_rule.optional_slots)
    assert any("card_tail" in item or "尾号" in item for item in card_rule.ask_when)

    # 只读与纯澄清意图不该被迫要求槽位。
    for intent in ("query_balance", "bill_analysis", "subscription", "reversal"):
        assert base.intents[intent].required_slots == ()


def test_allowed_tools_are_real() -> None:
    """意图允许的工具与工作流节点，名字必须是 tool_specs.py 里真实注册的工具。"""
    base = get_base()

    for rule in base.intents.values():
        unknown = set(rule.allowed_tools) - TOOL_NAMES
        assert not unknown, f"{rule.intent} 允许了不存在的工具：{sorted(unknown)}"

    for wf in base.workflows.values():
        unknown = set(wf.nodes) - TOOL_NAMES
        assert not unknown, f"{wf.id} 的节点不是真实工具：{sorted(unknown)}"


def test_workflows_have_dependency_contracts() -> None:
    """工作流必须写清前置条件、失败处理、确认点与依赖顺序。"""
    base = get_base()

    assert REQUIRED_WORKFLOW_IDS <= set(base.workflows)
    for wf in base.workflows.values():
        assert wf.name.strip(), f"{wf.id} 缺名称"
        assert wf.preconditions, f"{wf.id} 没有前置条件"
        assert wf.nodes, f"{wf.id} 没有节点"
        assert wf.failure_handling, f"{wf.id} 没有失败处理"
        assert wf.rules, f"{wf.id} 没有约束规则"
        assert wf.source_ids, f"{wf.id} 没有来源"
        for sid in wf.source_ids:
            assert sid in base.sources, f"{wf.id} 引用了不存在的来源 {sid}"

    redeem = base.workflows["wf_wealth_redeem_then_transfer"]
    joined = " | ".join(redeem.transitions)
    assert "redeem_fund -> query_balance" in joined
    assert "query_balance -> transfer" in joined
    # 依赖顺序不能反：赎回必须先于重新查余额，查余额必须先于转账。
    assert joined.index("redeem_fund -> query_balance") < joined.index("query_balance -> transfer")


def test_sources_all_resolvable_and_registered() -> None:
    """34 条来源都能被解析，且引用的来源 id 全部能在 sources.yaml 里找到。"""
    base = get_base()

    assert len(base.sources) == 34
    for src in base.sources.values():
        assert src.source_id, "来源缺 source_id"
        assert src.title.strip(), f"{src.source_id} 缺标题"
        assert src.issuer.strip(), f"{src.source_id} 缺发布机构"
        assert src.status in SOURCE_STATUSES
        assert src.rule_level in RULE_LEVELS
        assert src.title and src.issuer

    referenced: set[str] = set()
    for term in base.terms.values():
        referenced.update(term.source_ids)
    for entity in base.entities.values():
        referenced.update(entity.source_ids)
    for rel in base.relations.values():
        referenced.update(rel.source_ids)
    for rule in base.intents.values():
        referenced.update(rule.source_ids)
    for wf in base.workflows.values():
        referenced.update(wf.source_ids)

    missing = referenced - set(base.sources)
    assert missing == set(), f"知识条目引用了未登记来源：{sorted(missing)}"


def test_load_is_deterministic() -> None:
    """同一个目录加载两次，版本号必须一致（版本是内容哈希，不是时间戳）。"""
    first = load(KNOWLEDGE_DIR)
    second = load(KNOWLEDGE_DIR)

    assert first.version == second.version
    assert set(first.terms) == set(second.terms)
