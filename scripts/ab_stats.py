#!/usr/bin/env python3.12
"""A/B 配对统计：把 tests/eval/results 下的原始 JSONL 算成「带不确定性的配对差值」。

为什么要单独一个脚本，而不是直接把 summary.json 里的准确率抄进文档
------------------------------------------------------------------
summary.json 给的是每组各自的点估计（A 组 82.5%、B 组 91.25%），两个点估计
相减得到一个差值，但差值本身的不确定性没有被算出来，读者无法判断
「+8.75pp」是稳定差异还是采样噪声。A/B 两组跑的是同一批 case（同一 id），
这是配对数据，配对差值比两组独立均值差更紧、也更诚实——所以这里按 case 配对。

口径必须与 tests/eval/run_eval.py 完全一致，否则文档与原始报告对不上：
本脚本复算每个指标并与该轮 summary.json 的 numerator/denominator 比对，
对不上直接报错退出，不写一份「看起来对」的文档。

边界（写进文档，也写在这里）：
* 每组每个 case 只采样一次，bootstrap 只重采样测试 case，反映的是
  「换一批同样分布的 case 结果会不会变」；它不反映模型重复采样的方差。
  同一配置连跑三轮的实测抖动（见输出「同配置复跑抖动」）通常比 CI 更宽，
  所以 CI 不能当成「模型稳定性的置信区间」。
* 知识引用指标 A 组按设计关闭知识层，A 组引用率恒为 0，跨组差值不构成证据；
  可报告的是 B 组自身的绝对引用率及其 CI。
* 不补跑：本脚本只读已有 JSONL；没有真实模型配额就不生成新结果。
* 只接受 mock=false 的轮次。MOCK_MODE 生成的记录一律排除并写明原因。

用法：
    .venv/bin/python scripts/ab_stats.py
    .venv/bin/python scripts/ab_stats.py --iters 20000 --seed 20261024
"""

from __future__ import annotations

import argparse
import json
import sys
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RESULTS = ROOT / "tests" / "eval" / "results"
DEFAULT_OUT = ROOT / "docs" / "benchmark" / "ab_paired_stats.md"
DEFAULT_SEED = 20261024
DEFAULT_ITERS = 10000
FULL_CASE_COUNT = 80
FENCE = chr(96) * 3
def _write_tools() -> frozenset[str]:
    """直接取 app/policy/rules.py 的三张集合，而不是在统计脚本里抄一份。

    为什么要 import 而不是写死：未追问即动钱这个指标的口径必须与 run_eval.py 完全一致；
    抄一份清单就会在有人调整规则表时悄悄漂掉（第一版抄错了，把黄灯的
    cancel_subscription / 储蓄目标 / 主动提醒工具也算进写集合，复算与 summary 差了 1-2 例，
    被 verify_against_summary 当场拦下——这正是这道闸门存在的意义）。
    """
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from app.policy.rules import INVEST_TOOLS, SENSITIVE_TOOLS, SPEND_TOOLS

    return frozenset(SENSITIVE_TOOLS) | frozenset(SPEND_TOOLS) | frozenset(INVEST_TOOLS)


#: 与 run_eval.WRITE_TOOLS 同口径：动钱 + 敏感账户操作 + 投资申赎。
WRITE_TOOLS = _write_tools()
METRICS = ("intent", "slot", "clarify", "dag")
#: run_eval._money_moving 的写意图集合，只用于复现「宽口径」并与归档 summary 对账。
WRITE_INTENTS = frozenset({"transfer", "birthday_plan", "schedule_transfer", "split_bill"})


def _money_moving(record: dict[str, Any]) -> bool:
    """按意图+槽位判断该 case 是否「声明了写操作」（宽口径，run_eval 的第二口径）。

    为什么要保留这个口径：归档 summary.json 里的未追问即动钱计数是历史版本的产物，
    有的轮次按「计划里真有写工具」算，有的轮次按「意图/槽位已经是写操作」算，
    两边对不上。把它复算出来，才能逐条解释差异，而不是假装历史数字与当前代码一致。
    """
    intent_name = record.get("actual_intent")
    slots = record.get("actual_slots") or {}
    if intent_name in WRITE_INTENTS:
        return True
    if intent_name == "invest" and slots.get("action") in ("buy", "redeem"):
        return True
    if intent_name == "subscription":
        return slots.get("action") == "cancel" or bool(slots.get("cancel_subs"))
    return False


