"""浏览器级冒烟的 pytest 外壳：把「答辩当天不会白屏」钉成一条回归测试。

为什么单独一条、而不是并进 tests/test_frontend.py：
那条是纯正则扫 HTML 源码，一行 JavaScript 都不执行；这条要真起后端、真开
Chromium、真敲字、真点按钮，代价是十几秒和两个真实进程。塞进默认套件会把
「662 条秒级跑完」的体验拖垮，所以默认跳过，要跑就显式开：

    RUN_BROWSER_SMOKE=1 python3.12 -m pytest tests/test_browser_smoke.py -q
    RUN_BROWSER_SMOKE=1 python3.12 -m pytest tests/ -q        # 跟全套一起跑

脚本本身在 scripts/browser_smoke.py，沙箱隔离（不碰演示库）由脚本内部断言。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SHOT_DIR = ROOT / "docs" / "交付" / "截图"
SHOTS = ("01_balance_query.png", "02_confirm_card.png", "03_confirm_done.png")

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_BROWSER_SMOKE") != "1",
    reason="浏览器冒烟默认不跑（要真起后端 + 真开浏览器）；设 RUN_BROWSER_SMOKE=1 显式开启",
)


@pytest.fixture(scope="module")
def smoke() -> subprocess.CompletedProcess[str]:
    """跑一次真浏览器冒烟，整条用例共用这一份结果。"""
    return subprocess.run(
        [sys.executable, "scripts/browser_smoke.py"],
        cwd=ROOT, capture_output=True, text=True, timeout=420, check=False,
    )


def test_smoke_passes(smoke: subprocess.CompletedProcess[str]) -> None:
    assert smoke.returncode == 0, (
        f"浏览器冒烟没通过（退出码 {smoke.returncode}）\n"
        f"--- stdout ---\n{smoke.stdout[-4000:]}\n"
        f"--- stderr ---\n{smoke.stderr[-2000:]}"
    )


def test_demo_db_untouched(smoke: subprocess.CompletedProcess[str]) -> None:
    """冒烟不许改答辩现场那份演示库 data/bank.db（沙箱隔离的反证）。"""
    assert "[不变量] 演示库 data/bank.db sha256 未变" in smoke.stdout, (
        "冒烟没有打印演示库未变的反证行；隔离可能被 --reuse 之类的路径绕开了"
    )


def test_screenshots_are_real_and_distinct() -> None:
    """截图必须是真 PNG、够大、且三张互不相同（挡住「复制一份充数」）。"""
    digests: set[bytes] = set()
    for name in SHOTS:
        path = SHOT_DIR / name
        assert path.exists(), f"截图没生成：{path}"
        raw = path.read_bytes()
        assert raw[:8] == b"\x89PNG\r\n\x1a\n", f"{name} 不是 PNG"
        assert len(raw) > 8000, f"{name} 只有 {len(raw)} 字节，太小，不像真截图"
        assert raw[12:16] == b"IHDR", f"{name} 缺 IHDR 块"
        digests.add(raw)
    assert len(digests) == len(SHOTS), "三张截图里有内容完全相同的，不是三场不同的现场"


def test_report_link_not_rot() -> None:
    """README 里链的冒烟报告必须真的在（挡住链接腐烂）。"""
    report = ROOT / "docs" / "交付" / "浏览器冒烟报告.md"
    assert report.exists(), f"README 链的报告不存在：{report}"
