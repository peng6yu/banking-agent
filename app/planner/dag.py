"""把意图变成执行图：docs/07 7.3 的固定模板，不允许模型自由发挥。

三条硬规矩：
1. 模板是死的——每个意图走哪几个工具、什么顺序，规格里写死了，模型改不了；
2. 节点之间用 @node.field.path 引用传递结果，不把上一步的原始 JSON 硬抄进下一步；
3. 规划阶段出错（E8）不抛给用户，回退成只跑模板第一个工具的单步图。
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Final

from app import audit, clock, db
from app.ids import new_id
from app.models import Dag, DAGNode
from app.planner.intent import ACCOUNT_OWNER_SLOT
from app.planner.card_actions import CARD_TOOLS, card_params, card_template
from app.planner.due import parse_due_ms
from app.planner.split_plan import SPLIT_INTENT, build_split
from app.planner.subscription import cancel_params, resolve_match, subscription_template
from app.tools.base import ValidationError

log = logging.getLogger(__name__)

REF_PREFIX: Final[str] = "@"
# 理财意图按 action 分三路：买=申购、赎回=卖出、查=看产品清单。
# 槽位名 product_code 与 app/tools/tool_specs.py 的 buy_fund/redeem_fund 参数同名，零映射。
# 认不出 action 时走「查」这一路：宁可给产品清单，也不带着猜出来的参数去动钱。
INVEST_TEMPLATES: Final[dict[str, tuple[tuple[str, str], ...]]] = {
    "buy": (("n1", "buy_fund"),),
    "redeem": (("n1", "redeem_fund"),),
    "query": (("n1", "list_products"),),
}
INVEST_DEFAULT_ACTION: Final[str] = "query"
# docs/07 7.3 意图 → 模板，逐条照抄，不许增删改顺序
# 理财那一条登记的是它的默认（只读）一路，另外两路见 INVEST_TEMPLATES。
INTENT_TEMPLATES: Final[dict[str, tuple[tuple[str, str], ...]]] = {
    "query_balance": (("n1", "query_balance"),),
    "bill_analysis": (("n1", "analyze_bill"),),
    "subscription": (("n1", "list_subs"),),
    "transfer": (
        ("n1", "resolve_payee"),
        ("n2", "query_balance"),
        ("n3", "transfer"),
    ),
    "birthday_plan": (
        ("n1", "resolve_payee"),
        ("n2", "query_balance"),
        ("n3", "reserve_funds"),
        ("n4", "pay_merchant"),
    ),
    "invest": INVEST_TEMPLATES["query"],
    # 定时转账：先认收款人，再落一条预约（钱到日子才划，不当场占 hold）
    "schedule_transfer": (
        ("n1", "resolve_payee"),
        ("n2", "schedule_transfer"),
    ),
    # 冲正：先查最近的流水，再按那一笔的 id 和金额冲回（金额不取模型报的）
    "reversal": (
        ("n1", "query_transactions"),
        ("n2", "reverse_transfer"),
    ),
    # 卡片管理的默认一路（只查卡）；另外五路见 app/planner/card_actions.py
    "card_manage": (("n1", "list_cards"),),
}
# 会读账户信息的工具：意图层判出 account_owner=other 就原样带进参数，策略层据此判红。
OWNER_AWARE_TOOLS: Final[tuple[str, ...]] = ("query_balance", "list_subs", "analyze_bill")
DEFAULT_INTENT: Final[str] = "query_balance"
# 模型编了一个没登记的意图时落到的哨兵工具名。
# 它不在 KNOWN_TOOLS 里，策略层 R03 会亮红灯并落 policy.decided / confirm.rejected；
# 之所以不叫 query_balance：悄悄回退成一次绿灯查余额，等于把「这个请求我不会」
# 答成「这是你的余额」，用户以为请求被受理了，审计里也只留下一条绿灯（t16 验收第 7 条）。
UNSUPPORTED_TOOL: Final[str] = "unsupported_request"
BILL_DEFAULT_PERIOD: Final[str] = "month"
BIRTHDAY_PURPOSE: Final[str] = "生日"
BIRTHDAY_MERCHANT: Final[str] = "flower_shop"
BIRTHDAY_ITEM: Final[str] = "生日鲜花"


def build(intent: dict[str, Any]) -> Dag:
    """按模板生成执行图；缺必填槽位抛 ValidationError，由调用方回退单步。"""
    name, slots = split(intent)
    if name == SPLIT_INTENT:
        # AA 拆分要按人数动态造图（同名工具不能出现在固定模板里），见 split_plan.py
        return build_split(slots)
    template = _template(name, slots)
    if template is None:
        raise ValidationError("没有这个意图的模板", intent=name)
    dag = Dag(id=new_id("r"), nodes=_nodes(template, slots))
    validate(dag)
    _audit_ignored_slots(dag, name, slots)
    return dag


def build_or_single(intent: dict[str, Any]) -> Dag:
    """E8：模板在册但建图失败时，回退成模板第一个工具的单步图，不中断整轮会话。

    模板不在册（模型编了一个没登记的意图）不再回退到默认意图：那不是「降级」，
    是把一个说不出口的请求换成一个用户没提的请求去做。见 UNSUPPORTED_TOOL。
    """
    name, slots = split(intent)
    if name == SPLIT_INTENT:
        try:
            return build_split(slots)
        except ValidationError:
            # 人数不够或金额缺失：不猜，落到未支持请求上由策略层拒绝并留痕
            return Dag(
                id=new_id("r"),
                nodes=[DAGNode(id="n1", tool=UNSUPPORTED_TOOL, params={"intent": str(name)})],
            )
    template = _template(name, slots)
    if template is None:
        return Dag(
            id=new_id("r"),
            nodes=[DAGNode(id="n1", tool=UNSUPPORTED_TOOL, params={"intent": str(name)})],
        )
    try:
        return build(intent)
    except ValidationError:
        node_id, tool = template[0]
        if name == "invest":
            # 理财建图失败基本都是缺产品代码或金额。退到只读的产品清单，
            # 把代码和起购金额摆给用户，让他补一句再买；
            # 绝不带着半截参数去申购或者赎回。
            node_id, tool = INVEST_TEMPLATES[INVEST_DEFAULT_ACTION][0]
        return Dag(id=new_id("r"), nodes=[DAGNode(id=node_id, tool=tool, params={})])


def validate(dag: Dag) -> None:
    """四道校验：ID 不重复、依赖不悬空、工具在册、没有环。"""
    seen: set[str] = set()
    for node in dag.nodes:
        if node.id in seen:
            raise ValidationError("DAG 节点 ID 重复", reason="DAG_DUP_ID", node_id=node.id)
        seen.add(node.id)
    for node in dag.nodes:
        for parent in node.depends_on:
            if parent not in seen:
                raise ValidationError("DAG 依赖悬空", reason="DAG_DANGLING", node_id=node.id, parent=parent)
    _check_tools(dag)
    if len(_topo(dag)) != len(dag.nodes):
        raise ValidationError("DAG 里有环", reason="DAG_CYCLE", nodes=[node.id for node in dag.nodes])


def topo(dag: Dag) -> list[DAGNode]:
    """拓扑序；同层按节点 ID 排序，保证每次跑出来的顺序一样。"""
    return _topo(dag)


def ref(node_id: str, *path: str) -> str:
    """造一个 @node.field.path 引用串。"""
    return REF_PREFIX + ".".join([node_id, *path])


def resolve_refs(params: dict[str, Any], results: dict[str, Any]) -> dict[str, Any]:
    """把 @开头的字符串换成上游节点结果里的真实值。"""
    resolved: dict[str, Any] = {}
    for key, value in params.items():
        if isinstance(value, str) and value.startswith(REF_PREFIX):
            resolved[key] = _lookup(value, results)
        else:
            resolved[key] = value
    return resolved


def split(intent: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """兼容 {'intent':..,'slots':{..}} 与扁平两种入参，返回 (意图名, 槽位)。"""
    slots = dict(intent.get("slots") or {})
    for key, value in intent.items():
        if key not in ("intent", "slots") and value is not None:
            slots.setdefault(key, value)
    return str(intent.get("intent") or DEFAULT_INTENT), slots


def _template(name: str, slots: dict[str, Any]) -> tuple[tuple[str, str], ...] | None:
    """取这个意图要走哪张模板：理财按 action 分三路，订阅按要不要取消分两路，其余一张表走到底。"""
    if name == "subscription":
        return subscription_template(slots)
    if name == "card_manage":
        return card_template(slots)
    if name == "invest":
        action = str(slots.get("action") or "").strip().lower()
        return INVEST_TEMPLATES.get(action, INVEST_TEMPLATES[INVEST_DEFAULT_ACTION])
    return INTENT_TEMPLATES.get(name)


def _nodes(template: tuple[tuple[str, str], ...], slots: dict[str, Any]) -> list[DAGNode]:
    ids = {tool: node_id for node_id, tool in template}
    nodes: list[DAGNode] = []
    previous = ""
    for node_id, tool in template:
        nodes.append(
            DAGNode(
                id=node_id,
                tool=tool,
                params=_params(tool, slots, ids),
                depends_on=[previous] if previous else [],
            )
        )
        previous = node_id
    return nodes


def _params(tool: str, slots: dict[str, Any], ids: dict[str, str]) -> dict[str, Any]:
    """每个工具的参数；转账类一律用引用拿上游结果。"""
    params = _tool_params(tool, slots, ids)
    owner = str(slots.get(ACCOUNT_OWNER_SLOT) or "").strip()
    if owner and tool in OWNER_AWARE_TOOLS:
        params[ACCOUNT_OWNER_SLOT] = owner
    return params


def _tool_params(tool: str, slots: dict[str, Any], ids: dict[str, str]) -> dict[str, Any]:
    """只按工具名填参数，主体那一层由 _params 统一补。"""
    if tool == "query_balance":
        return _card_tail(slots, ids)
    if tool == "list_subs":
        return {}
    if tool == "cancel_subscription":
        return cancel_params(slots, ids)
    if tool == "analyze_bill":
        return _bill_params(slots)
    if tool == "resolve_payee":
        return _payee_params(slots)
    if tool == "transfer":
        return _transfer_params(slots, ids)
    if tool == "reserve_funds":
        return {"amount_fen": _amount(slots), "purpose": str(slots.get("purpose") or BIRTHDAY_PURPOSE)}
    if tool == "pay_merchant":
        return _merchant_params(slots, ids)
    if tool in ("buy_fund", "redeem_fund"):
        return _invest_params(slots)
    if tool == "list_products":
        return _product_params(slots)
    if tool == "query_transactions":
        return _txn_params(slots)
    if tool == "schedule_transfer":
        return _schedule_params(slots, ids)
    if tool == "reverse_transfer":
        return _reverse_params(slots, ids)
    if tool in CARD_TOOLS:
        return card_params(tool, slots)
    raise ValidationError("模板里出现未登记的工具", reason="DAG_UNKNOWN_TOOL", tool=tool)


def _txn_params(slots: dict[str, Any]) -> dict[str, Any]:
    """查流水：冲正只回看 7 天（与 transfers.REVERSAL_WINDOW_MS 同口径）。"""
    params: dict[str, Any] = {"days": 7}
    amount = _positive_int(slots.get("amount_fen"))
    if amount is not None:
        params["min_fen"] = amount
    return params


def _due_ms(slots: dict[str, Any]) -> int:
    """先认模型给的毫秒时间戳，没有再按中文时间说法换算，认不出就抛给 E8。"""
    stamp = _positive_int(slots.get("due_ms"))
    if stamp is not None:
        return stamp
    return parse_due_ms(str(slots.get("due_hint") or ""), clock.now_ms())


def _schedule_params(slots: dict[str, Any], ids: dict[str, str]) -> dict[str, Any]:
    """定时转账：收款人来自查询节点，时间来自 due_ms 或 due_hint。"""
    params: dict[str, Any] = {
        "payee_id": ref(_need(ids, "resolve_payee"), "candidates", "0", "payee_id"),
        "amount_fen": _amount(slots),
        "due_ms": _due_ms(slots),
    }
    recurrence = str(slots.get("recurrence") or "").strip()
    if recurrence in ("once", "monthly"):
        params["recurrence"] = recurrence
    note = str(slots.get("note") or "").strip()
    if note:
        params["note"] = note
    name = str(slots.get("payee_name") or "").strip()
    if name:
        params["payee_name"] = name
    return params


def _reverse_params(slots: dict[str, Any], ids: dict[str, str]) -> dict[str, Any]:
    """冲正：交易 id 与金额都取自查到的第一笔流水，不让模型报金额。"""
    query_node = _need(ids, "query_transactions")
    return {
        "txn_id": ref(query_node, "items", "0", "txn_id"),
        "amount_fen": ref(query_node, "items", "0", "amount_fen"),
        "reason": str(slots.get("note") or slots.get("txn_ref") or "用户申请冲正"),
    }


def _transfer_params(slots: dict[str, Any], ids: dict[str, str]) -> dict[str, Any]:
    """payee_id 取自 resolve_payee；说「全部」时金额取自 query_balance 的余额。

    payee_name 是给用户看的展示名：R06 的确认文案要念收款人的名字，
    没有它就只能回落到内部 id（p_xxx），等于把数据库主键念给用户听。
    """
    params = {
        "payee_id": ref(_need(ids, "resolve_payee"), "candidates", "0", "payee_id"),
        "amount_fen": _amount_ref(slots, ids),
        "note": str(slots.get("note") or ""),
    }
    name = str(slots.get("payee_name") or "").strip()
    if name:
        params["payee_name"] = name
    return params


def _merchant_params(slots: dict[str, Any], ids: dict[str, str]) -> dict[str, Any]:
    """hold_id 与金额都来自上一步的预授权。"""
    hold_node = _need(ids, "reserve_funds")
    params = {
        "merchant": str(slots.get("merchant") or BIRTHDAY_MERCHANT),
        # gift 是意图层的生日槽位（「花束」）；模型没给就退回默认的生日鲜花
        "item": str(slots.get("gift") or BIRTHDAY_ITEM),
        "hold_id": ref(hold_node, "hold_id"),
    }
    amount = _positive_int(slots.get("amount_fen"))
    params["amount_fen"] = amount if amount is not None else ref(hold_node, "amount_fen")
    return params


def _invest_params(slots: dict[str, Any]) -> dict[str, Any]:
    """申购/赎回：产品代码与金额都必填，缺了就抛给 E8 回退，不猜。"""
    params = {
        "product_code": _product_code(slots),
        "amount_fen": _amount(slots),
    }
    note = str(slots.get("note") or "").strip()
    if note:
        params["note"] = note
    return params


def _product_params(slots: dict[str, Any]) -> dict[str, Any]:
    """查产品：风险等级与分类都是可选筛选条件，缺了就不传。"""
    params: dict[str, Any] = {}
    risk = _positive_int(slots.get("risk_level"))
    if risk is not None:
        params["risk_level"] = risk
    category = str(slots.get("category") or "").strip()
    if category:
        params["category"] = category
    return params


def _product_code(slots: dict[str, Any]) -> str:
    code = str(slots.get("product_code") or "").strip()
    if not code:
        raise ValidationError("缺产品代码", reason="DAG_MISSING_SLOT", field="product_code")
    return code


def _bill_params(slots: dict[str, Any]) -> dict[str, Any]:
    """账单默认看上个自然月，年份按当前时间推。

    模型填的月份只作参考：只有 1–12 的整数才算数。非法值（0、13、"9月"、"2026-09"、
    True）一律忽略并回默认月份 —— 宁可算默认月份，也不按一个来路不明的月份算账：
    账单差一个月，金额照样像模像样，用户根本看不出来（真模型实测出现过一次，
    「上个月的账单」被算成了 8 月）。

    被忽略的原始值塞不进工具参数（工具 spec 是 additionalProperties: False），
    但这一轮按哪个月查账是留痕的，三处对得上：
    1) 审计里有一条 intent.parsed 行，记着模型原话的 month、生效的 month，
       以及「模型给的月份非法已忽略」这句话（app/planner/dag.py 的
       _audit_ignored_slots 写的）；
    2) 生效计划落在 dag_nodes.params_json 里（实际按几月查的）；
    3) 用户可见摘要带实际统计区间（app/planner/events.py 的 summarize）。
    """
    year, month = _previous_month(clock.now_ms())
    effective, _ignored = _bill_month(slots.get("month"), month)
    return {
        "period": str(slots.get("period") or BILL_DEFAULT_PERIOD),
        "year": _positive_int(slots.get("year")) or year,
        "month": effective,
    }


#: 账单月份只认 1–12：0 和 13 没人这么说话，模型写出来多半是把卡号当成了月份。
BILL_MONTH_MIN: Final[int] = 1
BILL_MONTH_MAX: Final[int] = 12


def _bill_month(raw: Any, default: int) -> tuple[int, Any]:
    """模型给的月份收敛成可用的月份：只认 1–12 的整数。

    返回 (生效月份, 被忽略的原始值)；没被忽略时第二项是 None。
    bool 是 int 的子类，True 不算月份（Python 会把它当 1）。
    """
    if raw is None:
        return default, None
    if isinstance(raw, bool) or not isinstance(raw, int):
        return default, raw
    if BILL_MONTH_MIN <= raw <= BILL_MONTH_MAX:
        return raw, None
    return default, raw


#: 账单节点的工具名：要审计被忽略的月份，先得找到这一轮实际生效的月份。
BILL_TOOL: Final[str] = "analyze_bill"
#: 模型给的月份非法被忽略时，审计行里写的那句话。
BILL_MONTH_IGNORED_NOTE: Final[str] = "模型给的月份非法已忽略"


def _audit_ignored_slots(dag: Dag, name: str, slots: dict[str, Any]) -> None:
    """模型给的槽位被系统忽略时，把这件事写进审计链（目前只有账单月份这一处）。

    为什么必须留痕：账单差一个月，金额照样像模像样，用户根本看不出来
    （真模型实测出现过一次，「上个月的账单」被算成了 8 月）。计划里落的是生效
    月份，摘要里落的是统计区间，但「模型本来说的是几月、被系统换成了几月」
    只有审计里才有。

    为什么写在这里：建图是全链路上唯一同时知道「模型原话」和「实际生效值」的
    地方——API 层写的那条 intent.parsed 审计不带 slots。

    为什么写不进去也不让对话失败：审计是留痕，不是业务；写失败只记日志，
    与 app/api/routes.py 的 _audit_intent 同一套取舍。

    已知局限：建图期还没有 session_id（会话要等执行器拿到这张图才开），
    所以这行审计的 session_id 是空的，靠 payload 里的 run_id 跟那一轮对上。
    """
    raw = slots.get("month")
    if raw is None:
        return
    effective = _effective_bill_month(dag)
    if effective is None:
        return
    _kept, ignored = _bill_month(raw, effective)
    if ignored is None:
        return
    _append_audit(
        {
            "run_id": dag.id,
            "intent": name,
            "note": BILL_MONTH_IGNORED_NOTE,
            "slot": "month",
            "raw_value": raw,
            "effective_value": effective,
            "source": "planner.dag",
        }
    )


def _effective_bill_month(dag: Dag) -> int | None:
    """从已经建好的图里读账单节点实际生效的月份；这一轮不走账单就返回 None。"""
    for node in dag.nodes:
        if node.tool != BILL_TOOL:
            continue
        month = node.params.get("month")
        if isinstance(month, int) and not isinstance(month, bool):
            return month
    return None


def _append_audit(payload: dict[str, Any]) -> None:
    """写一条审计；写不进去只记日志，不让对话失败。"""
    if not db.DEFAULT_DB_PATH.exists():
        # 库还没建（例如还没 seed）：没有审计表可写，留日志，别凭空造一个空库。
        log.warning("planner.dag.audit_db_missing path=%s", db.DEFAULT_DB_PATH)
        return
    try:
        conn = db.connect()
    except Exception:  # noqa: BLE001 - 审计写不进去也不能让对话失败
        log.warning("planner.dag.audit_connect_failed run_id=%s", payload.get("run_id"))
        return
    try:
        audit.append(conn, "intent.parsed", payload)
    except Exception:  # noqa: BLE001 - 同上：留痕失败不是业务失败
        log.warning("planner.dag.audit_append_failed run_id=%s", payload.get("run_id"))
    finally:
        conn.close()


def _card_tail(slots: dict[str, Any], ids: dict[str, str]) -> dict[str, Any]:
    """卡尾号槽位查的是自己名下的账户。

    模板里带 resolve_payee 时（转账、订花），句子里说的尾号指的是收款人的卡，
    拿去查账户只会得到 ACCOUNT_NOT_FOUND，这个槽位归收款人那边用。
    """
    if "resolve_payee" in ids:
        return {}
    tail = str(slots.get("card_tail") or slots.get("account_tail") or "").strip()
    return {"account_tail": tail} if tail else {}


def _payee_params(slots: dict[str, Any]) -> dict[str, Any]:
    """收款人查询：姓名必填；说了卡尾号就带上，用来在同名里收窄到一个。"""
    params: dict[str, Any] = {"name": _payee_name(slots)}
    tail = str(slots.get("card_tail") or "").strip()
    if tail:
        params["card_tail"] = tail
    return params


def _payee_name(slots: dict[str, Any]) -> str:
    name = str(slots.get("payee_name") or "").strip()
    if not name:
        raise ValidationError("缺收款人", reason="DAG_MISSING_SLOT", field="payee_name")
    return name


def _amount(slots: dict[str, Any]) -> int:
    amount = _positive_int(slots.get("amount_fen"))
    if amount is None:
        raise ValidationError("缺金额", reason="DAG_MISSING_SLOT", field="amount_fen")
    return amount


def _amount_ref(slots: dict[str, Any], ids: dict[str, str]) -> Any:
    """有明确金额就用金额；说「全部」时引用上一步查到的余额。"""
    amount = _positive_int(slots.get("amount_fen"))
    if amount is not None:
        return amount
    if slots.get("amount_all"):
        return ref(_need(ids, "query_balance"), "accounts", "0", "balance_fen")
    raise ValidationError("缺金额", reason="DAG_MISSING_SLOT", field="amount_fen")


def _need(ids: dict[str, str], tool: str) -> str:
    node_id = ids.get(tool)
    if node_id is None:
        raise ValidationError("模板里缺少必需的上游节点", reason="DAG_MISSING_REF", tool=tool)
    return node_id


def _positive_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


def _previous_month(now_ms: int) -> tuple[int, int]:
    moment = datetime.fromtimestamp(now_ms // 1000)
    return (moment.year - 1, 12) if moment.month == 1 else (moment.year, moment.month - 1)


def _lookup(reference: str, results: dict[str, Any]) -> Any:
    parts = reference[len(REF_PREFIX):].split(".")
    current: Any = results.get(parts[0]) if parts else None
    for part in parts[1:]:
        if "[" in part:
            # 带过滤的一段：@n1.subs[merchant=爱奇艺会员] —— 唯一匹配的那一条
            current = resolve_match(current, part)
        elif isinstance(current, dict):
            current = current.get(part)
        elif isinstance(current, (list, tuple)) and part.lstrip("-").isdigit():
            current = current[int(part)] if -len(current) <= int(part) < len(current) else None
        else:
            current = None
    if current is None:
        raise ValidationError("引用取不到值", reason="DAG_REF_MISSING", ref=reference)
    return current


def _check_tools(dag: Dag) -> None:
    from app.tools.registry import TOOL_NAMES

    for node in dag.nodes:
        if node.tool not in TOOL_NAMES:
            raise ValidationError("DAG 用了没登记的工具", reason="DAG_UNKNOWN_TOOL", tool=node.tool)


def _topo(dag: Dag) -> list[DAGNode]:
    index = {node.id: node for node in dag.nodes}
    pending = {node.id: list(node.depends_on) for node in dag.nodes}
    order: list[DAGNode] = []
    while pending:
        ready = sorted(node_id for node_id, deps in pending.items() if not deps)
        if not ready:
            break
        for node_id in ready:
            order.append(index[node_id])
            pending.pop(node_id)
            for deps in pending.values():
                if node_id in deps:
                    deps.remove(node_id)
    return order


__all__ = [
    "INTENT_TEMPLATES",
    "INVEST_TEMPLATES",
    "OWNER_AWARE_TOOLS",
    "UNSUPPORTED_TOOL",
    "REF_PREFIX",
    "build",
    "build_or_single",
    "ref",
    "resolve_refs",
    "split",
    "topo",
    "validate",
]
