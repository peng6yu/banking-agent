"""把「下周三」「月底」「三天后」这类说法换成毫秒时间戳。

为什么单独成文件：dag.py 已顶 400 行硬上限（docs/08）。
为什么不由模型填时间戳：真模型对「下周三」换算成 epoch 毫秒的结果不可信，
而定时转账一旦把日期算错，钱会在用户没想到的日子划出去。
所以只认固定的几种说法，认不出来就问，绝不猜。
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Final

from app.clock import ValidationError

DAY_MS: Final[int] = 86_400_000
WEEKDAYS: Final[dict[str, int]] = {
    "一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6,
}
RELATIVE: Final[tuple[tuple[str, int], ...]] = (
    ("今天", 0), ("明天", 1), ("后天", 2), ("大后天", 3),
)


def _at_nine(moment: datetime) -> int:
    return int(moment.replace(hour=9, minute=0, second=0, microsecond=0).timestamp() * 1000)


def parse_due_ms(text: str, now_ms: int) -> int:
    """认得出就返回时间戳；认不出抛 ValidationError 让意图层去问。"""
    raw = str(text or "").strip()
    if not raw:
        raise ValidationError("没说什么时候转", reason="DUE_UNPARSED", field="due_ms")
    now = datetime.fromtimestamp(now_ms // 1000)
    for word, days in RELATIVE:
        if word in raw:
            return _at_nine(now + timedelta(days=days))
    if "月底" in raw:
        first = now.replace(day=1)
        nxt = (first + timedelta(days=32)).replace(day=1)
        return _at_nine(nxt - timedelta(days=1))
    if "下个月" in raw or "下月" in raw:
        month, year = (now.month % 12) + 1, now.year + (1 if now.month == 12 else 0)
        return _at_nine(now.replace(year=year, month=month, day=1))
    match = re.search(r"(\d{1,3})\s*(天|日)后", raw)
    if match:
        return _at_nine(now + timedelta(days=int(match.group(1))))
    for key, index in WEEKDAYS.items():
        if ("下周" + key) in raw or ("下星期" + key) in raw:
            ahead = (index - now.weekday()) % 7
            return _at_nine(now + timedelta(days=ahead + 7))
        if ("本周" + key) in raw or ("这周" + key) in raw or ("周" + key) in raw or ("星期" + key) in raw:
            ahead = (index - now.weekday()) % 7
            return _at_nine(now + timedelta(days=ahead))
    raise ValidationError("没听懂这个时间", reason="DUE_UNPARSED", field="due_ms")
