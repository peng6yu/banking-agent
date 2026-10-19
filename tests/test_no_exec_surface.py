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
"""

from __future__ import annotations

from pathlib import Path

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
