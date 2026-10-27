"""造一份满数据演示库，供演示 / 比赛 / 手点验收使用。

为什么要有这个脚本：仓库自带的 data/bank.db 只有流水和卡，计划、目标、预约、接管单、
助手提醒都是空的（空态本身是对的，但演示时六个页签不能全是「还没有」）。这个脚本在
**另一个文件**里复制一套种子 + 补上演示数据，绝不碰 data/bank.db。

跑法：
    .venv/bin/python scripts/make_demo_db.py            # 写 data/demo_full.db
    .venv/bin/python scripts/make_demo_db.py --force    # 覆盖已有的
    BANK_DB_PATH=data/demo_full.db make run             # 用这份库起服务

补进去的东西（都用业务自己的落库函数，不裸写状态码）：
- 1 个储蓄目标（换一台相机，6,000 元，已存 1,500，每月 600，带进度/预算两条监测规则）
- 1 个跨场景计划（3 步：查余额 / 订花 / 生日提醒）
- 2 条提醒（一条今天到期、一条每月循环）
- 1 笔预约转账（3 天后，200 元，给张三）
- 1 张待人工接管的单（收款人歧义，还没人领）
- 1 条预算告警（本月花超了）
- 助手提醒若干条（由真实探测器从这份流水里算出来，facts 里的数字都能对账）
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

DAY_MS = 24 * 3600 * 1000
HOUR_MS = 3600 * 1000


def _drop(path: Path) -> None:
    for suffix in ("", "-wal", "-shm"):
        target = Path(str(path) + suffix)
        if target.exists():
            target.unlink()


def build(target: Path) -> dict[str, int]:
    """在 target 上建库、播种、补演示数据；返回各表行数。"""
    import app.db as db
    import app.handoff_store as handoff_store
    import app.messages as messages
    import app.plans as plans
    import app.proactive as proactive
    import app.planner.assist as assist
    from app.clock import now_ms
    from scripts.seed import seed as seed_db

    conn = db.connect(str(target))
    try:
        db.init_db(conn)
        seed_db(conn)
        user_id = str(conn.execute("SELECT id FROM users LIMIT 1").fetchone()["id"])
        account = conn.execute(
            "SELECT id FROM accounts WHERE user_id = ? AND status = 'active'"
            " ORDER BY balance_fen DESC LIMIT 1",
            (user_id,),
        ).fetchone()
        account_id = str(account["id"]) if account is not None else ""
        payee = conn.execute(
            "SELECT id FROM payees WHERE user_id = ? ORDER BY name LIMIT 1", (user_id,)
        ).fetchone()
        now = now_ms()

        goal = proactive.create_goal(
            conn,
            user_id=user_id,
            name="换一台相机",
            target_fen=600_000,
            target_date="2027-03-31",
            saved_fen=150_000,
            budget_fen=60_000,
            progress_tolerance=10,
            period="monthly",
        )
        # create_goal 返回 {"goal": {...}, "rule_ids": [...]}，目标 id 在 goal.goal_id 里
        goal_body = goal.get("goal") if isinstance(goal, dict) else {}
        goal_id = str((goal_body or {}).get("goal_id") or "")

        plans.create(
            conn,
            user_id=user_id,
            title="十月生日计划",
            steps=[
                {
                    "kind": "check",
                    "title": "先看存款够不够",
                    "payload": {"check": "balance_at_least", "amount_fen": 500_000},
                    "due_in_ms": 2 * DAY_MS,
                },
                {
                    "kind": "action",
                    "title": "订一束鲜花",
                    "payload": {"tool": "pay_merchant", "params": {"merchant": "鲜花店", "amount_fen": 40_000}},
                    "due_in_ms": 3 * DAY_MS,
                },
                {
                    "kind": "reminder",
                    "title": "生日当天取蛋糕",
                    "payload": {"text": "生日当天取蛋糕 600 元"},
                    "due_in_ms": 5 * DAY_MS,
                },
            ],
            meta={"note": "演示数据：提醒只提醒，付款仍要你在助手里确认"},
        )

        # 提醒必须挂在资金时间点上（app/scope.py:REMINDER_ANCHORS），所以文案里要有
        # 「还信用卡 / 扣费 / 预算 / 存够 …」这类锚点；「交房租」不在锚点里，会被拒。
        assist.create_reminder(
            conn,
            text="提醒我这个月还信用卡 1500 元",
            user_id=user_id,
            due_ms=now + 2 * HOUR_MS,
            repeat="none",
        )
        assist.create_reminder(
            conn,
            text="每月 8 号还信用卡 1500 元",
            user_id=user_id,
            due_ms=now + 20 * DAY_MS,
            repeat="monthly",
            hour=10,
            day=8,
        )

        if payee is not None:
            with db.tx(conn):
                conn.execute(
                    "INSERT INTO scheduled_transfers"
                    " (id, user_id, account_id, payee_id, amount_fen, note, due_ms, recurrence,"
                    "  status, created_ms, idempotency_key, factor_verified_ms)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, 'once', 'scheduled', ?, ?, 0)",
                    (
                        "sch_demo01",
                        user_id,
                        account_id,
                        str(payee["id"]),
                        20_000,
                        "房租",
                        now + 3 * DAY_MS,
                        now,
                        "idem_demo01",
                    ),
                )

        # 接管单必须挂在一轮真实存在的执行上（handoff_store._run 会核对 run_id），
        # 所以这里先按助手的落库形状写一轮「收款人歧义」的执行记录：一节失败、一节未跑。
        with db.tx(conn):
            conn.execute(
                "INSERT OR IGNORE INTO sessions (id, created_ms, last_ms, title, status)"
                " VALUES ('s_demo', ?, ?, '演示：给张三转账', 'active')",
                (now, now),
            )
            conn.execute(
                "INSERT INTO dag_runs (id, session_id, dag_json, status, meta_json, created_ms,"
                " updated_ms) VALUES ('r_demo_handoff', 's_demo', ?, 'running', ?, ?, ?)",
                (
                    json.dumps(
                        {
                            "run_id": "r_demo_handoff",
                            "nodes": [
                                {"id": "n1", "tool": "resolve_payee", "params": {"payee_name": "张三"}},
                                {"id": "n2", "tool": "transfer", "params": {"amount_fen": 50_000}},
                            ],
                        },
                        ensure_ascii=False,
                    ),
                    json.dumps({"account_id": account_id, "user_text": "帮我转 500 元给张三"}, ensure_ascii=False),
                    now,
                    now,
                ),
            )
            conn.execute(
                "INSERT INTO dag_nodes (id, run_id, node_id, tool, params_json, state,"
                " decision_json, result_json, ts_ms)"
                " VALUES ('n_demo01', 'r_demo_handoff', 'n1', 'resolve_payee', ?, 'failed', ?, ?, ?)",
                (
                    json.dumps({"payee_name": "张三"}, ensure_ascii=False),
                    json.dumps(
                        {"level": "green", "rule_id": "R01_readonly", "required_factors": [],
                         "reason_code": "READ_ONLY", "reason_text": "只读工具，不会动钱"},
                        ensure_ascii=False,
                    ),
                    json.dumps(
                        {"ok": False, "error_code": "PAYEE_AMBIGUOUS",
                         "summary": "有 2 位都叫「张三」：张三（尾号 4417）、张三（尾号 6620）。"},
                        ensure_ascii=False,
                    ),
                    now,
                ),
            )
            conn.execute(
                "INSERT INTO dag_nodes (id, run_id, node_id, tool, params_json, state, ts_ms)"
                " VALUES ('n_demo02', 'r_demo_handoff', 'n2', 'transfer', ?, 'skipped', ?)",
                (json.dumps({"amount_fen": 50_000, "payee_name": "张三"}, ensure_ascii=False), now),
            )

        handoff_store.create(
            conn,
            run_id="r_demo_handoff",
            session_id="s_demo",
            reason="PAYEE_AMBIGUOUS",
            request_text="帮我转 500 元给张三",
            account_id=account_id,
            user_id=user_id,
            note="有 2 位都叫「张三」，要你本人选一位；操作员不能替你选。",
        )

        if goal_id:
            with db.tx(conn):
                proactive.create_alert(
                    conn,
                    goal_id=goal_id,
                    reason=proactive.REASON_BUDGET_OVERSPENT,
                    evidence={
                        "spent_fen": 72_000,
                        "limit_fen": 60_000,
                        "period": "monthly",
                        "ledger": "transactions",
                    },
                    suggestion="这个月已经花了 720.00 元，超过你定的 600.00 元；后面几笔先压一压。",
                    user_id=user_id,
                )

        messages.generate(conn, user_id=user_id)
        messages.generate(conn, user_id=user_id)  # 第二条受频控，不会重复；正好验一次去重

        return {
            "transactions": _count(conn, "transactions"),
            "savings_goals": _count(conn, "savings_goals"),
            "plans": _count(conn, "plans"),
            # 提醒不是独立表：一条提醒就是 plans 里只有一步 reminder 的计划（和页面同一口径）
            "reminders": int(
                conn.execute(
                    "SELECT COUNT(*) AS n FROM plan_steps WHERE kind = 'reminder'"
                ).fetchone()["n"]
            ),
            "scheduled_transfers": _count(conn, "scheduled_transfers"),
            "handoffs": _count(conn, "handoffs"),
            "proactive_alerts": _count(conn, "proactive_alerts"),
            "messages": _count(conn, "messages"),
        }
    finally:
        conn.close()


def _count(conn, table: str) -> int:
    return int(conn.execute("SELECT COUNT(*) AS n FROM " + table).fetchone()["n"])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="造一份满数据演示库（默认 data/demo_full.db）")
    parser.add_argument("--target", default=str(ROOT / "data" / "demo_full.db"))
    parser.add_argument("--force", action="store_true", help="覆盖已存在的演示库")
    args = parser.parse_args(argv)
    target = Path(args.target)
    if target.exists() and not args.force:
        print(f"演示库已存在：{target}（要重建加 --force）")
        return 1
    _drop(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    # 演示库路径通过环境变量交给 app.db；脚本自己也只动这个路径。
    os.environ["BANK_DB_PATH"] = str(target)
    counts = build(target)
    print(f"演示库已生成：{target}")
    print("  " + " · ".join(f"{k} {v}" for k, v in counts.items()))
    print(f"用起来：BANK_DB_PATH={target} .venv/bin/python -m uvicorn app.main:app --port 8000")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
