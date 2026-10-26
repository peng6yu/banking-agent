"""视图用的固定说法与口径（第 5 层，纯常量，不 import 业务模块）。

为什么要单独一个文件：状态→中文说法、异常规则元信息、快捷入口这三组常量，前后端
必须共用同一套说法（一处叫「处理中」一处叫「执行中」就是两套语义）。放这里以后，
视图模块只 import 常量，不会为了拿一句文案去 import 一个会查库的模块。
"""

from typing import Any, Final


#: 收支趋势最多回看多少个月
MAX_TREND_MONTHS: Final[int] = 12
#: 默认回看 6 个月：足够看出一个季度以上的实际节奏
DEFAULT_TREND_MONTHS: Final[int] = 6
#: 单次列表最多返回多少条
MAX_LIST: Final[int] = 50

#: 交易状态的中文标签。前后端必须共用同一套说法，不能一处叫"处理中"一处叫"执行中"。
TASK_STATE_LABELS: Final[dict[str, str]] = {
    # dag_runs.status 也会是 waiting（图建好了、还没轮到这一步）。
    # 少这一条，页面就把英文原样显示出来。
    "waiting": "还没轮到",
    "draft": "草稿",
    "pending": "还没执行",
    "need_info": "等待补充信息",
    "waiting_confirm": "等待确认",
    "waiting_factor": "等待验证",
    "running": "执行中",
    "done": "已成功",
    "success": "已成功",
    "failed": "已失败",
    "cancelled": "已取消",
    "skipped": "已跳过",
    "manual": "需要人工处理",
}
#: 提醒步骤的状态 -> 用户能看懂的说法（计划步骤的 state 与交易状态的词不一样）
REMINDER_STATE_LABELS: Final[dict[str, str]] = {
    "pending": "等待到点",
    "running": "正在提醒",
    "done": "已提醒",
    "failed": "提醒失败",
    "skipped": "已跳过",
}
#: 预约转账的行状态 -> 用户能看懂的说法
SCHEDULED_STATE_LABELS: Final[dict[str, str]] = {
    "scheduled": "已安排",
    "needs_verification": "等待你补验证",
    "executed": "已完成",
    "cancelled": "已取消",
    "failed": "已失败",
}

#: 三类异常规则的元信息。规则命中只说明"值得核实"，不等于欺诈认定——
#: 这行字必须跟着每一条异常一起返回，不能只写在文档里。
ANOMALY_RULES: Final[dict[str, dict[str, Any]]] = {
    "split_orders": {
        "rule_id": "R-BILL-01",
        "rule_text": "同一商户 60 分钟内 ≥3 笔，且单笔落在 500—1000 元",
        "severity": "info",
        "confidence": "low",
        "possible_false_positive": "分期付款、多人 AA、商家分单结算都会命中；规则只说明值得核实。",
        "actions": [
            {"id": "view_transactions", "label": "查看这几笔流水", "kind": "inspect"},
            {"id": "ask_assistant", "label": "让 AI 助手解释这一段", "kind": "prompt",
             "prompt": "帮我看一下最近在同一个商户连着付的几笔，是拆单吗？"},
        ],
    },
    "odd_hour_overseas": {
        "rule_id": "R-BILL-02",
        "rule_text": "00:00—05:00 之间、境外商户、单笔 ≥500 元的支出",
        "severity": "warning",
        "confidence": "medium",
        "possible_false_positive": "时差、夜间国际航班消费、境外订阅自动续费都可能命中。",
        "actions": [
            {"id": "freeze_card", "label": "临时冻结这张卡", "kind": "tool", "tool": "freeze_card"},
            {"id": "ask_assistant", "label": "让 AI 助手核这一笔", "kind": "prompt",
             "prompt": "帮我核实一下凌晨那笔境外消费，是不是我自己刷的。"},
        ],
    },
    "subscription_hike": {
        "rule_id": "R-BILL-03",
        "rule_text": "同一订阅商户连续两个自然月金额上涨 ≥30%",
        "severity": "info",
        "confidence": "medium",
        "possible_false_positive": "套餐升级、一次性加购、按用量计费都会命中。",
        "actions": [
            {"id": "open_subscriptions", "label": "看订阅管理", "kind": "inspect"},
            {"id": "ask_assistant", "label": "查这个订阅能不能停", "kind": "prompt",
             "prompt": "我这个订阅涨价了，能帮我看下怎么处理吗？"},
        ],
    },
}

#: 常见业务快捷入口。只是给 AI 助手预填一句话，不代表这些业务已经执行过。
QUICK_ACTIONS: Final[tuple[dict[str, str], ...]] = (
    {"id": "transfer", "label": "转账", "prompt": "帮我转 500 元给张三"},
    {"id": "bill", "label": "看看这个月花了多少", "prompt": "这个月我的钱花在哪了？"},
    {"id": "savings", "label": "定一个储蓄目标", "prompt": "我想半年内存 6000 元"},
    {"id": "subs", "label": "管理订阅", "prompt": "我这个月有哪些订阅在扣钱？"},
    {"id": "card", "label": "卡片管理", "prompt": "帮我看一下我的卡，能不能先冻结一下"},
)

#: 人工接管单的状态 -> 页面说法。和 app/handoffs.py 的状态机一一对应。
HANDOFF_STATUS_LABELS: Final[dict[str, str]] = {
    "pending": "等待接管",
    "claimed": "已领取",
    "need_info": "需要补充信息",
    "rejected": "已拒绝",
    "cancelled": "已取消",
    "resumed": "已恢复执行",
    "failed": "恢复失败",
}
#: 自动转人工只有两种原因，都要给人一句能看懂的话。
HANDOFF_REASON_LABELS: Final[dict[str, str]] = {
    "PAYEE_AMBIGUOUS": "收款人有歧义，等你选定",
    "PAYEE_NOT_FOUND": "没找到这个收款人",
}
#: 安全拒绝的原因（denials.reason 存的是拦下这次操作的错误码）。
DENIAL_REASON_LABELS: Final[dict[str, str]] = {
    "POLICY_BLOCKED": "策略拒绝：这类操作不在允许范围内",
    "INJECTION_DETECTED": "疑似提示词注入，已拦下",
    "BREAKER_OPEN": "连续被拒后熔断，本会话暂时停止处理",
    "FACTOR_REQUIRED": "缺少必须的验证步骤",
}
