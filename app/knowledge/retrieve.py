"""知识检索：按意图、场景与关键词挑出相关业务规则。

对应文档：docs/11_知识层设计.md 2.4

检索口径（任务书阶段 3.3）：
- 第一版只用**结构化匹配 + 关键词 + 元数据筛选**，不引入向量检索。
  理由：当前知识条目量级是几十条，关键词 + 场景过滤的召回已经够用，
  加向量库要引入新依赖、新索引构建与新的失效模式（任务书原则第 4 条）。
- 返回值必须带 knowledge_ids 与来源，调用方要能把「答案用了哪条知识」写进审计。
"""

from __future__ import annotations

from app.knowledge.models import IntentRule, KnowledgeBase, KnowledgeWarning, Retrieved, Source, Term, Workflow


def _match_terms(base: KnowledgeBase, scenario: str | None, keywords: tuple[str, ...]) -> tuple[Term, ...]:
    """按场景与关键词挑术语。"""
    picked: list[Term] = []
    for term in base.terms.values():
        hit = False
        if scenario and scenario in term.scenarios:
            hit = True
        if not hit and keywords:
            haystack = " ".join((term.term, term.definition, *term.aliases))
            hit = any(kw and kw in haystack for kw in keywords)
        if hit:
            picked.append(term)
    return tuple(picked)


def _match_workflows(base: KnowledgeBase, intent_rule: IntentRule | None, scenario: str | None) -> tuple[Workflow, ...]:
    """挑相关工作流：意图允许的工具与工作流节点有交集，或场景名对得上。"""
    tools = set(intent_rule.allowed_tools) if intent_rule else set()
    picked: list[Workflow] = []
    for wf in base.workflows.values():
        hit = bool(tools & set(wf.nodes))
        if not hit and scenario:
            hit = scenario in wf.id.lower() or scenario in wf.name.lower()
        if hit:
            picked.append(wf)
    return tuple(picked)


def _collect_sources(base: KnowledgeBase, ref_ids: tuple[str, ...]) -> tuple[Source, ...]:
    """把条目引用的来源 id 解析成来源对象（去重，保留登记顺序）。"""
    out: list[Source] = []
    for sid in ref_ids:
        src = base.sources.get(sid)
        if src is not None and src not in out:
            out.append(src)
    return tuple(out)


def _source_refs(base: KnowledgeBase, items: tuple) -> tuple[str, ...]:
    """收集一批条目引用到的全部来源 id。"""
    refs: list[str] = []
    for item in items:
        for sid in item.source_ids:
            if sid not in refs:
                refs.append(sid)
    return tuple(refs)


def retrieve(
    base: KnowledgeBase,
    *,
    intent: str | None = None,
    scenario: str | None = None,
    keywords: tuple[str, ...] = (),
    top_k: int = 8,
) -> Retrieved:
    """检索一次知识。

    intent 决定取哪条意图规则；scenario 与 keywords 决定取哪些术语与工作流。
    top_k 限制术语条数，避免把整份知识表塞进模型上下文。
    """
    warnings: list[KnowledgeWarning] = list(base.warnings)
    intent_rule = base.intents.get(intent) if intent else None
    if intent and intent_rule is None:
        warnings.append(
            KnowledgeWarning(
                code="KNOW_NO_INTENT_RULE",
                detail=f"意图 {intent} 没有对应的业务规则条目",
                ref=intent,
            )
        )
    terms = _match_terms(base, scenario, keywords)[:top_k]
    workflows = _match_workflows(base, intent_rule, scenario)
    refs: list[str] = []
    if intent_rule is not None:
        refs.extend(intent_rule.source_ids)
    for item in (*terms, *workflows):
        refs.extend(item.source_ids)
    sources = _collect_sources(base, tuple(dict.fromkeys(refs)))
    knowledge_ids = tuple(
        ([intent_rule.knowledge_id] if intent_rule is not None else [])
        + [t.id for t in terms]
        + [w.id for w in workflows]
    )
    return Retrieved(
        intent_rule=intent_rule,
        terms=terms,
        workflows=workflows,
        sources=sources,
        knowledge_ids=knowledge_ids,
        warnings=tuple(warnings),
    )
