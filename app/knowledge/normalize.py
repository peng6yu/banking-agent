"""业务术语规范化。

对应文档：docs/11_知识层设计.md 2.3

为什么需要它：用户说的是「打款」「划钱」「把那笔转过去」，
模型看到的是同一个意思的十几种写法。术语表把口语映射回标准术语，
让意图识别、参数抽取和后续检索都建立在同一套词上，
同时把「这句到底指哪个业务概念」的歧义显式记下来。
"""

from __future__ import annotations

from dataclasses import dataclass

from app.knowledge.models import KnowledgeBase, Term


@dataclass(frozen=True)
class Normalized:
    """一次规范化的结果。"""

    text: str
    term_ids: tuple[str, ...] = ()
    matches: tuple[tuple[str, str], ...] = ()  # (原文里的说法, 术语 id)
    ambiguous: tuple[str, ...] = ()  # 命中但术语自带歧义说明的 id


def _alias_pairs(base: KnowledgeBase) -> list[tuple[str, str]]:
    """列出所有（说法, 术语 id）。长的说法优先替换，避免「代扣协议」被「代扣」先吃掉。"""
    pairs: list[tuple[str, str]] = []
    for term in base.terms.values():
        for alias in term.aliases:
            if alias and alias != term.term:
                pairs.append((alias, term.id))
    pairs.sort(key=lambda p: (-len(p[0]), p[0]))
    return pairs


def normalize(base: KnowledgeBase, text: str) -> Normalized:
    """把一段话里的口语说法换成标准术语。

    只做字面替换，不做语义推断——语义判断仍然交给模型，
    这里负责把「同一件事的不同说法」收敛掉。
    """
    if not text:
        return Normalized(text=text)
    out = text
    matches: list[tuple[str, str]] = []
    for alias, term_id in _alias_pairs(base):
        if alias in out:
            term = base.terms.get(term_id)
            if term is None:
                continue
            out = out.replace(alias, term.term)
            matches.append((alias, term_id))
    hits = tuple(dict.fromkeys(tid for _, tid in matches))
    ambiguous = tuple(tid for tid in hits if base.terms[tid].ambiguity)
    return Normalized(text=out, term_ids=hits, matches=tuple(matches), ambiguous=ambiguous)


def terms_in_text(base: KnowledgeBase, text: str) -> tuple[Term, ...]:
    """取文本里命中的术语对象（按命中数从多到少排）。"""
    hits = normalize(base, text)
    counts: dict[str, int] = {}
    for _, tid in hits.matches:
        counts[tid] = counts.get(tid, 0) + 1
    ranked = sorted(hits.term_ids, key=lambda tid: (-counts.get(tid, 0), tid))
    return tuple(base.terms[tid] for tid in ranked if tid in base.terms)


def scenarios_of(base: KnowledgeBase, term_ids: tuple[str, ...]) -> tuple[str, ...]:
    """把一组术语 id 映射到它们涉及的业务场景（去重）。"""
    out: list[str] = []
    for tid in term_ids:
        term = base.terms.get(tid)
        if term is None:
            continue
        for scenario in term.scenarios:
            if scenario not in out:
                out.append(scenario)
    return tuple(out)
