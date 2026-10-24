"""静态证明：app/ 下不存在任何代码执行面，因此赛题的「沙箱」条目不成立。

为什么这是安全属性而不是风格偏好
--------------------------------
赛题 §3.2 要求「生成的代码在受限、可监控的逻辑隔离环境中运行」。常规做法是写
一个沙箱（subprocess + AST 白名单 + rlimit + 超时），但沙箱的前提是**存在执行面**：
先有 eval / exec / 子进程，才谈得上把它关进笼子。本系统走的是另一条路——把执行面
整个删掉，于是「沙箱可不可靠」不再取决于沙箱实现得好不好，而取决于一条任何人都能
当场复核的事实：app/ 里没有任何一处能执行代码。

这句话本身不证明任何事，除非它能被机器检查。检查由两级组成：
1. scripts/prove_no_exec_surface.py 是检查器，对 AST 做判定（不是 grep，避免把
   conn.execute / re.compile / tools.execute 这类同名调用误判成 exec）；
2. 本文件是断言——扫描范围必须等于 app/ 下全部 .py，执行面命中必须为 0，
   并且检查器自己必须被「植入样本」逼出真命中（第 3 条用例），否则整套证明
   退化成一句「扫描没发现问题」，而扫描器坏了也会这样报。

同时把出网面照实列出来并钉死：零代码执行面不等于零 I/O 面，app/llm.py 的模型
API 出网是赛题要求的模型调用，不是用户代码执行面，但它必须被显式允许而不是被
口径藏起来。

「零命中」还可以用两种更弱的方式骗过去，本文件因此补了三类结构化断言：
1. 工具注册表——handler 必须是静态的「模块名.函数名」，不能是 Call/Lambda/名字
   拼接出来的可调用对象；否则模型只要能让注册表多出一条，就等于拿到任意函数；
2. API schema——请求体里不允许出现 code/script/command/shell/sql/exec 这类
   代码字符串字段，接口层不接受「一段代码」这种入参；
3. 动态 SQL——拼接串逐条进人工复核清单，并要求 docs/security_sandbox.md 里
   对每个出现的文件写出复核结论；新增拼接而没复核，测试就红。
"""

from __future__ import annotations

import ast
import re
from pathlib import Path
from types import FunctionType

import pytest

from scripts.prove_no_exec_surface import (
    PLANTED,
    Report,
    main,
    scan_source,
    scan_tree,
    selfcheck,
)

REPO = Path(__file__).resolve().parent.parent
APP_DIR = REPO / "app"
DOC_TECH = REPO / "docs" / "技术文档.md"
DOC_CHECKLIST = REPO / "docs" / "提交物对照表.md"
EVIDENCE_TEST = "tests/test_no_exec_surface.py"
EVIDENCE_SCRIPT = "scripts/prove_no_exec_surface.py"
PHRASE = "零代码执行面"


@pytest.fixture(scope="module")
def report() -> Report:
    return scan_tree(APP_DIR)


def test_scan_covers_every_module_under_app(report: Report) -> None:
    """扫描范围必须等于 app/ 下现存的全部 .py，少扫一个文件结论就不成立。"""
    on_disk = {p.relative_to(REPO).as_posix() for p in APP_DIR.rglob("*.py") if p.is_file()}
    scanned = set(report.files)
    assert on_disk, "app/ 下没有任何 .py，扫描范围是空的"
    assert scanned == on_disk
    assert report.lines > 0


def test_app_has_zero_code_execution_surface(report: Report) -> None:
    """核心断言：执行面为零。命中时把每一条的位置和符号都打出来，便于当场定位。"""
    assert report.exec_hits == (), "\n".join(hit.render() for hit in report.exec_hits)
    assert report.ok is True


