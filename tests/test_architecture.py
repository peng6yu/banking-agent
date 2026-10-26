"""架构体检的守卫：基线只保证「不许变坏」，不要求存量债立刻达标。

W1-A 立规矩。scripts/arch_report.py 负责量，这个文件负责守。语义只有一句：
**基线里的存量债可以存在，新债必须失败。**

于是四个指标各自有一道门：
- **文件行数**：上限 = max(基线值, FILE_LINE_LIMIT)。没登记的新文件按 600 硬上限卡；
  已经超限的存量文件按自己的基线卡 —— 不许再变长。
- **长函数**：基线里登记过的允许存在（还允许随功能演进变长 1.5 倍），没登记过的
  「超过 80 行」一律失败。
- **顶层重名**：同名函数的个数超过基线就算变坏（重名让 grep 与阅读都要靠猜）。
- **前端缺失 id / 跨层违规 / 未登记依赖**：只许减少，不许新增。

失败信息里点名 文件:行、指标名、基线值、当前值、怎么修 —— 报告与测试共用
scripts/arch_report.py 的同一份量法，别在测试里另写一套口径。
"""

from __future__ import annotations

import math
from functools import lru_cache
from pathlib import Path

import pytest

from scripts.arch_report import (
    BASELINE_HINT,
    BASELINE_VERSION,
    FILE_LINE_LIMIT,
    LONG_FUNCTION_LIMIT,
    Metrics,
    _limit_for,
    collect,
    load_baseline,
)

ROOT = Path(__file__).resolve().parents[1]
BASELINE_PATH = ROOT / "scripts" / "arch_baseline.json"

#: 基线里的长函数允许随功能演进变长 —— 但翻倍就是「又长回来了」，得重新拆。
LONG_FUNCTION_GROWTH = 1.5


@pytest.fixture(scope="module")
def metrics() -> Metrics:
    """整次扫描只跑一次（82 个模块 + 105 个测试文件 + 前端单页，约 1 秒）。"""
    return collect()


@lru_cache(maxsize=1)
def _baseline() -> dict[str, object]:
    if not BASELINE_PATH.exists():
        pytest.fail(
            "缺少架构基线 scripts/arch_baseline.json。\n"
            "    基线是「不许变坏」的锚点：先把当前状况记下来，守卫以后才只骂新债。\n"
            f"    生成命令：{BASELINE_HINT}\n"
            "    生成后 review 一遍 diff 再提交 —— 别把「这次主动新增的债」一起洗进基线。"
        )
    data = load_baseline(BASELINE_PATH)
    version = data.get("version")
    if version != BASELINE_VERSION:
        pytest.fail(
            f"基线版本 {version!r} 与口径版本 {BASELINE_VERSION} 不一致。\n"
            "    报告口径（指标集合）变了就要重新生成基线，否则新旧字段对不上。\n"
            f"    重新生成：{BASELINE_HINT}"
        )
    return data


def _dict_of_ints(data: dict[str, object], key: str) -> dict[str, int]:
    raw = data.get(key) or {}
    return {str(k): int(v) for k, v in raw.items()}  # type: ignore[union-attr]


def _list_of_str(data: dict[str, object], key: str) -> list[str]:
    raw = data.get(key) or []
    return [str(item) for item in raw]  # type: ignore[union-attr]