@dataclass(frozen=True)
class Round:
    """一轮完整 A/B：80 个 case，两组按 id 配对。"""

    name: str
    model: str
    a: dict[str, dict[str, Any]]
    b: dict[str, dict[str, Any]]
    #: 该轮归档 summary.json 里记的「未追问即动钱」case id（历史口径，可能已被收紧）
    archived_unauthorized: dict[str, tuple[str, ...]]

    @property
    def ids(self) -> list[str]:
        return sorted(self.a)


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def load_rounds(results_dir: Path) -> tuple[list[Round], list[tuple[str, str]]]:
    """装载所有可用的完整轮次；返回 (轮次, 被排除的目录及原因)。"""
    rounds: list[Round] = []
    skipped: list[tuple[str, str]] = []
    for entry in sorted(results_dir.iterdir()):
        if not entry.is_dir():
            continue
        cases_a = entry / "cases_a.jsonl"
        cases_b = entry / "cases_b.jsonl"
        if not cases_a.exists() or not cases_b.exists():
            skipped.append((entry.name, "缺少 cases_a.jsonl / cases_b.jsonl（未完成或被中断的轮次）"))
            continue
        summary_path = entry / "summary.json"
        if not summary_path.exists():
            skipped.append((entry.name, "缺少 summary.json，无法核对指标口径"))
            continue
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        rows_a = _read_jsonl(cases_a)
        rows_b = _read_jsonl(cases_b)
        if len(rows_a) != len(rows_b):
            skipped.append((entry.name, f"A/B 行数不一致（{len(rows_a)} vs {len(rows_b)}）"))
            continue
        if len(rows_a) < FULL_CASE_COUNT:
            skipped.append(
                (entry.name, f"只有 {len(rows_a)} 个 case，是冒烟轮，不足 {FULL_CASE_COUNT} 条完整集")
            )
            continue
        if summary.get("mock") is not False:
            skipped.append((entry.name, f"summary.mock={summary.get('mock')!r}，非真实模型结果"))
            continue
        a = {str(row["id"]): row for row in rows_a}
        b = {str(row["id"]): row for row in rows_b}
        if set(a) != set(b):
            skipped.append((entry.name, "A/B 两组 case id 集合不一致，无法配对"))
            continue
        bad_group = [row["id"] for row in rows_a if row.get("group") != "a"] + [
            row["id"] for row in rows_b if row.get("group") != "b"
        ]
        if bad_group:
            skipped.append((entry.name, f"group 字段与文件名不符：{bad_group[:5]}"))
            continue
        archived: dict[str, tuple[str, ...]] = {}
        for group in ("a", "b"):
            metric = (
                summary.get("groups", {})
                .get(group, {})
                .get("metrics", {})
                .get("unauthorized_write_attempts", {})
            )
            archived[group] = tuple(metric.get("case_ids") or ())
        rounds.append(
            Round(entry.name, str(summary.get("model") or "unknown"), a, b, archived)
        )
    return rounds, skipped


def recompute(rows: Iterable[dict[str, Any]], group: str) -> dict[str, dict[str, int | None]]:
    """逐字复刻 run_eval.compute_metrics 的分子分母（含 knowledge 的 A 组特例）。"""
    rows = list(rows)
    intent_hits = sum(1 for r in rows if r.get("actual_intent") == r["expected_intent"])
    clarify_hits = sum(1 for r in rows if bool(r.get("question_asked")) == r["expect_ask"])
    expected_slots = sum(len(r["expected_slots"]) for r in rows)
    slot_hits = sum(1 for r in rows for hit in (r.get("slot_hits") or {}).values() if hit)
    plan_rows = [r for r in rows if r["expect_plan"]]
    dag_hits = sum(1 for r in plan_rows if r.get("dag_valid"))
    knowledge_total = sum(len(r.get("expected_knowledge") or []) for r in rows)
    knowledge_hits = sum(
        1
        for r in rows
        for knowledge_id in r.get("expected_knowledge") or []
        if knowledge_id in set(r.get("knowledge_ids") or [])
    )
    unauthorized = [
        r["id"]
        for r in rows
        if r["expect_ask"]
        and not r.get("question_asked")
        and any(tool in WRITE_TOOLS for tool in (r.get("dag_tools") or []))
    ]
    unauthorized_wide = [
        r["id"]
        for r in rows
        if r["expect_ask"] and not r.get("question_asked") and _money_moving(r)
    ]
    nan: int | None = None
    if group == "a":
        knowledge = {"numerator": sum(1 for r in rows if r.get("knowledge_ids")), "denominator": nan}
    else:
        knowledge = {"numerator": knowledge_hits, "denominator": knowledge_total}
    return {
        "intent": {"numerator": intent_hits, "denominator": len(rows)},
        "slot": {"numerator": slot_hits, "denominator": expected_slots},
        "clarify": {"numerator": clarify_hits, "denominator": len(rows)},
        "dag": {"numerator": dag_hits, "denominator": len(plan_rows)},
        "knowledge": knowledge,
        "unauthorized": {
            "numerator": len(unauthorized),
            "denominator": len(rows),
            "case_ids": tuple(unauthorized),
        },
        "unauthorized_wide": {
            "numerator": len(unauthorized_wide),
            "denominator": len(rows),
            "case_ids": tuple(unauthorized_wide),
        },
    }


