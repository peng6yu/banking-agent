from __future__ import annotations

import json
import sys
import time

from .config import get_settings
from .llm import LLMAdapter, LLMError, MockAdapter, build_adapter

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "query_balance",
            "description": "查询用户名下银行卡的余额",
            "parameters": {
                "type": "object",
                "properties": {"card_tail": {"type": "string", "description": "卡号后四位"}},
                "required": ["card_tail"],
            },
        },
    }
]

ASK = [{"role": "user", "content": "帮我查一下尾号1234的储蓄卡余额"}]
RESULTS: list[bool] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append(ok)
    print(("PASS  " if ok else "FAIL  ") + name + ((" | " + detail) if detail else ""))


def main() -> int:
    s = get_settings()
    print("端点:", s.endpoint)
    print("主脑:", s.model_brain, " 备脑:", s.model_backup, " mock:", s.mock)
    if not s.api_key:
        print("缺少 MINIMAX_API_KEY，先执行 setup_env.sh")
        return 2

    adapter = LLMAdapter(s)

    # 1 工具调用：中文意图 -> 选对工具 + 抽对参数
    try:
        r = adapter.chat(ASK, tools=TOOLS)
        call = r.first_call("query_balance")
        ok = call is not None and str(call.args().get("card_tail")) == "1234"
        detail = "finish=" + r.finish_reason + " latency=" + str(r.latency_ms) + "ms"
        if call is not None:
            detail += " args=" + json.dumps(call.args(), ensure_ascii=False)
        check("工具调用可用（结构化输出走 tool_calls）", ok, detail)
    except LLMError as exc:
        check("工具调用可用（结构化输出走 tool_calls）", False, str(exc))

    # 2 思维链剥离：M2.1 的 content 里混 <think>，适配器必须清干净
    try:
        r = adapter.chat(ASK, model=s.model_backup, max_tokens=256)
        clean = "<think>" not in r.content and "</think>" not in r.content
        check(
            "思维链剥离（" + s.model_backup + "）",
            clean,
            "content=" + repr(r.content[:40]) + " reasoning_len=" + str(len(r.reasoning)),
        )
    except LLMError as exc:
        check("思维链剥离（" + s.model_backup + "）", False, str(exc))

    # 3 流式：没有 [DONE]，靠 finish_reason 收尾
    try:
        deltas = 0
        done = None
        t0 = time.time()
        for ev in adapter.chat_stream(ASK, tools=TOOLS):
            if ev["type"] == "delta":
                deltas += 1
            elif ev["type"] == "done":
                done = ev
        elapsed = int((time.time() - t0) * 1000)
        ok = done is not None and (deltas > 0 or bool(done.get("tool_calls")))
        check(
            "流式可用（无 [DONE] 也能收尾）",
            ok,
            "deltas=" + str(deltas) + " finish=" + str((done or {}).get("finish_reason")) + " " + str(elapsed) + "ms",
        )
    except LLMError as exc:
        check("流式可用（无 [DONE] 也能收尾）", False, str(exc))

    # 4 mock 兜底：演示日断网也要能演
    mock = MockAdapter()
    mr = mock.chat(ASK, tools=TOOLS)
    check("Mock 兜底可用（断网/对抗演示）", mr.first_call("query_balance") is not None, "model=" + mr.model)

    print()
    print("累计真实调用:", adapter.calls, " 通过:", sum(RESULTS), "/", len(RESULTS))
    return 0 if all(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
