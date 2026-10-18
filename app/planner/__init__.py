"""规划层：意图解析 → 固定模板 DAG → 执行器。

对应文档：docs/07_规划器_API与前端规格.md。
依赖方向（docs/01 1.4）：api → planner → policy → tools → db，只允许向下。
"""

from __future__ import annotations

__all__ = ["dag", "executor", "intent"]
