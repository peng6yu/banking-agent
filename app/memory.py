"""长期记忆：用户偏好与事实跨会话持久化，下次对话当默认值用。

会话记忆（app/chat_history.py）记的是「这次说过什么」，只在一个会话里有效；
这里记的是「这个人是怎样的」——称呼、习惯、常去商户、沟通偏好。它不绑会话，
换一个会话、重启服务之后都还在，这就是「越用越懂这个人」的那一层。

三条边界：
1. 只收用户自己说过或确认过的事，不猜、不从流水里反推；
2. 记忆是**默认值**，不是事实依据：涉及金额、账户、收款人的判断一律以本次原话
   与库里的账务数据为准——记忆永远不能变成一次授权；
3. 写入与删除都落审计（memory.written / memory.forgotten），用户能查、能改、能删。
"""

from __future__ import annotations

import time
from typing import Any, Final

import app.db as db
from app import audit
from app.clock import ValidationError, now_ms

#: 记忆的键最长多少字：键是「标签」，不是句子，超过就该拆成两条。
KEY_MAX: Final[int] = 40
#: 记忆的值最长多少字：够记一句归纳，不够记一篇流水。
VALUE_MAX: Final[int] = 200
#: kind 闭集，与 agent_memory 表的 CHECK 一一对应（写错不要靠库报错，先在这里拦）。
KINDS: Final[tuple[str, ...]] = ("fact", "preference", "profile")
#: 给界面/提示词看的中文标签。
KIND_LABELS: Final[dict[str, str]] = {
    "fact": "事实",
    "preference": "偏好",
    "profile": "画像",
}
#: 提示词块的抬头：安全边界写在抬头里，模型每次都能看到，不用靠调用方记得加。
PROMPT_HEAD: Final[str] = (
    "关于这位用户的长期记忆（用户自己说过或确认过的事，可作默认值，"
    "但涉及金额/账户仍以本次原话为准）："
)
#: 一次最多给多少条记忆进提示词：太多会挤掉本轮真正要处理的信息。
PROMPT_LIMIT: Final[int] = 20


def _updated_text(ms: int) -> str:
    """毫秒时间戳 -> 本地「YYYY-MM-DD HH:MM」，和账单/提醒用同一套显示口径。"""
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(int(ms) / 1000))


def _fact(row: Any) -> dict[str, Any]:
    """把一行 agent_memory 变成上层（接口/提示词）能直接用的结构。"""
    kind = str(row["kind"])
    updated_ms = int(row["updated_ms"])
    return {
        "key": str(row["mem_key"]),
        "value": str(row["mem_value"]),
        "kind": kind,
        "kind_label": KIND_LABELS.get(kind, kind),
        "source": str(row["source"]),
        "updated_ms": updated_ms,
        "updated_text": _updated_text(updated_ms),
    }


def _clean_user(user_id: Any) -> str:
    owner = str(user_id or "").strip()
    if not owner:
        raise ValidationError("记忆的归属用户不能是空的", field="user_id")
    return owner


def _clean_key(key: Any) -> str:
    """键：非空、去空白、最长 KEY_MAX。超长直接报错，不静默截断。"""
    text = str(key or "").strip()
    if not text:
        raise ValidationError("记忆的键不能是空的", field="key")
    if len(text) > KEY_MAX:
        raise ValidationError(f"记忆的键最长 {KEY_MAX} 个字，再长就拆成两条", field="key")
    return text


def _clean_value(value: Any) -> str:
    """值：非空、去空白、最长 VALUE_MAX。超长直接报错，不静默截断。"""
    text = str(value or "").strip()
    if not text:
        raise ValidationError("记忆的内容不能是空的", field="value")
    if len(text) > VALUE_MAX:
        raise ValidationError(f"一条记忆最长 {VALUE_MAX} 个字，再长就拆成两条", field="value")
    return text


def _clean_kind(kind: Any) -> str:
    text = str(kind or "").strip()
    if text not in KINDS:
        raise ValidationError("记忆的类型只能是事实/偏好/画像", kind=str(kind or ""))
    return text


