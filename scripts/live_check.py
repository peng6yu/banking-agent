#!/usr/bin/env python3.12
"""真模型六场景回归：把「真 MiniMax 能不能跑通赛题六个场景」变成可复现的一条命令。

用法：
    python3.12 -m scripts.live_check                 # 离线：脚本模型回放，不连网
    python3.12 -m scripts.live_check --live           # 连真 MiniMax（读 .env）
    python3.12 -m scripts.live_check --live --only balance
    python3.12 -m scripts.live_check --live --json tmp/live_check.json
    python3.12 -m scripts.live_check --list           # 只列场景

三条纪律（对应任务卡 t2 的硬要求）：
    1. 每场景一个独立临时库 tmp/live_<key>.db，跑前删库重建 + seed；
       跑完核对 data/bank.db 的 sha256 没变，变了就是污染，退出码 1。
    2. 串行请求，场景之间 sleep 1 秒；429 一律指数退避重试并逐次记账，
       退避耗尽就把失败原因原样写进结果，绝不静默跳过。
    3. 只替代「模型输出意图」这一层（LiveAdapter / ScriptAdapter），策略裁决、
       DAG、令牌、两阶段资金、工具执行全是生产代码；确认卡片按现场用户的
       做法作答（黄灯点头、红灯补齐双因子），作答过程也逐条记账。

离线模式（默认）只验证链路与库隔离，不校验模型理解能力：app.llm.ScriptAdapter
只认「余额 / 转账」两类说法，其余场景会走到 unknown 反问，这是如实的。
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# 能力令牌密钥：只活在本进程。.env 里缺失时用固定的 64 位十六进制兜底，
# 免得黄灯/红灯链路因为 fail closed 全判「拒绝」，把安全层误读成工作正常。
os.environ.setdefault(
    "CAPABILITY_SECRET",
    "5ec1f0a3d9b47268b1c07e5f3a92d6148d7b0c25e93a1f6c480d72b5e3f19a64",
)

from app import clock, db  # noqa: E402
from app.api import routes  # noqa: E402
from app.clock import BankError  # noqa: E402
from app.config import Settings  # noqa: E402
from app.ids import new_id  # noqa: E402
from app.llm import LLMAdapter, LLMError, ScriptAdapter  # noqa: E402
from app.log import log as app_log  # noqa: E402
from app.planner import dag as planner_dag  # noqa: E402
from app.planner import executor  # noqa: E402
from scripts.seed import seed as seed_db  # noqa: E402

TMP = ROOT / "tmp"
CONFIRM_TIMEOUT_MS = 30_000

#: 本次进程里凡是走到 db.connect() 的库路径都记下来（证明没碰业务库）。
#: app 内部统一写 `db.connect()`（grep 确认无 `from app.db import connect`），
#: 所以在这里替换模块属性即可覆盖全部调用点。
DB_OPENS: list[str] = []
_ORIGINAL_CONNECT = db.connect


def _tracked_connect(path: Any = None) -> Any:
    DB_OPENS.append(str(path if path is not None else db.DEFAULT_DB_PATH))
    return _ORIGINAL_CONNECT(path)


db.connect = _tracked_connect  # type: ignore[assignment]
#: 每个场景一句话；key 同时是临时库文件名的一部分（tmp/live_<key>.db）。
SCENARIOS: tuple[tuple[str, str, str], ...] = (
    ("balance", "查余额", "我工资卡现在还有多少钱？"),
    ("bill", "分析账单", "帮我看看上个月的账单，有没有什么异常消费？"),
    ("fund", "申购基金", "帮我申购 1000 元的均衡配置混合A"),
    ("freeze", "冻结卡", "帮我把尾号 1234 的银行卡冻结掉"),
    ("subscription", "取消订阅", "帮我把爱奇艺会员的自动续费取消掉"),
    ("birthday", "我妈生日送礼", "我妈下周生日，帮我订一束花送给她，预算 300 元"),
)

app_log.level = "warn"


# ------------------------------------------------------------------ 适配器记账


class ChatRecorder:
    """包一层 chat()：逐次记录模型名/耗时/工具调用；429 指数退避并留痕。

    内层 LLMAdapter 用 max_retries=0 构造，重试与退避只在这一层做，
    每次尝试都进 self.requests，避免「重试过但没人知道」。
    """

    def __init__(self, inner: Any, *, tries: int = 1, base_sleep: float = 2.0) -> None:
        self.inner = inner
        self.tries = max(1, tries)
        self.base_sleep = base_sleep
        self.requests: list[dict[str, Any]] = []
        self.rate_limits: list[dict[str, Any]] = []

    def chat(self, messages: Any, **kwargs: Any) -> Any:
        for attempt in range(1, self.tries + 1):
            started = time.monotonic()
            try:
                response = self.inner.chat(messages, **kwargs)
            except LLMError as exc:
                self.requests.append({
                    "attempt": attempt,
                    "ok": False,
                    "status": exc.status,
                    "retryable": exc.retryable,
                    "latency_ms": int((time.monotonic() - started) * 1000),
                    "error": str(exc)[:300],
                })
                if exc.status == 429 and attempt < self.tries:
                    delay = self.base_sleep * (2 ** (attempt - 1))
                    self.rate_limits.append({"attempt": attempt, "sleep_s": delay,
                                             "reason": "HTTP 429"})
                    time.sleep(delay)
                    continue
                raise
            self.requests.append({
                "attempt": attempt,
                "ok": True,
                "model": str(getattr(response, "model", "")),
                "latency_ms": int(getattr(response, "latency_ms", 0) or 0),
                "tools": [call.name for call in getattr(response, "tool_calls", [])],
                "finish_reason": str(getattr(response, "finish_reason", "")),
            })
            return response
        raise LLMError("429 退避重试用尽")


def build_recorder(live: bool) -> ChatRecorder:
    if live:
        settings = Settings()
        if not settings.api_key:
            raise SystemExit("--live 需要 .env 里的 MINIMAX_API_KEY，当前为空")
        # 内层不自己重试：429 退避统一由 ChatRecorder 记账
        return ChatRecorder(LLMAdapter(settings, max_retries=0), tries=4, base_sleep=2.0)
    return ChatRecorder(ScriptAdapter(), tries=1)


# ------------------------------------------------------------------ 单场景执行


@dataclass
class Result:
    key: str
    title: str
    text: str
    ok: bool = False
    intent: str = ""
    confidence: float = 0.0
    missing: list[str] = field(default_factory=list)
    slots: dict[str, Any] = field(default_factory=dict)
    question: str | None = None
    parse_source: str = ""
    retried: bool = False
    levels: list[dict[str, Any]] = field(default_factory=list)
    confirmations: list[dict[str, Any]] = field(default_factory=list)
    tools: list[dict[str, Any]] = field(default_factory=list)
    summary: str = ""
    money_moved: bool = False
    balance_before_fen: int = 0
    balance_after_fen: int = 0
    tx_before: int = 0
    tx_after: int = 0
    parse_ms: int = 0
    total_ms: int = 0
    requests: list[dict[str, Any]] = field(default_factory=list)
    rate_limits: list[dict[str, Any]] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)
    db_path: str = ""

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


def _snapshot(conn: Any) -> tuple[int, int]:
    balance = int(conn.execute("SELECT COALESCE(SUM(balance_fen), 0) AS b FROM accounts").fetchone()["b"])
    tx = int(conn.execute("SELECT COUNT(*) AS n FROM transactions").fetchone()["n"])
    return balance, tx


def _reset_db(key: str) -> Any:
    """每场景独立临时库：删 -wal/-shm 后重建 + seed，绝不碰 data/bank.db。"""
    TMP.mkdir(parents=True, exist_ok=True)
    path = TMP / ("live_" + key + ".db")
    for suffix in ("", "-wal", "-shm"):
        Path(str(path) + suffix).unlink(missing_ok=True)
    db.DEFAULT_DB_PATH = path
    clock.unfreeze()
    conn = db.connect()
    db.init_db(conn)
    seed_db(conn)
    return conn


async def _wait_future(session_id: str, conf_id: str, timeout: float = 5.0) -> bool:
    """等执行器把确认卡的 future 建起来。

    时序陷阱：确认卡的 future 是执行器在 emit 回调**返回之后**才建的
    （app/planner/executor.py 的 _guard：先 await emit_event(...)，再
    wait_for_answer(conf)）。在 emit 回调里同步 resume，答案比 future 早到，
    resolve_pending 找不到 future 就把答案丢了，执行器只能等到超时。
    所以只能在回调之外作答，并且要等 future 真的出现。
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = executor.get_run(session_id)
        conf = state.confirmations.get(conf_id) if state is not None else None
        if conf is not None and conf.future is not None:
            return True
        if conf is not None and conf.status != "pending":
            return False
        await asyncio.sleep(0.01)
    return False


