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
        "跨轮方向不一致：8 轮完整 80 例真实 A/B（5 轮 MiniMax + 3 轮 DeepSeek）中，"
        "逐轮 intent 的 B−A 为 −1.25/+2.50/0/+2.50/+5.00 与 +5.00/−2.50/+8.75pp，符号反复；"
        "同配置 A 组复跑极差 6.25pp（MiniMax）/11.25pp（DeepSeek），与差值同量级。"
        "只有 20261024T160805 的 +8.75pp CI[+2.50,+16.25] 不含 0，但它是单轮单次采样、"
        "不能替代重复采样的方差。稳定成立的是行为面：B 组知识引用正确率 41–42/51，A 组为 0。"
        "结论：不宣称知识层稳定提升准确率；逐轮配对与 bootstrap 明细见 docs/benchmark/ab_paired_stats.md。"
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
            "status": "方向不稳定（8 轮配对统计）",
            "detail": "5 轮 MiniMax + 3 轮 DeepSeek 的逐轮 B−A 符号反复；只有单轮单次采样的 +8.75pp CI 不含 0，"
                      "不能宣称稳定提升。稳定成立的是知识引用（B 41–42/51 vs A 0）。详见 docs/benchmark/ab_paired_stats.md。",
        },
        {
            "item": "人工接管",
            "status": "本地操作员队列",
            "detail": "app/handoffs.py + /api/handoffs：pending/claimed/need_info/rejected/cancelled/resumed/failed 状态机，"
                      "领取用条件 UPDATE 保证同单只被一人领走；恢复前重查确认卡有效期、计划授权、账户状态与余额。"
                      "没有真实客服系统、没有 RBAC，operator 只是审计里的一个字符串。",
        },
        {
            "item": "沙箱",
            "status": "消除执行面（非容器沙箱）",
            "detail": "主 Agent 不执行模型生成的代码：模型输出只落 intent/slots/tool+params，经 registry 校验后调用静态 handler。"
                      "没有 Python/shell/SQL 动态执行面（scripts/prove_no_exec_surface.py 实测 0 命中），"
                      "因此未部署容器沙箱；与赛题沙箱条款的对应与剩余风险见 docs/security_sandbox.md。",
        },
        {
            "item": "生日预算",
            "status": "真实预留（影响可支配金额）",
            "detail": "plan_budgets 记 account_id/hold_id，创建时校验可用余额；funds.available_fen = 余额 − 在途 hold − 有效预算预留，"
                      "所有资金写操作都经过 _create_hold 的余额检查与 commit 的条件扣款。",
        },
        {
            "item": "生日一次性授权",
            "status": "版本绑定 + 到期重判",
            "detail": "plan_authorizations 绑定计划版本、账户与金额范围；到点执行前按用户维度日累计重新裁决，"
                      "升级红色或参数变化则暂停要求新的强验证。",
        },
        {
            "item": "跨会话幂等",
            "status": "未实现",
            "detail": "幂等键含 session_id；换会话重新发起视为新的一笔。",
        },
        {
            "item": "浏览器旅程",
            "status": "7 条（j1/j2/j4/j5/j6/j7 + j3）",
            "detail": "Playwright 真浏览器 + 服务端状态双证。j3 用真模型后端单独跑（其余六条为 "
                      "MOCK_MODE=script 确定性桩），J4/J5 的步骤是 API 驱动的流程级断言，不是纯点击级；"
                      "J6/J7 是点击级。详见 docs/benchmark/browser_journey_audit.md。",
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
        "note": "文档里的数字必须与本文件一致；改代码后重新跑本脚本，不要手工改文档里的计数。git.head 是生成本清单时的提交；清单自身与其后的纯文档/PPT 提交不改变代码事实。",
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