# --------------------------------------------------------------------------- #
# 基线自身：别把它改成橡皮图章
# --------------------------------------------------------------------------- #
def test_baseline_limits_are_canonical(metrics: Metrics) -> None:
    """阈值写在代码里，基线只是「当时的存量」。改基线放宽阈值等于废掉守卫。"""
    data = _baseline()
    limits = data.get("limits") or {}
    bad: list[str] = []
    if limits.get("file_lines") != FILE_LINE_LIMIT:  # type: ignore[union-attr]
        bad.append(
            f"limits.file_lines = {limits.get('file_lines')!r}，应为 {FILE_LINE_LIMIT}"  # type: ignore[union-attr]
        )
    if limits.get("function_lines") != LONG_FUNCTION_LIMIT:  # type: ignore[union-attr]
        bad.append(
            f"limits.function_lines = {limits.get('function_lines')!r}，应为 {LONG_FUNCTION_LIMIT}"  # type: ignore[union-attr]
        )
    assert not bad, (
        "基线里的阈值被改松了：\n    "
        + "\n    ".join(bad)
        + "\n    阈值是规矩本身（scripts/arch_report.py 的 FILE_LINE_LIMIT / LONG_FUNCTION_LIMIT），"
        "要动就改代码并同步 docs/00，不要在 JSON 里偷偷放水。"
    )


def test_layer_table_is_available(metrics: Metrics) -> None:
    """分层指标依赖 tests/test_imports.py 的 LAYERS/ALLOWED；读不到就别装作没违规。"""
    if metrics.layer_table_available:
        return
    pytest.fail(
        "分层清单读不出来，跨层违规与未登记依赖这两项等于没量：\n"
        f"    {metrics.layer_table_error}\n"
        "    修法：修好 tests/test_imports.py 的 LAYERS / ALLOWED 表（本守卫依赖它），"
        "别让架构体检静默退化成「全绿」。"
    )


# --------------------------------------------------------------------------- #
# 指标 1：文件行数
# --------------------------------------------------------------------------- #
def test_no_file_grows_past_its_limit(metrics: Metrics) -> None:
    """文件行数上限 = max(基线值, 600)：存量按基线，新文件按硬上限。"""
    data = _baseline()
    known = _dict_of_ints(data, "files")
    violations: list[str] = []
    for path, lines in metrics.largest_files:
        limit = _limit_for(path, data)
        if lines <= limit:
            continue
        base = known.get(path)
        detail = f"基线 {base} 行" if base is not None else f"基线未登记（按硬上限 {FILE_LINE_LIMIT}）"
        violations.append(
            f"{path}: {lines} 行 > 上限 {limit}（{detail}）。\n"
            "    修法：按职责拆成多个模块（视图/编排/纯函数分开），或把常量与数据搬出去；"
            f"确属合理增长（新增功能而非堆逻辑）就先拆、review 后重新生成基线：{BASELINE_HINT}。"
        )
    assert not violations, (
        f"文件行数指标变坏（上限 = max(基线, {FILE_LINE_LIMIT})，基线 {BASELINE_PATH.name}）：\n    "
        + "\n    ".join(violations)
    )


# --------------------------------------------------------------------------- #
# 指标 2：长函数
# --------------------------------------------------------------------------- #
def test_no_new_long_functions(metrics: Metrics) -> None:
    """超过 LONG_FUNCTION_LIMIT 行的函数：基线里登记过的放行，新的一律失败。"""
    known = _dict_of_ints(_baseline(), "long_functions")
    violations: list[str] = []
    for span in metrics.long_functions:
        key = span.key
        where = f"{span.path}:{span.lineno} {span.qualname}"
        if key not in known:
            violations.append(
                f"{where} 现在 {span.lines} 行 > 阈值 {LONG_FUNCTION_LIMIT} 行。\n"
                f"    基线：未登记（存量长函数共 {len(known)} 个）。\n"
                "    修法：按「读入 → 判定 → 执行 → 收尾」抽成几个有名字的小函数；"
                "只是同一件事写太长，先找重复分支。"
            )
            continue
        allowed = math.ceil(known[key] * LONG_FUNCTION_GROWTH)
        if span.lines > allowed:
            violations.append(
                f"{where} 现在 {span.lines} 行，基线 {known[key]} 行，允许上限 {allowed} 行"
                f"（{LONG_FUNCTION_GROWTH}× 余量）。\n"
                "    修法：别再往这个函数里加分支 —— 新增逻辑请开新函数，"
                "老函数保持只调度；确实要重构就现在拆，拆完重新生成基线。"
            )
    assert not violations, (
        f"长函数指标变坏（阈值 {LONG_FUNCTION_LIMIT} 行，基线 {len(known)} 个存量）：\n    "
        + "\n    ".join(violations)
    )


