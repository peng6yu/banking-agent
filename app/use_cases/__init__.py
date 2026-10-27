"""写操作的唯一入口（W3）。

规矩：
- 路由只 import 这个包，不直接 import 领域写函数；
- 用例只收基本类型（不 import app.api.schemas），返回 Outcome（见 base.py）；
- 读接口（GET）仍走 app/api 里的视图函数，等 W4 再把它们搬到 app/views。

每个子模块对应一个业务对象，函数名就是动作：create / update / pay / claim …
"""

from app.use_cases import (
    base,
    bill,
    confirmations,
    demo,
    handoffs,
    investments,
    memory,
    plans,
    proactive,
    reminders,
    savings,
    scheduled,
    subscriptions,
    transactions,
)

__all__ = [
    "base",
    "bill",
    "confirmations",
    "demo",
    "handoffs",
    "investments",
    "memory",
    "plans",
    "proactive",
    "reminders",
    "savings",
    "scheduled",
    "subscriptions",
    "transactions",
]