def test_detector_reports_planted_violations() -> None:
    """反证：检查器必须对真实的绕过写法逐条报出，否则上面的零命中是空转。"""
    missed = selfcheck()
    assert not missed, f"检查器漏报: {missed}"
    assert len(PLANTED) >= 10
    for code, expect in PLANTED:
        found = scan_source(code, "<planted>")
        assert found, f"检查器对 {code!r} 没有任何反应"
        assert any(expect in hit.symbol for hit in found), f"{code!r} → {[h.symbol for h in found]}"


def test_detector_ignores_benign_lookalikes() -> None:
    """SQL 的 conn.execute、正则的 re.compile、工具执行 tools.execute 都不是执行面。"""
    benign = (
        "conn.execute('DELETE FROM holds')",
        "re.compile(r'\\s+')",
        "tools.execute(node.tool, params)",
        "asyncio.run(executor.run(session_id, dag, emit))",
        "value = compile_report(rows)",
    )
    for code in benign:
        assert scan_source(code, "<benign>") == [], code


def test_network_egress_is_confined_to_the_llm_module(report: Report) -> None:
    """出网面照实报并钉死：只允许 app/llm.py 的模型 API，别处不许有。"""
    modules = {hit.path for hit in report.egress_hits}
    assert modules <= {"app/llm.py"}, modules
    assert {hit.symbol for hit in report.egress_hits} <= {
        "import urllib.request",
        "urllib.request.Request",
        "urllib.request.urlopen",
    }


def test_missing_target_fails_loudly(tmp_path: Path) -> None:
    """扫描目标不存在必须抛错，不能悄悄当成「零命中」过关。"""
    with pytest.raises(FileNotFoundError):
        scan_tree(tmp_path / "not-there")
    (tmp_path / "empty").mkdir()
    with pytest.raises(FileNotFoundError):
        scan_tree(tmp_path / "empty")


def test_proof_script_runs_and_reports_scope(capsys: pytest.CaptureFixture[str]) -> None:
    """演示脚本可独立运行：自己跑一遍 main()，断言输出里有扫描范围与命中数。"""
    assert main([]) == 0
    out = capsys.readouterr().out
    assert "扫描范围" in out
    assert "命中（代码执行面）: 0" in out
    assert "不存在任何代码执行面" in out


def test_both_docs_state_the_same_sandbox_guarantee() -> None:
    """两份文档对沙箱的口径必须一致，且都指向可运行的证据。"""
    for doc in (DOC_TECH, DOC_CHECKLIST):
        text = doc.read_text(encoding="utf-8")
        assert PHRASE in text, doc.name
        assert EVIDENCE_TEST in text, doc.name
        assert EVIDENCE_SCRIPT in text, doc.name
        lines = text.splitlines()
        sandbox_lines = [line for line in lines if "沙箱" in line]
        assert sandbox_lines, doc.name
        guaranteed = [
            line for line in sandbox_lines if PHRASE in line or "不引入任何代码执行面" in line
        ]
        assert guaranteed, f"{doc.name} 没把沙箱写成有证据的架构性保证"
        # 只盯「沙箱环境」那一行（对照表 3.2 的表格行 / 技术文档的 R5 行），
        # 概览句里别的条目带 ⛔ 与沙箱无关，不能连带判失败
        stale = [
            line
            for line in sandbox_lines
            if line.lstrip().startswith(("| **沙箱环境**", "| R5"))
            and ("⛔" in line or "未实现" in line)
        ]
        assert stale == [], f"{doc.name} 仍把沙箱环境标成未实现: {stale}"


TOOL_SPECS = REPO / "app" / "tools" / "tool_specs.py"
SCHEMAS = REPO / "app" / "api" / "schemas.py"
SECURITY_DOC = REPO / "docs" / "security_sandbox.md"


def _tool_spec_handler_nodes() -> list[ast.AST]:
    """取出 tool_specs.py 里每个 ToolSpec(handler=…) 的 handler 表达式节点。"""
    tree = ast.parse(TOOL_SPECS.read_text(encoding="utf-8"))
    nodes: list[ast.AST] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
        if name != "ToolSpec":
            continue
        for keyword in node.keywords:
            if keyword.arg == "handler":
                nodes.append(keyword.value)
    return nodes


