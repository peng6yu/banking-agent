"""全项目的数据契约（pydantic 模型与字面量类型）。

对应文档：docs/04_核心模块实现规格.md 4.5、docs/08_编码规范.md 8.2/8.3。

所有模型 extra='forbid'：前端多塞一个字段就直接报错，防止偷偷混进额外参数。
金额字段一律 int 且以 _fen 结尾，时间字段一律以 _ms 结尾。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

RiskLevel = Literal["green", "yellow", "red"]
DecisionType = Literal["allow", "confirm", "deny"]
NodeState = Literal[
    "pending", "running", "waiting_confirm", "waiting_factor", "done", "failed", "skipped",
]
ActionSource = Literal["user", "agent", "system"]

_BASE_CONFIG = ConfigDict(extra="forbid")


class Action(BaseModel):
    """一次结构化动作，策略引擎的唯一输入。"""

    model_config = _BASE_CONFIG

    tool: str
    params: dict[str, Any]
    account_id: str
    amount_fen: int | None = None
    source: ActionSource = "agent"
    untrusted_fields: list[str] = Field(default_factory=list)


class Decision(BaseModel):
    """策略引擎的裁决结果；reason_text 必须能直接展示给用户。"""

    model_config = _BASE_CONFIG

    level: RiskLevel
    rule_id: str
    reason_code: str
    reason_text: str
    required_factors: list[str] = Field(default_factory=list)
    limit_fen: int | None = None
    spent_today_fen: int | None = None


class DAGNode(BaseModel):
    """执行图的一个步骤。"""

    model_config = _BASE_CONFIG

    id: str
    tool: str
    params: dict[str, Any]
    depends_on: list[str] = Field(default_factory=list)
    state: NodeState = "pending"


class Dag(BaseModel):
    """一次规划产出的执行图。"""

    model_config = _BASE_CONFIG

    id: str
    nodes: list[DAGNode]


class ToolResult(BaseModel):
    """工具执行结果；失败也返回结构化结果，由 API 层决定怎么呈现。"""

    model_config = _BASE_CONFIG

    ok: bool
    data: dict[str, Any] = Field(default_factory=dict)
    error_code: str | None = None
    error_text: str | None = None
    audit_seq: int | None = None


class ChatRequest(BaseModel):
    """POST /api/chat 的入参。"""

    model_config = _BASE_CONFIG

    session_id: str | None = None
    text: str = Field(min_length=1, max_length=500)


class FactorProof(BaseModel):
    """一条多因子证明：服务端签发的挑战 + 用户手上的验证码。"""

    model_config = _BASE_CONFIG

    factor: str
    challenge_id: str
    code: str


class ConfirmRequest(BaseModel):
    """POST /api/confirm 的入参。"""

    model_config = _BASE_CONFIG

    session_id: str
    confirmation_id: str
    approve: bool
    #: 历史字段，只表示「用户说自己试了哪几个因子」，不再作为放行依据
    factor: str | None = None
    #: 真正的放行依据：每条都是服务端签发、服务端校验、一次性的
    proofs: list[FactorProof] | None = None


class FactorChallengeRequest(BaseModel):
    """POST /api/factor/challenge 的入参。"""

    model_config = _BASE_CONFIG

    session_id: str
    confirmation_id: str
    factor: str | None = None


__all__ = [
    "FactorChallengeRequest",
    "FactorProof",
    "RiskLevel",
    "DecisionType",
    "NodeState",
    "ActionSource",
    "Action",
    "Decision",
    "DAGNode",
    "Dag",
    "ToolResult",
    "ChatRequest",
    "ConfirmRequest",
]
