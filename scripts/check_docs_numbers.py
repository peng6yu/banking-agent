#!/usr/bin/env python3
"""校验文档里的数字与 `docs/benchmark/release_manifest.json` 一致（W6：文档单一事实源）。

为什么要有这个脚本：清单（`scripts/release_manifest.py` 生成）已经是代码事实的唯一出处，
但文档正文里的「1719 passed」「47 条 API 路径」「33 张表」这些数字仍然是手写的——
改一次代码就要手工追十几处，迟早对不上。这里把「数字该是多少」从人脑搬到机器：
清单说多少，文档就得写多少；只有旧数字、没有新数字的那一行，直接报错。

判定口径（故意窄：宁可漏报，不要天天误报）：
  1. 只认清单里有事实源的数：pytest 通过数 / 跳过数、受管工具数、数据库表数、审计事件类数、
     API 路径数、红队 N/N、e2e N/N、前端门禁 N/N。
  2. 只认**带量词或带上下文**的写法（「47 条 API 路径」「33 张表」「66 类审计事件」「红队 43/43」）。
     裸数字不认——子模块计数（「4 张表 + 7 工具」）、行号（`test_x.py:68/82`）、HTTP 状态码、
     像素值都不是版本事实，认了只会天天误报。数字一律取两位以上，一位数的子模块计数天然被排除。
  3. 同一行里只要出现过一次**正确值**就算过关：文档经常一行里同时写「本轮 1710（改造前 904）」。
  4. 带历史标记的行整行不看（历史/基线/改造前/旧值/作废/此前/当时/快照/降为）。
  5. 按轮次记事的快照类文档整文件不看（改造前基线、审计快照、评审记录、已知问题记录、
     A/B 与浏览器旅程的当轮审计）。原则：**陈述「现在」的文档要跟着清单走；记录「当时」的文档是证据，不许改。**

用法：
    .venv/bin/python scripts/check_docs_numbers.py
    .venv/bin/python scripts/check_docs_numbers.py --verbose   # 顺便打印每类规则命中几行

退出码：0 全部一致；1 有对不上的地方（逐条列 文件:行号 + 期望值 + 该行原文）。
"""

from __future__ import annotations

import argparse
import functools
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "docs" / "benchmark" / "release_manifest.json"
SKIP_DIRS = {".venv", "tmp", ".git", "__pycache__", "node_modules"}

#: 记录「当时」的文档：数字是那一轮的证据，不跟清单走，也不许被本脚本改。
SKIP_FILES = {
    "docs/benchmark/release_manifest.json",
    "docs/改造实施基线.md",
    "docs/12_主动财务管理设计.md",
    "docs/14_安全回归报告.md",
    "docs/已知问题处理记录.md",
    "docs/交付/演练记录.md",
    "docs/交付/浏览器冒烟报告.md",
    "docs/benchmark/air_vs_banking_agent.md",
    "docs/benchmark/browser_journey_audit.md",
    "docs/红队失败归因.md",
}
SKIP_PREFIXES = ("docs/audit/", "docs/交付/评审记录_", "docs/benchmark/ab_")
SKIP_LINE = re.compile(r"历史|基线|改造前|旧值|作废|此前|当时|快照|降为")


@dataclass(frozen=True)
class Claim:
    """一条「文档里怎么说的」→「清单里是多少」的规则。"""

    key: str
    label: str
    patterns: tuple[re.Pattern[str], ...]
    expect: Callable[[dict], int]


def fact(manifest: dict, *path: str) -> int:
    node = manifest
    for part in path:
        node = node[part]
    return int(node)


@functools.lru_cache(maxsize=1)
def frontend_checks() -> int:
    """前端门禁的检查项数：直接问 check_frontend.py，不在文档里再抄一遍。"""
    proc = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "check_frontend.py")],
        capture_output=True,
        text=True,
    )
    match = re.search(r"前端检查通过：(\d+)/(\d+)", proc.stdout)
    if not match:
        raise RuntimeError("check_frontend.py 没有输出「前端检查通过：N/M」，取不到前端检查项数")
    return int(match.group(2))


