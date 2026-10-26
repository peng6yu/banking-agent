"""两条「更远一点」的洞察：钱一直闲着、接管单挂着没人管。

为什么单独一个文件：app/insights.py 已经贴着 600 行硬上限，再塞两个探测器会越界。
这里只放「探测器 + 目录」两件事，**不 import app.insights**（否则循环导入），
动作字典就地拼，不共用那边的私有 helper。

门槛都是可以当面解释的数字，不调模型、不猜：
- I9：活期余额 ≥ 10,000 元，且最近 90 天没有任何理财类流水；
- I10：有处于待接管状态的单，最老的一张已挂 ≥ 24 小时。
"""

from __future__ import annotations

import sqlite3
import time
from typing import Any, Callable, Final

from app.money import fen_to_yuan_str

DAY_MS: Final[int] = 24 * 3600 * 1000
IDLE_MIN_FEN: Final[int] = 1_000_000
IDLE_QUIET_DAYS: Final[int] = 90
STALE_HANDOFF_HOURS: Final[int] = 24
HOUR_MS: Final[int] = 3600 * 1000
_ACTIVE_STATUSES: Final[tuple[str, ...]] = ("pending", "claimed", "need_info")

CATALOG_EXTRA: Final[tuple[dict[str, str], ...]] = (
    {"id": "I9_idle_balance", "title": "这笔钱一直放在活期",
     "why": "活期余额超过 1 万元，且最近 90 天没有理财类流水",
     "source": "accounts + transactions（分类=理财）", "severity": "info"},
    {"id": "I10_stale_handoff", "title": "接管单挂着没人管",
     "why": "有处于待接管状态的单，最老的一张已挂超过 24 小时",
     "source": "handoffs（pending/claimed/need_info）", "severity": "warning"},
)


def _dedupe_month(now: int) -> str:
    return time.strftime("%Y-%m", time.localtime(now / 1000))


def _idle_candidate(conn: sqlite3.Connection, user_id: str, now: int) -> dict[str, Any] | None:
    """活期余额够大、又很久没动过 —— 只陈述事实，不推荐产品、不承诺收益。"""
    row = conn.execute(
        "SELECT balance_fen FROM accounts"
        " WHERE user_id = ? AND status = 'active' ORDER BY balance_fen DESC LIMIT 1",
        (user_id,),
    ).fetchone()
    if row is None or int(row["balance_fen"] or 0) < IDLE_MIN_FEN:
        return None
    moved = conn.execute(
        "SELECT 1 FROM transactions t JOIN accounts a ON a.id = t.account_id"
        " WHERE a.user_id = ? AND t.category = '理财' AND t.ts_ms >= ? LIMIT 1",
        (user_id, now - IDLE_QUIET_DAYS * DAY_MS),
    ).fetchone()
    if moved is not None:
        return None
    balance = int(row["balance_fen"])
    balance_yuan = fen_to_yuan_str(balance)
    return {
        "insight_id": "I9_idle_balance",
        "severity": "info",
        "dedupe_key": f"I9:{_dedupe_month(now)}",
        "facts": {"balance_yuan": balance_yuan, "quiet_days": IDLE_QUIET_DAYS},
        "title": f"卡上有 {balance_yuan} 元一直没动",
        "body": (
            f"这张卡上放着 {balance_yuan} 元，最近 {IDLE_QUIET_DAYS} 天没有理财类流水。"
            "这不是推荐某个产品：理财有风险，买不买、买什么由你决定；"
            "也可以先把这笔钱定成一个储蓄目标。"
        ),
        "actions": [
            {"key": "goto", "kind": "goto", "label": "去看看产品", "view": "manage"},
            {"key": "ask", "kind": "ask", "label": "让助手帮我看看", "prompt": "帮我看看现在有哪些理财产品"},
        ],
    }


def _stale_handoff_candidate(conn: sqlite3.Connection, user_id: str, now: int) -> dict[str, Any] | None:
    """接管单超过一天没人处理：钱不会自己动，但它会一直卡着。"""
    row = conn.execute(
        "SELECT COUNT(*) AS n, MIN(created_ms) AS oldest FROM handoffs"
        " WHERE status IN (?, ?, ?) AND created_ms <= ? AND (user_id = ? OR user_id = '')",
        (*_ACTIVE_STATUSES, now - STALE_HANDOFF_HOURS * HOUR_MS, user_id),
    ).fetchone()
    count = int(row["n"] or 0)
    if count <= 0:
        return None
    oldest_hours = int((now - int(row["oldest"] or now)) / HOUR_MS)
    return {
        "insight_id": "I10_stale_handoff",
        "severity": "warning",
        "dedupe_key": f"I10:{_dedupe_month(now)}",
        "facts": {"count": count, "oldest_hours": oldest_hours},
        "title": f"有 {count} 张接管单挂了 {oldest_hours} 小时",
        "body": "这些单在等人工处理，钱不会自己动。去总览的「待人工接管的单」看看是哪一笔、要你定什么。",
        "actions": [{"key": "goto", "kind": "goto", "label": "去总览看这些单", "view": "overview"}],
    }


#: 分两档登记：warning 插在「安全」一档末尾，info 排在「习惯」一档末尾。
EXTRA_WARNING_PROBES: Final[tuple[Callable[[sqlite3.Connection, str, int], dict[str, Any] | None], ...]] = (
    _stale_handoff_candidate,
)
EXTRA_INFO_PROBES: Final[tuple[Callable[[sqlite3.Connection, str, int], dict[str, Any] | None], ...]] = (
    _idle_candidate,
)

__all__ = ["CATALOG_EXTRA", "EXTRA_INFO_PROBES", "EXTRA_WARNING_PROBES"]