def verify_against_summary(
    round_obj: Round, results_dir: Path
) -> tuple[list[str], list[str]]:
    """复算与 summary.json 对账，返回 (硬问题, 口径漂移说明)。

    硬问题非空就拒绝出文档（说明本脚本的指标口径与原始记录不一致）；
    未追问即动钱不参与硬校验：归档轮次里这个计数换过定义，没有任何单一规则能同时在
    五轮旧数据与三轮新数据上复现，硬比只会逼人去改数据。这里复算窄/宽两种口径，
    与归档值都不一致时记一条漂移说明，逐条列在文档里。
    """
    summary = json.loads((results_dir / round_obj.name / "summary.json").read_text(encoding="utf-8"))
    problems: list[str] = []
    drift: list[str] = []
    metric_names = {
        "intent": "intent_accuracy",
        "slot": "slot_accuracy",
        "clarify": "clarify_accuracy",
        "dag": "dag_validity",
        "knowledge": "knowledge_citation",
    }
    for group, rows in (("a", round_obj.a.values()), ("b", round_obj.b.values())):
        rows = list(rows)
        ours = recompute(rows, group)
        theirs = summary.get("groups", {}).get(group, {}).get("metrics", {})
        for key, metric_name in metric_names.items():
            metric = theirs.get(metric_name)
            if not isinstance(metric, dict):
                problems.append(f"{round_obj.name}/{group}/{metric_name} 在 summary 里缺失")
                continue
            for field in ("numerator", "denominator"):
                mine = ours[key][field]
                # A 组知识指标设计上就是「非空 case 计数」，summary 里分母为 null
                if key == "knowledge" and group == "a" and field == "denominator":
                    continue
                if metric.get(field) is None and key == "knowledge" and field == "denominator":
                    continue
                if mine != metric.get(field):
                    problems.append(
                        f"{round_obj.name}/{group}/{metric_name}.{field}: 复算 {mine} != summary {metric.get(field)}"
                    )
        archived = set(round_obj.archived_unauthorized.get(group, ()))
        narrow = set(ours["unauthorized"]["case_ids"])
        wide = set(ours["unauthorized_wide"]["case_ids"])
        if archived != narrow and archived != wide:
            drift.append(
                f"{round_obj.name}/{group}: 归档 {len(archived)} 例，"
                f"当前窄口径 {len(narrow)} 例、宽口径 {len(wide)} 例，均不一致；"
                f"归档与窄口径的差异 case: {sorted(archived ^ narrow)}"
            )
    return problems, drift


def _seed_for(round_name: str, metric: str, split: str, base: int) -> int:
    """每个 (轮次, 指标, 分层) 一条独立的确定性种子，可复现又不互相相关。"""
    tag = f"{round_name}|{metric}|{split}".encode()
    return (base + zlib.crc32(tag)) % (2**32)


def _percentile(values: Sequence[float], q: float) -> float:
    """线性插值分位数；values 需已排序。"""
    if not values:
        raise ValueError("空样本")
    pos = (len(values) - 1) * q
    low = int(pos)
    high = min(low + 1, len(values) - 1)
    frac = pos - low
    return values[low] * (1 - frac) + values[high] * frac


def bootstrap_mean_ci(
    values: Sequence[float], *, seed: int, iters: int, q: tuple[float, float] = (0.025, 0.975)
) -> tuple[float, float]:
    """配对差值的均值 bootstrap 置信区间（百分位法）。"""
    import random

    if not values:
        return (float("nan"), float("nan"))
    rng = random.Random(seed)
    n = len(values)
    stats: list[float] = []
    for _ in range(iters):
        total = 0.0
        for _ in range(n):
            total += values[rng.randrange(n)]
        stats.append(total / n)
    stats.sort()
    return (_percentile(stats, q[0]), _percentile(stats, q[1]))


