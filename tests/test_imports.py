"""依赖方向铁律：api → planner → policy → tools → db，不得反向依赖。

docs/00 §0 的十条铁律里有一条「policy 不许 import llm，由 tests/test_imports.py 拦」，
docs/02 §2.4 要求每个新增文件登记进本表的允许依赖表。这里把那条铁律自动化：

1. 每个 app/ 下的模块都必须在本文件登记 —— 没登记就是没走 docs/02 §2.4 的三个动作，
   测试直接失败，逼着人补文档而不是绕过文档；
2. 表里的允许依赖必须与代码现状一致（既不许偷偷加依赖，也不许留着过期条目）；
3. 分层只允许「上层引用下层」，policy 不得碰 llm/tools，security 不得碰 planner，
   tools 不得碰 planner/api —— 越界即失败。

解析用 ast 静态读 import，不 import 任何模块，所以本文件本身不会触发循环导入。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

APP_DIR = Path(__file__).resolve().parents[1] / "app"

#: 分层序号：数字越小越底层。上层可以引用下层，反过来一律失败。
LAYERS: dict[str, int] = {
    "app": 0,
    "app.audit": 0,
    "app.clock": 0,
    "app.config": 0,
    "app.db": 0,
    "app.ids": 0,
    "app.log": 0,
    "app.models": 0,
    "app.money": 0,
    "app.security.envelope": 0,
    "app.security.breaker": 1,
    "app.security.injection": 1,
    "app.security.token": 1,
    "app.funds": 2,
    "app.policy.engine": 2,
    "app.policy.rules": 2,
    "app.tools.base": 3,
    "app.tools.bill": 3,
    "app.tools.cards": 3,
    "app.tools.invest": 3,
    "app.tools.products": 3,
    "app.tools.tool_specs": 3,
    "app.tools.queries": 3,
    "app.tools.registry": 3,
    "app.tools.subs": 3,
    "app.tools.transfers": 3,
    "app.planner.dag": 4,
    "app.planner.events": 4,
    "app.planner.executor": 4,
    "app.planner.intent": 4,
    "app.planner.runner": 4,
    "app.planner.state": 4,
    "app.api.routes": 5,
    "app.api.schemas": 5,
    "app.main": 5,
    # 包本身没有逻辑，聚合再导出不算越层
    "app.api": 5,
    "app.planner": 4,
    "app.policy": 2,
    "app.security": 1,
    "app.tools": 3,
}

#: 模型层是独立叶子：只有这几处允许碰它（意图解析、API 装配、冒烟脚本）。
LLM_ALLOWED: frozenset[str] = frozenset(
    {"app.planner.intent", "app.api.routes", "app.main", "app.smoke"}
)

#: 允许依赖表：模块 → 它可以 import 的 app 内模块（由代码现状反推，改代码就要改这张表）。
ALLOWED: dict[str, frozenset[str]] = {
    "app": frozenset(),
    "app.api": frozenset(),
    "app.api.routes": frozenset({
        "app.api.schemas",
        "app.config",
        "app.ids",
        "app.llm",
        "app.models",
        "app.planner",
        "app.tools.registry",
    }),
    "app.api.schemas": frozenset(),
    "app.audit": frozenset({
        "app.clock",
        "app.log",
    }),
    "app.clock": frozenset(),
    "app.config": frozenset(),
    "app.db": frozenset({
        "app.clock",
    }),
    "app.funds": frozenset({
        "app.clock",
        "app.db",
        "app.ids",
        "app.log",
        "app.models",
        "app.security",
    }),
    "app.ids": frozenset({
        "app.clock",
    }),
    "app.llm": frozenset(),
    "app.log": frozenset({
        "app.clock",
    }),
    "app.main": frozenset({
        "app.api",
        "app.clock",
        "app.llm",
    }),
    "app.models": frozenset(),
    "app.money": frozenset({
        "app.clock",
    }),
    "app.planner": frozenset(),
    "app.planner.dag": frozenset({
        "app.ids",
        "app.models",
        "app.planner.intent",
        "app.tools.base",
        "app.tools.registry",
    }),
    "app.planner.events": frozenset({
        "app.models",
        "app.money",
        "app.planner.state",
        "app.policy.engine",
    }),
    "app.planner.executor": frozenset({
        "app.clock",
        "app.models",
        "app.planner",
        "app.planner.events",
        "app.planner.runner",
        "app.planner.state",
        "app.policy.engine",
        "app.policy.rules",
        "app.security",
        "app.tools",
    }),
    "app.planner.intent": frozenset({
        "app.llm",
        "app.money",
        "app.security",
    }),
    "app.planner.runner": frozenset({
        "app.clock",
        "app.models",
        "app.planner",
        "app.planner.events",
        "app.planner.state",
        "app.policy.engine",
        "app.policy.rules",
        "app.security",
        "app.tools",
    }),
    "app.planner.state": frozenset({
        "app.clock",
        "app.ids",
        "app.models",
        "app.money",
        "app.security",
        "app.tools",
    }),
    "app.policy": frozenset({
        "app.policy.engine",
        "app.policy.rules",
    }),
    "app.policy.engine": frozenset({
        "app.clock",
        "app.models",
        "app.money",
        "app.policy.rules",
    }),
    "app.policy.rules": frozenset({
        "app.models",
        "app.money",
    }),
    "app.security": frozenset(),
    "app.security.breaker": frozenset({
        "app.clock",
        "app.ids",
        "app.log",
    }),
    "app.security.envelope": frozenset({
        "app.clock",
    }),
    "app.security.injection": frozenset({
        "app.log",
        "app.models",
    }),
    "app.security.token": frozenset({
        "app.clock",
        "app.ids",
        "app.log",
        "app.models",
    }),
    "app.smoke": frozenset(),
    "app.tools": frozenset({
        "app.tools.base",
        "app.tools.registry",
    }),
    "app.tools.base": frozenset({
        "app.clock",
        "app.funds",
        "app.models",
    }),
    "app.tools.bill": frozenset({
        "app.clock",
        "app.models",
        "app.money",
        "app.tools.base",
        "app.tools.queries",
    }),
    "app.tools.cards": frozenset({
        "app.models",
        "app.money",
        "app.tools.base",
    }),
    "app.tools.invest": frozenset({
        "app.clock",
        "app.db",
        "app.funds",
        "app.log",
        "app.models",
        "app.money",
        "app.tools.base",
        "app.tools.products",
    }),
    "app.tools.products": frozenset({
        "app.clock",
        "app.models",
        "app.money",
        "app.tools.base",
    }),
    "app.tools.queries": frozenset({
        "app.clock",
        "app.models",
        "app.money",
        "app.tools.base",
    }),
    "app.tools.registry": frozenset({
        "app.audit",
        "app.clock",
        "app.db",
        "app.log",
        "app.models",
        "app.tools.base",
        "app.tools.tool_specs",
    }),
    "app.tools.subs": frozenset({
        "app.clock",
        "app.models",
        "app.money",
        "app.tools.base",
    }),
    "app.tools.tool_specs": frozenset({
        "app.models",
        "app.money",
        "app.tools.base",
        "app.tools.bill",
        "app.tools.cards",
        "app.tools.invest",
        "app.tools.products",
        "app.tools.queries",
        "app.tools.subs",
        "app.tools.transfers",
    }),
    "app.tools.transfers": frozenset({
        "app.clock",
        "app.funds",
        "app.models",
        "app.money",
        "app.security",
        "app.tools.base",
    }),
}


def _module_name(path: Path) -> str:
    parts = path.relative_to(APP_DIR).with_suffix("").parts
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(("app", *parts)) if parts else "app"


def _exists(dotted: str) -> bool:
    rel = Path(*dotted.split("."))
    return (APP_DIR / rel).with_suffix(".py").exists() or (APP_DIR / rel / "__init__.py").exists()


def _imports_of(path: Path) -> frozenset[str]:
    """静态读出该文件 import 的全部 app 内模块；不执行 import，所以不会触发循环导入。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    deps: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("app"):
                    deps.add(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("app"):
            base = node.module
            deps.add(base)
            for alias in node.names:
                child = f"{base}.{alias.name}"
                if _exists(child):
                    deps.add(child)
    deps.discard("app")  # 裸包名没有约束力
    return frozenset(deps)


def _actual() -> dict[str, frozenset[str]]:
    return {
        _module_name(path): _imports_of(path)
        for path in sorted(APP_DIR.rglob("*.py"))
    }


def _rank(module: str) -> int:
    """取最长匹配前缀的分层序号；没登记的模块按根包算 0（会被登记测试挡下）。"""
    best, best_len = 0, -1
    for known, level in LAYERS.items():
        if (module == known or module.startswith(known + ".")) and len(known) > best_len:
            best, best_len = level, len(known)
    return best


def test_every_app_module_is_registered() -> None:
    """新增文件必须同时登记进 LAYERS 与 ALLOWED（docs/02 §2.4 的三个动作之一）。"""
    actual = set(_actual())
    missing = sorted(actual - set(ALLOWED))
    assert not missing, f"这些模块没登记进 tests/test_imports.py 的允许依赖表：{missing}"
    stale = sorted(set(ALLOWED) - actual)
    assert not stale, f"允许依赖表里有文件已经不存在的条目：{stale}"


@pytest.mark.parametrize("module", sorted(ALLOWED))
def test_imports_stay_inside_the_allowed_table(module: str) -> None:
    """表内允许的照旧，超出表的一律失败（加依赖要先改表、改文档）。"""
    actual = _actual()[module]
    extra = sorted(actual - ALLOWED[module])
    assert not extra, f"{module} 新增了未登记的依赖：{extra}"
    missing = sorted(ALLOWED[module] - actual)
    assert not missing, f"允许依赖表里列了但代码里没有：{module} → {missing}（清掉过期条目）"


@pytest.mark.parametrize("module", sorted(ALLOWED))
def test_layers_never_depend_upward(module: str) -> None:
    """分层只允许上层引用下层：app/main.py 这类顶层模块不许被底层 import。"""
    own = _rank(module)
    upward = sorted(dep for dep in _actual()[module] if _rank(dep) > own)
    assert not upward, f"{module} 反向依赖了更上层的 {upward}"


def test_policy_layer_never_imports_llm_or_tools() -> None:
    """docs/00 铁律：策略层是纯函数层，不许碰模型与工具。"""
    for module, deps in _actual().items():
        if not module.startswith(("app.policy", "app.policy.")):
            continue
        assert not deps & {"app.llm", "app.tools", "app.tools.registry"}, f"{module} 越界"


def test_security_layer_never_depends_on_planner() -> None:
    """安全层不许知道规划层存在，否则权限判定会被业务编排反向污染。"""
    for module, deps in _actual().items():
        if not module.startswith(("app.security", "app.security.")):
            continue
        assert not deps & {"app.planner", "app.planner.executor", "app.planner.runner"}, module
        assert not deps & {"app.api", "app.api.routes", "app.tools", "app.llm"}, module


def test_tools_never_depend_on_planner_or_api() -> None:
    """工具层只认令牌与参数，不认意图与 HTTP。"""
    for module, deps in _actual().items():
        if not module.startswith(("app.tools", "app.tools.")):
            continue
        banned = {d for d in deps if d.startswith(("app.planner", "app.api"))}
        assert not banned, f"{module} 反向依赖 {sorted(banned)}"
        assert "app.llm" not in deps, f"{module} 不该碰模型层"


def test_planner_never_depends_on_api() -> None:
    """规划层是接口层的下游，反过来 import 会形成环。"""
    for module, deps in _actual().items():
        if not module.startswith(("app.planner", "app.planner.")):
            continue
        assert not deps & {"app.api", "app.api.routes", "app.main"}, module


def test_data_layer_never_depends_on_business_layers() -> None:
    """数据层（clock/db/ids/money/models/audit/log/config）不认识业务。"""
    business = ("app.policy", "app.tools", "app.planner", "app.api", "app.security", "app.funds")
    for module, deps in _actual().items():
        if module not in {"app", "app.audit", "app.clock", "app.config", "app.db",
                          "app.ids", "app.log", "app.models", "app.money"}:
            continue
        bad = sorted(d for d in deps if d.startswith(business))
        assert not bad, f"{module} 引入了业务层依赖 {bad}"


def test_llm_layer_is_a_leaf() -> None:
    """模型层只在意图解析、API 装配与冒烟脚本里出现。"""
    users = sorted(m for m, deps in _actual().items() if "app.llm" in deps)
    assert set(users) <= set(LLM_ALLOWED), f"这些模块不该直接调模型：{users}"
