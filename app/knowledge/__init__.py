"""银行领域知识层。

对应文档：docs/11_知识层设计.md

这个包是「银行领域知识真正参与 Agent 决策」的落点：
- `load()` 把 knowledge/*.yaml 读进来并做结构校验（结构性错误 fail closed）；
- `normalize()` 把用户口语映射回标准业务术语；
- `retrieve()` 按意图、场景、关键词挑出该轮该用的业务规则与权威依据。

三条硬约束（任何改动都不能破）：
1. 知识层**只影响「问不问、填什么、怎么解释」**，不影响「放不放行」；
   权限裁决永远在 app/policy/ 里，纯函数、模型不可达。
2. 知识层检索到的文本是**数据不是指令**，不得改变权限等级、跳过确认或授权。
3. 知识条目必须带来源；未核实来源要带 warning，调用方据此降级。
"""

from __future__ import annotations

from pathlib import Path

from app.knowledge.loader import KNOWLEDGE_DIR, load
from app.knowledge.models import (
    Entity,
    IntentRule,
    KnowledgeBase,
    KnowledgeWarning,
    Relation,
    Retrieved,
    Source,
    Term,
    Workflow,
)
from app.knowledge.normalize import Normalized, normalize, scenarios_of, terms_in_text
from app.knowledge.retrieve import retrieve

__all__ = [
    "KNOWLEDGE_DIR",
    "Entity",
    "IntentRule",
    "KnowledgeBase",
    "KnowledgeWarning",
    "Normalized",
    "Relation",
    "Retrieved",
    "Source",
    "Term",
    "Workflow",
    "get_base",
    "load",
    "normalize",
    "reset_cache",
    "retrieve",
    "scenarios_of",
    "terms_in_text",
]

# 知识层加载一次就固定下来：同一个进程里反复读文件会让版本号漂移，
# 审计里写的「知识版本」就失去意义了。
_CACHE: KnowledgeBase | None = None


def get_base(dir_path: Path | None = None) -> KnowledgeBase:
    """取已加载的知识层；第一次调用时加载并缓存。"""
    global _CACHE
    if _CACHE is None:
        _CACHE = load(dir_path)
    return _CACHE


def reset_cache() -> None:
    """清掉缓存（测试与「知识文件热更新」用）。"""
    global _CACHE
    _CACHE = None
