"""原话级金融操作防护评估（离线、确定性）。

用法：
    .venv/bin/python tests/eval/run_utterance_eval.py            # 打印表格 + 指标
    .venv/bin/python tests/eval/run_utterance_eval.py --json     # 额外输出 JSON
    .venv/bin/python tests/eval/run_utterance_eval.py --out f.json

它测三层确定性能力，不测模型：
1. app/planner/payment_guard.inspect —— 原话里像不像有多收款人/多笔付款；
2. app/security/injection.scan —— 引用转述与备注里的注入特征；
3. app/planner/intent._fallback_result —— 没有模型时的兜底解析，以及由它派生的
   计划里到底有没有资金写操作（dag.build_or_single + policy.rules.WRITE_TOOLS）。

为什么用兜底解析而不是线上模型：离线可复跑、结果可复核，任何人 clone 下来都能跑。
模型的准确率另有一套（tests/eval/run_eval.py，需要模型服务），两者结论不能互相替代。

局限（报告里必须原样带上）：
- 不覆盖模型幻觉；不覆盖真实多轮会话状态（multi_turn_conflict 是单轮重解析近似）；
- 「未授权写操作」只统计「该问没问却建出了资金写操作计划」，不等于线上一定执行了；
- 数据集 33 条，只能说明这些表达方式，不能外推成「系统对所有话术都安全」。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# 兜底解析依赖知识层的术语表（app/planner/kb_bridge.py 的 TERM_INTENT）：
# 关掉知识层，兜底就只剩 unknown，等于把「意图」这一项测成 0 分。这里显式打开，
# 与线上默认一致；知识层本身的好坏另由 run_eval.py 的 A/B 负责。
os.environ.setdefault("KNOWLEDGE_ENABLED", "1")

from app import money  # noqa: E402
from app.planner import dag as dag_mod  # noqa: E402
from app.planner import intent as intent_mod  # noqa: E402
from app.planner import payment_guard  # noqa: E402
from app.policy import rules as policy_rules  # noqa: E402
from app.security import injection  # noqa: E402

DEFAULT_CASES = Path("tests/eval/utterance_cases.yaml")
MONEY_TOOLS = frozenset(policy_rules.SPEND_TOOLS) | frozenset(policy_rules.INVEST_TOOLS)
CATEGORY_ORDER = (
    "multi_payee",
    "multi_amount",
    "negation",
    "conditional",
    "colloquial",
    "quoted_injection",
    "note_injection",
    "vague_object",
    "mid_change",
    "multi_turn_conflict",
)
CATEGORY_LABELS = {
    "multi_payee": "多收款人",
    "multi_amount": "多金额",
    "negation": "否定表达",
    "conditional": "条件表达",
    "colloquial": "口语表达",
    "quoted_injection": "引用内容里的恶意指令",
    "note_injection": "交易备注里的提示注入",
    "vague_object": "模糊对象",
    "mid_change": "用户中途修改",
    "multi_turn_conflict": "参数冲突",
}


def load_cases(path: Path = DEFAULT_CASES) -> list[dict[str, Any]]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    cases = data.get("cases") if isinstance(data, dict) else None
    if not isinstance(cases, list) or not cases:
        raise ValueError(f"{path} 里没有 cases")
    return cases


def _plan_tools(parsed: Any) -> tuple[list[str], list[str]]:
    """返回（计划里的工具, 其中的资金写工具）。建图失败不算写操作。"""
    if not getattr(parsed, "ok", False):
        return [], []
    payload = {"intent": parsed.intent, "slots": dict(getattr(parsed, "slots", {}) or {})}
    try:
        plan = dag_mod.build_or_single(payload)
    except Exception:  # noqa: BLE001 - 建图失败本身就是一种结论
        return [], []
    tools = [node.tool for node in plan.nodes]
    return tools, [tool for tool in tools if tool in MONEY_TOOLS]


def _flag(value: Any) -> str:
    """YAML 1.1 把 yes/no 读成布尔，这里统一成字符串，避免数据集写法影响判定。"""
    if value is True:
        return "yes"
    if value is False:
        return "no"
    return str(value or "any")


def evaluate(case: dict[str, Any]) -> dict[str, Any]:
    text = str(case["text"])
    expect = case.get("expect") or {}
    finding = payment_guard.inspect(text)
    scan = injection.scan(text)
    parsed = intent_mod._fallback_result(text)
    # 线上 parse() 在兜底结果之后还会过一遍原话预检查（_guard_payment）：歧义句会被
    # 换成一句反问，不会建出任何计划。评估必须走同一条路，否则统计的是「没接守护的解析」。
    guarded = intent_mod._guard_payment(parsed, text)
    if guarded is not None:
        parsed = guarded
    tools, money_tools = _plan_tools(parsed)

    guard_blocked = bool(getattr(finding, "ambiguous", False))
    reason = str(getattr(finding, "reason", "") or "") if guard_blocked else ""
    asked = (parsed.ok is False) and bool(getattr(parsed, "question", ""))
    slots = dict(getattr(parsed, "slots", {}) or {})
    payee = str(slots.get("payee_name") or "")
    amount_fen = slots.get("amount_fen")

    checks: list[tuple[str, bool]] = []
    want_guard = _flag(expect.get("guard"))
    if want_guard == "block":
        checks.append(("原话预检查命中", guard_blocked))
    elif want_guard == "pass":
        checks.append(("原话预检查未误伤", not guard_blocked))
    if expect.get("reason"):
        checks.append(("理由码正确", reason == str(expect["reason"])))
    want_clarify = _flag(expect.get("clarify"))
    if want_clarify == "yes":
        checks.append(("必须先追问", asked))
    elif want_clarify == "no":
        checks.append(("不应反问", not asked))
    want_intent = expect.get("intent")
    if want_intent:
        checks.append(("意图正确", str(getattr(parsed, "intent", "")) == str(want_intent)))
    want_money = _flag(expect.get("money")) if expect.get("money") else ("forbidden" if want_clarify == "yes" else "any")
    if want_money == "forbidden":
        checks.append(("没有资金写操作计划", not money_tools))
    elif want_money == "allowed":
        checks.append(("允许构建资金计划", bool(money_tools)))
    if case.get("assert_payee"):
        checks.append(("收款人是 " + str(case["assert_payee"]), payee == str(case["assert_payee"])))
    if case.get("assert_not_payee"):
        checks.append(("收款人不是 " + str(case["assert_not_payee"]), payee != str(case["assert_not_payee"])))
    if case.get("assert_amount_fen") is not None:
        checks.append(("金额是 " + money.fen_to_yuan_str(int(case["assert_amount_fen"])) + " 元", amount_fen == int(case["assert_amount_fen"])))

    # 未授权写操作：该先澄清却没澄清，同时画出了资金写操作的计划。
    unauthorized_write = bool(money_tools) and (want_clarify == "yes" or want_guard == "block")
    failed = [name for name, ok in checks if not ok]
    return {
        "id": case["id"],
        "category": case["category"],
        "text": text,
        "note": case.get("note", ""),
        "expect": expect,
        "observed": {
            "guard_blocked": guard_blocked,
            "guard_reason": reason,
            "guard_question": str(getattr(finding, "question", "") or ""),
            "injection_hit": bool(getattr(scan, "hit", False)),
            "injection_reasons": list(getattr(scan, "reasons", []) or []),
            "intent": str(getattr(parsed, "intent", "")),
            "asked": asked,
            "question": str(getattr(parsed, "question", "") or ""),
            "missing": list(getattr(parsed, "missing", []) or []),
            "slots": slots,
            "tools": tools,
            "money_tools": money_tools,
        },
        "checks": [{"name": name, "ok": ok} for name, ok in checks],
        "failed": failed,
        "passed": not failed,
        "unauthorized_write": unauthorized_write,
    }


def summarize(results: list[dict[str, Any]]) -> dict[str, Any]:
    total = len(results)
    passed = sum(1 for row in results if row["passed"])
    unauthorized = [row["id"] for row in results if row["unauthorized_write"]]
    per_category: dict[str, dict[str, Any]] = {}
    buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in results:
        buckets[row["category"]].append(row)
    for category in CATEGORY_ORDER + tuple(sorted(set(buckets) - set(CATEGORY_ORDER))):
        rows = buckets.get(category) or []
        if not rows:
            continue
        per_category[category] = {
            "label": CATEGORY_LABELS.get(category, category),
            "cases": len(rows),
            "passed": sum(1 for row in rows if row["passed"]),
            "pass_rate": round(sum(1 for row in rows if row["passed"]) / len(rows) * 100, 1),
            "failed_ids": [row["id"] for row in rows if not row["passed"]],
            "clarify_asked": sum(1 for row in rows if row["observed"]["asked"]),
            "guard_hits": sum(1 for row in rows if row["observed"]["guard_blocked"]),
            "injection_hits": sum(1 for row in rows if row["observed"]["injection_hit"]),
        }
    clarify_rows = [row for row in results if _flag((row.get("expect") or {}).get("clarify")) in ("yes", "no")]
    clarify_correct = sum(1 for row in clarify_rows if all(item["ok"] for item in row["checks"] if item["name"].startswith("必须") or item["name"].startswith("不应")))
    intent_rows = [row for row in results if (row.get("expect") or {}).get("intent")]
    intent_correct = sum(1 for row in intent_rows if all(item["ok"] for item in row["checks"] if item["name"] == "意图正确"))
    return {
        "generated_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "dataset": str(DEFAULT_CASES),
        "total": total,
        "passed": passed,
        "pass_rate": round(passed / total * 100, 1) if total else 0.0,
        "clarify_cases": len(clarify_rows),
        "clarify_accuracy": round(clarify_correct / len(clarify_rows) * 100, 1) if clarify_rows else 0.0,
        "intent_cases": len(intent_rows),
        "intent_accuracy": round(intent_correct / len(intent_rows) * 100, 1) if intent_rows else 0.0,
        "unauthorized_writes": len(unauthorized),
        "unauthorized_write_ids": unauthorized,
        "wrong_business_flows": [row["id"] for row in results if not row["passed"]],
        "per_category": per_category,
        "limitations": [
            "不覆盖模型幻觉：这里跑的是无模型兜底解析，模型路径见 tests/eval/run_eval.py。",
            "多轮冲突只做单轮重解析近似，没有真实的会话状态机回放。",
            "「未授权写操作」只统计「该问没问却建出了资金写操作计划」，不等于线上真的执行了。",
            "样本 " + str(total) + " 条，只代表这些表达方式，不能外推成对所有话术的安全保证。",
        ],
    }


def render(result: dict[str, Any], rows: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    lines.append("原话级金融操作防护评估（离线 · 确定性层 · 不含模型）")
    lines.append("数据集：" + result["dataset"])
    lines.append("")
    header = f"{'用例':<8}{'类别':<14}{'预检查':<7}{'反问':<6}{'意图':<14}{'资金工具':<18}结论"
    lines.append(header)
    lines.append("-" * len(header))
    for row in rows:
        observed = row["observed"]
        guard = ("命中" if observed["guard_blocked"] else "未命中")
        asked = ("是" if observed["asked"] else "否")
        money_text = ",".join(observed["money_tools"]) or "—"
        verdict = "通过" if row["passed"] else "不通过：" + "、".join(row["failed"])
        lines.append(
            f"{row['id']:<8}{row['category']:<14}{guard:<7}{asked:<6}{observed['intent']:<14}{money_text:<18}{verdict}"
        )
    lines.append("")
    lines.append("分类汇总：")
    for category, block in result["per_category"].items():
        lines.append(
            f"  {block['label']}（{category}）：{block['passed']}/{block['cases']} 通过，"
            f"追问 {block['clarify_asked']} 次，预检查命中 {block['guard_hits']} 次"
            + ("，未通过：" + "、".join(block["failed_ids"]) if block["failed_ids"] else "")
        )
    lines.append("")
    lines.append(
        f"总计：{result['passed']}/{result['total']} 通过（{result['pass_rate']}%）；"
        f"追问准确率 {result['clarify_accuracy']}%（{result['clarify_cases']} 条）；"
        f"意图准确率 {result['intent_accuracy']}%（{result['intent_cases']} 条）；"
        f"未授权写操作 {result['unauthorized_writes']} 次"
    )
    if result["unauthorized_write_ids"]:
        lines.append("未授权写操作用例：" + "、".join(result["unauthorized_write_ids"]))
    if result["wrong_business_flows"]:
        lines.append("业务流程不正确（含全部未通过项）：" + "、".join(result["wrong_business_flows"]))
    lines.append("")
    lines.append("局限（读结论前先读这一段）：")
    for item in result["limitations"]:
        lines.append("  · " + item)
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="原话级金融操作防护评估（离线）")
    parser.add_argument("--cases", default=str(DEFAULT_CASES))
    parser.add_argument("--json", action="store_true", help="把 JSON 也打到 stdout")
    parser.add_argument("--out", default="", help="把 JSON 写到这个文件")
    parser.add_argument("--strict", action="store_true", help="有一次未授权写操作就返回非零")
    args = parser.parse_args(argv)

    cases = load_cases(Path(args.cases))
    rows = [evaluate(case) for case in cases]
    result = summarize(rows)
    print(render(result, rows))
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    if args.out:
        Path(args.out).write_text(json.dumps({"summary": result, "cases": rows}, ensure_ascii=False, indent=2), encoding="utf-8")
        print("\n已写入 " + args.out)
    if args.strict and result["unauthorized_writes"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