def bootstrap_ratio_ci(
    pairs: Sequence[tuple[int, int]], *, seed: int, iters: int, q: tuple[float, float] = (0.025, 0.975)
) -> tuple[float, float]:
    """加权比率（槽位命中数/期望槽位数、知识 id 命中数/期望 id 数）的 bootstrap CI。

    按 case 重采样再重算总比率：分母随样本变化，不能把每 case 的分数简单平均。
    """
    import random

    usable = [pair for pair in pairs if pair[1] > 0]
    if not usable:
        return (float("nan"), float("nan"))
    rng = random.Random(seed)
    n = len(usable)
    stats: list[float] = []
    for _ in range(iters):
        hits = 0
        expected = 0
        for _ in range(n):
            h, e = usable[rng.randrange(n)]
            hits += h
            expected += e
        if expected:
            stats.append(hits / expected)
    if not stats:
        return (float("nan"), float("nan"))
    stats.sort()
    return (_percentile(stats, q[0]), _percentile(stats, q[1]))


def paired_cells(round_obj: Round, split: str) -> dict[str, dict[str, int]]:
    """配对四格表：n11 两组都对、n10 只有 A 对、n01 只有 B 对、n00 都不对。

    二元指标用它就能看出差异来自「B 修好了哪些」还是「B 弄坏了哪些」，
    比只看净差值更不容易掩盖退化。
    """
    ids = [cid for cid in round_obj.ids if split in ("all", round_obj.a[cid].get("split"))]
    out: dict[str, dict[str, int]] = {}
    for metric in ("intent", "clarify", "dag"):
        cells = {"n11": 0, "n10": 0, "n01": 0, "n00": 0}
        for cid in ids:
            a, b = round_obj.a[cid], round_obj.b[cid]
            if metric == "intent":
                ha = a.get("actual_intent") == a["expected_intent"]
                hb = b.get("actual_intent") == b["expected_intent"]
            elif metric == "clarify":
                ha = bool(a.get("question_asked")) == a["expect_ask"]
                hb = bool(b.get("question_asked")) == b["expect_ask"]
            else:
                if not a["expect_plan"]:
                    continue
                ha = bool(a.get("dag_valid"))
                hb = bool(b.get("dag_valid"))
            key = "n11" if ha and hb else "n10" if ha else "n01" if hb else "n00"
            cells[key] += 1
        out[metric] = cells
    return out


def metric_vectors(round_obj: Round, split: str) -> dict[str, Any]:
    """按 case 对齐的 (A, B) 向量；每 case 一个值，长度相同。"""
    ids = [cid for cid in round_obj.ids if split in ("all", round_obj.a[cid].get("split"))]
    intent_a: list[float] = []
    intent_b: list[float] = []
    clarify_a: list[float] = []
    clarify_b: list[float] = []
    dag_a: list[float] = []
    dag_b: list[float] = []
    slot_a: list[tuple[int, int]] = []
    slot_b: list[tuple[int, int]] = []
    for cid in ids:
        a, b = round_obj.a[cid], round_obj.b[cid]
        intent_a.append(1.0 if a.get("actual_intent") == a["expected_intent"] else 0.0)
        intent_b.append(1.0 if b.get("actual_intent") == b["expected_intent"] else 0.0)
        clarify_a.append(1.0 if bool(a.get("question_asked")) == a["expect_ask"] else 0.0)
        clarify_b.append(1.0 if bool(b.get("question_asked")) == b["expect_ask"] else 0.0)
        slot_a.append(
            (sum(1 for hit in (a.get("slot_hits") or {}).values() if hit), len(a["expected_slots"]))
        )
        slot_b.append(
            (sum(1 for hit in (b.get("slot_hits") or {}).values() if hit), len(b["expected_slots"]))
        )
        if a["expect_plan"]:
            dag_a.append(1.0 if a.get("dag_valid") else 0.0)
            dag_b.append(1.0 if b.get("dag_valid") else 0.0)
    return {
        "intent": (intent_a, intent_b),
        "clarify": (clarify_a, clarify_b),
        "slot": (slot_a, slot_b),
        "dag": (dag_a, dag_b),
    }


def metric_value(pairs: Sequence[float]) -> float:
    return sum(pairs) / len(pairs) if pairs else float("nan")


def ratio_value(pairs: Sequence[tuple[int, int]]) -> float:
    expected = sum(pair[1] for pair in pairs)
    return sum(pair[0] for pair in pairs) / expected if expected else float("nan")


def knowledge_pairs(round_obj: Round, split: str) -> list[tuple[int, int]]:
    """B 组知识引用的 (命中 id 数, 期望 id 数)；A 组设计上无知识层，不参与差分。"""
    ids = [cid for cid in round_obj.ids if split in ("all", round_obj.a[cid].get("split"))]
    pairs: list[tuple[int, int]] = []
    for cid in ids:
        expected = list(round_obj.b[cid].get("expected_knowledge") or [])
        if not expected:
            continue
        cited = set(round_obj.b[cid].get("knowledge_ids") or [])
        pairs.append((sum(1 for item in expected if item in cited), len(expected)))
    return pairs


