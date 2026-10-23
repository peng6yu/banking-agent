"""知识 YAML 的结构解析与字段校验。

对应文档：docs/11_知识层设计.md 2.2

为什么单独成文件：loader.py 负责「加载流程 + 内容性告警」，
这里负责「一个字段行怎么变成一个知识对象」，两者关注点不同，
混在一起会把 loader 撑过编码规范的单文件行数上限。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from app.clock import ValidationError
from app.knowledge.models import (
    RULE_LEVELS,
    SOURCE_STATUSES,
    Entity,
    IntentRule,
    Relation,
    Source,
    Term,
    Workflow,
)


def _rows(doc: Any, path: Path, key: str) -> list[dict]:
    """取出一个 YAML 文档里的记录列表，顺手做基本类型检查。"""
    if doc is None:
        return []
    if not isinstance(doc, dict):
        raise ValidationError(f"知识文件 {path.name} 顶层必须是映射")
    rows = doc.get(key)
    if rows is None:
        return []
    if not isinstance(rows, list):
        raise ValidationError(f"知识文件 {path.name} 的 {key} 必须是列表")
    for row in rows:
        if not isinstance(row, dict):
            raise ValidationError(f"知识文件 {path.name} 的 {key} 里出现了非映射条目")
    return rows


def _str_tuple(value: Any, field: str, where: str) -> tuple[str, ...]:
    """把 YAML 里的字符串或字符串列表统一成 tuple。"""
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    if isinstance(value, list) and all(isinstance(v, str) for v in value):
        return tuple(value)
    raise ValidationError(f"{where} 的 {field} 必须是字符串或字符串列表")


def _need(row: dict, field: str, where: str) -> str:
    """取必填字段，缺失即结构性问题。"""
    value = row.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{where} 缺少必填字段 {field}")
    return value.strip()


def _unique_id(row: dict, where: str, seen: set[str], field: str = "id") -> str:
    """取 id 并检查唯一性。"""
    value = _need(row, field, where)
    if value in seen:
        raise ValidationError(f"{where} 的 {field} 重复：{value}")
    seen.add(value)
    return value


def parse_terms(doc: Any, path: Path) -> dict[str, Term]:
    """解析 terms.yaml。"""
    out: dict[str, Term] = {}
    seen: set[str] = set()
    for row in _rows(doc, path, "terms"):
        where = f"terms.yaml 里的 {row.get('id', '<无 id>')}"
        term_id = _unique_id(row, where, seen)
        out[term_id] = Term(
            id=term_id,
            term=_need(row, "term", where),
            aliases=_str_tuple(row.get("aliases"), "aliases", where),
            definition=str(row.get("definition", "") or ""),
            related=_str_tuple(row.get("related"), "related", where),
            scenarios=_str_tuple(row.get("scenarios"), "scenarios", where),
            source_ids=_str_tuple(row.get("source_ids"), "source_ids", where),
            ambiguity=str(row.get("ambiguity", "") or ""),
        )
    return out


def parse_entities(doc: Any, path: Path) -> dict[str, Entity]:
    """解析 ontology.yaml 里的实体。"""
    out: dict[str, Entity] = {}
    seen: set[str] = set()
    for row in _rows(doc, path, "entities"):
        where = f"ontology.yaml 里的 {row.get('id', '<无 id>')}"
        entity_id = _unique_id(row, where, seen)
        out[entity_id] = Entity(
            id=entity_id,
            label=_need(row, "label", where),
            type=_need(row, "type", where),
            definition=str(row.get("definition", "") or ""),
            source_ids=_str_tuple(row.get("source_ids"), "source_ids", where),
        )
    return out


def parse_relations(doc: Any, path: Path) -> dict[str, Relation]:
    """解析 ontology.yaml 里的关系。"""
    out: dict[str, Relation] = {}
    seen: set[str] = set()
    for row in _rows(doc, path, "relations"):
        where = f"ontology.yaml 里的 {row.get('id', '<无 id>')}"
        rel_id = _unique_id(row, where, seen)
        out[rel_id] = Relation(
            id=rel_id,
            label=_need(row, "label", where),
            kind=_need(row, "kind", where),
            domain=_need(row, "domain", where),
            range=_need(row, "range", where),
            check=str(row.get("check", "") or ""),
            source_ids=_str_tuple(row.get("source_ids"), "source_ids", where),
        )
    return out


def parse_intents(doc: Any, path: Path) -> dict[str, IntentRule]:
    """解析 intent_rules.yaml。"""
    out: dict[str, IntentRule] = {}
    seen: set[str] = set()
    for row in _rows(doc, path, "intents"):
        where = f"intent_rules.yaml 里的 {row.get('intent', '<无 intent>')}"
        name = _unique_id(row, where, seen, field="intent")
        out[name] = IntentRule(
            intent=name,
            required_slots=_str_tuple(row.get("required_slots"), "required_slots", where),
            optional_slots=_str_tuple(row.get("optional_slots"), "optional_slots", where),
            ask_when=_str_tuple(row.get("ask_when"), "ask_when", where),
            preconditions=_str_tuple(row.get("preconditions"), "preconditions", where),
            allowed_tools=_str_tuple(row.get("allowed_tools"), "allowed_tools", where),
            forbidden_ops=_str_tuple(row.get("forbidden_ops"), "forbidden_ops", where),
            ambiguity=_str_tuple(row.get("ambiguity"), "ambiguity", where),
            source_ids=_str_tuple(row.get("source_ids"), "source_ids", where),
        )
    return out


def parse_workflows(doc: Any, path: Path) -> dict[str, Workflow]:
    """解析 workflows.yaml。"""
    out: dict[str, Workflow] = {}
    seen: set[str] = set()
    for row in _rows(doc, path, "workflows"):
        where = f"workflows.yaml 里的 {row.get('id', '<无 id>')}"
        wf_id = _unique_id(row, where, seen)
        out[wf_id] = Workflow(
            id=wf_id,
            name=_need(row, "name", where),
            preconditions=_str_tuple(row.get("preconditions"), "preconditions", where),
            nodes=_str_tuple(row.get("nodes"), "nodes", where),
            transitions=_str_tuple(row.get("transitions"), "transitions", where),
            failure_handling=_str_tuple(row.get("failure_handling"), "failure_handling", where),
            required_confirmations=_str_tuple(row.get("required_confirmations"), "required_confirmations", where),
            rules=_str_tuple(row.get("rules"), "rules", where),
            source_ids=_str_tuple(row.get("source_ids"), "source_ids", where),
        )
    return out


def parse_sources(doc: Any, path: Path) -> dict[str, Source]:
    """解析 sources.yaml。"""
    out: dict[str, Source] = {}
    seen: set[str] = set()
    for row in _rows(doc, path, "sources"):
        where = f"sources.yaml 里的 {row.get('source_id', '<无 source_id>')}"
        sid = _unique_id(row, where, seen, field="source_id")
        status = str(row.get("status", "unverified") or "unverified")
        if status not in SOURCE_STATUSES:
            raise ValidationError(f"{where} 的 status 非法：{status}")
        level = str(row.get("rule_level", "contest_simulation") or "contest_simulation")
        if level not in RULE_LEVELS:
            raise ValidationError(f"{where} 的 rule_level 非法：{level}")
        out[sid] = Source(
            source_id=sid,
            title=_need(row, "title", where),
            issuer=_need(row, "issuer", where),
            published_at=str(row.get("published_at", "") or ""),
            effective_from=str(row.get("effective_from", "") or ""),
            source_url=str(row.get("source_url", "") or ""),
            sections=_str_tuple(row.get("sections"), "sections", where),
            applicable_scenarios=_str_tuple(row.get("applicable_scenarios"), "applicable_scenarios", where),
            last_verified_at=str(row.get("last_verified_at", "") or ""),
            status=status,
            rule_level=level,
            limitations=_str_tuple(row.get("limitations"), "limitations", where),
            notes=str(row.get("notes", "") or ""),
        )
    return out
