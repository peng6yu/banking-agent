#!/usr/bin/env python3.12
"""架构体检报告：把「屎山指标」变成可复现的数字，并给守卫测试提供基线。

用法：
    .venv/bin/python scripts/arch_report.py
    .venv/bin/python scripts/arch_report.py --json
    .venv/bin/python scripts/arch_report.py --write-baseline scripts/arch_baseline.json

口径（改这里就要同步 tests/test_architecture.py 的断言）：
1. 文件行数：物理行数（含空行、注释），范围 app/**/*.py + tests/**/*.py + web/**（index.html / app.css / js/*.js）。
2. 长函数：ast 里每个 FunctionDef/AsyncFunctionDef（含 async、含嵌套、含装饰器行）的
   end_lineno - start + 1；超过 80 行即记录。只统计 app/（运行时代码），测试文件不在此列。
3. 顶层重名：模块顶层（tree.body）的函数名，同名出现在 >1 个模块即记录。
4. 跨层违规：复用 tests/test_imports.py 的 LAYERS/_rank/_actual（唯一真源，不另抄一份），
   依赖方的层序号 < 被依赖方 即反向依赖；不在 ALLOWED 表里的依赖另记一条「未登记依赖」。
5. 前端 id：web/index.html + web/js/*.js 里 $('xxx') / querySelector('#xxx') 的字面量引用 vs
   index.html 里 id="xxx" 的声明；
   JS 动态生成（element.id = '...'）的 id 走 DYNAMIC_ID_WHITELIST 白名单，每条必须写明来历。

稳定可复现：所有列表按固定键排序，基线里不写时间戳，所以同一份工作区永远生成同一份基线。
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP_DIR = ROOT / "app"
TESTS_DIR = ROOT / "tests"
WEB_DIR = ROOT / "web"
WEB_INDEX = WEB_DIR / "index.html"
#: 前端参与行数统计的扩展名：壳、样式表、脚本。别的（截图、字体）不计。
WEB_SUFFIXES = (".html", ".css", ".js")

#: 单个文件的行数硬上限（不在基线里的文件按这个卡）。
FILE_LINE_LIMIT = 600
#: 单个函数的行数上限（超过就是「该拆了」）。
LONG_FUNCTION_LIMIT = 80
#: 基线格式版本；口径变了就 +1，守卫测试会挡下旧基线。
BASELINE_VERSION = 1

#: 前端 id 白名单：这些 id 由 JS 动态创建，HTML 里静态找不到，不算缺失。
#: 每条都必须写清「谁在哪里创建」，否则下一个人无法判断它还成不成立。
DYNAMIC_ID_WHITELIST: dict[str, str] = {
    "plan-run-now": "web/js/84-view-plans.js 的 btn.id = 'plan-run-now'（按计划动态生成的「立刻跑」按钮）",
    "mg-memory-key": "web/js/90-view-manage.js 的 bxMemoryField('mg-memory-key', …)（记忆表单动态字段）",
    "mg-memory-value": "web/js/90-view-manage.js 的 bxMemoryField('mg-memory-value', …)（记忆表单动态字段）",
    "mg-memory-kind": "web/js/90-view-manage.js 的 kindSel.id = 'mg-memory-kind'（记忆类型下拉，动态插入）",
}

#: JS 里取元素的两种写法：$('id') 与 querySelector('#id…')。
_DOLLAR_RE = re.compile(r"""\$\(\s*(['"])(?P<id>[A-Za-z0-9_:.\-]+)\1\s*\)""")
_SELECTOR_RE = re.compile(r"""querySelector(?:All)?\(\s*['"]#(?P<id>[A-Za-z0-9_:\-]+)""")
# 注意 (?<![\w.\-])：JS 里 btn.id = 'x' 这类动态赋值不是「静态声明」，
# 否则自建元素会被算成已声明、白名单也会误报过期，缺失检查就此失效。
_DECLARED_RE = re.compile(r"""(?<![\w.\-])\bid\s*=\s*["'](?P<id>[^"']+)["']""")
_DYNAMIC_RE = re.compile(r"""\.id\s*=\s*['"](?P<id>[^'"]+)['"]""")


# --------------------------------------------------------------------------- 数据结构


@dataclass(frozen=True)
class FunctionSpan:
    """一个函数的物理跨度，报告与守卫都用这一份。"""

    path: str  # 相对仓库根，例如 app/handoffs.py
    qualname: str  # 嵌套函数用点号连接，例如 RunState.resume 或 _helper.inner
    lineno: int
    lines: int

    @property
    def key(self) -> str:
        """基线主键：只含文件与限定名，行号漂移不会让基线失效。"""
        return f"{self.path}::{self.qualname}"


@dataclass
class Metrics:
    """一次扫描的全部指标（report 与 baseline 都从这里派生）。"""

    files: dict[str, int] = field(default_factory=dict)
    long_functions: list[FunctionSpan] = field(default_factory=list)
    per_module_function_counts: dict[str, int] = field(default_factory=dict)
    duplicate_names: dict[str, list[str]] = field(default_factory=dict)
    top_level_function_total: int = 0
    duplicate_extra_total: int = 0
    layer_violations: list[str] = field(default_factory=list)
    unregistered_dependencies: list[str] = field(default_factory=list)
    frontend_declared: int = 0
    frontend_referenced: int = 0
    frontend_missing: list[str] = field(default_factory=list)
    frontend_whitelist_stale: list[str] = field(default_factory=list)
    layer_table_available: bool = True
    layer_table_error: str = ""

    # ---- 派生属性：报告顶部要用，别在渲染处重算
    @property
    def module_count(self) -> int:
        return sum(1 for path in self.files if path.startswith("app/"))

    @property
    def test_file_count(self) -> int:
        return sum(1 for path in self.files if path.startswith("tests/"))

    @property
    def app_total_lines(self) -> int:
        return sum(lines for path, lines in self.files.items() if path.startswith("app/"))

    @property
    def largest_files(self) -> list[tuple[str, int]]:
        return sorted(self.files.items(), key=lambda item: (-item[1], item[0]))


# --------------------------------------------------------------------------- 扫描


def _relative(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def _iter_app_files() -> list[Path]:
    return sorted(APP_DIR.rglob("*.py"))


def _iter_tracked_files() -> list[Path]:
    """参与行数统计的全部文件：app + tests + web/（壳、样式、脚本）。"""
    files = list(_iter_app_files())
    if TESTS_DIR.exists():
        files.extend(sorted(TESTS_DIR.rglob("*.py")))
    if WEB_DIR.exists():
        files.extend(sorted(path for path in WEB_DIR.rglob("*") if path.suffix in WEB_SUFFIXES))
    return sorted(files)


def _count_lines(path: Path) -> int:
    """物理行数：与 wc -l 一致（末尾没有换行也计最后一行）。"""
    return len(path.read_text(encoding="utf-8").splitlines())


def _function_span(node: ast.FunctionDef | ast.AsyncFunctionDef, path: str, qualname: str) -> FunctionSpan:
    start = node.lineno
    if node.decorator_list:
        start = min(start, node.decorator_list[0].lineno)
    end = node.end_lineno or start
    return FunctionSpan(path=path, qualname=qualname, lineno=start, lines=end - start + 1)


def _iter_functions(tree: ast.Module) -> list[tuple[ast.FunctionDef | ast.AsyncFunctionDef, str]]:
    """递归遍历所有函数（含 async、含嵌套、含 if/try/class 里的），返回 (节点, 限定名)。"""
    found: list[tuple[ast.FunctionDef | ast.AsyncFunctionDef, str]] = []

    def walk(node: ast.AST, prefix: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                qualname = f"{prefix}{child.name}"
                found.append((child, qualname))
                walk(child, qualname + ".")
            elif isinstance(child, ast.ClassDef):
                walk(child, f"{prefix}{child.name}.")
            else:
                walk(child, prefix)

    walk(tree, "")
    return found


def _parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def collect() -> Metrics:
    """扫描整个工作区，返回指标快照（不写任何文件）。"""
    metrics = Metrics()

    for path in _iter_tracked_files():
        metrics.files[_relative(path)] = _count_lines(path)

    module_top_names: dict[str, list[str]] = {}
    for path in _iter_app_files():
        rel = _relative(path)
        tree = _parse(path)
        functions = _iter_functions(tree)
        metrics.per_module_function_counts[rel] = len(functions)
        for node, qualname in functions:
            span = _function_span(node, rel, qualname)
            if span.lines > LONG_FUNCTION_LIMIT:
                metrics.long_functions.append(span)
        top_names = [
            node.name
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        ]
        metrics.top_level_function_total += len(top_names)
        for name in top_names:
            module_top_names.setdefault(name, []).append(rel)

    for name, modules in module_top_names.items():
        if len(modules) > 1:
            metrics.duplicate_names[name] = sorted(modules)
    metrics.duplicate_extra_total = sum(len(modules) - 1 for modules in metrics.duplicate_names.values())

    metrics.long_functions.sort(key=lambda span: (-span.lines, span.path, span.qualname, span.lineno))
    _collect_layer(metrics)
    _collect_frontend(metrics)
    return metrics


def _collect_layer(metrics: Metrics) -> None:
    """跨层违规：唯一真源是 tests/test_imports.py（LAYERS/ALLOWED/_actual）。"""
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    try:
        from tests.test_imports import ALLOWED, _actual, _rank
    except Exception as exc:  # pragma: no cover - 只在测试表白手起家/语法坏掉时发生
        metrics.layer_table_available = False
        metrics.layer_table_error = f"{type(exc).__name__}: {exc}"
        return

    actual = _actual()
    for module in sorted(actual):
        own = _rank(module)
        for dep in sorted(actual[module]):
            if _rank(dep) > own:
                metrics.layer_violations.append(f"{module} -> {dep}")
            if module in ALLOWED and dep not in ALLOWED[module]:
                metrics.unregistered_dependencies.append(f"{module} -> {dep}")
    metrics.layer_violations.sort()
    metrics.unregistered_dependencies.sort()


def _collect_frontend(metrics: Metrics) -> None:
    """前端 id：脚本里的引用 vs 壳里的声明，动态生成的走白名单。

    拆分之后（W5）引用散在 web/js/*.js，声明仍只认 index.html——
    因为页面结构只有一份，脚本里 element.id = 'x' 属于动态创建，走白名单。
    """
    if not WEB_INDEX.exists():
        return
    html = WEB_INDEX.read_text(encoding="utf-8")
    js = "\n".join(
        path.read_text(encoding="utf-8") for path in sorted((WEB_DIR / "js").glob("*.js"))
    )
    declared = {match.group("id") for match in _DECLARED_RE.finditer(html)}
    referenced: set[str] = set()
    for pattern in (_DOLLAR_RE, _SELECTOR_RE):
        referenced.update(match.group("id") for match in pattern.finditer(html + "\n" + js))

    metrics.frontend_declared = len(declared)
    metrics.frontend_referenced = len(referenced)
    metrics.frontend_missing = sorted(referenced - declared - set(DYNAMIC_ID_WHITELIST))
    metrics.frontend_whitelist_stale = sorted(
        name
        for name in DYNAMIC_ID_WHITELIST
        if name in declared or name not in referenced
    )


# --------------------------------------------------------------------------- 基线


def build_baseline(metrics: Metrics) -> dict[str, object]:
    """把指标压成基线：只保留守卫测试需要的字段，键全部排序。"""
    return {
        "version": BASELINE_VERSION,
        "limits": {
            "file_lines": FILE_LINE_LIMIT,
            "function_lines": LONG_FUNCTION_LIMIT,
        },
        "counts": {
            "modules": metrics.module_count,
            "test_files": metrics.test_file_count,
            "app_lines": metrics.app_total_lines,
            "top_level_functions": metrics.top_level_function_total,
            "long_functions": len(metrics.long_functions),
            "duplicate_name_groups": len(metrics.duplicate_names),
            "duplicate_name_extra": metrics.duplicate_extra_total,
            "layer_violations": len(metrics.layer_violations),
            "unregistered_dependencies": len(metrics.unregistered_dependencies),
            "frontend_referenced_ids": metrics.frontend_referenced,
            "frontend_declared_ids": metrics.frontend_declared,
            "frontend_missing_ids": len(metrics.frontend_missing),
        },
        "files": dict(sorted(metrics.files.items())),
        "long_functions": dict(sorted((span.key, span.lines) for span in metrics.long_functions)),
        "duplicate_names": {name: len(modules) for name, modules in sorted(metrics.duplicate_names.items())},
        "layer_violations": list(metrics.layer_violations),
        "unregistered_dependencies": list(metrics.unregistered_dependencies),
        "missing_frontend_ids": list(metrics.frontend_missing),
    }


def load_baseline(path: Path = ROOT / "scripts" / "arch_baseline.json") -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


#: 没有基线文件时给的提示，脚本与测试共用一句话，别写两遍。
BASELINE_HINT = (
    ".venv/bin/python scripts/arch_report.py --write-baseline scripts/arch_baseline.json"
)


# --------------------------------------------------------------------------- 渲染


def _limit_for(path: str, baseline: dict[str, object] | None = None) -> int:
    """文件行数上限：max(基线值, FILE_LINE_LIMIT)。

    不在基线里的文件按 FILE_LINE_LIMIT 卡；基线里已经超限的存量文件按自己的基线卡，
    「不许变坏」而不是「立刻达标」。
    """
    value = 0
    if baseline:
        files = baseline.get("files") or {}
        if isinstance(files, dict):
            value = int(files.get(path, 0))
    return max(value, FILE_LINE_LIMIT)


def render_human(metrics: Metrics, baseline: dict[str, object] | None) -> str:
    lines: list[str] = []
    add = lines.append

    add("=" * 78)
    add("架构体检报告")
    add("=" * 78)
    add(f"仓库根      : {ROOT}")
    add(f"模块数      : {metrics.module_count} 个 app/**/*.py")
    add(f"测试文件数  : {metrics.test_file_count} 个 tests/**/*.py")
    add(f"app 总行数  : {metrics.app_total_lines}")
    add(f"顶层函数数  : {metrics.top_level_function_total}")
    add(f"长函数(>{LONG_FUNCTION_LIMIT}) : {len(metrics.long_functions)} 个")
    add(f"重名函数组  : {len(metrics.duplicate_names)} 组（多出 {metrics.duplicate_extra_total} 个定义）")
    if metrics.layer_table_available:
        add(f"跨层违规    : {len(metrics.layer_violations)} 条（反向依赖）")
        add(f"未登记依赖  : {len(metrics.unregistered_dependencies)} 条")
    else:
        add(f"跨层违规    : 跳过（读不到 tests/test_imports.py：{metrics.layer_table_error}）")
    add(
        f"前端 id     : 引用 {metrics.frontend_referenced} 个 / 声明 {metrics.frontend_declared} 个 "
        f"/ 缺失 {len(metrics.frontend_missing)} 个（动态生成白名单 {len(DYNAMIC_ID_WHITELIST)} 条）"
    )
    add("")

    add("-" * 78)
    add(f"① 文件行数 Top 10（上限 = max(基线, {FILE_LINE_LIMIT})）")
    add("-" * 78)
    for path, count in metrics.largest_files[:10]:
        limit = _limit_for(path, baseline)
        flag = "OK " if count <= limit else "!! "
        add(f"  {flag}{count:>6} 行 / 上限 {limit:>5}  {path}")
    over = [(path, count) for path, count in metrics.files.items() if count > _limit_for(path, baseline)]
    add(f"  超限文件：{len(over)} 个" + (f" → {sorted(over)}" if over else ""))
    add("")

    add("-" * 78)
    add(f"② 超过 {LONG_FUNCTION_LIMIT} 行的函数（全部 {len(metrics.long_functions)} 个，按行数降序）")
    add("-" * 78)
    for span in metrics.long_functions:
        add(f"  {span.lines:>5} 行  {span.path}:{span.lineno}  {span.qualname}")
    add("")

    add("-" * 78)
    add(f"③ 顶层函数重名（{len(metrics.duplicate_names)} 组）")
    add("-" * 78)
    for name, modules in sorted(metrics.duplicate_names.items()):
        add(f"  {name}（{len(modules)} 处）: " + ", ".join(modules))
    add("")

    add("-" * 78)
    add("④ 分层（app/api → app/planner → app/policy → app/tools → app/db）")
    add("-" * 78)
    if metrics.layer_violations:
        add("  反向依赖（下层引上层）：")
        for item in metrics.layer_violations:
            add(f"    {item}")
    else:
        add("  反向依赖：无")
    if metrics.unregistered_dependencies:
        add("  未登记依赖（ALLOWED 表里没有）：")
        for item in metrics.unregistered_dependencies:
            add(f"    {item}")
    add("")

    add("-" * 78)
    add("⑤ 前端 $('id') 引用 vs HTML 声明")
    add("-" * 78)
    if metrics.frontend_missing:
        for name in metrics.frontend_missing:
            add(f"  缺失：{name}")
    else:
        add("  缺失：无（动态生成的走白名单）")
    if metrics.frontend_whitelist_stale:
        add("  白名单已过期（应删除，别再替我兜底）：")
        for name in metrics.frontend_whitelist_stale:
            add(f"    {name}")
    add("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="输出架构指标（文件行数/长函数/重名/分层/前端 id）")
    parser.add_argument("--json", action="store_true", help="打印机器可读 JSON")
    parser.add_argument("--write-baseline", metavar="PATH", help="把当前指标写成守卫测试用的基线文件")
    args = parser.parse_args(argv)

    metrics = collect()
    baseline: dict[str, object] | None = None
    baseline_path = Path(args.write_baseline) if args.write_baseline else None
    if baseline_path is not None:
        baseline_path.parent.mkdir(parents=True, exist_ok=True)
        payload = build_baseline(metrics)
        baseline_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        baseline = payload
        print(f"基线已写入 {baseline_path}（{len(metrics.files)} 个文件 / {len(metrics.long_functions)} 个长函数）")
    else:
        existing = ROOT / "scripts" / "arch_baseline.json"
        if existing.exists():
            baseline = load_baseline(existing)

    if args.json:
        print(json.dumps(build_baseline(metrics), ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    print(render_human(metrics, baseline))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