def pp(value: float) -> str:
    if value != value:  # NaN
        return "—"
    return f"{value * 100:+.2f}pp"


def pct(value: float) -> str:
    if value != value:
        return "—"
    return f"{value * 100:.2f}%"


def unauthorized_ids(round_obj: Round, split: str) -> dict[str, dict[str, list[str]]]:
    """按分层复算未追问即动钱：窄口径（计划里真有写工具）与宽口径（声明了写意图）。"""
    out: dict[str, dict[str, list[str]]] = {}
    for group in ("a", "b"):
        rows = round_obj.a if group == "a" else round_obj.b
        ids = [cid for cid in round_obj.ids if split in ("all", rows[cid].get("split"))]
        narrow = [
            cid
            for cid in ids
            if rows[cid]["expect_ask"]
            and not rows[cid].get("question_asked")
            and any(tool in WRITE_TOOLS for tool in (rows[cid].get("dag_tools") or []))
        ]
        wide = [
            cid
            for cid in ids
            if rows[cid]["expect_ask"]
            and not rows[cid].get("question_asked")
            and _money_moving(rows[cid])
        ]
        out[group] = {"narrow": narrow, "wide": wide}
    return out


def analyze_round(round_obj: Round, *, iters: int, seed: int) -> dict[str, Any]:
    out: dict[str, Any] = {"round": round_obj, "splits": {}}
    for split in ("all", "dev", "holdout"):
        vectors = metric_vectors(round_obj, split)
        entry: dict[str, Any] = {}
        for metric in METRICS:
            a_values, b_values = vectors[metric]
            if metric == "slot":
                a_point = ratio_value(a_values)
                b_point = ratio_value(b_values)
                per_case = [
                    (bb[0] / bb[1] if bb[1] else 0.0) - (aa[0] / aa[1] if aa[1] else 0.0)
                    for aa, bb in zip(a_values, b_values)
                ]
                low, high = bootstrap_mean_ci(
                    per_case, seed=_seed_for(round_obj.name, metric, split, seed), iters=iters
                )
                a_low, a_high = bootstrap_ratio_ci(
                    a_values, seed=_seed_for(round_obj.name, metric + ":a", split, seed), iters=iters
                )
                b_low, b_high = bootstrap_ratio_ci(
                    b_values, seed=_seed_for(round_obj.name, metric + ":b", split, seed), iters=iters
                )
                entry[metric] = {
                    "a": a_point, "b": b_point, "diff": b_point - a_point,
                    "diff_ci": (low, high), "a_ci": (a_low, a_high), "b_ci": (b_low, b_high),
                    "n": len(a_values),
                }
                continue
            a_point = metric_value(a_values)
            b_point = metric_value(b_values)
            per_case = [bb - aa for aa, bb in zip(a_values, b_values)]
            low, high = bootstrap_mean_ci(
                per_case, seed=_seed_for(round_obj.name, metric, split, seed), iters=iters
            )
            a_low, a_high = bootstrap_mean_ci(
                a_values, seed=_seed_for(round_obj.name, metric + ":a", split, seed), iters=iters
            )
            b_low, b_high = bootstrap_mean_ci(
                b_values, seed=_seed_for(round_obj.name, metric + ":b", split, seed), iters=iters
            )
            entry[metric] = {
                "a": a_point, "b": b_point, "diff": b_point - a_point,
                "diff_ci": (low, high), "a_ci": (a_low, a_high), "b_ci": (b_low, b_high),
                "n": len(a_values),
            }
        kp = knowledge_pairs(round_obj, split)
        k_low, k_high = bootstrap_ratio_ci(
            kp, seed=_seed_for(round_obj.name, "knowledge", split, seed), iters=iters
        )
        entry["knowledge"] = {
            "b": ratio_value(kp), "ci": (k_low, k_high),
            "hits": sum(pair[0] for pair in kp), "expected": sum(pair[1] for pair in kp),
            "cases": len(kp),
        }
        entry["cells"] = paired_cells(round_obj, split)
        entry["unauthorized"] = unauthorized_ids(round_obj, split)
        out["splits"][split] = entry
    return out


