"""银行领域知识层的加载与校验。

对应文档：docs/11_知识层设计.md 2.2

设计要点（任务书阶段 3.3）：
1. 只做**结构化匹配 + 关键词检索 + 元数据筛选**，不引入向量库（原则第 4 条）。
2. 结构性错误（YAML 语法错、缺必填字段、id 重复）→ 直接抛 ValidationError，**fail closed**：
   知识层坏掉时不能让系统悄悄按「没有知识」的方式继续放行。
3. 内容性问题（来源未核实、引用悬空、别名冲突、来源过期）→ 收集成 warning，
   由调用方决定降级，不阻断服务。
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Iterable

import yaml

from app.clock import ValidationError, now_ms
from app.knowledge.models import KnowledgeBase, KnowledgeWarning, Term
from app.knowledge.parsers import (
    parse_entities,
    parse_intents,
    parse_relations,
    parse_sources,
    parse_terms,
    parse_workflows,
)

# 知识文件放在仓库根的 knowledge/ 下，与 app/ 平级，方便人工审阅与版本管理
ROOT = Path(__file__).resolve().parents[2]
KNOWLEDGE_DIR = ROOT / "knowledge"

# 文件名 → 知识类别。sources.yaml 由阶段 2 的资料核验产出，其余由阶段 3 产出。
FILE_MAP: dict[str, str] = {
    "terms": "terms.yaml",
    "entities": "ontology.yaml",
    "intents": "intent_rules.yaml",
    "workflows": "workflows.yaml",
    "sources": "sources.yaml",
}

# 来源超过这个天数没重新核验就算过期
STALE_AFTER_DAYS = 180


def _read_yaml(path: Path) -> Any:
    """读一个 YAML 文件。语法错直接抛 ValidationError。"""
    if not path.exists():
        raise ValidationError(f"知识文件缺失：{path.name}")
    try:
        with path.open("r", encoding="utf-8") as fh:
            return yaml.safe_load(fh)
    except yaml.YAMLError as exc:  # YAML 解析失败属于结构性问题，必须 fail closed
        raise ValidationError(f"知识文件 {path.name} 解析失败：{exc}") from exc


def _version_of(paths: Iterable[Path]) -> str:
    """知识文件版本号：把参与计算的文件内容哈希一遍。

    审计与评估报告要写「当时用的是哪一版知识」，这个值就是那把钥匙。
    """
    digest = hashlib.sha256()
    for path in sorted(paths, key=lambda p: p.name):
        digest.update(path.name.encode("utf-8"))
        digest.update(b"\x00")
        digest.update(path.read_bytes())
        digest.update(b"\x00")
    return digest.hexdigest()[:16]


def _alias_conflicts(terms: dict[str, Term]) -> list[KnowledgeWarning]:
    """同一个别名挂到两个术语上 → 内容冲突，必须报出来。"""
    owner: dict[str, str] = {}
    warnings: list[KnowledgeWarning] = []
    for term in terms.values():
        for alias in (*term.aliases, term.term):
            if not alias:
                continue
            if alias in owner and owner[alias] != term.id:
                warnings.append(
                    KnowledgeWarning(
                        code="KNOW_ALIAS_CONFLICT",
                        detail=f"别名「{alias}」同时指向 {owner[alias]} 与 {term.id}",
                        ref=term.id,
                    )
                )
            else:
                owner[alias] = term.id
    return warnings


def _days_since(date_str: str, today_ms: int) -> int | None:
    """算「距今天多少天」。日期格式不认得就返回 None（不当成过期）。"""
    if not date_str:
        return None
    try:
        import datetime as _dt

        day = _dt.date.fromisoformat(date_str[:10])
        today = _dt.datetime.fromtimestamp(today_ms / 1000, _dt.timezone.utc).date()
        return (today - day).days
    except (ValueError, OverflowError, OSError):
        return None


def _stale_sources(sources: dict) -> list[KnowledgeWarning]:
    """过期或未核验的来源 → 提醒（不阻断，但要能被调用方看见）。"""
    warnings: list[KnowledgeWarning] = []
    today_ms = now_ms()
    for src in sources.values():
        if src.status == "superseded":
            warnings.append(
                KnowledgeWarning(
                    code="KNOW_SOURCE_SUPERSEDED",
                    detail=f"来源已被更现行文件取代：{src.title}",
                    ref=src.source_id,
                )
            )
            continue
        if src.status != "verified":
            warnings.append(
                KnowledgeWarning(
                    code="KNOW_SOURCE_UNVERIFIED",
                    detail=f"来源尚未核验：{src.title}",
                    ref=src.source_id,
                )
            )
            continue
        days = _days_since(src.last_verified_at, today_ms)
        if days is not None and days > STALE_AFTER_DAYS:
            warnings.append(
                KnowledgeWarning(
                    code="KNOW_SOURCE_STALE",
                    detail=f"来源已 {days} 天未重新核验：{src.title}",
                    ref=src.source_id,
                )
            )
    return warnings


def _dangling_refs(base: KnowledgeBase) -> list[KnowledgeWarning]:
    """检查各知识条目引用的来源 id、术语 id、实体 id 是否存在。"""
    warnings: list[KnowledgeWarning] = []
    holders: list[tuple[str, tuple[str, ...]]] = [
        *(("term:" + t.id, t.source_ids) for t in base.terms.values()),
        *(("entity:" + e.id, e.source_ids) for e in base.entities.values()),
        *(("relation:" + r.id, r.source_ids) for r in base.relations.values()),
        *(("intent:" + i.intent, i.source_ids) for i in base.intents.values()),
        *(("workflow:" + w.id, w.source_ids) for w in base.workflows.values()),
    ]
    for holder, source_ids in holders:
        for sid in source_ids:
            if sid not in base.sources:
                warnings.append(
                    KnowledgeWarning(
                        code="KNOW_UNKNOWN_SOURCE",
                        detail=f"{holder} 引用了不存在的来源 {sid}",
                        ref=holder,
                    )
                )
    for term in base.terms.values():
        for ref in term.related:
            if ref not in base.terms:
                warnings.append(
                    KnowledgeWarning(
                        code="KNOW_UNKNOWN_TERM_REF",
                        detail=f"术语 {term.id} 关联了不存在的术语 {ref}",
                        ref=term.id,
                    )
                )
    for rel in base.relations.values():
        for endpoint, name in ((rel.domain, "domain"), (rel.range, "range")):
            if endpoint not in base.entities:
                warnings.append(
                    KnowledgeWarning(
                        code="KNOW_BAD_RELATION_ENDPOINT",
                        detail=f"关系 {rel.id} 的 {name} 指向不存在的实体 {endpoint}",
                        ref=rel.id,
                    )
                )
    return warnings


def load(dir_path: Path | None = None) -> KnowledgeBase:
    """加载整个知识层。

    结构性错误直接抛 ValidationError（fail closed）；
    内容性问题收集成 warnings 一并返回。
    """
    base_dir = Path(dir_path) if dir_path is not None else KNOWLEDGE_DIR
    docs: dict[str, Any] = {}
    paths: list[Path] = []
    for key, filename in FILE_MAP.items():
        path = base_dir / filename
        docs[key] = _read_yaml(path)
        paths.append(path)

    sources = parse_sources(docs["sources"], base_dir / FILE_MAP["sources"])
    base = KnowledgeBase(
        version=_version_of(paths),
        terms=parse_terms(docs["terms"], base_dir / FILE_MAP["terms"]),
        entities=parse_entities(docs["entities"], base_dir / FILE_MAP["entities"]),
        relations=parse_relations(docs["entities"], base_dir / FILE_MAP["entities"]),
        intents=parse_intents(docs["intents"], base_dir / FILE_MAP["intents"]),
        workflows=parse_workflows(docs["workflows"], base_dir / FILE_MAP["workflows"]),
        sources=sources,
    )
    warnings = [
        *_alias_conflicts(base.terms),
        *_stale_sources(base.sources),
        *_dangling_refs(base),
    ]
    return KnowledgeBase(
        version=base.version,
        terms=base.terms,
        entities=base.entities,
        relations=base.relations,
        intents=base.intents,
        workflows=base.workflows,
        sources=base.sources,
        warnings=tuple(warnings),
    )