# --------------------------------------------------------------------------- #
# 指标 3：顶层重名
# --------------------------------------------------------------------------- #
def test_no_new_duplicate_function_names(metrics: Metrics) -> None:
    """同一个函数名在 app/ 里出现多处：超过基线数量就是变坏。"""
    known = _dict_of_ints(_baseline(), "duplicate_names")
    violations: list[str] = []
    for name, modules in sorted(metrics.duplicate_names.items()):
        current = len(modules)
        base = known.get(name, 0)
        if current <= base:
            continue
        violations.append(
            f"{name}: 当前 {current} 处，基线 {base} 处（多出 {current - base}）。\n"
            f"    位置：{', '.join(sorted(modules))}\n"
            "    修法：重名会让 grep 与调用方都要靠猜 —— 抽到 modules 层共用一份，"
            "或按职责改名（如 plan_resume / run_resume）；改完重新生成基线。"
        )
    assert not violations, (
        f"顶层函数重名指标变坏（重复组数 {len(metrics.duplicate_names)}，多出 {metrics.duplicate_extra_total}）：\n    "
        + "\n    ".join(violations)
    )


# --------------------------------------------------------------------------- #
# 指标 4：前端 id、跨层违规、未登记依赖（只许减少）
# --------------------------------------------------------------------------- #
def _growth_report(
    *,
    metric: str,
    current: list[str],
    baseline: list[str],
    fix: str,
) -> None:
    known = set(baseline)
    added = [item for item in current if item not in known]
    assert not added, (
        f"{metric} 变坏（基线 {len(known)} 条，当前 {len(current)} 条）：\n    "
        + "\n    ".join(added)
        + f"\n    修法：{fix}"
    )


def test_frontend_missing_ids_do_not_grow(metrics: Metrics) -> None:
    """JS 里 $(...) 取的 id，HTML 里必须真的存在；新增缺失一律失败。"""
    _growth_report(
        metric="前端缺失 id",
        current=list(metrics.frontend_missing),
        baseline=_list_of_str(_baseline(), "missing_frontend_ids"),
        fix=(
            "二选一：① 在 web/index.html 里补上这个元素（或渲染它的模板）；"
            "② 它确实由 JS 动态创建 —— 登记到 scripts/arch_report.py 的 DYNAMIC_ID_WHITELIST "
            "并写清「谁在哪一行创建」，否则下一个人无法判断这个 id 还成不成立。"
        ),
    )


def test_layer_violations_do_not_grow(metrics: Metrics) -> None:
    """反向依赖（低层 import 高层）：只许靠豁免表与拆分减少，不许新增。"""
    _growth_report(
        metric="跨层反向依赖",
        current=list(metrics.layer_violations),
        baseline=_list_of_str(_baseline(), "layer_violations"),
        fix=(
            "把「低层需要高层」的那段逻辑上移到高层；确属 W4 拆分才能解决的存量债，"
            "请在 tests/test_imports.py 的 UPWARD_EXCEPTIONS 里登记（带理由与消债阶段），"
            "并重新生成基线。"
        ),
    )


def test_unregistered_dependencies_do_not_grow(metrics: Metrics) -> None:
    """新依赖必须先想清楚属于哪一层：没登记就等于架构开始漂。"""
    _growth_report(
        metric="未登记依赖",
        current=list(metrics.unregistered_dependencies),
        baseline=_list_of_str(_baseline(), "unregistered_dependencies"),
        fix=(
            "在 tests/test_imports.py 的 ALLOWED 里给该模块登记这条依赖并写一句用途；"
            "同时确认 LAYERS 的层号没写错（跨层会被 test_layers_never_depend_upward 再拦一次）。"
        ),
    )
