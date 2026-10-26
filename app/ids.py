"""带前缀的短 ID、一次性随机数与幂等键。

对应文档：docs/04_核心模块实现规格.md 4.4、docs/08_编码规范.md 8.5。

随机只能从这里出：固定随机种子时种子脚本能复现，生产上又不用 uuid4() 散落各处。
"""

from __future__ import annotations

import hashlib
import secrets
from typing import Final

from app.clock import ValidationError

# docs/04 4.4：前缀白名单，避免日志里出现看不懂的 id
ID_PREFIXES: Final[frozenset[str]] = frozenset(
    {
        "u", "a", "c", "p", "t", "h", "s", "sch", "r", "n", "cf", "dn", "au", "tr",
        # 阶段 5 主动财务管理：储蓄目标 / 监测规则 / 提醒 / 监测运行
        "sg", "mr", "pa", "mnr",
        # 多因子挑战与本轮新增的跨场景计划
        "fc", "pl", "ps",
        # 生日计划的项目预算占用（不是 hold）
        "pb",
        # 人工接管单（handoff）：不用 "h"，那个前缀已经是资金 hold
        "ho",
        # 助手主动提醒（message）：用户能逐条采纳/忽略，必须有个能贴进对话的短 id
        "m",
    }
)

ID_HEX_LEN: Final[int] = 8
NONCE_HEX_LEN: Final[int] = 32


def new_id(prefix: str = "t") -> str:
    """生成 '前缀_8位十六进制' 短 ID，例如 new_id('a') -> 'a_3f9c1b2e'。"""
    if prefix not in ID_PREFIXES:
        raise ValidationError(
            "未知的 ID 前缀",
            prefix=prefix,
            allowed=sorted(ID_PREFIXES),
        )
    # 碰撞概率极低，按 docs/04 4.4 不重试：真撞了让 DB 主键约束自然暴露
    return f"{prefix}_{secrets.token_hex(ID_HEX_LEN // 2)}"


def nonce(n_hex_len: int = NONCE_HEX_LEN) -> str:
    """生成一次性随机数，用于 capability token 防重放。"""
    if n_hex_len <= 0 or n_hex_len % 2 != 0:
        raise ValidationError("nonce 长度必须是正偶数", n_hex_len=n_hex_len)
    return secrets.token_hex(n_hex_len // 2)


def idempotency_key(session_id: str, node_id: str, params_hash: str) -> str:
    """幂等键：同一会话同一节点同一参数，重试多少次都是同一个键。"""
    if not session_id or not node_id:
        raise ValidationError("幂等键缺少会话或节点标识")
    material = f"{session_id}|{node_id}|{params_hash}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


__all__ = ["ID_PREFIXES", "new_id", "nonce", "idempotency_key"]