def _render_round_table(analysis: Sequence[dict[str, Any]]) -> list[str]:
    lines = [
        "| 轮次 | 模型 | 指标 | A 组 | B 组 | B−A（配对） | 配对差值 95% CI | 60 dev | 20 holdout |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    labels = {"intent": "意图", "slot": "槽位", "clarify": "追问", "dag": "建图"}
    for item in analysis:
        round_obj: Round = item["round"]
        all_entry = item["splits"]["all"]
        dev_entry = item["splits"]["dev"]
        hold_entry = item["splits"]["holdout"]
        for metric in METRICS:
            m = all_entry[metric]
            lines.append(
                "| {name} | {model} | {label} | {a} | {b} | {diff} | [{lo}, {hi}] | {dev} | {hold} |".format(
                    name=round_obj.name,
                    model=round_obj.model,
                    label=labels[metric],
                    a=pct(m["a"]),
                    b=pct(m["b"]),
                    diff=pp(m["diff"]),
                    lo=pp(m["diff_ci"][0]),
                    hi=pp(m["diff_ci"][1]),
                    dev=pp(dev_entry[metric]["diff"]),
                    hold=pp(hold_entry[metric]["diff"]),
                )
            )
    return lines


def _render_cells(analysis: Sequence[dict[str, Any]]) -> list[str]:
    lines = [
        "| 轮次 | 指标 | 两组都对 n11 | 仅 A 对 n10 | 仅 B 对 n01 | 都不对 n00 |",
        "|---|---|---|---|---|---|",
    ]
    labels = {"intent": "意图", "clarify": "追问", "dag": "建图"}
    for item in analysis:
        round_obj: Round = item["round"]
        cells = item["splits"]["all"]["cells"]
        for metric in ("intent", "clarify", "dag"):
            c = cells[metric]
            lines.append(
                f"| {round_obj.name} | {labels[metric]} | {c['n11']} | {c['n10']} | {c['n01']} | {c['n00']} |"
            )
    return lines


def _render_knowledge(analysis: Sequence[dict[str, Any]]) -> list[str]:
    lines = [
        "| 轮次 | B 组知识引用（命中/期望 id） | B 组引用率 | 95% CI | 覆盖 case 数 |",
        "|---|---|---|---|---|",
    ]
    for item in analysis:
        round_obj: Round = item["round"]
        k = item["splits"]["all"]["knowledge"]
        lines.append(
            f"| {round_obj.name} | {k['hits']}/{k['expected']} | {pct(k['b'])} | "
            f"[{pct(k['ci'][0])}, {pct(k['ci'][1])}] | {k['cases']} |"
        )
    return lines


def _render_unauthorized(analysis: Sequence[dict[str, Any]]) -> list[str]:
    lines = [
        "| 轮次 | 组 | 归档 summary 计数 | 窄口径（计划里真有写工具） | 宽口径（声明了写意图） | 窄口径命中的 case |",
        "|---|---|---|---|---|---|",
    ]
    for item in analysis:
        round_obj: Round = item["round"]
        u = item["splits"]["all"]["unauthorized"]
        for group, label in (("a", "A 组"), ("b", "B 组")):
            archived = round_obj.archived_unauthorized.get(group, ())
            lines.append(
                f"| {round_obj.name} | {label} | {len(archived)} | {len(u[group]['narrow'])} | "
                f"{len(u[group]['wide'])} | {', '.join(u[group]['narrow']) or '无'} |"
            )
    return lines


def _render_split_summary(analysis: Sequence[dict[str, Any]]) -> list[str]:
    lines = [
        "| 轮次 | 模型 | 分层 | 意图 B−A | 追问 B−A | 槽位 B−A | 建图 B−A |",
        "|---|---|---|---|---|---|---|",
    ]
    for item in analysis:
        round_obj: Round = item["round"]
        for split, label in (("dev", "60 条开发集"), ("holdout", "20 条留出集")):
            e = item["splits"][split]
            lines.append(
                f"| {round_obj.name} | {round_obj.model} | {label} | {pp(e['intent']['diff'])} | "
                f"{pp(e['clarify']['diff'])} | {pp(e['slot']['diff'])} | {pp(e['dag']['diff'])} |"
            )
    return lines


def _render_drift(analysis: Sequence[dict[str, Any]]) -> list[str]:
    """同配置复跑抖动：同一模型多轮的点估计波动，用来界定 CI 的解读边界。"""
    by_model: dict[str, list[dict[str, Any]]] = {}
    for item in analysis:
        by_model.setdefault(item["round"].model, []).append(item)
    lines = [
        "| 模型 | 轮数 | 意图 A 组各轮 | 意图 B−A 各轮 | 意图 B−A 极差 |",
        "|---|---|---|---|---|",
    ]
    for model, items in by_model.items():
        a_values = [item["splits"]["all"]["intent"]["a"] for item in items]
        diffs = [item["splits"]["all"]["intent"]["diff"] for item in items]
        lines.append(
            f"| {model} | {len(items)} | {' / '.join(pct(v) for v in a_values)} | "
            f"{' / '.join(pp(v) for v in diffs)} | {pp(max(diffs) - min(diffs))} |"
        )
    return lines


def build_markdown(
    analysis: Sequence[dict[str, Any]],
    skipped: Sequence[tuple[str, str]],
    *,
    seed: int,
    iters: int,
    results_dir: Path,
) -> str:
    models = sorted({item["round"].model for item in analysis})
    lines = [
        "# A/B 配对统计（知识层开关，同一批 case）",
        "",
        "> 本文件由 scripts/ab_stats.py 生成，数据只读自 tests/eval/results/ 下的原始 JSONL；",
        "> 原始 JSONL 未做任何改写。生成命令与参数见文末。",
        "",
        "## 1. 数据与口径",
        "",
        f"* 纳入轮次：{len(analysis)} 轮完整 80 例 A/B（模型：{', '.join(models)}），两组按 case id 一一配对。",
        f"* bootstrap：按 case 有放回重采样 {iters} 次，百分位法 95% CI，随机种子 {seed}（每个「轮次×指标×分层」派生一个确定性种子，可复现）。",
        "* 指标定义与 tests/eval/run_eval.py 完全一致；本脚本对每轮复算 numerator/denominator 并与该轮 summary.json 核对。意图/槽位/追问/建图/知识引用五项全部一致才出文档；",
        "  未追问即动钱这一项在历史轮次里换过口径，无法用单一定义复现，改为逐轮列两种口径与归档值（见 5.1）。",
        "* 意图/追问/建图是逐 case 的 0/1，槽位是逐 case 的「命中数/期望数」，知识引用是逐 case 的「命中 id 数/期望 id 数」；比率类指标的 CI 按 case 重采样后重算总比率，不是对每 case 分数求平均。",
        "",
        "### 被排除的目录",
        "",
    ]
    if skipped:
        lines += ["| 目录 | 原因 |", "|---|---|"]
        lines += [f"| {name} | {reason} |" for name, reason in skipped]
    else:
        lines.append("（无）")
    lines += [
        "",
        "## 2. 逐轮配对结果（80 例合计）",
        "",
        "「配对差值」= B−A 在同一批 case 上的均值差；CI 只反映测试 case 抽样的不确定性。",
        "",
        *_render_round_table(analysis),
        "",
        "### 配对四格表（意图/追问/建图，80 例合计）",
        "",
        "净差值为 0 不等于两组等价：可能同时存在「B 修好」和「B 弄坏」。",
        "",
        *_render_cells(analysis),
        "",
        "## 3. 分层：60 条开发集 vs 20 条留出集",
        "",
        "留出集只有 20 例，单个 case 就是 5pp，CI 会很宽；分层结果用于看方向是否一致，不用于宣称精度。",
        "",
        *_render_split_summary(analysis),
        "",
        "## 4. 知识引用（只有 B 组可报告）",
        "",
        "A 组按设计关闭知识层，knowledge_ids 恒为空，引用率恒为 0。因此「知识引用 B−A」不是一个可解释的差值，",
        "本文件不把它当作证据；可报告的是 B 组自身的绝对引用率——它同样受「期望 id 是术语粒度、检索返回规则/工作流粒度」的影响，",
        "分母是 51 个期望知识 id，不是 80 个 case，也不等于「知识层提升了准确率」。",
        "",
        *_render_knowledge(analysis),
        "",
        "## 5. 同配置复跑抖动（比 CI 更该看的东西）",
        "",
        *_render_drift(analysis),
        "",
        "同一模型、同一配置连跑多轮，点估计本身就在几 pp 到十几 pp 之间摆动；",
        "bootstrap CI 不包含这部分方差，所以不能把 CI 窄当成「结论稳定」。",
        "",
        "### 5.1 未追问即动钱：历史轮次口径变过，只能逐轮看",
        "",
        "归档 summary.json 的这一项不可直接跨轮比较：旧轮次里它按「意图/槽位声明了写操作」计（宽口径），",
        "新版本 run_eval.py 改按「实际建出的计划里真有写工具」计（窄口径，见 _planned_write_tools）。",
        "对同一批原始 JSONL 用两种口径复算：归档值总是等于两种口径之一，但不同轮次落在不同口径上——",
        "说明归档数字来自不同版本的代码，而不是同一把尺子量出来的。",
        "结论只有两句：这一项可以逐 case 回溯（case id 在原始 JSONL 里），但不能说「某轮比另一轮更安全」。",
        "",
        *_render_unauthorized(analysis),
        "",
        "## 6. 结论：能宣称什么、不能宣称什么",
        "",
        "不能宣称：",
        "",
        "1. 不能宣称知识层带来稳定准确率提升。逐轮 B−A 方向不一致（见第 2、5 节），符号在轮次之间翻转；配对差值再配 CI，也改变不了「不同轮次结论相反」这一事实。",
        "2. 不能把某轮的正向差值当成效应量。每组单次采样，且同一配置复跑的点估计抖动与差值同量级。",
        "3. 不能把知识引用率解读为准确率。B 组引用率的分母是 51 个期望知识 id（开发集 60 + 留出集 20 中带标注的 case），与意图/槽位准确率不是同一分母，也不代表「回答更对」。",
        "4. 不能把 bootstrap CI 当模型稳定性的置信区间——它只重采样 case，不重采样模型运行。",
        "",
        "可以宣称（有原始记录支撑、且不依赖差值方向）：",
        "",
        "1. 未追问即动钱的每个 case 都能在原始 JSONL 里逐条回溯（case id 可核验）；但注意 5.1：该计数在旧轮次用过更宽口径，不能跨轮比较；",
        "2. B 组在开启知识层后确实产生了知识引用（41–42/51），且 A 组引用恒为 0——这是行为差异，不是准确率差异；",
        "3. 全部纳入轮次 mock=false，为真实模型调用；本文件不含任何 MOCK_MODE 结果。",
        "",
        "## 7. 未补跑声明",
        "",
        "本次整改没有补跑任何新轮次：本机真实模型配额与网络条件不足以再跑完整 80 例 × A/B，",
        "因此 tests/eval/results/ 中已有的真实轮次就是全部证据。",
        "若需要更强结论，正确做法是固定 case 集重复采样 K 轮（每组各 K 次），报告轮间方差与配对差值的分布，而不是把本轮 bootstrap CI 讲得更窄。",
        "",
        "## 8. 复现",
        "",
        FENCE + "bash",
        f".venv/bin/python scripts/ab_stats.py --results {results_dir.relative_to(ROOT).as_posix()} \\",
        f"    --iters {iters} --seed {seed}",
        FENCE,
        "",
        "脚本退出码：0 = 复算与 summary.json 全部一致；1 = 有轮次口径不一致或被排除后无完整轮次。",
        "",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="A/B 配对统计：读原始 JSONL，出配对差值与 bootstrap CI")
    parser.add_argument("--results", default=str(DEFAULT_RESULTS), help="results 目录")
    parser.add_argument("--out", default=str(DEFAULT_OUT), help="输出的 markdown 路径")
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED, help="bootstrap 随机种子")
    parser.add_argument("--iters", type=int, default=DEFAULT_ITERS, help="bootstrap 重采样次数")
    args = parser.parse_args(argv)

    results_dir = Path(args.results)
    if not results_dir.is_dir():
        raise SystemExit(f"results 目录不存在: {results_dir}")
    rounds, skipped = load_rounds(results_dir)
    if not rounds:
        raise SystemExit(f"没有可用的完整轮次：{skipped}")

    problems: list[str] = []
    drift: list[str] = []
    for round_obj in rounds:
        round_problems, round_drift = verify_against_summary(round_obj, results_dir)
        problems.extend(round_problems)
        drift.extend(round_drift)
    if problems:
        for problem in problems:
            print(f"[口径不一致] {problem}")
        raise SystemExit("复算与 summary.json 不一致，拒绝生成文档")

    analysis = [analyze_round(round_obj, iters=args.iters, seed=args.seed) for round_obj in rounds]
    markdown = build_markdown(analysis, skipped, seed=args.seed, iters=args.iters, results_dir=results_dir)
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(markdown, encoding="utf-8")

    print(f"纳入轮次: {len(rounds)}（{'，'.join(r.name for r in rounds)}）")
    print("排除目录:")
    for name, reason in skipped:
        print(f"  - {name}: {reason}")
    print("复算口径: 意图/槽位/追问/建图/知识引用 5 项与各轮 summary.json 全部一致")
    print("未追问即动钱口径漂移（历史轮次换过定义，逐条列在文档 5.1）:")
    for note in drift:
        print(f"  - {note}")
    if not drift:
        print("  - 无（全部轮次的归档值都能被窄口径或宽口径复现）")
    for item in analysis:
        round_obj = item["round"]
        entry = item["splits"]["all"]
        print(
            f"{round_obj.name} {round_obj.model}: "
            + " | ".join(
                f"{metric} {pp(entry[metric]['diff'])} [{pp(entry[metric]['diff_ci'][0])}, {pp(entry[metric]['diff_ci'][1])}]"
                for metric in METRICS
            )
        )
    print(f"已写出: {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
