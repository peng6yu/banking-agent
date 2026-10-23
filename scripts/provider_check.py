#!/usr/bin/env python3.12
"""厂商兼容性自检：实测某个 OpenAI 兼容厂商能不能被本项目用起来。

用法：
    python3.12 -m scripts.provider_check
    python3.12 -m scripts.provider_check --base-url https://api.deepseek.com --model deepseek-chat
    python3.12 -m scripts.provider_check --json
    python3.12 -m scripts.provider_check --help

为什么要有这条命令：
    换厂商时最容易踩的坑是「接口能通，但能力不兼容」——比如厂商收 tools 却不回
    tool_calls、或者不接受 tool_choice:"auto" 直接 400、或者流式不按 SSE 走。
    本项目只依赖 OpenAI 兼容协议（app/llm.py:POST {base}/chat/completions + Bearer +
    tool_calls + SSE），所以能不能用，靠实测四下就够，不用靠任何人的背书。

四条纪律：
    1. 只发 OpenAI 兼容请求，别的协议一律不试（不试 Anthropic / Gemini 原生接口）。
    2. key 只从环境变量读（LLM_API_KEY，兼容旧名 MINIMAX_API_KEY），
       命令行永远不提供 key——避免 key 落进 shell history；输出里也永远不出现 key
       （连厂商报错里回显的 key 也会被打码）。
    3. 本脚本只读不改：不写业务库、不改配置、不动 .env，跑完不留痕迹。
    4. 结论只看实测：4 项全过才算「可用」；有 FAIL 就照 FAIL 说，不替厂商圆场。

四项检查：
    a) 连通性：最小 messages 的 POST 返回 200，且响应是 OpenAI 兼容结构。
    b) 工具调用：带最小 tools 定义时，响应里真的有 tool_calls（结构化输出的地基）。
    c) tool_choice:auto：带 tool_choice:"auto" 的请求被接受（不被 400 拒绝）。
    d) SSE 流式：stream=true 时能读到 data: 行、delta.content 有内容、以 finish_reason 收尾
       （兼容没有 data: [DONE] 的厂商，比如 MiniMax）。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import unicodedata
import urllib.error
import urllib.request
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import Settings, build_endpoint  # noqa: E402

# 最小工具定义：一个函数、一条必填参数，够验证 tool_calls 通路即可
MINIMAL_TOOLS: list[dict[str, Any]] = [
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

# a) 连通性用：只要一句最短的话
PING_MESSAGES: list[dict[str, Any]] = [{"role": "user", "content": "只回复一个字：好"}]
# b) c) 用：明确要求调用工具，逼模型走 tool_calls 而不是自由发挥
TOOL_MESSAGES: list[dict[str, Any]] = [
    {"role": "user", "content": "请务必调用 query_balance 工具，查询尾号 1234 的储蓄卡余额。"}
]
# d) 用：不带 tools，保证流式里一定有 delta.content 文本可看
STREAM_MESSAGES: list[dict[str, Any]] = [{"role": "user", "content": "用一句话介绍你自己。"}]

# 每项检查的固定名字（表格与 JSON 都用它，方便脚本化消费）
CHECK_CONNECTIVITY = "connectivity"
CHECK_TOOL_CALLS = "tool_calls"
CHECK_TOOL_CHOICE = "tool_choice_auto"
CHECK_SSE = "sse_stream"

CHECK_NAMES: dict[str, str] = {
    CHECK_CONNECTIVITY: "连通性",
    CHECK_TOOL_CALLS: "工具调用",
    CHECK_TOOL_CHOICE: "tool_choice:auto",
    CHECK_SSE: "SSE 流式",
}

# 失败时给的排查方向：只说现象对应的常见原因，不替厂商猜具体型号
FAIL_HINTS: dict[str, str] = {
    CHECK_CONNECTIVITY: "先看 base url / endpoint 拼得对不对、key 是不是同一家、网络通不通",
    CHECK_TOOL_CALLS: "该厂商可能不支持 tools/tool_calls；本项目的结构化输出只走 tool_calls，不支持就得换",
    CHECK_TOOL_CHOICE: "该厂商不接受 tool_choice:\"auto\"，会直接 400（app/llm.py 每次都带这个字段）",
    CHECK_SSE: "该厂商的流式不标准：要么不是 SSE，要么不发 finish_reason 收尾",
}

_EVIDENCE_MAX_WIDTH = 92


# --------------------------------------------------------------------------
# 数据结构
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class Target:
    """本次要测的厂商目标。

    api_key 只在内存里用来拼请求头，任何输出（表格、JSON、报错）都不许带上它。
    """

    provider: str
    base_url: str
    endpoint: str
    model: str
    api_key: str
    timeout: int


@dataclass
class CheckResult:
    id: str
    name: str
    ok: bool
    evidence: str


@dataclass
class HttpReply:
    status: int | None = None
    text: str = ""
    data: Any = None
    error: str | None = None


@dataclass
class StreamProbe:
    status: int | None = None
    content_type: str = ""
    data_lines: int = 0
    content: str = ""
    finish_reason: str = ""
    saw_done_marker: bool = False
    error: str | None = None


# --------------------------------------------------------------------------
# 小工具
# --------------------------------------------------------------------------
def _display_width(text: str) -> int:
    """中文按 2 列算，英文按 1 列——表格要能对齐。"""
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in text)


def _truncate(text: str, max_width: int = _EVIDENCE_MAX_WIDTH) -> str:
    if _display_width(text) <= max_width:
        return text
    out = ""
    width = 0
    for ch in text:
        step = 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
        if width + step > max_width - 1:
            break
        out += ch
        width += step
    return out + "…"


def _scrub(text: str, secret: str) -> str:
    """把可能出现的 key 打码。任何输出都不允许带 key。"""
    if not text:
        return text
    if secret:
        text = text.replace(secret, "***")
    return text


def _snippet(text: str, limit: int = 160) -> str:
    flat = " ".join((text or "").split())
    if len(flat) <= limit:
        return flat
    return flat[: limit - 1] + "…"


def _describe(exc: BaseException) -> str:
    """异常转一句话证据。"""
    if isinstance(exc, urllib.error.HTTPError):
        return "HTTP " + str(exc.code)
    reason = getattr(exc, "reason", None)
    if reason is not None:
        return type(exc).__name__ + "：" + str(reason)
    return type(exc).__name__ + "：" + str(exc)


def _status_of(resp: Any) -> int | None:
    status = getattr(resp, "status", None)
    if isinstance(status, int):
        return status
    try:
        return int(resp.getcode())
    except (AttributeError, TypeError, ValueError):
        return None


def _header_of(resp: Any, name: str) -> str:
    headers = getattr(resp, "headers", None)
    if headers is None:
        return ""
    try:
        value = headers.get(name)
    except (AttributeError, TypeError):
        return ""
    return str(value or "")


def _headers(api_key: str) -> dict[str, str]:
    """鉴权固定走 OpenAI 兼容协议的标准做法：Bearer。"""
    return {
        "Authorization": "Bearer " + api_key,
        "Content-Type": "application/json",
    }


def _read_http_error(exc: urllib.error.HTTPError, api_key: str = "") -> str:
    try:
        return _scrub(exc.read().decode("utf-8", "replace"), api_key)
    except (OSError, ValueError):
        return ""


# --------------------------------------------------------------------------
# 配置解析
# --------------------------------------------------------------------------
def resolve_target(args: argparse.Namespace) -> Target:
    """算出这次要测的 base / endpoint / model / key。

    规则与 app/config.py 一致：
      * base：--base-url > LLM_BASE_URL > MINIMAX_API_HOST > 代码默认值；
      * endpoint：base 按 app/config.py:build_endpoint 的规则拼；
        只有在没传 --base-url 时才认 LLM_ENDPOINT（传了就说明要测别家，
        不能让旧的 LLM_ENDPOINT 把请求打回原厂）；
      * model：--model > LLM_MODEL > MODEL_BRAIN > 代码默认值；
      * key：只从 LLM_API_KEY / MINIMAX_API_KEY 读，命令行一律不给。
    """
    settings = Settings()
    base_url = (args.base_url or settings.base_url or "").strip()
    model = (args.model or settings.model_brain or "").strip()
    endpoint_override = "" if args.base_url else settings.endpoint_override
    endpoint = build_endpoint(base_url, endpoint_override)
    effective = replace(settings, base_url=base_url, model_brain=model)
    timeout = int(args.timeout) if args.timeout else int(settings.timeout or 60)
    return Target(
        provider=effective.provider,
        base_url=base_url,
        endpoint=endpoint,
        model=model,
        api_key=settings.api_key,
        timeout=timeout,
    )


# --------------------------------------------------------------------------
# HTTP 探测
# --------------------------------------------------------------------------
def _post_json(url: str, body: dict[str, Any], api_key: str, timeout: int) -> HttpReply:
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=_headers(api_key), method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            status = _status_of(resp)
            text = _scrub(raw.decode("utf-8", "replace"), api_key)
    except urllib.error.HTTPError as exc:
        return HttpReply(status=exc.code, text=_read_http_error(exc, api_key))
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return HttpReply(error=_describe(exc))
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        parsed = None
    return HttpReply(status=status, text=text, data=parsed)


def _stream_probe(url: str, body: dict[str, Any], api_key: str, timeout: int) -> StreamProbe:
    """读一次 SSE 流，记录 data 行数、增量文本与收尾方式。

    收尾判定刻意做成「finish_reason 或 data: [DONE] 任一」：
    MiniMax 这类厂商不发 [DONE]，只靠 finish_reason 收尾（app/llm.py 同款逻辑）。
    """
    probe = StreamProbe()
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=_headers(api_key), method="POST")
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as exc:
        probe.status = exc.code
        probe.error = "HTTP " + str(exc.code) + "：" + _snippet(_read_http_error(exc, api_key))
        return probe
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        probe.error = "网络错误：" + _describe(exc)
        return probe

    try:
        probe.status = _status_of(resp)
        probe.content_type = _header_of(resp, "Content-Type")
        while True:
            try:
                line = resp.readline()
            except (TimeoutError, OSError) as exc:
                probe.error = "读取中断：" + _describe(exc)
                break
            if not line:
                break
            text = line.decode("utf-8", "replace").strip()
            if not text or not text.startswith("data:"):
                continue
            payload = text[5:].strip()
            if payload == "[DONE]":
                probe.saw_done_marker = True
                break
            try:
                chunk = json.loads(payload)
            except json.JSONDecodeError:
                continue
            if not isinstance(chunk, dict):
                continue
            probe.data_lines += 1
            choice = (chunk.get("choices") or [{}])[0]
            delta = choice.get("delta") or {}
            piece = delta.get("content") or ""
            if isinstance(piece, str) and piece:
                probe.content += piece
            finish = choice.get("finish_reason")
            if finish:
                probe.finish_reason = str(finish)
                break
    finally:
        try:
            resp.close()
        except OSError:
            pass
    return probe


# --------------------------------------------------------------------------
# 四项检查
# --------------------------------------------------------------------------
def check_connectivity(target: Target) -> CheckResult:
    """a) 连通性：最小 messages 的 POST 返回 200。"""
    body = {
        "model": target.model,
        "messages": PING_MESSAGES,
        "temperature": 0,
        "max_tokens": 32,
        "stream": False,
    }
    started = time.time()
    reply = _post_json(target.endpoint, body, target.api_key, target.timeout)
    elapsed = int((time.time() - started) * 1000)
    if reply.error:
        return CheckResult(CHECK_CONNECTIVITY, CHECK_NAMES[CHECK_CONNECTIVITY], False, "网络错误：" + reply.error)
    if reply.status != 200:
        return CheckResult(
            CHECK_CONNECTIVITY,
            CHECK_NAMES[CHECK_CONNECTIVITY],
            False,
            "HTTP " + str(reply.status) + "：" + _snippet(reply.text),
        )
    data = reply.data
    if not isinstance(data, dict) or not isinstance(data.get("choices"), list):
        return CheckResult(
            CHECK_CONNECTIVITY,
            CHECK_NAMES[CHECK_CONNECTIVITY],
            False,
            "HTTP 200 但响应不是 OpenAI 兼容结构：" + _snippet(reply.text),
        )
    choices = data.get("choices") or []
    return CheckResult(
        CHECK_CONNECTIVITY,
        CHECK_NAMES[CHECK_CONNECTIVITY],
        True,
        "HTTP 200，choices " + str(len(choices)) + " 条，耗时 " + str(elapsed) + "ms",
    )


def check_tool_calls(target: Target) -> CheckResult:
    """b) 工具调用：带最小 tools 定义时响应里有 tool_calls。

    这里故意不带 tool_choice，把「厂商支不支持工具」和「厂商收不收 tool_choice」
    拆成两项独立结论。
    """
    body = {
        "model": target.model,
        "messages": TOOL_MESSAGES,
        "temperature": 0,
        "max_tokens": 256,
        "stream": False,
        "tools": MINIMAL_TOOLS,
    }
    reply = _post_json(target.endpoint, body, target.api_key, target.timeout)
    if reply.error:
        return CheckResult(CHECK_TOOL_CALLS, CHECK_NAMES[CHECK_TOOL_CALLS], False, "网络错误：" + reply.error)
    if reply.status != 200:
        return CheckResult(
            CHECK_TOOL_CALLS,
            CHECK_NAMES[CHECK_TOOL_CALLS],
            False,
            "HTTP " + str(reply.status) + "：" + _snippet(reply.text),
        )
    data = reply.data if isinstance(reply.data, dict) else {}
    choices = data.get("choices") or []
    message = (choices[0].get("message") or {}) if choices else {}
    calls = message.get("tool_calls") or []
    finish = choices[0].get("finish_reason") if choices else None
    if not calls:
        content = message.get("content") or ""
        return CheckResult(
            CHECK_TOOL_CALLS,
            CHECK_NAMES[CHECK_TOOL_CALLS],
            False,
            "响应里没有 tool_calls（finish_reason=" + str(finish) + "，content=" + _snippet(str(content), 60) + "）",
        )
    names = []
    for raw in calls[:3]:
        fn = raw.get("function") or {}
        names.append(str(fn.get("name") or "?"))
    return CheckResult(
        CHECK_TOOL_CALLS,
        CHECK_NAMES[CHECK_TOOL_CALLS],
        True,
        "返回 tool_calls " + str(len(calls)) + " 个（" + "、".join(names) + "），finish_reason=" + str(finish),
    )


def check_tool_choice(target: Target) -> CheckResult:
    """c) tool_choice:auto：带 tool_choice:"auto" 的请求被接受（不被 400 拒绝）。"""
    body = {
        "model": target.model,
        "messages": TOOL_MESSAGES,
        "temperature": 0,
        "max_tokens": 256,
        "stream": False,
        "tools": MINIMAL_TOOLS,
        "tool_choice": "auto",
    }
    reply = _post_json(target.endpoint, body, target.api_key, target.timeout)
    if reply.error:
        return CheckResult(CHECK_TOOL_CHOICE, CHECK_NAMES[CHECK_TOOL_CHOICE], False, "网络错误：" + reply.error)
    if reply.status is not None and reply.status >= 400:
        return CheckResult(
            CHECK_TOOL_CHOICE,
            CHECK_NAMES[CHECK_TOOL_CHOICE],
            False,
            "HTTP " + str(reply.status) + " 被拒：" + _snippet(reply.text),
        )
    data = reply.data if isinstance(reply.data, dict) else {}
    choices = data.get("choices") or []
    calls = ((choices[0].get("message") or {}) if choices else {}).get("tool_calls") or []
    return CheckResult(
        CHECK_TOOL_CHOICE,
        CHECK_NAMES[CHECK_TOOL_CHOICE],
        True,
        "HTTP " + str(reply.status) + " 被接受（未 400），" + str(len(calls)) + " 个 tool_calls",
    )


def check_sse_stream(target: Target) -> CheckResult:
    """d) SSE 流式：读得到 data: 行、delta.content 有内容、以 finish_reason 收尾。"""
    body = {
        "model": target.model,
        "messages": STREAM_MESSAGES,
        "temperature": 0,
        "max_tokens": 128,
        "stream": True,
    }
    probe = _stream_probe(target.endpoint, body, target.api_key, target.timeout)
    if probe.error:
        return CheckResult(CHECK_SSE, CHECK_NAMES[CHECK_SSE], False, probe.error)
    if probe.status is not None and probe.status >= 400:
        return CheckResult(
            CHECK_SSE,
            CHECK_NAMES[CHECK_SSE],
            False,
            "HTTP " + str(probe.status) + "：" + _snippet(probe.error or ""),
        )
    if probe.data_lines == 0:
        return CheckResult(
            CHECK_SSE,
            CHECK_NAMES[CHECK_SSE],
            False,
            "没有读到 data: 行（content-type=" + (probe.content_type or "空") + "），不是 SSE",
        )
    if not probe.content:
        return CheckResult(
            CHECK_SSE,
            CHECK_NAMES[CHECK_SSE],
            False,
            "读到 " + str(probe.data_lines) + " 条 data 行，但 delta.content 为空",
        )
    if not probe.finish_reason:
        tail = "收到 data: [DONE]" if probe.saw_done_marker else "连接已结束"
        return CheckResult(
            CHECK_SSE,
            CHECK_NAMES[CHECK_SSE],
            False,
            tail + "，但没有 finish_reason 收尾",
        )
    marker = "（另有 [DONE]）" if probe.saw_done_marker else "（无 [DONE]）"
    return CheckResult(
        CHECK_SSE,
        CHECK_NAMES[CHECK_SSE],
        True,
        "data 行 " + str(probe.data_lines) + " 条，delta.content " + str(len(probe.content)) + " 字，"
        + "finish_reason=" + probe.finish_reason + marker,
    )


def run_checks(target: Target) -> list[CheckResult]:
    """跑完四项检查。缺 key 时不联网，四项全判 FAIL。"""
    if not target.api_key:
        missing = "未配置 API key（LLM_API_KEY，兼容旧名 MINIMAX_API_KEY）"
        return [
            CheckResult(cid, CHECK_NAMES[cid], False, missing)
            for cid in (CHECK_CONNECTIVITY, CHECK_TOOL_CALLS, CHECK_TOOL_CHOICE, CHECK_SSE)
        ]
    runners = (
        (CHECK_CONNECTIVITY, check_connectivity),
        (CHECK_TOOL_CALLS, check_tool_calls),
        (CHECK_TOOL_CHOICE, check_tool_choice),
        (CHECK_SSE, check_sse_stream),
    )
    results: list[CheckResult] = []
    for cid, runner in runners:
        try:
            item = runner(target)
        except Exception as exc:  # noqa: BLE001  兜底：自检脚本不能自己炸，失败也要给证据
            item = CheckResult(cid, CHECK_NAMES[cid], False, "检查过程异常：" + _describe(exc))
        # 最后一道保险：任何证据里都不许带 key
        item.evidence = _scrub(item.evidence, target.api_key)
        results.append(item)
    return results


# --------------------------------------------------------------------------
# 报告
# --------------------------------------------------------------------------
def build_report(target: Target, results: list[CheckResult]) -> dict[str, Any]:
    passed = sum(1 for item in results if item.ok)
    total = len(results)
    ok = total > 0 and passed == total
    suggestion: dict[str, str] = {
        "LLM_BASE_URL": target.base_url,
        "LLM_MODEL": target.model,
    }
    if build_endpoint(target.base_url, "") != target.endpoint:
        suggestion["LLM_ENDPOINT"] = target.endpoint
    return {
        "ok": ok,
        "provider": target.provider,
        "base_url": target.base_url,
        "endpoint": target.endpoint,
        "model": target.model,
        "api_key_configured": bool(target.api_key),
        "checks": [
            {
                "id": item.id,
                "name": item.name,
                "status": "PASS" if item.ok else "FAIL",
                "ok": item.ok,
                "evidence": item.evidence,
            }
            for item in results
        ],
        "passed": passed,
        "total": total,
        "conclusion": "可用" if ok else "不可用",
        "env_suggestion": suggestion,
        "exit_code": 0 if ok else 1,
    }


def _pad(text: str, width: int) -> str:
    return text + " " * max(0, width - _display_width(text))


def render_table(checks: list[dict[str, Any]]) -> str:
    name_w = max([_display_width("检查项")] + [_display_width(str(c["name"])) for c in checks])
    status_w = max([_display_width("结果")] + [_display_width(str(c["status"])) for c in checks])
    lines = [
        _pad("检查项", name_w) + "  " + _pad("结果", status_w) + "  证据",
        "-" * name_w + "  " + "-" * status_w + "  " + "-" * 24,
    ]
    for item in checks:
        lines.append(
            _pad(str(item["name"]), name_w)
            + "  "
            + _pad(str(item["status"]), status_w)
            + "  "
            + _truncate(str(item["evidence"]))
        )
    return "\n".join(lines)


def render_text(report: dict[str, Any]) -> str:
    lines: list[str] = []
    lines.append("厂商兼容性自检（实测，不替厂商背书）")
    lines.append("厂商: " + str(report["provider"]))
    lines.append("端点: " + str(report["endpoint"]))
    lines.append("模型: " + str(report["model"]))
    lines.append("")
    lines.append(render_table(report["checks"]))
    lines.append("")
    lines.append(
        "结论："
        + str(report["conclusion"])
        + "（"
        + str(report["passed"])
        + "/"
        + str(report["total"])
        + " 项通过）"
    )
    if report["ok"]:
        lines.append("")
        lines.append("建议写入 .env（key 请自己填，本工具绝不输出 key）：")
        for key, value in report["env_suggestion"].items():
            lines.append(key + "=" + str(value))
    else:
        failed = [c for c in report["checks"] if not c["ok"]]
        lines.append("")
        lines.append("排查方向：")
        for item in failed:
            hint = FAIL_HINTS.get(str(item["id"]), "")
            if hint:
                lines.append("- " + str(item["name"]) + "：" + hint)
        lines.append("改完配置后重跑本命令复验。")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# 命令行
# --------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python3.12 -m scripts.provider_check",
        description="厂商兼容性自检：实测某个 OpenAI 兼容厂商能不能被本项目用起来（4 项检查，全过退出码 0）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  python3.12 -m scripts.provider_check\n"
            "  python3.12 -m scripts.provider_check --base-url https://api.deepseek.com --model deepseek-chat\n"
            "  python3.12 -m scripts.provider_check --json\n"
            "\n"
            "注意：key 只从环境变量读（LLM_API_KEY，兼容旧名 MINIMAX_API_KEY），"
            "命令行不提供 key，也不接受 key 参数（避免 key 落进 shell history）。"
        ),
    )
    parser.add_argument(
        "--base-url",
        default=None,
        help="要测的厂商 base url（覆盖 .env 里的 LLM_BASE_URL / MINIMAX_API_HOST）",
    )
    parser.add_argument(
        "--model",
        default=None,
        help="要测的模型名（覆盖 .env 里的 LLM_MODEL / MODEL_BRAIN）",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=0,
        help="单次请求超时秒数（0 表示用 .env 里的 LLM_TIMEOUT，默认 60）",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="输出机器可读 JSON（默认输出中文表格）",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    target = resolve_target(args)
    results = run_checks(target)
    report = build_report(target, results)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(render_text(report))
    return int(report["exit_code"])


if __name__ == "__main__":
    sys.exit(main())
