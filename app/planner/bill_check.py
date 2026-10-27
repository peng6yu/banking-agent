"""账单异常的人工核实：用户问「这笔是不是我刷的」时，答的是那一笔，不是一张订阅清单。

为什么单独成模块：
1. 账单页异常卡上的「让 AI 助手核这一笔」发出去的是 app/views/labels.py 里那句
   「帮我核实一下凌晨那笔境外消费，是不是我自己刷的。」——这句话里有「核实」和「那笔」，
   两个词都命中原先的订阅核对分支，于是用户问一笔消费、助手答「3 条疑似订阅要核哪一条」。
   账单异常（三条规则命中的流水）和订阅（商户的周期扣款）是两件事，得分开认、分开答；
2. 异常是读时算的（app/tools/bill.py 的 _detect_anomalies），这里只读、不写库：没有
   「已核实」这种状态，也就不能假装记下了什么。到底是不是本人刷的只能由用户拿商户订单去对，
   这里能给的是账上四项事实（时间、商户、金额、卡尾号）加流水号。

第 4 层：只依赖 app.clock / app.timefmt（0 层）与 app.tools.bill（3 层）。
"""

from __future__ import annotations

import re
import time
from typing import Any, Final

import app.timefmt as timefmt
import app.tools.bill as bill_tool
from app.clock import now_ms
from app.money import fen_to_yuan_str

#: 像是在问账单异常的话术。注意「核实」「那笔」这些词本身属于订阅核对，别单列；
#: 这里只认账单侧的对象词（境外/凌晨/拆单/涨价/盗刷）。
_BILL_CHECK_RE: Final[re.Pattern[str]] = re.compile(
    r"境外|凌晨|半夜|夜里|盗刷|可疑|异常|重复扣款|重复收费|多扣|拆单|分单"
    r"|涨价|涨了|涨到|对不上账|这笔消费|这笔支出|这几笔消费"
)
#: 用户已经确认「不是我刷的」：那要的是处置步骤，不是再核一遍。
#: 前面那个否定环视是为了放过「是不是我自己刷的」这种问句——问句里也含「不是我」。
_BILL_FRAUD_RE: Final[re.Pattern[str]] = re.compile(
    r"(?<!是)不是我(?:自己)?(?:刷|消费|付|花)|不是我刷的"
)
#: 原话里的规则词指向哪一类命中（顺序即优先级：先认「境外/凌晨」这种具体场景）。
_BILL_RULE_WORDS: Final[tuple[tuple[tuple[str, ...], str], ...]] = (
    (("境外", "凌晨", "半夜", "夜里", "时差", "海外"), "odd_hour_overseas"),
    (("拆单", "分单", "连着", "重复扣", "多扣"), "split_orders"),
    (("涨价", "涨了", "涨到"), "subscription_hike"),
)
#: 原话里带了流水号（卡片上的按钮现在会带）：精确到行，不用再靠商户名猜。
_BILL_TX_RE: Final[re.Pattern[str]] = re.compile(r"\bt_[0-9a-zA-Z]{4,}\b")
#: 场景词的人话名字：用来诚实交代「这一类没有命中」，不拿别的异常冒充。
_BILL_SCENES: Final[dict[str, str]] = {
    "odd_hour_overseas": "凌晨、境外",
    "split_orders": "拆单",
    "subscription_hike": "订阅涨价",
    "fraud": "被盗刷（不是你刷的）",
}
#: 看最近几个自然月：当月的流水还在来，上个月的异常可能刚被看见。
_BILL_LOOKBACK_MONTHS: Final[int] = 2
_BILL_LIST_LIMIT: Final[int] = 5


def looks_like_bill(text: str) -> bool:
    """这句话是在问账单异常吗（境外/凌晨/拆单/涨价/盗刷/是不是我刷的）。"""
    clean = re.sub(r"\s+", " ", str(text or "")).strip()
    if not clean:
        return False
    return bool(_BILL_CHECK_RE.search(clean) or _BILL_FRAUD_RE.search(clean))


def _bill_months(count: int) -> list[tuple[int, int]]:
    """最近 count 个自然月（含当月），新的在前。"""
    parts = time.localtime(now_ms() / 1000)
    year, month = int(parts.tm_year), int(parts.tm_mon)
    keys: list[tuple[int, int]] = []
    for _ in range(max(1, int(count))):
        keys.append((year, month))
        month -= 1
        if month == 0:
            year, month = year - 1, 12
    return keys


