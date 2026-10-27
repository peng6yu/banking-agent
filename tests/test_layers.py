"""分层规矩的可执行版：API 只传话、领域不借道、策略层不碰模型与工具。

W1-A 立规矩（docs/00 §0 铁律的可执行补充）。三组规矩都用 ast 静态读，不 import 被测模块：

1. **API 层只传话**（app/api/**）：路由/视图不许自己判定「这件事能不能做」——
   decide()、build_action()、can_confirm_here()、can_verify_here() 一律只能在
   app/planner 或 app/policy 里出现。API 里出现这些词，意味着同一件事有两套判定，
   改一处漏一处，前端还会照着其中一套写按钮。
2. **领域模块不借道 L5**（app/plans.py、app/subscriptions.py、app/funds.py、
   app/proactive.py、app/handoffs.py、app/birthday.py）：领域逻辑不许 import
   app.planner / app.tools 反向借道编排层与工具层。存量债登记在 DOMAIN_WHITELIST，
   每条都带 TODO 与消债阶段；新增一条就失败，债还清了不删也失败。
3. **策略层是纯函数层**（app/policy/**）：不许碰 app.llm / app.planner / app.tools / app.db。

失败信息里点名 文件:行 —— 不用再回来问「到底哪一行」。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from tests.test_imports import _actual, _exists

APP_DIR = Path(__file__).resolve().parents[1] / "app"
API_DIR = APP_DIR / "api"
#: W4 起视图装配搬到 app/views/，但「只往外说事实、不在这里判定」的规矩跟着文件走：
#: 扫描范围同时覆盖 api 与 views，否则把代码挪个位置就悄悄绕过了这条铁律。
VIEWS_DIR = APP_DIR / "views"

#: API 层不许出现的判定入口。前两个是「做决定」，后两个是「能不能做」。
POLICY_DECISION_TOKENS: tuple[str, ...] = (
    "decide(",
    "build_action(",
    "can_confirm_here",
    "can_verify_here",
)

#: 文本级命中的白名单：只允许「响应 JSON 的字段名」，不允许真正的判定调用。
#: 出处（2026-10-26 W4 视图归位后快照）：
#:   app/views/queue.py:147  "can_confirm_here": not expired    ← 响应字段
#:   app/views/queue.py:152  "can_verify_here": not expired     ← 响应字段
#:   app/views/queue.py:289  "can_verify_here": bool(...)       ← 响应字段
#: 这些是给前端看的布尔字段名，不是策略调用；真要判定仍走 planner/policy。
#: 视图已经搬出 api 层，但字段名是前端契约（web/index.html 直接读），所以豁免跟着
#: 文件一起挪到这里，而不是随 business.py 一起消失。
API_TEXT_WHITELIST: dict[tuple[str, str], int] = {
    ("app/views/queue.py", "can_confirm_here"): 1,
    ("app/views/queue.py", "can_verify_here"): 2,
}

#: 调用级（ast 解析出的真实 Call）零容忍：一条都不许。
API_CALL_WHITELIST: dict[tuple[str, str], str] = {}

#: 领域模块：这些文件承载业务规则，不该反向认识编排层/工具层。
DOMAIN_MODULES: tuple[str, ...] = (
    "app.plans",
    "app.subscriptions",
    "app.funds",
    "app.proactive",
    "app.handoffs",
    "app.birthday",
)

#: 领域模块 → 编排层（app.planner*）的存量债。每条都要写清消债阶段。
DOMAIN_PLANNER_WHITELIST: dict[tuple[str, str], str] = {
    ("app.handoffs", "app.planner"): (
        "接管队列把「恢复」交给 planner.recover。TODO(W4)：handoffs 拆成 domain/use_cases 后只依赖 use_cases，本行删除。"
    ),
    ("app.handoffs", "app.planner.recover"): (
        "重启恢复要重建 RunState。TODO(W4)：恢复逻辑下沉成 use_cases 的纯函数后由 handoffs 调 use_cases，本行删除。"
    ),
    ("app.handoffs", "app.planner.runner"): (
        "接管后要继续跑原 run。TODO(W4)：runner 变成 use_cases 编排后改注入接口，本行删除。"
    ),
    ("app.birthday", "app.planner"): (
        "生日计划要建/推进 DAG。TODO(W4)：birthday 拆成 domain（计划规则）+ use_cases（编排）后，domain 不 import planner，本行删除。"
    ),
    ("app.birthday", "app.planner.dag"): (
        "预算预留要落成计划步。TODO(W4)：改由 use_cases 组装 DAG，本行删除。"
    ),
    ("app.birthday", "app.planner.executor"): (
        "到期付款要走确认/令牌路径。TODO(W4)：改由 use_cases 调 executor，本行删除。"
    ),
}

#: 领域模块 → 工具层（app.tools*）的存量债。
DOMAIN_TOOLS_WHITELIST: dict[tuple[str, str], str] = {
    ("app.birthday", "app.tools.base"): (
        "商户白名单复用工具层常量。TODO(W4)：常量下沉到 domain/constants 后删除本行。"
    ),
    ("app.birthday", "app.tools.transfers"): (
        "到期付款构造转账 action。TODO(W4)：action 构造下沉到 use_cases 后删除本行。"
    ),
}

POLICY_BANNED_PREFIXES: tuple[str, ...] = ("app.llm", "app.planner", "app.tools", "app.db")


def _rel(path: Path) -> str:
    """失败信息里点名用仓库相对路径（不带 ../ 噪音）。"""
    return path.relative_to(APP_DIR.parent).as_posix()


def _api_files() -> list[Path]:
    return sorted(API_DIR.rglob("*.py")) + sorted(VIEWS_DIR.rglob("*.py"))


def _code_lines(path: Path) -> list[tuple[int, str]]:
    """返回 (行号, 原文)，跳过纯注释行 —— 注释里提一句禁令不该被判违规。"""
    out = []
    for lineno, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if raw.lstrip().startswith("#"):
            continue
        out.append((lineno, raw))
    return out


def _token_hits(path: Path, token: str) -> list[int]:
    return [lineno for lineno, raw in _code_lines(path) if token in raw]


def _called_names(path: Path) -> list[tuple[int, str]]:
    """ast 读出所有被调用的名字（含 xxx.yyy() 的末段），带行号。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name):
            found.append((node.lineno, func.id))
        elif isinstance(func, ast.Attribute):
            found.append((node.lineno, func.attr))
    return found