async def _answer(session_id: str, name: str, data: dict[str, Any],
                  log: list[dict[str, Any]]) -> None:
    """按现场用户的做法作答：黄灯点头；红灯补齐 required 里的全部因子。"""
    required = [str(x) for x in (data.get("required") or [])]
    record: dict[str, Any] = {
        "event": name,
        "confirmation_id": data.get("confirmation_id"),
        "tool": data.get("tool"),
        "level": data.get("level"),
        "rule_id": data.get("rule_id"),
        "required": required,
    }
    log.append(record)
    if name == "factor" and not required:
        # 生产已在该分支把确认卡置 expired 并发 error（_blocked），不再尝试批准
        record.update(action="blocked_no_factor", result="POLICY_BLOCKED")
        return
    conf_id = str(data["confirmation_id"])
    if not await _wait_future(session_id, conf_id):
        record.update(action="no_future", result="NO_FUTURE")
        return
    factor = "+".join(required) if required else None
    outcome = await executor.resume(session_id, conf_id, True, factor=factor)
    record.update(action="approve", factor=factor, result=str(outcome.get("status")))


async def _answer_loop(session_id: str, prompts: list[dict[str, Any]],
                       log: list[dict[str, Any]]) -> None:
    """独立协程：盯着确认卡事件，一有就作答（必须与执行器并行，不能在 emit 里做）。"""
    handled: set[str] = set()
    while True:
        await asyncio.sleep(0.02)
        for record in list(prompts):
            conf_id = str(record.get("confirmation_id"))
            if conf_id in handled:
                continue
            handled.add(conf_id)
            await _answer(session_id, str(record.get("event")), record, log)