def _bill_cards(conn, clean: str) -> list[tuple[str, str]]:
    """先问句子里点到的那张卡，再问另一张：返回 [(账户 id, 尾号)]。"""
    want = "credit" if "信用卡" in clean else "debit"
    rows = conn.execute(
        "SELECT id, tail, type FROM accounts WHERE type IN ('debit', 'credit') ORDER BY id"
    ).fetchall()
    ordered = [row for row in rows if str(row["type"]) == want]
    ordered += [row for row in rows if str(row["type"]) != want]
    return [(str(row["id"]), str(row["tail"])) for row in ordered]


def _bill_hits(conn, account_id: str) -> list[dict[str, Any]]:
    """某张卡最近两个月命中的异常，新的在前，每条带上月份。读时算，只读。"""
    if not account_id:
        return []
    hits: list[dict[str, Any]] = []
    for year, month in _bill_months(_BILL_LOOKBACK_MONTHS):
        for item in bill_tool.anomalies_for_month(conn, account_id, year, month):
            hits.append({**item, "month_text": f"{year} 年 {month} 月"})
    hits.sort(key=lambda item: -int(item.get("window_start_ms") or 0))
    return hits


def _bill_when(item: dict[str, Any]) -> str:
    """有毫秒窗口就精确到分钟；订阅涨价按自然月比较，只能说「这个月扣的那笔」。"""
    start = item.get("window_start_ms")
    return timefmt.minute_text(int(start)) if start else f"{item.get('month_text') or ''}扣的那笔"


def _bill_merchant(item: dict[str, Any]) -> str:
    return str(item.get("merchant") or item.get("title") or "这笔消费")


def _bill_amount(item: dict[str, Any]) -> str:
    """这次到底扣了多少：涨价异常记的是「涨了多少」，要显示的是这个月扣了多少。"""
    comparison = item.get("comparison") or {}
    if item.get("kind") == "subscription_hike" and comparison.get("current_fen") is not None:
        return fen_to_yuan_str(int(comparison["current_fen"]))
    return str(item.get("amount_yuan") or fen_to_yuan_str(int(item.get("amount_fen") or 0)))


def _bill_amount_note(item: dict[str, Any]) -> str:
    """涨价类补一句上月与涨幅，否则「35.00 元」看不出哪里不对。"""
    comparison = item.get("comparison") or {}
    if item.get("kind") != "subscription_hike" or comparison.get("previous_fen") is None:
        return ""
    previous = fen_to_yuan_str(int(comparison["previous_fen"]))
    current = int(comparison.get("current_fen") or 0)
    return f"（上月 {previous} 元，涨了 {fen_to_yuan_str(current - int(comparison['previous_fen']))} 元）"


def _bill_ids(item: dict[str, Any]) -> str:
    ids = [str(tx_id) for tx_id in item.get("tx_ids") or []]
    return "、".join(ids) if ids else "（这条是两个月的汇总比对，没有单笔流水号）"


def _bill_by_tx(hits: list[dict[str, Any]], clean: str) -> list[dict[str, Any]]:
    """原话点了流水号：只认含这串号的那几条，比商户名更可靠。"""
    wanted = {str(tx_id) for tx_id in _BILL_TX_RE.findall(clean)}
    if not wanted:
        return []
    return [
        item
        for item in hits
        if wanted & {str(tx_id) for tx_id in item.get("tx_ids") or []}
    ]


def _bill_scene(clean: str) -> str:
    """这句话点的是哪一类规则；一个场景词都没提就返回空，由调用方按「全列出来」处理。"""
    for words, kind in _BILL_RULE_WORDS:
        if any(word in clean for word in words):
            return kind
    return ""


def _bill_by_rule(hits: list[dict[str, Any]], clean: str) -> list[dict[str, Any]]:
    """原话里的规则词指向哪一类命中；一个词都没提就返回空，由调用方兜底。"""
    for words, kind in _BILL_RULE_WORDS:
        if any(word in clean for word in words):
            matched = [item for item in hits if str(item.get("kind")) == kind]
            if matched:
                return matched
    return []