def build_claims(manifest: dict) -> list[Claim]:
    """把清单里的事实翻成一组正则规则。数字一律两位以上，避免把子模块计数当总数。"""
    return [
        Claim(
            "pytest",
            "pytest 通过数",
            (
                re.compile(r"(?P<n>\d{4,5})\s*passed"),
                re.compile(r"(?P<n>\d{4,5})\s*条测试(?:全绿|通过)"),
            ),
            lambda m: fact(m, "tests", "passed"),
        ),
        Claim(
            "skipped",
            "pytest 跳过数",
            (re.compile(r"(?P<n>\d{1,2})\s*skipped"),),
            lambda m: fact(m, "tests", "skipped"),
        ),
        Claim(
            "tools",
            "受管工具数",
            (
                re.compile(r"(?P<n>\d{2,3})\s*(?:个|款)\s*受管工具"),
                re.compile(r"(?P<n>\d{2,3})\s*工具\s*[/／|]\s*(?:\d|规则)"),
            ),
            lambda m: fact(m, "code", "tools", "total"),
        ),
        Claim(
            "tables",
            "数据库表数",
            (
                re.compile(r"(?P<n>\d{2,3})\s*(?:张|个)\s*(?:数据库|SQLite)?\s*表(?!单)"),
                re.compile(r"\|\s*数据表\s*\|[^|\n]*\|\s*(?P<n>\d{1,3})\s*\|"),
            ),
            lambda m: fact(m, "code", "db_tables"),
        ),
        Claim(
            "events",
            "审计事件类数",
            (
                re.compile(r"(?P<n>\d{2,3})\s*(?:类|个)\s*审计事件"),
                re.compile(r"\|\s*审计事件(?:类型)?\s*\|[^|\n]*\|\s*(?P<n>\d{1,3})\s*\|"),
            ),
            lambda m: fact(m, "code", "audit_event_types"),
        ),
        Claim(
            "paths",
            "API 路径数",
            (re.compile(r"(?P<n>\d{2,3})\s*(?:条)?\s*API\s*路径"),),
            lambda m: fact(m, "code", "api_paths"),
        ),
        Claim(
            "redteam",
            "红队通过数",
            (re.compile(r"红队[^。；，\n]{0,10}?(?P<n>\d{2})\s*/\s*(?P=n)"),),
            lambda m: fact(m, "redteam", "pass"),
        ),
        Claim(
            "e2e",
            "e2e 通过数",
            (re.compile(r"(?:e2e|端到端)[^。；，\n]{0,10}?(?P<n>\d{2})\s*/\s*(?P=n)"),),
            lambda m: fact(m, "e2e", "pass"),
        ),
        Claim(
            "frontend",
            "前端检查通过数",
            (
                re.compile(r"前端[^。；，\n]{0,12}?(?P<n>\d{2,3})\s*/\s*(?P=n)"),
                re.compile(r"前端\s*(?P<n>\d{2,3})\s*项\s*静态校验"),
            ),
            lambda _m: frontend_checks(),
        ),
    ]


def iter_docs() -> list[Path]:
    """README + docs 下的 .md/.py；快照类文档与 A/B 统计排除。"""
    files: list[Path] = []
    readme = ROOT / "README.md"
    if readme.exists():
        files.append(readme)
    for path in sorted((ROOT / "docs").rglob("*")):
        if path.suffix not in {".md", ".py"}:
            continue
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        rel = path.relative_to(ROOT).as_posix()
        if rel in SKIP_FILES or rel.startswith(SKIP_PREFIXES):
            continue
        files.append(path)
    return files


def _numbers(claim: Claim, line: str) -> list[int]:
    found: list[int] = []
    for pattern in claim.patterns:
        found.extend(int(m.group("n")) for m in pattern.finditer(line))
    return found


def check(
    verbose: bool = False, docs: list[Path] | None = None
) -> list[tuple[str, int, str, str]]:
    """返回 [(位置, 期望值, 规则名, 该行原文)]；空列表表示全部一致。

    docs 只在测试里用：给一份临时文档，验证「写错能被抓到 / 带历史标记的行放行」。
    """
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    claims = build_claims(manifest)
    expects = {claim.key: claim.expect(manifest) for claim in claims}
    problems: list[tuple[str, int, str, str]] = []
    hits = {claim.key: 0 for claim in claims}

    for path in (iter_docs() if docs is None else docs):
        rel = path.relative_to(ROOT).as_posix() if path.is_relative_to(ROOT) else path.name
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if SKIP_LINE.search(line):
                continue
            for claim in claims:
                found = _numbers(claim, line)
                if not found:
                    continue
                hits[claim.key] += 1
                if expects[claim.key] not in found:
                    problems.append(
                        (f"{rel}:{lineno}", expects[claim.key], claim.label, line.strip()[:160])
                    )

    if verbose:
        for claim in claims:
            print(f"  {claim.label}：命中 {hits[claim.key]} 行（清单值 {expects[claim.key]}）")
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description="校验文档数字与 release_manifest.json 一致")
    parser.add_argument("--verbose", action="store_true", help="打印每类规则命中了几行")
    args = parser.parse_args()

    problems = check(verbose=args.verbose)
    if not problems:
        print("文档数字与清单一致。")
        return 0

    print(f"有 {len(problems)} 处数字与 release_manifest.json 对不上：")
    for where, expect, label, text in problems:
        print(f"  {where}  {label}应为 {expect}，这一行写的是：{text}")
    print("\n改法：把文档改成清单里的数；若这一行记的确实是过去某一轮的数，补上「历史/基线/当时」标记。")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())