"""银行领域知识层的数据结构。

对应文档：docs/11_知识层设计.md 2.1
本模块只放**不可变数据结构**，不放加载与检索逻辑（那两块在 loader.py / retrieve.py）。
所有 dataclass 都是 frozen：知识一旦加载进内存就不许被调用方就地改写，
避免「检索结果被上游悄悄篡改」这类难查的问题。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping


# 知识条目在来源登记里的三类归属。区分它们是任务书阶段 2 的硬要求：
# 法条要求、某家银行的公开规则、竞赛原型自设的模拟规则，三者权威性不同。
RULE_LEVELS = ("law", "bank_public", "contest_simulation")

# 来源核验状态。unverified 的来源照样可以进知识层，但必须带 warnings，
# 且调用方要按「未核实」降级处理（少拿权限、该追问就追问）。
SOURCE_STATUSES = ("verified", "unverified", "superseded")


@dataclass(frozen=True)
class Term:
    """一个业务术语。

    id 是稳定引用：审计与评估报告都靠它回指「当时用的是哪条知识」。
    """

    id: str
    term: str
    aliases: tuple[str, ...] = ()
    definition: str = ""
    related: tuple[str, ...] = ()
    scenarios: tuple[str, ...] = ()
    source_ids: tuple[str, ...] = ()
    ambiguity: str = ""


@dataclass(frozen=True)
class Entity:
    """本体里的一个实体（用户、账户、理财产品……）。"""

    id: str
    label: str
    type: str
    definition: str = ""
    source_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class Relation:
    """本体里的一条关系（用户拥有账户、赎回关联理财产品……）。"""

    id: str
    label: str
    kind: str
    domain: str
    range: str
    check: str = ""
    source_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class IntentRule:
    """某个意图的业务规则。

    意图名必须与 app.planner.intent.INTENT_NAMES 对齐，
    但本模块不 import 规划器（会形成循环依赖），对齐由调用方与测试保证。
    """

    intent: str
    required_slots: tuple[str, ...] = ()
    optional_slots: tuple[str, ...] = ()
    ask_when: tuple[str, ...] = ()
    preconditions: tuple[str, ...] = ()
    allowed_tools: tuple[str, ...] = ()
    forbidden_ops: tuple[str, ...] = ()
    ambiguity: tuple[str, ...] = ()
    source_ids: tuple[str, ...] = ()

    @property
    def knowledge_id(self) -> str:
        """知识条目的稳定引用 id（审计回指用）。"""
        return f"intent:{self.intent}"


@dataclass(frozen=True)
class Workflow:
    """一个跨节点业务流程的约束。

    nodes 里写的是工具名，transitions 里写的是「A 完成后才能 B」这类依赖。
    用途是让建图阶段能拒绝「先赎回理财再转账」被压成两个可并行调用的节点。
    """

    id: str
    name: str
    preconditions: tuple[str, ...] = ()
    nodes: tuple[str, ...] = ()
    transitions: tuple[str, ...] = ()
    failure_handling: tuple[str, ...] = ()
    required_confirmations: tuple[str, ...] = ()
    rules: tuple[str, ...] = ()
    source_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class Source:
    """一条权威来源登记。

    rule_level 决定这条依据在回答用户时能被说到什么程度：
    law 可以说「法规要求」，bank_public 只能说「某家银行的公开规则是」，
    contest_simulation 只能说「本原型设定为」。
    """

    source_id: str
    title: str
    issuer: str
    published_at: str = ""
    effective_from: str = ""
    source_url: str = ""
    sections: tuple[str, ...] = ()
    applicable_scenarios: tuple[str, ...] = ()
    last_verified_at: str = ""
    status: str = "unverified"
    rule_level: str = "contest_simulation"
    limitations: tuple[str, ...] = ()
    notes: str = ""


@dataclass(frozen=True)
class KnowledgeWarning:
    """加载或检索期发现的问题。

    warning 不是异常：知识层要能在「部分资料缺失」时继续工作，
    但必须把问题显式带出来，让调用方降级而不是假装没这回事。
    """

    code: str
    detail: str
    ref: str = ""


@dataclass(frozen=True)
class KnowledgeBase:
    """加载完成的整个知识层。"""

    version: str
    terms: Mapping[str, Term] = field(default_factory=dict)
    entities: Mapping[str, Entity] = field(default_factory=dict)
    relations: Mapping[str, Relation] = field(default_factory=dict)
    intents: Mapping[str, IntentRule] = field(default_factory=dict)
    workflows: Mapping[str, Workflow] = field(default_factory=dict)
    sources: Mapping[str, Source] = field(default_factory=dict)
    warnings: tuple[KnowledgeWarning, ...] = ()

    def source(self, source_id: str) -> Source | None:
        """按 id 取来源；取不到返回 None（不抛，调用方决定怎么降级）。"""
        return self.sources.get(source_id)

    def terms_for_scenario(self, scenario: str) -> tuple[Term, ...]:
        """取某个业务场景下的全部术语。"""
        return tuple(t for t in self.terms.values() if scenario in t.scenarios)


@dataclass(frozen=True)
class Retrieved:
    """一次检索的结果。

    knowledge_ids 记录「本次回答引用了哪些知识条目」，审计要落它，
    这样每条结论都能回指到具体条目与来源。
    """

    intent_rule: IntentRule | None
    terms: tuple[Term, ...] = ()
    workflows: tuple[Workflow, ...] = ()
    sources: tuple[Source, ...] = ()
    knowledge_ids: tuple[str, ...] = ()
    warnings: tuple[KnowledgeWarning, ...] = ()

    @property
    def has_unverified_source(self) -> bool:
        """结果里是否混进了未核实的来源——调用方据此降级。"""
        return any(s.status != "verified" for s in self.sources)

    def source_ids(self) -> tuple[str, ...]:
        """本次结果引用到的来源 id。"""
        return tuple(dict.fromkeys(s.source_id for s in self.sources))