def _bill_closing(item: dict[str, Any], tail: str) -> str:
    """两个场景问的不是一件事：小偷刷的要对作案时间，订阅涨价要问还要不要续。"""
    when, merchant = _bill_when(item), _bill_merchant(item)
    amount = _bill_amount(item)
    if item.get("kind") == "subscription_hike":
        return (
            "这不是一笔来路不明的消费，是同一家商户连续两个月扣得更多了。"
            f"你能对的是这四项：{when}有没有印象、{merchant} 还在不在用、"
            f"尾号 {tail} 是不是这张、{amount} 元对不对得上。\n"
            "对不上：银行侧代扣可以在「业务管理 → 订阅与代扣」停掉，会员本身还得去商户那边退订；"
            "对得上就不用管它——规则命中只说明值得核实，不是欺诈认定。"
        )
    return (
        "是不是你自己刷的，光看这份流水我判断不了——我手里只有银行记的商户名、金额、时间"
        "和卡尾号，没有商户订单号、设备或位置。你能对的是这四项："
        f"{when} 这个时间你在不在用这张卡、{merchant} 你去没去过、"
        f"尾号 {tail} 是不是这张、{amount} 元这个数对不对得上。\n"
        "对不上就先冻结这张卡，再找银行报争议交易；对得上就不用管它——"
        "规则命中只说明值得核实，不是欺诈认定。"
    )


def _bill_detail(item: dict[str, Any], tail: str) -> str:
    """一条命中：把这笔的事实摊开，并说清我能判断到哪、判断不到哪。"""
    when, merchant = _bill_when(item), _bill_merchant(item)
    amount = f"{_bill_amount(item)} 元{_bill_amount_note(item)}"
    return (
        "我按这句话在账上找到的是这一笔：\n"
        f"· {when} · {merchant} · {amount} · 尾号 {tail}\n"
        f"· 命中规则 {item.get('rule_id') or ''}：{item.get('detail') or ''}\n"
        f"· 流水号 {_bill_ids(item)}\n" + _bill_closing(item, tail)
    )


def _bill_list(chosen: list[dict[str, Any]], total: int) -> str:
    """多条命中：先列清单（时间/商户/金额/规则号），再让用户点一条。"""
    lines = [
        f"{index}）{_bill_when(item)} · {_bill_merchant(item)} · {_bill_amount(item)} 元"
        f"（{item.get('rule_id') or ''} {item.get('title') or ''}）"
        for index, item in enumerate(chosen[:_BILL_LIST_LIMIT], start=1)
    ]
    if total > len(chosen):
        head = f"这句话里没听出是哪一条，最近两个月共有 {total} 条账单异常，先列这几条：\n"
    else:
        head = "这句话里没听出是哪一条，最近两个月命中账单规则的是这几笔：\n"
    return head + "\n".join(lines) + "\n点一条，我把这笔的时间、金额、卡尾号和流水号念给你听。"


def _bill_scene_miss(
    scene: str, hits: list[dict[str, Any]], tail: str
) -> dict[str, Any]:
    """原话点名了一类场景，但这一类一条都没命中：说没有，再把别的列出来让它自己选。"""
    text = (
        f"最近两个月账上没有命中「{_BILL_SCENES.get(scene, scene)}」这类规则的消费，"
        "所以这句话指的那笔我没找到——这不是我核实过了，是这一类确实没有可核的。"
    )
    if hits:
        lines = [
            f"{index}）{_bill_when(item)} · {_bill_merchant(item)} · "
            f"{_bill_amount(item)} 元（{item.get('rule_id') or ''} {item.get('title') or ''}）"
            for index, item in enumerate(hits[:_BILL_LIST_LIMIT], start=1)
        ]
        text += f"\n倒是另有 {len(hits)} 条别的异常：\n" + "\n".join(lines) + "\n要看哪一条？"
    else:
        text += "换张卡（比如信用卡）或者换个月份再看看？"
    return {
        "intent": "assist",
        "action": "bill_check",
        "summary": text,
        "choices": [
            *[
                {
                    "label": f"核对「{_bill_merchant(item)}」{_bill_amount(item)} 元",
                    "value": f"帮我核实一下「{_bill_merchant(item)}」{_bill_amount(item)} 元这笔消费",
                }
                for item in hits[:_BILL_LIST_LIMIT]
            ],
            {"label": "看看这个月的账单", "value": "帮我看看账单"},
        ],
        "data": {"anomalies": [], "other_anomalies": hits},
    }


