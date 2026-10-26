"""业务管理：卡片、订阅、工具清单与用户自定的事实。

只读：本模块不写库、不发令牌、不调执行器。
"""

from __future__ import annotations

import sqlite3
from typing import Any, Final

from app import memory
from app import subscriptions
from app import suitability
from app import timefmt
from app.proactive import default_user_id
from app.money import fen_to_yuan_str
from app.clock import now_ms
from app.tools.base import primary_account

from app.views.money import _accounts
from app.views.queue import _scheduled_tasks



def management_view(conn: sqlite3.Connection) -> dict[str, Any]:
    """业务管理：转账、卡片、订阅、理财各自的入口、边界与数据来源。

    总装：每块业务各有一个 _xxx_view，它们只读库、只算数、只拼字段；
    这里只负责把五块拼成一份响应。拆开的理由很实际——原先一个函数 228 行，
    谁也不敢改其中一段，而且「哪块是数据、哪块是注释性的边界」永远分不清。
    """
    stamp = now_ms()
    account = primary_account(conn)
    account_id = str(account["id"])
    user_id = str(account["user_id"])
    products, holdings_summary = _products_view(conn, user_id)
    return {
        "ok": True,
        "accounts": _accounts(conn),
        "cards": _cards_view(conn, user_id),
        "subscriptions": _subscriptions_view(conn, account_id, user_id),
        "scheduled": _scheduled_tasks(conn, user_id, limit=5),
        "investments": _investments_view(conn, user_id, products, holdings_summary),
        "capabilities": CAPABILITIES,
    }


def _cards_view(conn: sqlite3.Connection, user_id: str) -> list[dict[str, Any]]:
    """卡片清单：状态、单笔限额，按「正常在前」排序。"""
    card_rows = conn.execute(
        "SELECT c.*, a.tail AS account_tail, a.name AS account_name FROM cards c"
        " LEFT JOIN accounts a ON a.id = c.account_id"
        " WHERE a.user_id = ? ORDER BY CASE c.status WHEN 'active' THEN 0 ELSE 1 END, c.id",
        (user_id,),
    ).fetchall()
    card_labels = {"active": "正常", "frozen": "已冻结", "lost": "已挂失", "closed": "已销户"}
    cards = [
        {
            "card_id": str(row["id"]),
            "account_tail": row["account_tail"],
            "account_name": row["account_name"],
            "tail": str(row["tail"]),
            "kind": str(row["kind"]),
            "kind_label": "实体卡" if str(row["kind"]) == "physical" else "虚拟卡",
            "status": str(row["status"]),
            "status_label": card_labels.get(str(row["status"]), str(row["status"])),
            "per_tx_limit_fen": int(row["per_tx_limit_fen"]),
            "per_tx_limit_yuan": fen_to_yuan_str(int(row["per_tx_limit_fen"])),
        }
        for row in card_rows
    ]

    return cards


def _subscriptions_view(
    conn: sqlite3.Connection, account_id: str, user_id: str
) -> dict[str, Any]:
    """已知代扣 + 流水推断出的疑似订阅 + 近期扣费窗口。三者都不动钱。"""
    sub_rows = conn.execute(
        "SELECT * FROM subs WHERE account_id = ? ORDER BY status, merchant", (account_id,)
    ).fetchall()
    subs_items = [
        {
            "sub_id": str(row["id"]),
            "merchant": str(row["merchant"]),
            "amount_fen": int(row["amount_fen"]),
            "amount_yuan": fen_to_yuan_str(int(row["amount_fen"])),
            "period": str(row["period"]),
            "period_label": "每月" if str(row["period"]) == "monthly" else "每年",
            "status": str(row["status"]),
            "status_label": "生效中" if str(row["status"]) == "active" else "已停止（银行侧）",
            "next_charge_ms": int(row["next_charge_ms"]),
            "next_charge_text": timefmt.maybe_minute_text(int(row["next_charge_ms"])),
            # 这句必须跟着每条订阅一起给：本地表标记取消，只停银行侧代扣，
            # 商户那边的会员并不会因此注销。
            "boundary": "这里只能停止银行侧的周期性代扣，商户会员需要你自己去商户端取消。",
        }
        for row in sub_rows
    ]

    monthly_active = sum(
        int(row["amount_fen"])
        for row in sub_rows
        if str(row["period"]) == "monthly" and str(row["status"]) == "active"
    )
    return {
        "items": subs_items,
        "count": len(subs_items),
        # 页面把这句边界文案和「月合计」写在同一行，所以月合计必须由后端算好下发
        # （前端用 bxMoney(subs, 'monthly_active') 取，键名是拼出来的，别只看字面量）。
        "monthly_active_fen": monthly_active,
        "monthly_active_yuan": fen_to_yuan_str(monthly_active),
        "boundary": "订阅数据来自本行代扣记录，不是商户会员系统。",
        # 已知代扣之外：疑似订阅（流水推断）与近期扣费窗口。
        "inferred": _inferred_view(conn, account_id, user_id),
        "upcoming": subscriptions.upcoming(conn, account_id),
    }


