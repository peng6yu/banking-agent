"""投资者适当性问卷：买基金之前必须先有一张画像。

对应任务书 P0-6「理财必须区分产品查询 / 风险适配分析和实际交易」。

边界写清楚，免得文档又夸大：
- 这是一份**演示用**的五题问卷，不是监管口径的正式《投资者风险承受能力评估》；
  真实机构还要求双录、有效期（通常两年）、临柜或视频见证，这里都没有。
- 它只做一件事：把「你的风险承受等级」和「产品风险等级」对起来，拦掉明显不匹配的申购。
  它不能预测收益，也不能替你判断这只产品好不好。
- 测评结果落 investor_profiles 表；每次保存都写一条 suitability.assessed 审计。
  申购被拦时写 suitability.checked，说明是「没测过」还是「等级不够」。
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Final

import app.audit as audit  # noqa: PLR0402
from app.clock import ValidationError, now_ms

#: 五道题。option 的 score 相加得到 0—15 分，再映射到 C1—C5。
#: horizon_years / loss_label 只是把用户选项翻译成人话，不参与打分。
QUESTIONS: Final[tuple[dict[str, Any], ...]] = (
    {
        "id": "q_horizon",
        "text": "这笔钱大概多久不用？",
        "options": (
            {"value": "lt_6m", "label": "半年以内", "score": 0, "horizon_years": 0.5},
            {"value": "6m_1y", "label": "半年到一年", "score": 1, "horizon_years": 1.0},
            {"value": "1y_3y", "label": "一到三年", "score": 2, "horizon_years": 3.0},
            {"value": "gt_3y", "label": "三年以上", "score": 3, "horizon_years": 5.0},
        ),
    },
    {
        "id": "q_experience",
        "text": "你买过下面哪类产品？",
        "options": (
            {"value": "deposit_only", "label": "只有存款和国债", "score": 0},
            {"value": "money_bond", "label": "货币基金、债券基金", "score": 1},
            {"value": "mixed", "label": "混合基金、银行理财", "score": 2},
            {"value": "equity", "label": "股票、股票基金、期权", "score": 3},
        ),
    },
    {
        "id": "q_loss",
        "text": "如果一个月亏了 10%，你会怎么办？",
        "options": (
            {"value": "sell_all", "label": "马上全部卖掉", "score": 0, "loss_label": "几乎不能接受亏损"},
            {"value": "sell_part", "label": "卖掉一部分", "score": 1, "loss_label": "能接受小幅亏损"},
            {"value": "hold", "label": "拿着不动", "score": 2, "loss_label": "能接受阶段性回撤"},
            {"value": "buy_more", "label": "再加一点", "score": 3, "loss_label": "能接受较大回撤"},
        ),
    },
    {
        "id": "q_goal",
        "text": "这笔钱主要想用来做什么？",
        "options": (
            {"value": "preserve", "label": "保住本金，比活期强就行", "score": 0},
            {"value": "steady", "label": "稳一点增值", "score": 1},
            {"value": "growth", "label": "争取明显高于存款的收益", "score": 2},
            {"value": "aggressive", "label": "追求高收益，能承受高波动", "score": 3},
        ),
    },
    {
        "id": "q_share",
        "text": "这笔钱占你可投资资产的比例大概是？",
        "options": (
            {"value": "gt_50", "label": "一半以上", "score": 0},
            {"value": "30_50", "label": "三到五成", "score": 1},
            {"value": "10_30", "label": "一到三成", "score": 2},
            {"value": "lt_10", "label": "一成以内", "score": 3},
        ),
    },
)

QUESTION_IDS: Final[tuple[str, ...]] = tuple(item["id"] for item in QUESTIONS)

#: 分数 -> 等级。等级同时决定「最高能买 R 几」。
#: 真实机构会按 C1—C5 的监管映射（C1 只能 R1……C5 可到 R5），这里同构简化。
LEVEL_BANDS: Final[tuple[tuple[int, str, str, int], ...]] = (
    (0, "C1", "保守型", 1),
    (4, "C2", "稳健型", 2),
    (8, "C3", "平衡型", 3),
    (12, "C4", "成长型", 4),
    (14, "C5", "进取型", 5),
)

MAX_SCORE: Final[int] = 15


def _option(question: dict[str, Any], value: str) -> dict[str, Any]:
    for option in question["options"]:
        if option["value"] == value:
            return option
    raise ValidationError(
        "这道题的选项不存在",
        question=question["id"],
        allowed=[item["value"] for item in question["options"]],
    )


def normalize(answers: dict[str, Any] | None) -> dict[str, str]:
    """校验并规整答案：五题必答、不许夹带不认识的题号、选项必须在册。"""
    if not isinstance(answers, dict):
        raise ValidationError("测评答案必须是对象")
    unknown = sorted(set(answers) - set(QUESTION_IDS))
    if unknown:
        raise ValidationError("测评里有不认识的题目", unknown=unknown)
    missing = [qid for qid in QUESTION_IDS if qid not in answers]
    if missing:
        raise ValidationError(
            f"测评还没答完（还差 {len(missing)} 题），先别提交",
            missing=missing,
            user_text="还有题没答完，5 道题都选了才能生成测评结果。",
        )
    clean = {qid: str(answers[qid]) for qid in QUESTION_IDS}
    # 选项合法性也在规整阶段就查：save() 直接落库，不能等到 assess() 才算分时才发现
    # 模型编了一个不存在的选项。
    for question in QUESTIONS:
        _option(question, clean[question["id"]])
    return clean


def assess(answers: dict[str, Any] | None, *, now: int | None = None) -> dict[str, Any]:
    """纯函数：答案 -> 画像。不写库、不看时钟之外的东西，方便重放和单测。"""
    clean = normalize(answers)
    score = 0
    horizon_years = 0.0
    loss_label = ""
    for question in QUESTIONS:
        option = _option(question, clean[question["id"]])
        score += int(option["score"])
        if "horizon_years" in option:
            horizon_years = max(horizon_years, float(option["horizon_years"]))
        if "loss_label" in option:
            loss_label = str(option["loss_label"])
    level, level_label, capacity = _level_of(score)
    return {
        "score": score,
        "max_score": MAX_SCORE,
        "level": level,
        "level_label": level_label,
        "capacity": capacity,
        "capacity_label": f"最高可买 R{capacity}",
        "horizon_years": horizon_years,
        "loss_tolerance": loss_label,
        "answers": clean,
        "assessed_ms": int(now if now is not None else now_ms()),
        "disclaimer": "演示用简化问卷，不是监管口径的正式风险评估；不构成投资建议。",
    }


def _level_of(score: int) -> tuple[str, str, int]:
    level, label, capacity = "C1", "保守型", 1
    for floor, name, human, cap in LEVEL_BANDS:
        if score >= floor:
            level, label, capacity = name, human, cap
    return level, label, capacity


def save(conn: sqlite3.Connection, user_id: str, answers: dict[str, Any] | None) -> dict[str, Any]:
    """保存画像并留审计。同一用户重复测评时覆盖旧画像（保留首次 created_ms）。"""
    profile = assess(answers)
    existing = conn.execute(
        "SELECT created_ms FROM investor_profiles WHERE user_id = ?", (user_id,)
    ).fetchone()
    created_ms = int(existing["created_ms"]) if existing else profile["assessed_ms"]
    conn.execute(
        "INSERT INTO investor_profiles"
        " (user_id, score, level, capacity, horizon_years, loss_tolerance, answers_json, created_ms, updated_ms)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
        " ON CONFLICT(user_id) DO UPDATE SET score=excluded.score, level=excluded.level,"
        " capacity=excluded.capacity, horizon_years=excluded.horizon_years,"
        " loss_tolerance=excluded.loss_tolerance, answers_json=excluded.answers_json,"
        " updated_ms=excluded.updated_ms",
        (
            user_id,
            profile["score"],
            profile["level"],
            profile["capacity"],
            profile["horizon_years"],
            profile["loss_tolerance"],
            json.dumps(profile["answers"], ensure_ascii=False, sort_keys=True),
            created_ms,
            profile["assessed_ms"],
        ),
    )
    audit.append(
        conn,
        "suitability.assessed",
        {
            "user_id": user_id,
            "score": profile["score"],
            "level": profile["level"],
            "capacity": profile["capacity"],
        },
    )
    profile["created_ms"] = created_ms
    return profile


def load(conn: sqlite3.Connection, user_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM investor_profiles WHERE user_id = ?", (user_id,)
    ).fetchone()
    return view(row) if row else None


def view(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    answers: dict[str, str] = {}
    try:
        parsed = json.loads(row["answers_json"] or "{}")
        if isinstance(parsed, dict):
            answers = {str(key): str(value) for key, value in parsed.items()}
    except (ValueError, TypeError):
        answers = {}
    level = str(row["level"])
    capacity = int(row["capacity"])
    return {
        "score": int(row["score"]),
        "max_score": MAX_SCORE,
        "level": level,
        "level_label": _LABELS.get(level, level),
        "capacity": capacity,
        "capacity_label": f"最高可买 R{capacity}",
        "horizon_years": float(row["horizon_years"]),
        "loss_tolerance": str(row["loss_tolerance"]),
        "answers": answers,
        "created_ms": int(row["created_ms"]),
        "updated_ms": int(row["updated_ms"]),
        "disclaimer": "演示用简化问卷，不是监管口径的正式风险评估；不构成投资建议。",
    }


_LABELS: Final[dict[str, str]] = {level: label for _, level, label, _ in LEVEL_BANDS}


def check(
    conn: sqlite3.Connection,
    user_id: str,
    product: sqlite3.Row | dict[str, Any],
) -> dict[str, Any]:
    """申购前的适配性闸门。返回 {ok, reason, user_text, profile, product_risk}。

    三种结果：
    - 没测过 -> SUITABILITY_NOT_ASSESSED，先让用户做问卷；
    - 产品风险高于画像上限 -> PRODUCT_RISK_TOO_HIGH，建议换产品而不是改答案；
    - 通过 -> ok=True，把画像一起带回去，便于回执里写清"根据你的 C3 平衡型"。
    """
    profile = load(conn, user_id)
    risk = int(product["risk_level"])
    product_name = str(product["name"])
    if profile is None:
        return {
            "ok": False,
            "reason": "SUITABILITY_NOT_ASSESSED",
            "user_text": (
                "买基金之前要先做一次风险测评（5 道题，一分钟）。"
                "这一步不能跳过：我得先知道你能承受多大波动，才能判断这只产品适不适合你。"
            ),
            "profile": None,
            "product_risk": risk,
            "product_name": product_name,
            "next": "assess_suitability",
        }
    if risk > int(profile["capacity"]):
        return {
            "ok": False,
            "reason": "PRODUCT_RISK_TOO_HIGH",
            "user_text": (
                f"「{product_name}」是 R{risk} 风险，你的测评结果是 {profile['level']}"
                f"（{profile['level_label']}），最高只能买 R{profile['capacity']}。"
                "建议换一只风险更低的产品；测评结果不能为了买这一只临时改。"
            ),
            "profile": profile,
            "product_risk": risk,
            "product_name": product_name,
            "next": "list_products",
        }
    return {
        "ok": True,
        "reason": "",
        "user_text": "",
        "profile": profile,
        "product_risk": risk,
        "product_name": product_name,
        "next": "",
    }
