"""从一轮对话里抽出长期记忆（best-effort，绝不打断聊天）。

定位：这是「长期记忆」的**入口**，不是判断器。模型只负责把用户自己说过的稳定
偏好/事实挑出来，落库、去重、覆盖、审计都交给 app/memory.py。

刻意写死的三条约束：
1. 只抽用户**自己说出**的稳定偏好与事实；助手说过什么不算，模型推测更不算。
2. 不抽金额、不抽一次性的转账对象（「这次给小王转 200」明天就过期，
   把它当长期记忆会让下一次转账默认填错人）。
3. 抽取失败只是少记一条，绝不影响本轮回复：任何异常吞掉并记一条 warn 日志。
   记忆是增强项，不是主链路，不能因为它把好好的对话打断。

不 import app.planner：抽取是层 1 的事，planner 是层 2，反过来引用会成环。
"""

from __future__ import annotations

import difflib
from typing import Any, Final

import app.memory as memory
import app.scope as scope
from app.clock import ValidationError
from app.llm import LLMError, build_adapter
from app.log import log

#: 工具名固定，测试与日志都按它找。
TOOL_NAME: Final[str] = "remember_facts"
#: 一轮最多收几条：一轮闲聊能冒出十条「事实」的多半是模型在凑数。
MAX_ITEMS: Final[int] = 5
#: 用户原话短于这个字数就不抽（「嗯」「好」「转账」这种没有可记的稳定信息）。
MIN_USER_CHARS: Final[int] = 6
#: 送进模型的原文上限：抽取只需要一轮的语义，不需要整段历史。
MAX_USER_CHARS: Final[int] = 1200
MAX_ASSISTANT_CHARS: Final[int] = 600
#: 判「两条记忆是同一件事」的相似度门槛：0.82 能把「按周看/按周查看」并成一条，
#: 又不至于把「不吃辣」和「不吃香菜」这种真的两件事并掉。
SIMILAR_RATIO: Final[float] = 0.82

#: 纯寒暄：这些轮次一定没有可记的事，先本地拦掉，省一次模型调用。
CHITCHAT: Final[frozenset[str]] = frozenset(
    {
        "你好", "您好", "hi", "hello", "hey", "在吗", "在不在", "早", "早安", "早上好",
        "中午好", "下午好", "晚上好", "晚安", "谢谢", "多谢", "感谢", "好的", "好",
        "行", "可以", "ok", "okay", "收到", "明白", "懂了", "嗯", "哦", "哈", "哈哈",
        "再见", "拜拜", "没事", "没事了",
    }
)
#: 寒暄后面常跟的语气词，去掉再判一次（「你好呀」「谢谢啦」）。
_TAILS: Final[tuple[str, ...]] = ("呀", "啊", "啦", "哦", "呢", "吧", "哈", "了", "嘛")

REMEMBER_TOOL: Final[dict[str, Any]] = {
    "type": "function",
    "function": {
        "name": TOOL_NAME,
        "description": "把用户自己说出的、以后还用得上的长期偏好或事实记下来",
        "parameters": {
            "type": "object",
            "properties": {
                "items": {
                    "type": "array",
                    "description": "要记住的条目；没有值得长期记住的就传空数组",
                    "items": {
                        "type": "object",
                        "properties": {
                            "key": {"type": "string", "description": "标签，2~20 字，如「常去商户」「记账习惯」"},
                            "value": {"type": "string", "description": "一句话说清，不超过 60 字"},
                            "kind": {
                                "type": "string",
                                "enum": list(memory.KINDS),
                                "description": "fact=客观事实，preference=偏好，profile=画像",
                            },
                        },
                        "required": ["key", "value"],
                    },
                }
            },
            "required": ["items"],
        },
    },
}

SYSTEM_PROMPT: Final[str] = (
    "你是一个记忆整理器。从下面这轮对话里，只挑出**用户自己说出的、以后还用得上的"
    "稳定偏好或事实**，用工具记下来。\n"
    "规则：\n"
    "1. 只抽用户自己说出的、与资金/账户/计划有关的稳定偏好或事实"
    "（如「我常用招商银行那张卡」「记账喜欢按周看」「单笔超过 2000 我会犹豫」"
    "「不接受电话推销理财」）；助手说的话、你的推测一律不记。\n"
    "1.1 与钱无关的事一律不记（住址、称呼、天气、兴趣、心情都算），宁可不记。\n"
    "2. 不抽金额、不抽一次性的转账对象或一次性请求（「这次给小王转 200」「帮我查余额」都不记）。\n"
    "3. 不猜：用户没说清的不要补全，宁可不记。\n"
    "4. key 是短标签，value 是一句话；同一件事只出一条。\n"
    "5. 没有值得记的就调用工具并传空数组 items，不要解释。"
)


def _is_chitchat(text: str) -> bool:
    """纯寒暄判定：整句就是一个招呼/客套词（允许尾随语气词与标点）。"""
    core = text.strip().strip("！!。.,，、?？~～… ")
    if not core:
        return True
    low = core.lower()
    if low in CHITCHAT:
        return True
    for tail in _TAILS:
        if low.endswith(tail) and low[: -len(tail)] in CHITCHAT:
            return True
    return False