def _inferred_view(
    conn: sqlite3.Connection, account_id: str, user_id: str
) -> dict[str, Any]:
    """疑似订阅：推断结果 + 每条的可执行处置 + 已定性/已停止两栏。"""
    verdict_map = subscriptions.verdicts(conn, user_id)
    inferred = subscriptions.detect_recurring(conn, account_id, user_id=user_id)
    # 每条疑似订阅都要有一条「这是误报」的出口：只读清单 + 没有处置动作，
    # 就等于用户每次刷新都被同一批猜测打扰，却什么也做不了。
    for item in inferred.get("items") or []:
        merchant = str(item.get("merchant") or "")
        item["verdict"] = verdict_map.get(merchant)
        item["actions"] = [
            {
                "kind": "subscription_verdict",
                "merchant": merchant,
                "verdict": subscriptions.VERDICT_NOT_SUBSCRIPTION,
                "label": "不是我订的",
                "style": "ghost",
            },
            {
                "kind": "subscription_verdict",
                "merchant": merchant,
                "verdict": subscriptions.VERDICT_CONFIRMED,
                "label": "确实是我订的",
            },
            {
                # 核实必须真核实：这句会走聊天快路，把每一笔流水的日期、金额、
                # 备注原文念出来，而不是把同一句话再丢给模型猜一遍。
                "kind": "subscription_check",
                "merchant": merchant,
                "label": "逐笔核对这几笔",
                "prompt": f"帮我核实一下「{merchant}」这几笔是不是订阅",
            },
        ]
    inferred["verdicts"] = list(verdict_map.values())
    inferred["dismissed_count"] = sum(
        1
        for value in verdict_map.values()
        if value.get("verdict") == subscriptions.VERDICT_NOT_SUBSCRIPTION
    )
    inferred["confirmed_count"] = sum(
        1
        for value in verdict_map.values()
        if value.get("verdict") == subscriptions.VERDICT_CONFIRMED
    )
    inferred["settled_count"] = len(verdict_map)
    # 定性过的条目虽然不再算待办，但必须能被收回：点错了、商户改了收费方式，
    # 都该有「重新当成疑似」的一步，否则一条旧结论会永久生效。
    inferred["settled"] = [
        {**value, "actions": [{
            "kind": "subscription_verdict_clear",
            "merchant": value["merchant"],
            "label": "重新当成疑似",
            "style": "ghost",
        }]}
        for value in verdict_map.values()
    ]
    # 停掉银行侧代扣的商户已经从「疑似订阅」待办里排除（见 detect_recurring），
    # 但不能让它们凭空消失：单列一栏，让用户看得到自己处理过什么、还差商户端那一步。
    stopped = subscriptions.stopped_debits(conn, account_id)
    inferred["stopped"] = stopped
    inferred["stopped_count"] = len(stopped)
    inferred["note"] = (
        f"{inferred.get('note') or ''}"
        "停掉银行侧代扣的商户已不在待办里，列在「已停止银行侧代扣」一栏。"
    )

    return inferred