def _bill_fraud(item: dict[str, Any], tail: str) -> str:
    """用户已经确认不是你刷的：要说的是接下来三步，不是再核一遍。"""
    return (
        "那这笔先按「不是你刷的」处理，别拖：\n"
        f"1）我把尾号 {tail} 的卡临时冻结（立刻生效、随时能解冻），之后的消费刷不了；\n"
        "2）打银行客服报「争议交易」，让银行启动调单或拒付；\n"
        f"3）报的时候照着念：{_bill_when(item)} · {_bill_merchant(item)} · "
        f"{_bill_amount(item)} 元 · 尾号 {tail} · 流水号 {_bill_ids(item)}。\n"
        "要不要我现在就冻结这张卡？"
    )


def _bill_choices(
    chosen: list[dict[str, Any]], hits: list[dict[str, Any]], tail: str
) -> list[dict[str, str]]:
    """一条命中的下一步：冻结、确认不是我刷的、看别的异常、看整月账单。"""
    out: list[dict[str, str]] = [
        {"label": "临时冻结这张卡", "value": f"帮我把尾号{tail}的卡临时冻结一下"},
        {"label": "这笔不是我刷的", "value": "这笔不是我刷的，帮我处理"},
    ]
    if len(hits) > len(chosen):
        out.append({"label": "看看别的异常", "value": "帮我看看这个月还有哪些异常"})
    out.append({"label": "看看这个月的账单", "value": "帮我看看账单"})
    return out


def handle_bill_check(conn, text: str, user_id: str) -> dict[str, Any]:
    """账单异常核实：摊开事实、给出处置；不写库（异常每次读时重算，没有「已核实」状态）。"""
    clean = re.sub(r"\s+", " ", str(text or "")).strip()
    hits: list[dict[str, Any]] = []
    tail = ""
    for account_id, card_tail in _bill_cards(conn, clean):
        hits = _bill_hits(conn, account_id)
        if hits:
            tail = card_tail
            break
    if not hits:
        return {
            "intent": "assist",
            "action": "bill_check",
            "summary": (
                "最近两个月账上没有命中账单规则的消费，所以没有要核实的那一笔。"
                "这不是我核实过了，是确实没有可核实的记录——要看更早的月份，还是先看这个月的账单？"
            ),
            "choices": [
                {"label": "看看这个月的账单", "value": "帮我看看账单"},
                {"label": "看看我的总览", "value": "看看我的总览"},
            ],
            "data": {"anomalies": []},
        }
    by_tx = _bill_by_tx(hits, clean)
    named = [item for item in hits if item.get("merchant") and str(item["merchant"]) in clean]
    matched = _bill_by_rule(hits, clean)
    scene = _bill_scene(clean)
    if scene and not by_tx and not named and not matched:
        return _bill_scene_miss(scene, hits, tail)
    chosen = by_tx or named or matched or hits
    if _BILL_FRAUD_RE.search(clean):
        # 「不是我刷的」指的是某一笔单独消费；订阅涨价是月度比对，不能按盗刷处置。
        suspects = [
            item
            for item in (named or chosen)
            if str(item.get("kind")) != "subscription_hike"
        ]
        if not suspects:
            return _bill_scene_miss("fraud", hits, tail)
        item = suspects[0]
        return {
            "intent": "assist",
            "action": "bill_check",
            "summary": _bill_fraud(item, tail),
            "choices": _bill_choices([item], hits, tail),
            "data": {"anomalies": [item]},
        }
    if len(chosen) == 1:
        return {
            "intent": "assist",
            "action": "bill_check",
            "summary": _bill_detail(chosen[0], tail),
            "choices": _bill_choices(chosen, hits, tail),
            "data": {"anomalies": chosen},
        }
    return {
        "intent": "assist",
        "action": "bill_check",
        "summary": _bill_list(chosen, len(hits)),
        "choices": [
            *[
                {
                    "label": f"核对「{_bill_merchant(item)}」{_bill_amount(item)} 元",
                    "value": f"帮我核实一下「{_bill_merchant(item)}」{_bill_amount(item)} 元这笔消费",
                }
                for item in chosen[:_BILL_LIST_LIMIT]
            ],
            {"label": "临时冻结这张卡", "value": f"帮我把尾号{tail}的卡临时冻结一下"},
        ],
        "data": {"anomalies": chosen},
    }


__all__ = ["handle_bill_check", "looks_like_bill"]