def _collect_items(response: Any) -> list[Any]:
    """把这次回复里属于 TOOL_NAME 的所有 items 收集起来（容忍模型拆成多次调用）。"""
    items: list[Any] = []
    for call in getattr(response, "tool_calls", None) or []:
        if getattr(call, "name", "") != TOOL_NAME:
            continue
        try:
            args = call.args()
        except Exception:  # noqa: BLE001 - 模型给的 JSON 不合法就当这条没写
            continue
        raw = args.get("items") if isinstance(args, dict) else None
        if isinstance(raw, list):
            items.extend(raw)
    return items


def _norm(text: Any) -> str:
    """归一化：小写、只留字母数字，用来判断两种说法是不是同一件事。"""
    return "".join(ch for ch in str(text or "").lower() if ch.isalnum())


def _similar(left: str, right: str) -> bool:
    """两条归一化文本说的是不是同一件事。

    模型常把同一件事拆成两条回报：「记账喜欢按周看」和「记账喜欢按周查看」。
    都照收的话，用户很快会看到一堆重复记忆，还得自己一条条删——那记忆就成了负担。
    判定用「包含」加「相似度」两条：包含管「乐刻健身」vs「乐刻健身（常去）」，
    相似度管中间插字的情况（上面那两句话只差一个「查」，字符包含反而判不出来）。
    """
    if not left or not right:
        return False
    if len(left) < 4 or len(right) < 4:
        return left == right
    short, long = (left, right) if len(left) <= len(right) else (right, left)
    if short in long:
        return True
    return difflib.SequenceMatcher(None, left, right).ratio() >= SIMILAR_RATIO


def _known(conn, user_id: str) -> list[tuple[str, str]]:
    """已有记忆的 (归一化内容, 键)：新抽到的近义说法要覆盖它，而不是再长一条。"""
    return [
        (_norm(fact["value"]), str(fact["key"]))
        for fact in memory.all_facts(conn, user_id=user_id, limit=200)
    ]


def _write_items(conn, user_id: str, items: list[Any]) -> list[dict[str, Any]]:
    """逐条落库；单条不合法（超长、kind 不对）只跳过那条，不影响其它条。

    落库前先跟「已有记忆」和「本批已写的」比一次：近义说法沿用旧键覆盖写，
    所以同一件事只会有一条，用户不用自己去收拾重复。
    """
    known = _known(conn, user_id)
    written: list[dict[str, Any]] = []
    for item in items[:MAX_ITEMS]:
        if not isinstance(item, dict):
            continue
        key = str(item.get("key") or "").strip()
        value = str(item.get("value") or "").strip()
        if not key or not value:
            continue
        # 不变量：这个助手只处理钱的事，长期记忆也只收与资金相关的偏好与事实
        # （常用卡、记账习惯、预算尺度、常用收款人、风险偏好、还款习惯）。
        if not scope.is_money_text(key + value):
            log.info("memory.extract_item_out_of_scope", key=key[:20])
            continue
        kind = str(item.get("kind") or "fact").strip()
        if kind not in memory.KINDS:
            kind = "fact"
        norm = _norm(value)
        reuse = next((name for old_norm, name in known if _similar(old_norm, norm)), "")
        try:
            fact = memory.remember(
                conn,
                user_id=user_id,
                key=reuse or key,
                value=value,
                kind=kind,
                source="auto",
            )
        except ValidationError as exc:
            log.warn("memory.extract_item_skipped", key=key[:20], reason=str(exc)[:80])
            continue
        except Exception as exc:  # noqa: BLE001 - 一条写不进去不能拖垮整轮
            log.warn(
                "memory.extract_item_failed",
                key=key[:20],
                exc_type=type(exc).__name__,
                exc_text=str(exc)[:80],
            )
            continue
        written.append(fact)
        target = str(fact["key"])
        known = [pair for pair in known if pair[1] != target] + [(norm, target)]
    return written


def run(
    conn,
    *,
    user_id: str,
    user_text: str,
    assistant_text: str,
    adapter: Any = None,
) -> list[dict[str, Any]]:
    """抽一轮对话里的长期记忆，返回本次写入（或覆盖）的条目；永不抛异常。"""
    owner = str(user_id or "").strip()
    text = str(user_text or "").strip()
    if not owner or len(text) < MIN_USER_CHARS or _is_chitchat(text):
        return []
    try:
        client = adapter if adapter is not None else build_adapter()
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": (
                    "【用户原话】\n" + text[:MAX_USER_CHARS]
                    + "\n\n【助手回复】\n" + str(assistant_text or "")[:MAX_ASSISTANT_CHARS]
                ),
            },
        ]
        response = client.chat(messages, tools=[REMEMBER_TOOL], temperature=0, max_tokens=600)
    except LLMError as exc:
        log.warn("memory.extract_llm_failed", user_id=owner, exc_text=str(exc)[:120])
        return []
    except Exception as exc:  # noqa: BLE001 - 记忆是增强项，任何意外都只降级不报错
        log.warn(
            "memory.extract_failed",
            user_id=owner,
            exc_type=type(exc).__name__,
            exc_text=str(exc)[:120],
        )
        return []
    try:
        return _write_items(conn, owner, _collect_items(response))
    except Exception as exc:  # noqa: BLE001 - 连解析都出错也只当这轮没抽到
        log.warn(
            "memory.extract_store_failed",
            user_id=owner,
            exc_type=type(exc).__name__,
            exc_text=str(exc)[:120],
        )
        return []


__all__ = [
    "CHITCHAT",
    "MAX_ITEMS",
    "MIN_USER_CHARS",
    "REMEMBER_TOOL",
    "SYSTEM_PROMPT",
    "TOOL_NAME",
    "run",
]
