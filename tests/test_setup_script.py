"""一键配置脚本 scripts/setup.sh 的契约。

新电脑上「clone 完就能跑」这件事不能只靠 README 里的手写步骤，得有一个脚本兜住：
找解释器 -> 建 .venv -> 装依赖（绕开本机 pip.conf 的坏索引）-> 生成 .env ->
决定模型来源 -> 建库 -> 自检。这里只用静态与 dry-run 两层验它，真跑全流程会动
.venv 与 data/，不适合放进单测（端到端在提交前手工跑过一遍）。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "setup.sh"


def _run(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=120,
        env={**os.environ},
    )


def test_script_is_shell_syntax_clean() -> None:
    done = subprocess.run(["bash", "-n", str(SCRIPT)], capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr


def test_script_is_executable() -> None:
    assert os.access(SCRIPT, os.X_OK), "scripts/setup.sh 应当可直接执行"


def test_help_exits_zero_and_lists_the_options(tmp_path: Path) -> None:
    done = _run(["--help"], tmp_path)
    assert done.returncode == 0, done.stderr
    for flag in ("--ollama", "--mock", "--index", "--skip-seed", "--dry-run"):
        assert flag in done.stdout


def test_unknown_option_exits_two(tmp_path: Path) -> None:
    done = _run(["--no-such-flag"], tmp_path)
    assert done.returncode == 2
    assert "未知参数" in done.stderr


def _copy_minimal_project(tmp_path: Path) -> Path:
    """只带脚本运行必需的三样东西，保证 dry-run 没有别的东西可动。"""
    (tmp_path / "scripts").mkdir()
    shutil.copy2(SCRIPT, tmp_path / "scripts" / "setup.sh")
    shutil.copy2(REPO / "requirements.txt", tmp_path / "requirements.txt")
    shutil.copy2(REPO / ".env.example", tmp_path / ".env.example")
    return tmp_path


def test_dry_run_reports_the_plan_without_touching_anything(tmp_path: Path) -> None:
    root = _copy_minimal_project(tmp_path)
    before = sorted(p.name for p in root.iterdir())
    done = _run(["--dry-run"], root)
    assert done.returncode == 0, done.stderr + done.stdout
    assert "[dry-run]" in done.stdout
    assert not (root / ".venv").exists(), "dry-run 不该建 .venv"
    assert not (root / ".env").exists(), "dry-run 不该生成 .env"
    assert sorted(p.name for p in root.iterdir()) == before


def test_dry_run_honours_a_custom_index(tmp_path: Path) -> None:
    root = _copy_minimal_project(tmp_path)
    index = "https://mirrors.aliyun.com/pypi/simple"
    done = _run(["--dry-run", "--index", index], root)
    assert done.returncode == 0, done.stderr + done.stdout
    assert index in done.stdout


def test_dry_run_always_runs_the_pip_isolated_recipe(tmp_path: Path) -> None:
    """pip.conf 的坏索引是本机踩过的坑，脚本必须固定带上隔离参数。"""
    root = _copy_minimal_project(tmp_path)
    done = _run(["--dry-run"], root)
    assert "PIP_CONFIG_FILE=/dev/null" in done.stdout
    assert "--isolated" in done.stdout
    assert "requirements.txt" in done.stdout


def test_dry_run_on_mock_mode_skips_the_model_probe(tmp_path: Path) -> None:
    root = _copy_minimal_project(tmp_path)
    done = _run(["--dry-run", "--mock"], root)
    assert done.returncode == 0, done.stderr + done.stdout
    assert "MOCK_MODE=script" in done.stdout
    assert "provider_check" not in done.stdout
