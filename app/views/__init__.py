"""只读业务视图（第 5 层）：给六大信息区提供真实数据，一行写操作都没有。

原来的 app/api/business.py 同时是路由和视图装配，W4 拆开：

- app/views/labels.py  状态→中文说法、异常规则、快捷入口（纯常量）
- app/views/money.py   金额与流水积木（账户、账单、趋势）
- app/views/queue.py   待办与队列积木（待确认、接管单、预约、提醒、监测）
- app/views/<区>.py    每张卡片的装配
- app/api/business.py  只剩路由：鉴权 + 调视图 + 返回

三条硬规矩不变：只读、不编数据（查不到给空数组 + unavailable 说明）、钱一律整数分。
"""

from app.views.bill import bill_view
from app.views.executions import executions_view
from app.views.management import management_view
from app.views.overview import overview_view
from app.views.plans import plans_view

__all__ = ["bill_view", "executions_view", "management_view", "overview_view", "plans_view"]