def remember(
    conn,
    *,
    user_id: str,
    key: str,
    value: str,
    kind: str = "fact",
    source: str = "",
) -> dict[str, Any]:
    """记住一条：同一个 key 覆盖写，不新增第二行。

    覆盖而不是追加：记忆要的是「现在这个人的样子」，不是聊天记录。
    同一件事说两遍只应该更新时间和说法，库里不能出现两条互相矛盾的记忆。
    """
    owner = _clean_user(user_id)
    mem_key = _clean_key(key)
    mem_value = _clean_value(value)
    mem_kind = _clean_kind(kind)
    origin = str(source or "").strip()
    at = now_ms()
    with db.tx(conn):
        conn.execute(
            "INSERT INTO agent_memory (user_id, mem_key, mem_value, kind, source, updated_ms)"
            " VALUES (?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(user_id, mem_key) DO UPDATE SET"
            " mem_value = ?, kind = ?, source = ?, updated_ms = ?",
            (owner, mem_key, mem_value, mem_kind, origin, at, mem_value, mem_kind, origin, at),
        )
        # 审计只记「谁、哪条、什么类型」和一小段预览；整段原文不进审计链，
        # 免得审计里堆满用户隐私原文（原文就在 agent_memory，删得掉、改得动）。
        audit.append(
            conn,
            "memory.written",
            {
                "user_id": owner,
                "key": mem_key,
                "kind": mem_kind,
                "value_preview": mem_value[:40],
            },
        )
    return {
        "key": mem_key,
        "value": mem_value,
        "kind": mem_kind,
        "kind_label": KIND_LABELS.get(mem_kind, mem_kind),
        "source": origin,
        "updated_ms": at,
        "updated_text": _updated_text(at),
    }


def forget(conn, *, user_id: str, key: str) -> dict[str, Any]:
    """忘掉一条；幂等——本来就没有也算成功，只是 removed=False。"""
    owner = _clean_user(user_id)
    mem_key = _clean_key(key)
    with db.tx(conn):
        removed = conn.execute(
            "DELETE FROM agent_memory WHERE user_id = ? AND mem_key = ?", (owner, mem_key)
        ).rowcount
        audit.append(
            conn,
            "memory.forgotten",
            {"user_id": owner, "key": mem_key, "removed": bool(removed)},
        )
    return {"key": mem_key, "removed": bool(removed)}


def all_facts(conn, *, user_id: str, limit: int = 50) -> list[dict[str, Any]]:
    """某个用户的全部记忆，最近更新的在前（同刻按 key 排，顺序稳定可复现）。"""
    owner = _clean_user(user_id)
    rows = conn.execute(
        "SELECT * FROM agent_memory WHERE user_id = ?"
        " ORDER BY updated_ms DESC, mem_key ASC LIMIT ?",
        (owner, max(1, int(limit))),
    ).fetchall()
    return [_fact(row) for row in rows]


def prompt_block(conn, *, user_id: str, limit: int = PROMPT_LIMIT) -> str:
    """给系统提示词用的紧凑记忆块；没有记忆返回空串（调用方据此决定加不加这一段）。

    抬头自带安全边界：记忆可作默认值，但金额/账户以本次原话为准。
    这段文字会直接进模型上下文，所以不放时间戳、不放 source 之类的噪声字段。
    """
    owner = _clean_user(user_id)
    facts = all_facts(conn, user_id=owner, limit=limit)
    if not facts:
        return ""
    lines = [PROMPT_HEAD]
    for fact in facts:
        label = fact["kind_label"]
        lines.append(f"- [{label}] {fact['key']}：{fact['value']}")
    return "\n".join(lines) + "\n"


__all__ = [
    "KEY_MAX",
    "VALUE_MAX",
    "KINDS",
    "KIND_LABELS",
    "PROMPT_HEAD",
    "PROMPT_LIMIT",
    "all_facts",
    "forget",
    "prompt_block",
    "remember",
]
