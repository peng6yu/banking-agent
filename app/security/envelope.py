"""数据信封：把用户和外部文本包成模型读得懂、又冒充不了的数据块。

对应文档：docs/05_安全层实现规格.md 5.5。

渲染形如 <DATA source=user>转 500 给张三</DATA>：
模型必须把标签内部当数据而不是指令，所以用户原文里的 <DATA 会被转义掉，
否则用户自己打一个开标签就能把自己的话伪装成系统数据（闭合标签注入）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final, Literal

from app.clock import ValidationError

MAX_TEXT_LEN: Final[int] = 500
TRUNCATION_MARK: Final[str] = "…[truncated]"
ALLOWED_SOURCES: Final[tuple[str, ...]] = (
    "user",
    "transaction_note",
    "merchant_name",
    "im_forward",
    "payee_alias",
)
_OPEN_TAG: Final[str] = "<DATA"
_CLOSE_TAG: Final[str] = "</DATA>"


@dataclass(frozen=True)
class Envelope:
    """一个数据信封；跨模块只传这个类型，不传裸 dict。"""

    source: str
    text: str
    kind: Literal["data"] = "data"
    truncated: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "source": self.source, "text": self.text, "truncated": self.truncated}

    @classmethod
    def from_obj(cls, obj: "Envelope | dict[str, Any]") -> "Envelope":
        """接受 Envelope 或 dict，统一成 Envelope。"""
        if isinstance(obj, Envelope):
            return obj
        if isinstance(obj, dict) and {"source", "text"} <= set(obj.keys()):
            return cls(
                source=str(obj["source"]),
                text=str(obj["text"]),
                truncated=bool(obj.get("truncated", False)),
            )
        raise ValidationError("信封数据不完整", received=type(obj).__name__)


def escape(text: str) -> str:
    """转义标签本身：先转 & 再转标签，避免出现 &lt; 被二次转义。"""
    return text.replace("&", "&amp;").replace(_OPEN_TAG, "&lt;DATA").replace(_CLOSE_TAG, "&lt;/DATA>")


def wrap(text: str, source: str) -> Envelope:
    """把一段外部文本包成信封：校验来源、超长截断。"""
    if source not in ALLOWED_SOURCES:
        raise ValidationError("数据来源不在白名单内", source=source, allowed=list(ALLOWED_SOURCES))
    if not isinstance(text, str):
        raise ValidationError("信封只能装文本", received=type(text).__name__)
    truncated = len(text) > MAX_TEXT_LEN
    body = text[:MAX_TEXT_LEN] if truncated else text
    return Envelope(source=source, text=body, truncated=truncated)


def render(env: "Envelope | dict[str, Any]") -> str:
    """渲染成给模型看的文本；截断过的在尾部留标记，方便用户知道内容被砍过。"""
    item = Envelope.from_obj(env)
    body = escape(item.text)
    if item.truncated:
        body = f"{body}{TRUNCATION_MARK}"
    return f"<DATA source={item.source}>{body}</DATA>"


def as_data(items: list["Envelope | dict[str, Any]"]) -> str:
    """多段数据拼成一段文本，逐行一个信封，方便按 source 溯源。"""
    if not items:
        raise ValidationError("信封列表不能为空")
    return "\n".join(render(item) for item in items)


__all__ = ["ALLOWED_SOURCES", "MAX_TEXT_LEN", "TRUNCATION_MARK", "Envelope", "as_data", "escape", "render", "wrap"]