def test_tool_handlers_are_static_module_functions() -> None:
    """注册表的 handler 只能是静态引用；动态构造等于把「调用哪个函数」交给数据。"""
    nodes = _tool_spec_handler_nodes()
    assert len(nodes) >= 30, f"只找到 {len(nodes)} 个 handler= 关键字，注册表结构变了？"
    dynamic = [ast.dump(node) for node in nodes if not isinstance(node, ast.Attribute)]
    assert dynamic == [], f"handler 出现非静态写法: {dynamic}"
    for node in nodes:
        assert isinstance(node.value, ast.Name), ast.dump(node)


def test_registry_contains_only_static_whitelisted_tools() -> None:
    """运行时复核：每个工具都来自 app.tools 下的普通函数，没有 lambda / partial 混入。"""
    from app.tools import registry

    assert set(registry.TOOLS) == set(registry.TOOL_NAMES)
    assert len(registry.TOOL_NAMES) == len(set(registry.TOOL_NAMES))
    for name in registry.TOOL_NAMES:
        handler = registry.TOOLS[name].handler
        assert isinstance(handler, FunctionType), f"{name} 的 handler 不是普通函数: {handler!r}"
        assert handler.__module__.startswith("app.tools"), f"{name} 来自 {handler.__module__}"
        assert "<" not in handler.__qualname__, f"{name} 是动态拼出来的函数: {handler.__qualname__}"


def test_api_schemas_have_no_code_string_fields() -> None:
    """接口层不接受代码字符串入参：schema 字段名里不许出现 code/script/command 之类。"""
    tree = ast.parse(SCHEMAS.read_text(encoding="utf-8"))
    fields = [
        node.target.id
        for node in ast.walk(tree)
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)
    ]
    assert fields, "schemas.py 里一个字段都没解析到，检查器写错了？"
    codeish = [name for name in fields if re.search(r"(code|script|command|shell|sql|exec)", name, re.I)]
    assert codeish == [], f"API schema 出现代码字符串字段: {codeish}"


def test_dynamic_sql_is_reported_and_documented(report: Report) -> None:
    """动态 SQL 不进执行面判定，但必须被报出来，且每个文件都在安全文档里有复核结论。"""
    assert report.sql_hits, "动态 SQL 一处都没报：检查器坏了，或 app/ 的 SQL 写法整体变了"
    doc = SECURITY_DOC.read_text(encoding="utf-8")
    undocumented = sorted({hit.path for hit in report.sql_hits if hit.path not in doc})
    assert undocumented == [], (
        "这些文件里有动态 SQL 但 docs/security_sandbox.md 没写复核结论，"
        f"新增拼接必须重新人工确认参数化: {undocumented}"
    )
    for hit in report.sql_hits:
        assert hit.path.startswith("app/"), hit.render()
        assert ":" in hit.render(), hit.render()


def test_proof_script_lists_offline_script_commands(capsys: pytest.CaptureFixture[str]) -> None:
    """离线脚手架的系统命令要照实列出来，不能因为「不在 app/ 里」就当它不存在。"""
    assert main([]) == 0
    out = capsys.readouterr().out
    assert "命中（动态 SQL，需人工确认参数化）" in out
    assert "离线/测试脚本系统命令（不在在线 Agent 路径）" in out
    assert "scripts/ui_journeys.py" in out
    assert "subprocess.Popen" in out


def test_security_sandbox_doc_covers_clause_3_2() -> None:
    """安全文档必须能独立支撑 3.2 条：条款号、结论、两类证据、离线命令边界。"""
    assert SECURITY_DOC.is_file(), SECURITY_DOC
    doc = SECURITY_DOC.read_text(encoding="utf-8")
    for needle in (
        "3.2",
        PHRASE,
        "subprocess",
        EVIDENCE_TEST,
        EVIDENCE_SCRIPT,
        "不执行模型生成的代码",
    ):
        assert needle in doc, f"docs/security_sandbox.md 缺 {needle!r}"