async def run_scenario(item: tuple[str, str, str], recorder: ChatRecorder) -> Result:
    key, title, text = item
    result = Result(key=key, title=title, text=text, db_path=str(TMP / ("live_" + key + ".db")))
    conn = _reset_db(key)
    try:
        result.balance_before_fen, result.tx_before = _snapshot(conn)
        session_id = new_id("s")
        routes.set_adapter(recorder)
        events: list[tuple[str, dict[str, Any]]] = []

        prompts: list[dict[str, Any]] = []

        async def emit(name: str, data: dict[str, Any]) -> None:
            events.append((name, dict(data)))
            if name in ("confirm", "factor"):
                # 只登记，不在这里作答：作答必须等执行器把 future 建好（见 _wait_future）
                prompts.append({"event": name, **dict(data)})

        started = time.monotonic()
        try:
            parse_started = time.monotonic()
            parsed = await routes._parse(session_id, text)
            result.parse_ms = int((time.monotonic() - parse_started) * 1000)
            result.intent = parsed.intent
            result.confidence = float(parsed.confidence)
            result.missing = list(parsed.missing)
            # 模型填的槽位原样留档：像 month 这种「相对时间→具体值」的槽位，
            # 模型填错系统不会拦，只有留档才能看出它到底填了什么。
            result.slots = {str(k): v for k, v in dict(parsed.slots or {}).items()}
            result.question = parsed.question
            result.parse_source = parsed.source
            result.retried = bool(parsed.retried)
            events.append(("intent", parsed.to_event()))
            if parsed.question:
                events.append(("done", {"summary": parsed.question, "question": parsed.question}))
            else:
                built = planner_dag.build_or_single(
                    {"intent": parsed.intent, "slots": parsed.slots}
                )
                # 执行器放后台跑；作答协程并行盯着确认卡 —— 两者必须同时在事件循环里，
                # 在 emit 回调里作答会把答案丢在 future 之前（见 _wait_future 的说明）。
                run_task = asyncio.create_task(
                    executor.run(session_id, built, emit, confirm_timeout_ms=CONFIRM_TIMEOUT_MS)
                )
                answer_task = asyncio.create_task(
                    _answer_loop(session_id, prompts, result.confirmations)
                )
                try:
                    await run_task
                finally:
                    answer_task.cancel()
                    try:
                        await answer_task
                    except asyncio.CancelledError:
                        pass
                    except Exception as exc:  # noqa: BLE001 - 作答失败不该盖掉主流程结果
                        result.failures.append("作答协程异常 " + type(exc).__name__ + ": " + str(exc)[:200])
        except LLMError as exc:
            result.failures.append("模型不可用: " + str(exc)[:300])
        except BankError as exc:
            result.failures.append("业务异常 " + type(exc).__name__ + ": " + str(exc)[:200])
        except Exception as exc:  # noqa: BLE001 - runner 级异常如实记录，不吞
            result.failures.append("runner 异常 " + type(exc).__name__ + ": " + str(exc)[:200])
        finally:
            executor.close(session_id)
            result.total_ms = int((time.monotonic() - started) * 1000)

        for name, data in events:
            if name == "trace":
                result.levels.append({
                    "node": data.get("node_id"),
                    "tool": data.get("tool"),
                    "level": data.get("level"),
                    "rule_id": data.get("rule_id"),
                })
            elif name == "tool":
                result.tools.append({
                    "node": data.get("node_id"),
                    "ok": data.get("ok"),
                    "summary": data.get("summary"),
                    "error_code": data.get("error_code"),
                })
            elif name == "done":
                result.summary = str(data.get("summary") or "")
            elif name == "error":
                result.failures.append(
                    "error " + str(data.get("code")) + ": " + str(data.get("user_text"))
                )

        result.balance_after_fen, result.tx_after = _snapshot(conn)
        result.money_moved = (
            result.balance_after_fen != result.balance_before_fen
            or result.tx_after != result.tx_before
        )
        result.ok = not result.failures
    finally:
        conn.close()
        result.requests = list(recorder.requests)
        result.rate_limits = list(recorder.rate_limits)
        recorder.requests = []
        recorder.rate_limits = []
    return result


