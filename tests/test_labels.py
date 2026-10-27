"""标签字典守卫：不许空标签，也不许和权威状态常量脱队。

W2 为什么不是「把 19 个标签字典搬进一个 app/labels.py」：搬过去只会把「状态值」
和「它的中文」分到两个文件，反而多一处漂移点（改状态忘了改标签，编译器不报、
测试也不报）。所以这些字典留在各自的领域模块里，用这份守卫钉两件真事：

1. 任何 *LABELS 字典的值都不能是空串/纯空白（空标签在页面上就是一片空白）；
2. 有权威常量（PLAN_STATUSES / KINDS / ACTIVE_STATUSES / RiskLevel，以及种子库里
   真实出现的渠道、产品分类、风险等级）的地方，字典必须覆盖它：状态加了一个、
   标签没跟上，这里就红。
"""

from __future__ import annotations

import ast
import pathlib
from collections.abc import Iterator
from typing import Any, get_args

import pytest

from app import db, handoffs, memory, plans
from app.models import RiskLevel
from app.views import labels as view_labels
from app.planner import assist
from app.planner.events import LEVEL_LABELS
from app.subscriptions import CHANNEL_LABELS, VERDICT_ACTIONS, VERDICT_LABELS
from app.tools import products

APP_DIR = pathlib.Path(__file__).resolve().parent.parent / "app"


def _label_dicts() -> Iterator[tuple[str, dict[Any, Any]]]:
    """扫出所有模块级 *LABELS 的字面量字典；推导出来的（derived）跳过。"""
    for path in sorted(APP_DIR.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in tree.body:
            targets: list[str] = []
            value: ast.expr | None = None
            if isinstance(node, ast.Assign):
                targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
                value = node.value
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                targets = [node.target.id]
                value = node.value
            for name in targets:
                if "LABELS" not in name or value is None:
                    continue
                try:
                    data = ast.literal_eval(value)
                except (ValueError, SyntaxError):
                    continue
                if isinstance(data, dict):
                    yield f"{path.relative_to(APP_DIR)}:{name}", data


def _flatten(prefix: str, data: dict[Any, Any]) -> Iterator[tuple[str, Any]]:
    """嵌套一层也要看到：INTENT_MISSING_LABELS 是 intent -> {slot: 中文}。"""
    for key, value in data.items():
        where = f"{prefix}[{key!r}]"
        if isinstance(value, dict):
            yield from _flatten(where, value)
        else:
            yield where, value


def test_every_label_dict_is_readable() -> None:
    """标签是给人看的：值非空且不是纯空白（键允许 int，RISK_LABELS 是 1..5）。"""
    broken: list[str] = []
    found = 0
    for where, data in _label_dicts():
        found += 1
        for label_where, label in _flatten(where, data):
            if not isinstance(label, str):
                broken.append(f"{label_where}: 标签不是字符串（{type(label).__name__}）")
            elif not label.strip():
                broken.append(f"{label_where}: 标签是空的")
    assert found >= 15, f"只扫到 {found} 个标签字典，扫描逻辑可能失效了"
    assert broken == [], "\n".join(broken)


def test_plan_status_labels_cover_plan_statuses() -> None:
    missing = set(plans.PLAN_STATUSES) - set(assist.PLAN_STATUS_LABELS)
    assert missing == set(), f"计划状态没有中文标签：{sorted(missing)}"


def test_memory_kind_labels_cover_kinds() -> None:
    missing = set(memory.KINDS) - set(memory.KIND_LABELS)
    assert missing == set(), f"记忆类型没有中文标签：{sorted(missing)}"


def test_handoff_status_labels_cover_every_status() -> None:
    every = set(handoffs.ACTIVE_STATUSES) | set(handoffs.TERMINAL_STATUSES)
    # W4 起视图常量搬进 app/views/labels.py：标签是给界面用的，跟路由不再同文件。
    labels = set(view_labels.HANDOFF_STATUS_LABELS)
    assert every == labels, f"接管单状态与标签不一致：少 {sorted(every - labels)}，多 {sorted(labels - every)}"


def test_verdict_labels_cover_verdict_actions() -> None:
    missing = set(VERDICT_ACTIONS) - set(VERDICT_LABELS)
    assert missing == set(), f"订阅定性动作没有中文标签：{sorted(missing)}"


def test_risk_level_labels_cover_every_level() -> None:
    missing = set(get_args(RiskLevel)) - set(LEVEL_LABELS)
    assert missing == set(), f"风险等级没有中文标签：{sorted(missing)}"


@pytest.fixture()
def seeded(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(db, "DEFAULT_DB_PATH", tmp_path / "labels.db")
    conn = db.connect()
    try:
        db.init_db(conn)
        from scripts.seed import seed

        seed(conn)
    finally:
        conn.close()


def test_seed_data_values_all_have_labels(seeded: None) -> None:
    """种子库里真实出现的渠道/产品分类/风险等级，都必须有中文标签。

    字典键写错的典型症状是「页面上直接露英文枚举」，静态扫不出来，只能拿真数据对。
    """
    conn = db.connect()
    try:
        channels = {
            str(row["channel"]) for row in conn.execute("SELECT DISTINCT channel FROM transactions")
        }
        categories = {
            str(row["category"])
            for row in conn.execute("SELECT DISTINCT category FROM fund_products")
        }
        risks = {
            int(row["risk_level"])
            for row in conn.execute("SELECT DISTINCT risk_level FROM fund_products")
        }
    finally:
        conn.close()

    assert channels <= set(CHANNEL_LABELS), f"这些渠道没有标签：{sorted(channels - set(CHANNEL_LABELS))}"
    missing = categories - set(products.CATEGORY_LABELS)
    assert missing == set(), f"这些产品分类没有标签：{sorted(missing)}"
    missing_risk = risks - set(products.RISK_LABELS)
    assert missing_risk == set(), f"这些风险等级没有标签：{sorted(missing_risk)}"
