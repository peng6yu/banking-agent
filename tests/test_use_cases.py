"""W3 用例层守卫：写操作只有一条路径。

三条规矩，全部用静态扫描钉住（不跑业务、不依赖数据库）：

1. 每个写端点（POST/PATCH/DELETE）都必须经过 app.use_cases，路由只绑定入参；
   路由里不许再出现 conn.execute / db.tx / db.open_conn —— 事务与 SQL 都在用例或领域层。
2. 少数「编排型」端点是例外（流式对话、授权执行、恢复/取消执行、手动触发节拍器），
   它们不返回响应体而是驱动执行器，逐条写清理由；理由为空或端点已不存在都会失败。
3. 用例层不许反向 import app.api.*，并且 @write_case 装饰的函数第一个参数必须是 conn
   —— 事务边界是这一层唯一的开关。
"""

from __future__ import annotations

import ast
import importlib
import inspect
import pkgutil
from pathlib import Path

import pytest

import app.use_cases as use_cases_pkg

API_DIR = Path(__file__).resolve().parents[1] / "app" / "api"

#: 编排型写端点：它们驱动执行器或返回事件流，不产出普通响应体。
ORCHESTRATION: dict[str, str] = {
    "chat": "SSE 流式编排：解析意图并派发执行器，返回的是八种事件而不是响应体",
    "confirm": "授权执行：把用户的批准交给执行器，本身就是执行链的一环",
    "recover_session": "重启后续跑：把执行器状态从库里恢复出来",
    "cancel": "中途取消：释放预授权并把整轮 DAG 标 cancelled",
    "run_scheduled": "手动触发节拍器：直接跑一次到点的定时任务",
}

WRITE_VERBS = {"post", "patch", "put", "delete"}
FORBIDDEN_IN_ROUTE = ("conn.execute(", "db.tx(", "db.open_conn(", "db.connect(")


def _use_case_aliases(src: str) -> set[str]:
    """这个文件里「指向用例层的本地名字」：import app.use_cases as u / from app.use_cases import x as y。"""
    aliases: set[str] = set()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "app.use_cases" or alias.name.startswith("app.use_cases."):
                    aliases.add(alias.asname or alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and (node.module or "").startswith("app.use_cases"):
            for alias in node.names:
                aliases.add(alias.asname or alias.name)
    return aliases


def _write_handlers() -> list[tuple[str, str, str, set[str]]]:
    """(文件名, 函数名, 源码片段)：所有带写动作装饰器的路由处理器。"""
    found: list[tuple[str, str, str, set[str]]] = []
    for path in sorted(API_DIR.glob("*.py")):
        src = path.read_text(encoding="utf-8")
        lines = src.splitlines()
        aliases = _use_case_aliases(src)
        for node in ast.parse(src).body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            verbs = [
                d.func.attr
                for d in node.decorator_list
                if isinstance(d, ast.Call)
                and isinstance(d.func, ast.Attribute)
                and d.func.attr in WRITE_VERBS
            ]
            if not verbs:
                continue
            segment = "\n".join(lines[node.lineno - 1 : node.end_lineno])
            found.append((path.name, node.name, segment, aliases))
    return found


def test_every_write_endpoint_returns_through_payload() -> None:
    """写端点要么走 payload(outcome)，要么在白名单里写明为什么例外。"""
    offenders = []
    for file_name, name, segment, aliases in _write_handlers():
        if name in ORCHESTRATION:
            continue
        if "payload(" not in segment:
            offenders.append(f"{file_name}:{name} 没经过 app.api.http.payload")
        if not any(f"{alias}." in segment for alias in aliases):
            offenders.append(f"{file_name}:{name} 没调用用例层")
    assert not offenders, "写操作必须只有一条路径：\n" + "\n".join(offenders)


def test_write_endpoints_keep_no_sql_or_transactions() -> None:
    """路由不碰 SQL、也不自己开事务：这两件事分别属于领域层与用例层。"""
    offenders = []
    for file_name, name, segment, _aliases in _write_handlers():
        if name in ORCHESTRATION:
            continue
        for bad in FORBIDDEN_IN_ROUTE:
            if bad in segment:
                offenders.append(f"{file_name}:{name} 里还有 {bad}")
    assert not offenders, "\n".join(offenders)


def test_orchestration_allowlist_is_real_and_explained() -> None:
    """豁免表是债的登记簿：理由为空、或端点已经不存在，都要立刻报错。"""
    names = {name for _, name, _, _aliases in _write_handlers()}
    stale = sorted(set(ORCHESTRATION) - names)
    assert not stale, f"这些豁免端点已经不存在，请从 ORCHESTRATION 删掉：{stale}"
    empty = sorted(name for name, reason in ORCHESTRATION.items() if not reason.strip())
    assert not empty, f"豁免必须写清理由：{empty}"


def test_use_case_modules_never_import_the_api_layer() -> None:
    """依赖始终单向：api -> use_cases -> 领域。用例层反向 import api 会形成环。"""
    offenders = []
    for info in pkgutil.iter_modules(use_cases_pkg.__path__):
        module_name = f"app.use_cases.{info.name}"
        module = importlib.import_module(module_name)
        src = inspect.getsource(module)
        for node in ast.walk(ast.parse(src)):
            if isinstance(node, ast.Import):
                bad = [a.name for a in node.names if a.name.startswith("app.api")]
            elif isinstance(node, ast.ImportFrom):
                bad = [node.module or ""] if (node.module or "").startswith("app.api") else []
            else:
                continue
            if bad:
                offenders.append(f"{module_name} -> {bad}")
    assert not offenders, "\n".join(offenders)


def test_write_case_functions_take_a_connection_first() -> None:
    """@write_case 的意义就是「它自己开事务」，所以第一个参数必须是 conn。"""
    checked = 0
    for info in pkgutil.iter_modules(use_cases_pkg.__path__):
        module = importlib.import_module(f"app.use_cases.{info.name}")
        for name, obj in vars(module).items():
            if not inspect.isfunction(obj) or not hasattr(obj, "__wrapped__"):
                continue
            if getattr(obj, "__module__", "") != module.__name__:
                continue
            params = list(inspect.signature(obj).parameters)
            assert params and params[0] == "conn", f"{module.__name__}.{name} 第一个参数应是 conn"
            checked += 1
    assert checked >= 10, f"只扫到 {checked} 个写用例，装饰器或命名变了？"


@pytest.mark.parametrize("module_name", ["app.use_cases.base", "app.api.http"])
def test_outcome_contract_is_documented(module_name: str) -> None:
    """这层是全项目写操作的入口，两个薄模块必须留下说明，别让后人靠猜。"""
    module = importlib.import_module(module_name)
    assert (module.__doc__ or "").strip(), f"{module_name} 缺模块 docstring"