def _imports_with_lines(path: Path) -> list[tuple[int, str]]:
    """ast 读出该文件 import 的 app 内模块（含 from app import x 的子模块），带行号。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("app"):
                    found.append((node.lineno, alias.name))
        elif isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("app"):
            found.append((node.lineno, node.module))
            for alias in node.names:
                child = f"{node.module}.{alias.name}"
                if _exists(child):
                    found.append((node.lineno, child))
    return found


# --------------------------------------------------------------------------- #
# 规矩 1：API 层只传话
# --------------------------------------------------------------------------- #
def test_api_layer_never_speaks_policy_decision_tokens() -> None:
    """文本级：app/api/** 里出现判定入口，就是把策略复制了一份到视图层。"""
    violations: list[str] = []
    for path in _api_files():
        rel = _rel(path)
        for token in POLICY_DECISION_TOKENS:
            hits = _token_hits(path, token)
            allowed = API_TEXT_WHITELIST.get((rel, token), 0)
            for lineno in hits[allowed:]:
                violations.append(
                    f"{rel}:{lineno} 出现 {token!r}。"
                    "API 层只传话：判定请搬进 app/planner（能不能做）或 app/policy（该不该做），"
                    "路由只拿结论；确属响应字段名，请登记进 API_TEXT_WHITELIST 并写清为什么不是判定。"
                )
    assert not violations, "API 层越权判定：\n" + "\n".join(violations)


def test_api_layer_never_calls_policy_decisions() -> None:
    """ast 级：真实调用一条都不许（白名单默认空，放开必须写理由）。"""
    violations: list[str] = []
    for path in _api_files():
        rel = _rel(path)
        for lineno, name in _called_names(path):
            if name not in {"decide", "build_action", "can_confirm_here", "can_verify_here"}:
                continue
            if (rel, name) in API_CALL_WHITELIST:
                continue
            violations.append(
                f"{rel}:{lineno} 调用了 {name}()。"
                "API 层不许自己做判定 —— 同一个问题在 planner/policy 已经答过一次，"
                "两处答案迟早不一致。改成从 planner 的结果里取字段。"
            )
    assert not violations, "API 层越权调用：\n" + "\n".join(violations)


def test_api_text_whitelist_is_not_stale() -> None:
    """白名单是债：先修掉命中就把条目删掉，否则它以后会悄悄放过新违规。"""
    stale: list[str] = []
    by_file = {_rel(p): p for p in _api_files()}
    for (rel, token), expected in API_TEXT_WHITELIST.items():
        path = by_file.get(rel)
        if path is None:
            stale.append(f"{rel}：文件不存在了（{token}）")
            continue
        actual = len(_token_hits(path, token))
        if actual < expected:
            stale.append(f"{rel}：{token} 期望 {expected} 处豁免，实际只有 {actual} 处 —— 修完请删条目")
    assert not stale, "过期白名单：\n" + "\n".join(stale)


# --------------------------------------------------------------------------- #
# 规矩 2：领域模块不借道 L5
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("module", DOMAIN_MODULES)
def test_domain_module_never_borrows_planner(module: str) -> None:
    """领域逻辑不许 import app.planner*（存量债在 DOMAIN_PLANNER_WHITELIST，带 TODO）。"""
    actual = _actual()
    assert module in actual, f"{module} 不存在了？本测试的领域模块清单要跟着改：{sorted(actual)}"
    deps = sorted(d for d in actual[module] if d == "app.planner" or d.startswith("app.planner."))
    new = [dep for dep in deps if (module, dep) not in DOMAIN_PLANNER_WHITELIST]
    assert not new, (
        f"{module} 反向 import 了编排层：{new}。"
        "领域模块只该认识数据层与自己的规则；需要编排就让上层来调你。"
        "修法：把需要 planner 的那一段搬到 app/planner 的 use_cases 里，领域只提供纯函数；"
        "确属 W4 才拆的存量债，进 DOMAIN_PLANNER_WHITELIST 并写清消债阶段。"
    )


@pytest.mark.parametrize("module", DOMAIN_MODULES)
def test_domain_module_never_borrows_tools(module: str) -> None:
    """领域逻辑不许 import app.tools*（工具的入参由编排层拼，领域不该知道工具长什么样）。"""
    actual = _actual()
    deps = sorted(d for d in actual[module] if d == "app.tools" or d.startswith("app.tools."))
    new = [dep for dep in deps if (module, dep) not in DOMAIN_TOOLS_WHITELIST]
    assert not new, (
        f"{module} 反向 import 了工具层：{new}。"
        "修法：常量下沉到 domain/constants，action 构造交给编排层；"
        "确属存量债才进 DOMAIN_TOOLS_WHITELIST，并写清消债阶段。"
    )


def test_domain_whitelist_is_not_stale() -> None:
    """领域白名单同样是债：还清了就删，过期条目会掩盖下一次越层。"""
    actual = _actual()
    stale: list[str] = []
    for (module, dep), _reason in {**DOMAIN_PLANNER_WHITELIST, **DOMAIN_TOOLS_WHITELIST}.items():
        real = actual.get(module, frozenset())
        need = dep == "app.planner" or dep.startswith("app.planner.") or dep == "app.tools" or dep.startswith("app.tools.")
        if dep not in real or not need:
            stale.append(f"{module} → {dep}：代码里已经没有这条依赖，请删条目")
    assert not stale, "过期领域白名单：\n" + "\n".join(stale)


# --------------------------------------------------------------------------- #
# 规矩 3：策略层是纯函数层
# --------------------------------------------------------------------------- #
def test_policy_layer_never_imports_llm_planner_tools_db() -> None:
    """策略层只算「该不该做」，不知道模型、编排、工具与库表存在。"""
    violations: list[str] = []
    for module, deps in _actual().items():
        if not (module == "app.policy" or module.startswith("app.policy.")):
            continue
        for dep in sorted(deps):
            if dep.startswith(POLICY_BANNED_PREFIXES):
                path = APP_DIR / Path(*module.split(".")[1:])
                path = path.with_suffix(".py") if path.with_suffix(".py").exists() else path / "__init__.py"
                line = next(
                    (ln for ln, named in _imports_with_lines(path) if named == dep),
                    "?",
                )
                violations.append(
                    f"{_rel(path)}:{line} import 了 {dep}。"
                    "策略层是纯函数：只依赖 app.models / app.money / app.clock；"
                    "模型调用交给 planner.intent，工具执行交给 planner.executor，落库交给数据层。"
                )
    assert not violations, "策略层越界：\n" + "\n".join(violations)