def _products_view(
    conn: sqlite3.Connection, user_id: str
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """理财产品清单 + 持仓概况。表里没有的字段一律给 null，不编。"""
    product_rows = conn.execute(
        "SELECT * FROM fund_products ORDER BY risk_level, code LIMIT 12"
    ).fetchall()
    held = {
        str(row["product_code"]): row
        for row in conn.execute(
            "SELECT * FROM fund_holdings WHERE user_id = ?", (user_id,)
        ).fetchall()
    }
    products = []
    for row in product_rows:
        shares = int(held[str(row["code"])]["shares_milli"]) if str(row["code"]) in held else 0
        products.append(
            {
                "code": str(row["code"]),
                "name": str(row["name"]),
                "category": str(row["category"]),
                "risk_level": int(row["risk_level"]),
                "min_buy_fen": int(row["min_buy_fen"]),
                "min_buy_yuan": fen_to_yuan_str(int(row["min_buy_fen"])),
                "nav_micro": int(row["nav_micro"]),
                "nav_updated_text": timefmt.maybe_minute_text(int(row["nav_updated_ms"])),
                "description": str(row["description"]),
                "holding_shares_milli": shares,
                # 下面四个字段表里本来就没有，也没有可信来源：显式给 null
                # 比编一个「约 30 天持有期 / 0.15% 费率」诚实。
                "duration_days": None,
                "liquidity": None,
                "fee": None,
                "fee_text": "本演示环境不提供费率、持有期与流动性数据，不推测。",
                "data_source": "contest_simulation",
            }
        )

    holdings_summary = {
        "holding_count": sum(1 for p in products if p["holding_shares_milli"] > 0),
        "product_count": len(products),
    }
    return products, holdings_summary


def _investments_view(
    conn: sqlite3.Connection,
    user_id: str,
    products: list[dict[str, Any]],
    holdings_summary: dict[str, int],
) -> dict[str, Any]:
    """理财入口：产品、画像、问卷与数据来源边界。"""
    profile = suitability.load(conn, user_id)
    return {
            "products": products,
            "summary": holdings_summary,
            "data_source": "fund_products 表（演示数据：净值是固定的，不接行情）",
            # 数据来源必须能被一眼看穿：这是比赛用的模拟产品表，
            # 不是基金公司/行情源，也没有做过产品对比。
            "simulation": {
                "data_source": "contest_simulation",
                "verified_source": False,
                "note": (
                    "基金产品是本地种子数据（fund_products），没有接基金公司或行情源；"
                    "期限、流动性、费率、历史业绩一律不填，不编造。"
                ),
            },
            "comparison": {
                "supported": False,
                "note": "没有做产品对比/排行：只展示同一张表里能读到的字段。",
            },
            # 画像从 investor_profiles 读：没测过就是没测过，测过就照实给等级与可买范围。
            "suitability_assessed": profile is not None,
            "suitability": {
                "assessed": profile is not None,
                "profile": profile,
                # 题目跟着画像一起下发：界面不用为了填表再单独请求一次。
                "questions": [
                    {
                        "id": item["id"],
                        "text": item["text"],
                        "options": [
                            {"value": option["value"], "label": option["label"]}
                            for option in item["options"]
                        ],
                    }
                    for item in suitability.QUESTIONS
                ],
                "level_bands": [
                    {"floor": floor, "level": level, "label": label, "capacity": capacity}
                    for floor, level, label, capacity in suitability.LEVEL_BANDS
                ],
                "disclaimer": "演示用简化问卷，不是监管口径的正式风险评估；不构成投资建议。",
            },
            "suitability_note": (
                "买基金前必须先做一次 5 题风险测评（C1—C5）：没测评或产品风险高于画像上限，"
                "buy_fund 会直接拒绝，界面也不会替你判断。问卷是演示简化版。"
                if profile is None
                else f"已测评：{profile['level']}（{profile['level_label']}），"
                     f"{profile['capacity_label']} 风险的产品。"
            ),
    }


#: 各项业务在当前原型里的真实边界。前端直接照这个渲染，不用自己猜。
CAPABILITIES: Final[dict[str, dict[str, Any]]] = {
    "transfer": {
        "supported": True,
        "entry": "AI 助手（自然语言）",
        "requires_authorization": True,
        "notes": [
            "收款人重名时必须由你选择，系统不会自己挑第一个。",
            "确认后再改金额或收款人，旧授权作废，需要重新确认。",
            "1000 元以上需要短信+人脸双因子——这是赛题设定的分级策略，不代表所有真实银行的限额。",
        ],
    },
    "scheduled_transfer": {
        "supported": True,
        "entry": "AI 助手 / 计划与提醒",
        "requires_authorization": True,
        "notes": [
            "未执行的预约可以取消。",
            "已执行的转账不能一键撤回：只能发起冲正（记一笔反向流水），且在演示环境里是模拟账务。",
        ],
    },
    "card": {
        "supported": True,
        "entry": "业务管理 / AI 助手",
        "requires_authorization": True,
        "notes": ["冻结、挂失、调额、销卡都会写审计；挂失不可逆。"],
    },
    "subscription": {
        "supported": True,
        "entry": "账单分析 / 业务管理",
        "requires_authorization": True,
        "notes": ["只能停止银行侧代扣，不能注销商户会员。"],
    },
    "investment": {
        "supported": True,
        "entry": "业务管理 / AI 助手",
        "requires_authorization": True,
        "notes": ["有产品查询、风险等级与危险提示；没有适当性评估问卷。"],
    },
    "reversal": {
        "supported": True,
        "entry": "AI 助手",
        "requires_authorization": True,
        "notes": ["冲正是模拟账务，不代表真实银行追回；真实追回需要人工受理。"],
    },
}


#: 一次最多回多少条：演示库的记忆量很小，50 足够，也避免接口被刷。
MEMORY_LIMIT: Final[int] = 50


def memory_view(conn: sqlite3.Connection) -> dict[str, Any]:
    """这个用户现在被记住了什么（业务管理页的「长期记忆」卡）。"""
    user_id = default_user_id(conn)
    facts = memory.all_facts(conn, user_id=user_id, limit=MEMORY_LIMIT)
    return {
        "ok": True,
        "facts": facts,
        "count": len(facts),
        "prompt_block": memory.prompt_block(conn, user_id=user_id),
    }
