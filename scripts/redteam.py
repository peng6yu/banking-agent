#!/usr/bin/env python3.12
"""m7 红队 runner：把 tests/redteam/cases.yaml 的 43 条攻击用例跑通整链路并自动判定。

用法：
    python3.12 -m scripts.redteam            # 跑全部 43 条
    python3.12 scripts/redteam.py --case D01 # 只跑某一条（调试用）
    python3.12 -m scripts.redteam --list     # 只列用例

行为严格按 docs/10 §10.5：
    1. 每条用例前重置 data/bank.db 并加载种子数据（互不污染）；
    2. 落 setup 的前置数据与故障钩子 → 走完整链路 intent → dag → executor → tools；
       默认不响应任何 confirm（不点确认、不给人脸与短信因子）；
    3. 对 expect 四个字段逐条断言：level（下限 green<yellow<red）、denied、
       must_not、must_contain_audit（docs/03 §3.2 闭集）；
    4. 打印七列表格：id / 类别 / 期望级别 / 实际级别 / 是否拦住 / 审计是否齐 / 结果；
    5. 任一条 FAIL 退出码 1，全 PASS 退出码 0。

模块分工（每个文件都控制在 400 行以内，docs/08 硬上限）：
    scripts/redteam.py        本文件：离线脚本模型 + 用例循环 + 故障注入 + 表格与退出码
    scripts/redteam_setup.py  setup 前置数据落库与账务快照
    scripts/redteam_judge.py  must_not 探针、直探针与四字段判定

离线保证：模型调用全部走本文件的 ScriptedAdapter，不 import 任何真实 LLM 客户端、
不读 API key，答辩现场断网也能跑完。脚本模型只还原「模型输出意图与槽位」这一步，
策略裁决、DAG、令牌、两阶段资金、工具执行全是生产代码。

诚实原则：用例集 tests/redteam/cases.yaml 是冻结交付物，本 runner 只读不写；
跑不过的用例如实写进 tmp/redteam_failures.txt，绝不为全绿删改或放宽任何断言。

已知的 runner 侧约定（不削弱断言，只影响前置数据的落法）：
    * setup.payees 按「确保存在」落库：同名同尾号已有就不重复插，
      否则 A04—A06 的三个同名张三会让后续判定失真；
    * E04 的 hold 由 runner 用生产 funds.initiate 建立（等价于用户点确认后执行器
      要做的那一步），确认环节本身仍不由 runner 代答。
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import os
import sqlite3
import sys
import time
import unicodedata
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import yaml  # noqa: E402

from app import clock, db, funds  # noqa: E402
from app.api import routes  # noqa: E402
from app.clock import BankError  # noqa: E402
from app.ids import new_id  # noqa: E402
from app.log import log as _app_log  # noqa: E402
from app.llm import LLMError  # noqa: E402
from app.main import INTERNAL_TEXT, MODEL_CODE, MODEL_TEXT  # noqa: E402
from app.planner import dag as planner_dag  # noqa: E402
from app.planner import executor  # noqa: E402
from app.security import token as token_mod  # noqa: E402
from scripts.redteam_judge import Evidence, Verdict, judge  # noqa: E402
from scripts.redteam_model import ScriptedAdapter, obfuscate, understand  # noqa: E402
from scripts.redteam_setup import (  # noqa: E402
    TEST_SECRET,
    Applied,
    Snapshot,
    apply_setup,
    run_forged_payee_probe,
    payee_names,
    run_token_probes,
    snapshot_of,
    transfer_action,
)
from scripts.seed import seed  # noqa: E402

CASES_PATH = ROOT / "tests" / "redteam" / "cases.yaml"
FAILURE_PATH = ROOT / "tmp" / "redteam_failures.txt"

#: 本 runner 自带的 64 位 hex 测试密钥：只在进程内，不写盘、不进 .env。
#: app/security/token.py 的 load_secret() 只认环境变量里的 64 位十六进制，缺失就
#: fail closed（MissingCapability），黄灯/红灯路径会一律拒绝——那样测出来的全是
#: 「拒绝」，会把安全层误判成工作正常。这里无条件覆盖成测试密钥以保证确定性。
os.environ["CAPABILITY_SECRET"] = TEST_SECRET

#: 没有人工干预：把确认等待压到 0.08 秒，等价于「没人点确认」。
#: 注意生产把毫秒值直接当秒传给了 asyncio.wait_for（app/planner/executor.py:216，
#: CONFIRM_TIMEOUT_MS=120_000 会变成 33 小时），runner 这里按它的实际语义传秒值。
CONFIRM_NO_HUMAN_S = 0.08

NL = chr(10)
# 生产 JSON 日志（app/log.py）默认 info 且写 stderr；红队表格才是主输出，压到 warn。
_app_log.level = "warn"

# ------------------------------------------------------------------ 断线重置


def _reset_db() -> sqlite3.Connection:
    """删库重建 + 播种：43 条用例互不污染，denials 里的断路器状态也随之复位。"""
    for suffix in ("", "-wal", "-shm"):
        Path(str(db.DEFAULT_DB_PATH) + suffix).unlink(missing_ok=True)
    clock.unfreeze()
    conn = db.connect()
    db.init_db(conn)
    seed(conn)
    return conn


def _max_seq(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COALESCE(MAX(seq), 0) AS m FROM audit_log").fetchone()
    return int(row["m"]) if row else 0


def _audit_rows(conn: sqlite3.Connection, start_seq: int) -> list[dict[str, Any]]:
    """取本条用例独占的审计窗口；锁库读不出来就如实返回空，不猜。"""
    try:
        rows = conn.execute(
            "SELECT seq, event_type, payload_json, session_id FROM audit_log"
            " WHERE seq > ? ORDER BY seq",
            (start_seq,),
        ).fetchall()
    except sqlite3.Error:
        return []
    return [dict(row) for row in rows]


class _DbLock:
    """E05：另一个连接 BEGIN EXCLUSIVE，把写锁攥在手里逼出真实的锁竞争。"""

    def __init__(self) -> None:
        self.conn: sqlite3.Connection | None = None

    def __enter__(self) -> "_DbLock":
        self.conn = db.connect()
        self.conn.execute("BEGIN EXCLUSIVE")
        return self

    def __exit__(self, *_exc: object) -> None:
        if self.conn is None:
            return
        try:
            self.conn.execute("ROLLBACK")
        finally:
            self.conn.close()


# ------------------------------------------------------------------ 故障钩子


def _pending_confirm_id(events: list[tuple[str, dict[str, Any]]]) -> str | None:
    """从 SSE 事件里捞出那张待确认卡片的 id（confirm / factor 两种事件都带）。"""
    for name, data in events:
        if name in ("confirm", "factor", "confirm.requested") and data.get("confirmation_id"):
            return str(data["confirmation_id"])
    return None


async def _hook_concurrent_confirm(session_id: str, events: list[tuple[str, dict[str, Any]]],
                                   applied: Applied) -> None:
    """E03：同一张确认卡被并发点 n 次，按设计只能生效一次。"""
    times = int(applied.fault_params.get("repeat_confirm_n") or 3)
    conf_id = _pending_confirm_id(events)
    if not conf_id:
        events.append(("runner_note", {"hook": "concurrent_confirm", "detail": "本轮没有待确认卡片"}))
        return
    results = await asyncio.gather(
        *(executor.resume(session_id, conf_id, True) for _ in range(times)),
        return_exceptions=True,
    )
    approved = [item for item in results if isinstance(item, dict) and item.get("status") == "approved"]
    events.append(("runner_note", {
        "hook": "concurrent_confirm",
        "n": times,
        "approved": len(approved),
        "errors": [type(item).__name__ for item in results if isinstance(item, BaseException)],
    }))


def _hook_hold_expired(conn: sqlite3.Connection, session_id: str, applied: Applied,
                       understanding: Any, events: list[tuple[str, dict[str, Any]]]) -> None:
    """E04：确认来晚了 5 分钟。用生产 funds.initiate / commit 拿到真的 HoldExpired。"""
    action = transfer_action(conn, understanding.payee_name, int(understanding.amount_fen or 50_000))
    token = token_mod.issue(action, level="yellow", secret=TEST_SECRET)
    outcome = funds.initiate(conn, action, session_id=session_id, node_id="n_e04",
                             token=token, secret=TEST_SECRET)
    detail: dict[str, Any] = {"hold": outcome.hold_id, "expires_ms": outcome.expires_ms}
    clock.freeze(clock.now_ms() + int(applied.clock_advance_s or 301) * 1000)
    try:
        db.sweep_expired_holds(conn)
        funds.commit(conn, outcome.hold_id)
        detail["commit"] = "COMMITTED"
    except BankError as exc:
        detail["commit"] = type(exc).__name__
    finally:
        clock.unfreeze()
    events.append(("runner_note", {"hook": "hold_expired", **detail}))


# ------------------------------------------------------------------ 链路驱动


async def _drive(case: dict[str, Any]) -> Evidence:
    """复刻 app/api/routes.py 的 chat 链路：解析 → 建图 → 执行 → 工具，一路收证。"""
    setup = dict(case.get("setup") or {})
    conn = _reset_db()
    session_id = new_id("s")
    events: list[tuple[str, dict[str, Any]]] = []
    boundary_error: str | None = None

    before: Snapshot = snapshot_of(conn)
    start_seq = _max_seq(conn)
    applied: Applied = apply_setup(conn, setup)

    text = str(case.get("input") or "")
    if setup.get("text_obfuscation") == "mixed_language":
        text = obfuscate(text)
    understanding = understand(text, payee_names(conn))

    model_fault = applied.fault if applied.fault in ("llm_timeout", "llm_refusal") else None
    routes.set_adapter(ScriptedAdapter(understanding, fault=model_fault))

    async def emit(name: str, data: dict[str, Any]) -> None:
        events.append((name, dict(data)))

    lock = _DbLock() if applied.fault == "db_lock" else None
    try:
        with lock if lock is not None else contextlib.nullcontext():
            try:
                parsed = await routes._parse(session_id, text)
            except LLMError:
                # app/main.py 的 @app.exception_handler(LLMError)：503 + 一句人话
                events.append(("http_error", {"status": 503, "code": MODEL_CODE, "user_text": MODEL_TEXT}))
            except BankError as exc:
                events.append(("http_error", {"status": exc.http_status, **exc.to_dict()}))
            else:
                events.append(("start", {"session_id": session_id}))
                events.append(("intent", parsed.to_event()))
                if parsed.question:
                    events.append(("done", {"summary": parsed.question, "question": parsed.question}))
                else:
                    built = planner_dag.build_or_single({"intent": parsed.intent, "slots": parsed.slots})
                    try:
                        await executor.run(session_id, built, emit, secret=TEST_SECRET,
                                           confirm_timeout_ms=CONFIRM_NO_HUMAN_S)
                    except Exception:  # noqa: BLE001 - 与 routes._start_run.driver 同样兜底
                        events.append(("error", {"code": "INTERNAL", "user_text": routes.INTERNAL_USER_TEXT}))
            if applied.fault == "concurrent_confirm":
                await _hook_concurrent_confirm(session_id, events, applied)
            elif applied.fault == "hold_expired":
                _hook_hold_expired(conn, session_id, applied, understanding, events)
    except Exception as exc:  # noqa: BLE001 - 只记生产兜不住的 runner 级异常
        boundary_error = type(exc).__name__ + ": " + str(exc)[:200]
    finally:
        executor.close(session_id)

    evidence = Evidence(
        case=case,
        input_text=text,
        setup=setup,
        expect=dict(case.get("expect") or {}),
        session_id=session_id,
        events=events,
        audit_rows=_audit_rows(conn, start_seq),
        before=before,
        after=snapshot_of(conn),
        applied=applied,
        understanding=understanding,
        boundary_error=boundary_error,
    )
    if applied.token_plan:
        evidence.probes.update(run_token_probes(conn, applied))
    if applied.payee_override:
        evidence.probes.update(run_forged_payee_probe(conn, applied))
    # 直探针自己也会写审计（token.rejected）甚至会真的动账（伪造 payee 探针），
    # 所以取证必须在探针之后再取一遍，账务快照也重新拍一次，两边才对得上。
    evidence.audit_rows = _audit_rows(conn, start_seq)
    evidence.after = snapshot_of(conn)
    conn.close()
    return evidence


# ------------------------------------------------------------------ 表格与报告


def load_cases() -> list[dict[str, Any]]:
    cases = yaml.safe_load(CASES_PATH.read_text(encoding="utf-8"))
    if not isinstance(cases, list) or not cases:
        raise SystemExit("cases.yaml 顶层必须是非空列表：" + str(CASES_PATH))
    return cases


def _width(text: object) -> int:
    return sum(2 if unicodedata.east_asian_width(char) in "WF" else 1 for char in str(text))


def _pad(text: object, width: int) -> str:
    return str(text) + " " * max(1, width - _width(text))


def _table(results: list[tuple[dict[str, Any], Verdict]]) -> str:
    header = ["id", "类别", "期望级别", "实际级别", "拦住", "审计齐", "结果"]
    body: list[list[str]] = []
    for case, verdict in results:
        expect = case.get("expect") or {}
        denied = "n/a" if not expect.get("denied") else ("是" if verdict.denied_ok else "否")
        need_audit = expect.get("must_contain_audit") or []
        audit = "n/a" if not need_audit else ("是" if verdict.audit_ok else "否")
        tail = verdict.reasons[0] if verdict.reasons else ""
        body.append([
            str(case.get("id")),
            str(case.get("category")),
            str(expect.get("level")),
            verdict.level_actual,
            denied,
            audit,
            "PASS" if verdict.ok else "FAIL " + tail,
        ])
    widths = []
    for index in range(len(header)):
        widths.append(max([_width(header[index])] + [_width(row[index]) for row in body]))
    lines = ["  ".join(_pad(header[i], widths[i]) for i in range(len(header))).rstrip()]
    for row in body:
        lines.append("  ".join(_pad(row[i], widths[i]) for i in range(len(header))).rstrip())
    return NL.join(lines)


def _write_report(results: list[tuple[dict[str, Any], Verdict]]) -> tuple[int, int]:
    """失败明细如实落盘；跑不过就是跑不过，不在这里改用例。"""
    failed = [(case, verdict) for case, verdict in results if not verdict.ok]
    passed = len(results) - len(failed)
    chunks = [
        "m7 红队用例集执行报告 " + time.strftime("%Y-%m-%d %H:%M:%S"),
        "总数 " + str(len(results)) + "，PASS " + str(passed) + "，FAIL " + str(len(failed)),
        "",
    ]
    for case, verdict in failed:
        expect = case.get("expect") or {}
        chunks.append("[FAIL] " + str(case.get("id")) + "  " + str(case.get("category"))
                      + "  severity=" + str(case.get("severity")))
        chunks.append("  输入: " + str(case.get("input")))
        chunks.append("  期望: level=" + str(expect.get("level")) + " denied=" + str(expect.get("denied")))
        chunks.append("        must_not=" + str(expect.get("must_not")))
        chunks.append("        must_contain_audit=" + str(expect.get("must_contain_audit")))
        chunks.append("  实际: level=" + verdict.level_actual)
        for reason in verdict.reasons:
            chunks.append("  失败: " + reason)
        chunks.append("")
    FAILURE_PATH.parent.mkdir(parents=True, exist_ok=True)
    FAILURE_PATH.write_text(NL.join(chunks) + NL, encoding="utf-8")
    return passed, len(failed)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="m7 红队 runner：43 条攻击用例自动判定")
    parser.add_argument("--case", help="只跑一条用例（调试用）")
    parser.add_argument("--list", action="store_true", help="只列用例 id")
    args = parser.parse_args(argv)

    cases = load_cases()
    if args.list:
        for case in cases:
            print(case.get("id"), case.get("category"), case.get("severity"))
        return 0
    if args.case:
        cases = [case for case in cases if case.get("id") == args.case]
        if not cases:
            print("没有这条用例：" + str(args.case), file=sys.stderr)
            return 2

    results: list[tuple[dict[str, Any], Verdict]] = []
    for index, case in enumerate(cases, start=1):
        started = time.monotonic()
        verdict = judge(asyncio.run(_drive(case)))
        results.append((case, verdict))
        print("  跑第 " + str(index) + "/" + str(len(cases)) + " 条 " + str(case.get("id"))
              + " " + ("PASS" if verdict.ok else "FAIL")
              + " " + ("%.1fs" % (time.monotonic() - started)), file=sys.stderr, flush=True)

    table = _table(results)
    passed, failed = _write_report(results)
    print(table)
    print("")
    print("用例 " + str(len(results)) + " 条：PASS " + str(passed) + " / FAIL " + str(failed))
    if failed:
        print("失败明细已写入 " + str(FAILURE_PATH))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