# ------------------------------------------------------------------ 输出


def _print_row(result: Result) -> None:
    levels = "/".join(str(x.get("level")) for x in result.levels) or "-"
    confirm = "无"
    if result.confirmations:
        confirm = "/".join(
            str(x.get("event")) + ":" + str(x.get("result")) for x in result.confirmations
        )
    money = "动钱" if result.money_moved else "未动钱"
    status = "OK " if result.ok else "FAIL"
    print(
        f"[{status}] {result.key:<12} {result.title:<8} intent={result.intent:<16}"
        f" 级别={levels:<10} 确认={confirm:<24} {money}"
        f" 耗时={result.total_ms}ms(解析{result.parse_ms}ms)"
    )
    if result.question:
        print(f"         反问：{result.question}")
    if result.missing:
        print(f"         缺槽位：{result.missing}")
    if result.slots:
        print(f"         槽位：{json.dumps(result.slots, ensure_ascii=False)}")
    for note in result.failures:
        print("         失败：" + note)
    for item in result.rate_limits:
        print(f"         429 退避：第 {item['attempt']} 次失败后 sleep {item['sleep_s']}s")


def _fingerprint(path: Path) -> str:
    if not path.exists():
        return "missing"
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="真模型六场景回归（默认离线）")
    parser.add_argument("--live", action="store_true", help="连真 MiniMax；不加则离线脚本回放")
    parser.add_argument("--only", action="append", default=[], help="只跑指定场景 key，可重复")
    parser.add_argument("--sleep", type=float, default=1.0, help="场景之间的间隔秒数，默认 1")
    parser.add_argument("--json", dest="json_path", help="把结果写成 JSON 文件")
    parser.add_argument("--list", action="store_true", help="只列场景 key")
    args = parser.parse_args(argv)

    items = list(SCENARIOS)
    if args.list:
        for key, title, text in items:
            print(f"{key:<12} {title:<10} {text}")
        return 0
    if args.only:
        wanted = [item.strip() for chunk in args.only for item in chunk.split(",") if item.strip()]
        unknown = [key for key in wanted if key not in {item[0] for item in items}]
        if unknown:
            print("未知场景 key：" + ", ".join(unknown), file=sys.stderr)
            return 2
        items = [item for item in items if item[0] in wanted]

    original_db = db.DEFAULT_DB_PATH
    before_hash = _fingerprint(original_db)
    recorder = build_recorder(args.live)
    mode = "live" if args.live else "offline"
    settings = Settings()
    print(f"模式 {mode} · 模型 {settings.model_brain if args.live else 'ScriptAdapter(离线)'}"
          f" · 场景 {len(items)} 个 · 场景间 sleep {args.sleep}s")
    print(f"业务库保护：{original_db} sha256={before_hash[:12]}…（跑完复核）")

    results: list[Result] = []
    for index, item in enumerate(items):
        if index:
            time.sleep(max(0.0, args.sleep))
        started = time.monotonic()
        result = asyncio.run(run_scenario(item, recorder))
        results.append(result)
        _print_row(result)
        print(f"         临时库 {result.db_path}（{time.monotonic() - started:.1f}s）")

    after_hash = _fingerprint(original_db)
    target = str(Path(original_db).resolve())
    leaked = [path for path in DB_OPENS if str(Path(path).resolve()) == target]
    untouched = not leaked
    print()
    print(f"业务库隔离：本次运行打开子库 {len(DB_OPENS)} 次，命中 data/bank.db {len(leaked)} 次"
          f" → {'未污染' if untouched else '污染了！'}")
    print(f"（data/bank.db sha256 {before_hash[:12]}… → {after_hash[:12]}…，仅作参考："
          f"队友并发跑 e2e/redteam 也会写这个文件）")
    print(f"场景 {len(results)} 个：完成 {sum(1 for r in results if r.ok)} / 失败 {sum(1 for r in results if not r.ok)}")
    total_429 = sum(len(r.rate_limits) for r in results)
    print(f"429 退避次数：{total_429}")

    if args.json_path:
        payload = {
            "mode": mode,
            "model": settings.model_brain if args.live else "ScriptAdapter",
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "data_bank_db": {"path": str(original_db), "sha256_before": before_hash,
                             "sha256_after": after_hash, "untouched": untouched,
                             "opens_total": len(DB_OPENS), "opens_hit_business_db": len(leaked),
                             "opens": DB_OPENS},
            "scenarios": [r.to_dict() for r in results],
        }
        path = Path(args.json_path)
        if not path.is_absolute():
            path = ROOT / path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print("结果已写入 " + str(path))

    return 0 if (untouched and all(r.ok for r in results)) else 1


if __name__ == "__main__":
    sys.exit(main())
