#!/usr/bin/env python3
"""生成 docs/benchmark/release_manifest.json：一份「同一口径」的版本事实清单。

为什么要有这个文件：README、进度文件、实施优先级、AIR 差距矩阵、提交物对照表、
最终验收报告各写各的数字，一改代码就有五处对不上。这里的事实全部从代码与运行产物里读，
文档只引用本文件，不再各自抄数字。

用法：
    .venv/bin/python scripts/release_manifest.py \
        --pytest-log tmp/pytest_final.log \
        --redteam-log tmp/redteam_final.log \
        --e2e-log tmp/e2e_final.log
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
OUT = ROOT / "docs" / "benchmark" / "release_manifest.json"

PYTEST_RE = re.compile(
    r"(?P<passed>\d+) passed(?:, (?P<failed>\d+) failed)?(?:, (?P<skipped>\d+) skipped)?"
    r"(?:, \d+ warnings?)? in (?P<duration>[\d.]+)s"
)
REDTEAM_RE = re.compile(r"PASS (?P<pass>\d+)\s*/\s*FAIL (?P<fail>\d+)")
E2E_RE = re.compile(r"(?P<pass>\d+)\s*/\s*(?P<total>\d+)\s*PASS", re.IGNORECASE)


def sh(*args: str) -> str:
    try:
        return subprocess.run(args, cwd=ROOT, capture_output=True, text=True, timeout=20).stdout.strip()
    except Exception:  # noqa: BLE001 - 取不到就如实留空，不编
        return ""


def code_facts() -> dict[str, Any]:
    from app import db
    from app.api.main_probe import api_paths  # type: ignore  # noqa: F401 - 占位，见下方回退
    return {}


def gather_code() -> dict[str, Any]:
    """从代码里读计数；不写死任何数字。"""
    import app.main as main_mod
    from app import db
    from app.audit import EVENT_TYPES
    from app.knowledge import loader
    from app.planner.dag import INTENT_TEMPLATES
    from app.policy.rules import RULES
    from app.tools.registry import READONLY_TOOL_NAMES, TOOL_NAMES, WRITE_TOOL_NAMES

    kb = loader.load()
    return {
        "tools": {
            "total": len(TOOL_NAMES),
            "readonly": len(READONLY_TOOL_NAMES),
            "write": len(WRITE_TOOL_NAMES),
            "names": list(TOOL_NAMES),
        },
        "db_tables": len(db.TABLE_NAMES),
        "db_table_names": list(db.TABLE_NAMES),
        "audit_event_types": len(EVENT_TYPES),
        "api_paths": len(main_mod.app.openapi()["paths"]),
        "policy_rules": len(RULES),
        "intent_templates": len(INTENT_TEMPLATES),
        "test_files": len(sorted((ROOT / "tests").glob("test_*.py"))),
        "knowledge": {
            "version": kb.version,
            "terms": len(kb.terms),
            "entities": len(kb.entities),
            "relations": len(kb.relations),
            "intents": len(kb.intents),
            "workflows": len(kb.workflows),
            "sources": len(kb.sources),
            "warnings": len(kb.warnings),
        },
    }


def gather_git() -> dict[str, Any]:
    dirty = [line for line in sh("git", "status", "--porcelain").splitlines() if line.strip()]
    return {
        "head": sh("git", "rev-parse", "HEAD"),
        "head_short": sh("git", "rev-parse", "--short", "HEAD"),
        "subject": sh("git", "log", "-1", "--format=%s"),
        "branch": sh("git", "rev-parse", "--abbrev-ref", "HEAD"),
        "dirty": bool(dirty),
        "changed_paths": len(dirty),
    }


def parse_pytest(path: Path | None) -> dict[str, Any] | None:
    if path is None or not path.exists():
        return None
    text = path.read_text(encoding="utf-8", errors="replace")
    matches = list(PYTEST_RE.finditer(text))
    if not matches:
        return {"log": str(path.relative_to(ROOT)), "parsed": False}
    last = matches[-1]
    return {
        "command": ".venv/bin/python -m pytest -q",
        "passed": int(last.group("passed")),
        "failed": int(last.group("failed") or 0),
        "skipped": int(last.group("skipped") or 0),
        "duration_s": float(last.group("duration")),
        "log": str(path.relative_to(ROOT)),
        "parsed": True,
    }


def parse_redteam(path: Path | None) -> dict[str, Any] | None:
    if path is None or not path.exists():
        return None
    text = path.read_text(encoding="utf-8", errors="replace")
    total = len(re.findall(r"^\s*跑第 \d+/", text, re.MULTILINE))
    match = REDTEAM_RE.search(text)
    return {
        "command": ".venv/bin/python -m scripts.redteam",
        "cases_run": total,
        "pass": int(match.group("pass")) if match else None,
        "fail": int(match.group("fail")) if match else None,
        "log": str(path.relative_to(ROOT)),
    }


def parse_e2e(path: Path | None) -> dict[str, Any] | None:
    if path is None or not path.exists():
        return None
    text = path.read_text(encoding="utf-8", errors="replace")
    match = None
    for match in E2E_RE.finditer(text):
        pass
    if match is None:
        # 实际输出形如「合计 13 步，PASS 13，FAIL 0」，上面的通用式抓不到，退回本仓库的真实格式
        match = re.search(r"合计 (?P<total>\d+) 步，PASS (?P<pass>\d+)", text)
    return {
        "command": ".venv/bin/python scripts/e2e.py --offline",
        "pass": int(match.group("pass")) if match else None,
        "total": int(match.group("total")) if match else None,
        "log": str(path.relative_to(ROOT)),
    }


def gather_ab() -> dict[str, Any]:
    rounds: list[dict[str, Any]] = []
    for summary_path in sorted((ROOT / "tests" / "eval" / "results").glob("*/summary.json")):
        try:
            data = json.loads(summary_path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 - 坏文件跳过，不编数字
            continue
        groups = data.get("groups") or {}
        row: dict[str, Any] = {
            "dir": str(summary_path.parent.relative_to(ROOT)),
            "started_at": data.get("started_at"),
            "model": data.get("model"),
            "adapter": data.get("adapter"),
            "cases": data.get("case_count"),
            "knowledge_version": data.get("knowledge_version"),
            "groups": {},
        }
        for key, group in groups.items():
            metrics = group.get("metrics") or {}
            row["groups"][key] = {
                "intent_accuracy": (metrics.get("intent_accuracy") or {}).get("value"),
                "slot_accuracy": (metrics.get("slot_accuracy") or {}).get("value"),
                "clarify_accuracy": (metrics.get("clarify_accuracy") or {}).get("value"),
                "dag_validity": (metrics.get("dag_validity") or {}).get("value"),
                "knowledge_citation": (metrics.get("knowledge_citation") or {}).get("value"),
                "unauthorized_write_attempts": (metrics.get("unauthorized_write_attempts") or {}).get("value"),
            }
        rounds.append(row)
    latest = rounds[-1] if rounds else None
    conclusion = (
        "跨轮方向不一致：同一份 80 例、同一模型，B 组相对 A 组的 intent 差值在三轮之间反复变号，"
        "不能宣称知识层带来 x pp 的稳定提升。稳定成立的只有两项：B 组知识引用正确率（41/51、41/51、42/51）"
        "与未授权写操作在 r2/r3 归零（r1 两组各 1）。"
    )
    return {"rounds": rounds, "latest": latest, "conclusion": conclusion, "dataset": "tests/eval/intent_cases.yaml"}


def gather_browser() -> dict[str, Any]:
    out: dict[str, Any] = {}
    shots = ROOT / "docs" / "交付" / "截图" / "信息架构" / "browser_report.json"
    if shots.exists():
        data = json.loads(shots.read_text(encoding="utf-8"))
        out["screenshots"] = {
            "report": str(shots.relative_to(ROOT)),
            "views": len(data.get("shots") or []),
            "checks": len(data.get("checks") or []),
            "checks_all_ok": all(item.get("no_hscroll") for item in (data.get("checks") or [])),
            "js_errors": len(data.get("js_errors") or []),
            "payee_flow": bool(data.get("payee")),
            "red_flow": bool(data.get("red_flow")),
            "daily_block": bool(data.get("daily_block")),
            "idempotent_refresh": bool(data.get("idempotent_refresh")),
        }
    journeys = ROOT / "docs" / "交付" / "browser_journeys.json"
    if journeys.exists():
        data = json.loads(journeys.read_text(encoding="utf-8"))
        out["journeys"] = {
            "report": str(journeys.relative_to(ROOT)),
            "status": data.get("status"),
            "items": {key: (value or {}).get("status") for key, value in (data.get("journeys") or {}).items()},
            "demo_db_unchanged": data.get("demo_db_unchanged"),
        }
    return out


def gather_limits() -> list[dict[str, str]]:
    return [
        {
            "item": "多因子渠道",
            "status": "演示桩",
            "detail": "FACTOR_PROVIDER 未配置时走 demo_stub 回显；非演示模式（FACTOR_DEMO_ECHO=0）没有真实渠道会直接失败，不会放行。",
        },
        {
            "item": "理财产品数据",
            "status": "contest_simulation",
            "detail": "fund_products 是本地种子；期限/流动性/费率显式 null，不编造；没做产品对比。",
        },
        {
            "item": "后台节拍",
            "status": "单进程",
            "detail": "process 内 asyncio，进程退出即停；多 worker 未验证；只推进显式打开监测的目标。",
        },
        {
            "item": "知识层 A/B",
            "status": "方向不稳定",
            "detail": "三轮 DeepSeek 结果方向反复，只认知识引用正确率与未授权写归零两项。",
        },
        {
            "item": "跨会话幂等",
            "status": "未实现",
            "detail": "幂等键含 session_id；换会话重新发起视为新的一笔。",
        },
        {
            "item": "浏览器旅程",
            "status": "5 条流程级",
            "detail": "Playwright 真浏览器 + 服务端状态双证；不是点击级 DOM 单测覆盖。",
        },
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description="生成 release_manifest.json")
    parser.add_argument("--pytest-log", default="tmp/pytest_final.log")
    parser.add_argument("--redteam-log", default="tmp/redteam_final.log")
    parser.add_argument("--e2e-log", default="tmp/e2e_final.log")
    parser.add_argument("--out", default=str(OUT))
    args = parser.parse_args()

    def _p(raw: str) -> Path | None:
        path = Path(raw)
        path = path if path.is_absolute() else ROOT / path
        return path if path.exists() else None

    manifest: dict[str, Any] = {
        "schema_version": 1,
        "generated_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "git": gather_git(),
        "code": gather_code(),
        "tests": parse_pytest(_p(args.pytest_log)),
        "redteam": parse_redteam(_p(args.redteam_log)),
        "e2e": parse_e2e(_p(args.e2e_log)),
        "ab": gather_ab(),
        "browser": gather_browser(),
        "limits": gather_limits(),
        "note": "文档里的数字必须与本文件一致；改代码后重新跑本脚本，不要手工改文档里的计数。",
    }
    out = Path(args.out)
    out.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[manifest] {out}")
    code = manifest["code"]
    print(
        f"  git={manifest['git']['head_short']} dirty={manifest['git']['dirty']} "
        f"tools={code['tools']['total']} tables={code['db_tables']} events={code['audit_event_types']} "
        f"paths={code['api_paths']} tests={code['test_files']} kb={code['knowledge']['version']}"
    )
    if manifest["tests"]:
        print(f"  pytest: {manifest['tests']}")
    ab = manifest["ab"]
    if ab["latest"]:
        print(f"  A/B latest: {ab['latest']['dir']} model={ab['latest']['model']}")
        for key in ("a", "b"):
            print(f"    {key}: {ab['latest']['groups'].get(key)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
