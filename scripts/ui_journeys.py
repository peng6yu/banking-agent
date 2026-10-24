#!/usr/bin/env python3.12
"""五条高风险浏览器旅程：用真浏览器跑，同时核对 DOM 与服务端状态。

为什么单独一个脚本：截图脚本只证明「页面长什么样」，browser_smoke 只跑两条主链路。
这份旅程脚本要回答的是「用户真的点下去之后，服务端状态是不是也跟着对了」——
每条旅程都同时断言页面文案与后端返回（余额、节点状态、审计事件），
任何一条只满足一半就算失败，不允许拿截图当成功证据。

沙箱与演示库隔离沿用 browser_smoke.py 的那一套：app/ web/ scripts 复制到 tmp/，
种子播在沙箱库里（默认 12,860.50 元）；脚本结尾用 sha256 反证 data/bank.db 没被碰。

用法：
    /home/peng/.local/bin/python3.12 scripts/ui_journeys.py
    /home/peng/.local/bin/python3.12 scripts/ui_journeys.py --only j1,j2
    /home/peng/.local/bin/python3.12 scripts/ui_journeys.py --headed
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import browser_smoke as bs  # noqa: E402  沙箱/播种/拉起后端复用同一份实现

SANDBOX = ROOT / "tmp" / "ui_journey_sandbox"
SHOT_DIR = ROOT / "docs" / "交付" / "截图" / "信息架构"
REPORT = ROOT / "docs" / "交付" / "browser_journeys.json"
SESSION_RE = re.compile(r"已建立会话：\s*(s_[0-9a-z]+)")


class JourneyFail(RuntimeError):
    """旅程失败：带上「哪一条断言没过」，不要只留一个超时。"""


def api(
    base_url: str,
    token: str,
    path: str,
    payload: dict[str, Any] | None = None,
    method: str | None = None,
) -> Any:
    """带令牌调一次后端；空响应重试一次，失败时把路径与原因一起抛。

    method 显式给的时候用它（PATCH 这类不改金额的更新），否则按有没有 body 猜。
    """
    url = base_url.rstrip("/") + path
    last: Exception | None = None
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    headers = {"Authorization": "Bearer " + token} if token else {}
    if data is not None:
        headers["Content-Type"] = "application/json"
    verb = method or ("POST" if data is not None else "GET")
    for _ in range(2):
        try:
            request = urllib.request.Request(url, data=data, headers=headers, method=verb)
            with urllib.request.urlopen(request, timeout=20) as response:  # noqa: S310 - 本机后端
                raw = response.read()
                if not raw:
                    last = RuntimeError(f"{path} 返回空响应（HTTP {response.status}）")
                    time.sleep(0.6)
                    continue
                return json.loads(raw.decode("utf-8"))
        except Exception as exc:  # noqa: BLE001 - 统一带上路径再抛
            last = exc
            time.sleep(0.6)
    raise JourneyFail(f"[接口] {path} 调不通：{last}")


def balance_fen(base_url: str, token: str) -> int:
    return int(api(base_url, token, "/api/business/overview")["account"]["balance_fen"])


def session_id_of(page: Any) -> str:
    """从聊天区常驻的系统气泡里取会话号；取不到就让旅程失败，不要静默跳过服务端断言。"""
    match = SESSION_RE.search(page.inner_text("#chat-log"))
    if not match:
        raise JourneyFail("[会话] 页面上没有会话号，无法核对服务端状态")
    return match.group(1)


def session_view(base_url: str, token: str, session_id: str) -> dict[str, Any]:
    return api(base_url, token, f"/api/session/{session_id}")


def audit_events(base_url: str, token: str, session_id: str) -> list[dict[str, Any]]:
    data = api(base_url, token, f"/api/audit?session_id={session_id}&limit=200")
    return list(data.get("events") or [])


def event_types(events: list[dict[str, Any]]) -> set[str]:
    return {str(row.get("event_type")) for row in events}


def node_states(view: dict[str, Any]) -> dict[str, str]:
    return {str(node.get("tool")): str(node.get("state")) for node in (view.get("nodes") or [])}


def goto_view(page: Any, view: str, base_url: str) -> None:
    page.goto(base_url, wait_until="domcontentloaded")
    tab = page.wait_for_selector('nav#nav button.nav-tab[data-view="%s"]' % view, timeout=15000)
    tab.click()
    page.wait_for_function(
        """(name) => {
            const section = document.querySelector('#view-' + name);
            if (!section || !section.classList.contains('active')) { return false; }
            const text = section.innerText || '';
            return section.querySelectorAll('.skeleton').length === 0 && text.indexOf('加载中') === -1;
        }""",
        arg=view,
        timeout=20000,
    )
    page.wait_for_timeout(350)


def shoot(page: Any, path: Path, full_page: bool = False) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(path), full_page=full_page)
    print(f"[截图] {path.relative_to(ROOT)} ({path.stat().st_size} bytes)")
    return path


def send(page: Any, text: str) -> None:
    page.fill("#chat-input", text)
    page.click("#chat-send")


# ---------------------------------------------------------------- J1 小额转账
def journey_small_transfer(page: Any, base_url: str, token: str, out: Path) -> dict[str, Any]:
    """小额转账：确认卡字段齐全、点一次确认只扣一次钱、服务端节点真的 done。"""
    print("\n=== J1 小额转账确认（100 元给李静，黄灯） ===")
    goto_view(page, "assistant", base_url)
    before = balance_fen(base_url, token)
    send(page, "帮我转 100 元给李静")
    page.wait_for_selector(".confirm-card", timeout=30000)
    sid = session_id_of(page)
    card = page.inner_text(".confirm-card")
    for needle in ("100.00", "李静", "转出账户", "手续费", "权限等级", "验证步骤", "确认号"):
        if needle not in card:
            raise JourneyFail(f"[J1] 确认卡缺少「{needle}」：{card[:200]}")
    page.click(".confirm-card .btn-green")
    page.wait_for_function(
        "() => document.querySelector('#chat-log').innerText.includes('已转出 100.00 元')",
        timeout=30000,
    )
    page.wait_for_timeout(700)
    after = balance_fen(base_url, token)
    if after != before - 10_000:
        raise JourneyFail(f"[J1] 余额应只少 100.00 元：{before} → {after}")
    view = session_view(base_url, token, sid)
    states = node_states(view)
    if states.get("transfer") != "done":
        raise JourneyFail(f"[J1] 服务端 transfer 节点不是 done：{states}")
    kinds = event_types(audit_events(base_url, token, sid))
    if "confirm.approved" not in kinds or "tool.executed" not in kinds:
        raise JourneyFail(f"[J1] 审计缺少确认/执行事件：{sorted(kinds)}")
    shot = shoot(page, out / "journey_01_small_transfer.png")
    return {
        "status": "pass",
        "session_id": sid,
        "balance_before_fen": before,
        "balance_after_fen": after,
        "node_states": states,
        "audit_events": sorted(kinds),
        "shot": str(shot.relative_to(ROOT)),
    }


# ---------------------------------------------------------------- J2 高额强验证
def journey_red_mfa(page: Any, base_url: str, token: str, out: Path) -> dict[str, Any]:
    """高额转账：服务端下发两个挑战、验证后才扣款，审计里能看到挑战与验证。"""
    print("\n=== J2 高额强验证（2000 元给妈妈，红灯） ===")
    goto_view(page, "assistant", base_url)
    before = balance_fen(base_url, token)
    send(page, "帮我给妈妈转 2000 元")
    page.wait_for_selector(".confirm-card", timeout=30000)
    sid = session_id_of(page)
    card = page.inner_text(".confirm-card")
    if "2,000.00" not in card or "高" not in card:
        raise JourneyFail(f"[J2] 确认卡金额/等级不对：{card[:200]}")
    page.click(".confirm-card .btn-green")
    page.wait_for_selector("#panel-factor input[data-challenge]", timeout=30000)
    page.wait_for_timeout(400)
    rows = page.query_selector_all("#panel-factor .factor-code-row")
    codes = [row.query_selector("input").input_value() for row in rows]
    if len(rows) < 2 or any(not code for code in codes):
        raise JourneyFail(f"[J2] 挑战不足或验证码为空：{codes}")
    shot_mfa = shoot(page, out / "journey_02_red_mfa.png", full_page=True)
    page.click("#panel-factor .btn-red")
    page.wait_for_function(
        "() => document.querySelector('#chat-log').innerText.includes('已转出 2,000.00 元')",
        timeout=45000,
    )
    page.wait_for_timeout(800)
    after = balance_fen(base_url, token)
    if after != before - 200_000:
        raise JourneyFail(f"[J2] 余额应只少 2,000.00 元：{before} → {after}")
    kinds = event_types(audit_events(base_url, token, sid))
    for needed in ("factor.challenge_issued", "factor.verified", "confirm.approved", "tool.executed"):
        if needed not in kinds:
            raise JourneyFail(f"[J2] 审计缺少 {needed}：{sorted(kinds)}")
    shot = shoot(page, out / "journey_02_red_executed.png", full_page=False)
    return {
        "status": "pass",
        "session_id": sid,
        "challenges": len(rows),
        "balance_before_fen": before,
        "balance_after_fen": after,
        "audit_events": sorted(kinds),
        "shots": [str(shot_mfa.relative_to(ROOT)), str(shot.relative_to(ROOT))],
    }


# ---------------------------------------------------------------- J4 生日计划
def journey_birthday_plan(page: Any, base_url: str, token: str, out: Path) -> dict[str, Any]:
    """生日计划：落库 → 计划页看得见 → 刷新后仍在 → 服务端状态没变、钱没动。"""
    print("\n=== J4 生日计划建立 / 刷新 / 恢复 ===")
    birthday = (dt.date.today() + dt.timedelta(days=30)).isoformat()
    before = balance_fen(base_url, token)
    created = api(
        base_url,
        token,
        "/api/plans/birthday",
        {"birthday": birthday, "budget_fen": 100_000, "title": "妈妈的生日计划"},
    )
    plan = created.get("plan") or {}
    plan_id = str(plan.get("plan_id") or "")
    if not plan_id:
        raise JourneyFail(f"[J4] 建计划没返回 plan_id：{created}")
    budget = plan.get("budget") or {}
    items = plan.get("items") or []
    if budget.get("budget_fen") != 100_000:
        raise JourneyFail(f"[J4] 预算不是 1000 元：{budget}")
    if {item.get("item_key") for item in items} != {"flowers", "cake"}:
        raise JourneyFail(f"[J4] 项目不是鲜花+蛋糕：{items}")
    if balance_fen(base_url, token) != before:
        raise JourneyFail("[J4] 建计划就动钱了：预算预留应是账本记录，不是扣款")

    goto_view(page, "plans", base_url)
    page.wait_for_function(
        "() => document.querySelector('#plan-banner') && document.querySelector('#view-plans').innerText.includes('妈妈的生日计划')",
        timeout=25000,
    )
    text = page.inner_text("#view-plans")
    for needle in ("妈妈的生日计划", "鲜花", "蛋糕", "预算"):
        if needle not in text:
            raise JourneyFail(f"[J4] 计划页缺少「{needle}」")
    shot = shoot(page, out / "journey_04_birthday_plan.png", full_page=True)

    page.reload(wait_until="domcontentloaded")
    page.wait_for_selector('nav#nav button.nav-tab[data-view="plans"]', timeout=15000)
    page.click('nav#nav button.nav-tab[data-view="plans"]')
    page.wait_for_function(
        "() => document.querySelector('#view-plans').innerText.includes('妈妈的生日计划')",
        timeout=20000,
    )
    reloaded = page.inner_text("#view-plans")
    if "鲜花" not in reloaded or "蛋糕" not in reloaded:
        raise JourneyFail("[J4] 刷新后计划项丢了")

    restored = api(base_url, token, f"/api/plans/{plan_id}")
    plan_now = restored.get("plan") or restored
    if plan_now.get("status") != "active":
        raise JourneyFail(f"[J4] 刷新后计划状态不是 active：{plan_now.get('status')}")
    if balance_fen(base_url, token) != before:
        raise JourneyFail("[J4] 只是刷新/读取，余额不该变")
    shot2 = shoot(page, out / "journey_04_birthday_reloaded.png", full_page=False)
    return {
        "status": "pass",
        "plan_id": plan_id,
        "birthday": birthday,
        "budget_fen": budget.get("budget_fen"),
        "items": [item.get("item_key") for item in items],
        "balance_unchanged_fen": before,
        "plan_status_after_reload": plan_now.get("status"),
        "shots": [str(shot.relative_to(ROOT)), str(shot2.relative_to(ROOT))],
    }


# ---------------------------------------------------------------- J5 储蓄监测
def journey_savings_alert(page: Any, base_url: str, token: str, out: Path) -> dict[str, Any]:
    """储蓄目标监测：目标推进 → 生成提醒 → 页面上处理掉 → 服务端真的不再 open。"""
    print("\n=== J5 储蓄目标监测与提醒处理 ===")
    goal = api(
        base_url,
        token,
        "/api/savings/goals",
        {
            "name": "明年装修存 12 万",
            "target_fen": 12_000_000,
            "target_date": (dt.date.today() + dt.timedelta(days=45)).isoformat(),
            "saved_fen": 0,
            "period": "monthly",
        },
    )
    goal_id = str((goal.get("goal") or {}).get("goal_id") or "")
    if not goal_id:
        raise JourneyFail(f"[J5] 建目标没返回 goal_id：{goal}")
    api(base_url, token, f"/api/savings/goals/{goal_id}", {"monitor_enabled": True}, method="PATCH")
    api(base_url, token, "/api/proactive/monitor/run", {"goal_id": goal_id})
    alerts = api(base_url, token, "/api/proactive/alerts?status=open&limit=50")
    rows = list(alerts.get("alerts") or [])
    target = next((row for row in rows if row.get("goal_id") == goal_id), None)
    if target is None:
        raise JourneyFail(f"[J5] 监测没有生成提醒：{rows}")

    goto_view(page, "plans", base_url)
    page.wait_for_function(
        """(name) => {
            const box = document.querySelector('#plan-alerts');
            return !!box && box.innerText.includes(name);
        }""",
        arg="明年装修存 12 万",
        timeout=25000,
    )
    before_text = page.inner_text("#plan-alerts")
    # 视口截图必须先把提醒卡片滚进画面：计划页顶部是调度状态，不滚的话两张图会一模一样
    page.locator("#plan-alerts").scroll_into_view_if_needed()
    page.wait_for_timeout(300)
    shot = shoot(page, out / "journey_05_savings_alert.png", full_page=False)
    # 点这条提醒的「标记已处理」：文案与 pfResolveAlert 绑定，不靠位置猜按钮。
    resolved = page.evaluate(
        """() => {
            const box = document.querySelector('#plan-alerts');
            if (!box) { return false; }
            const buttons = Array.from(box.querySelectorAll('button'));
            const btn = buttons.find(b => (b.textContent || '').indexOf('标记已处理') >= 0);
            if (!btn) { return false; }
            btn.click();
            return true;
        }"""
    )
    if not resolved:
        raise JourneyFail("[J5] 提醒卡片上没有「标记已处理」按钮")
    page.wait_for_function(
        """() => {
            const box = document.querySelector('#plan-alerts');
            return !!box && box.innerText.indexOf('没有需要处理的提醒') >= 0;
        }""",
        timeout=20000,
    )
    left = api(base_url, token, "/api/proactive/alerts?status=open&limit=50")
    left_rows = [row for row in (left.get("alerts") or []) if row.get("goal_id") == goal_id]
    if left_rows:
        raise JourneyFail(f"[J5] 页面处理了但服务端还是 open：{left_rows}")
    page.locator("#plan-alerts").scroll_into_view_if_needed()
    page.wait_for_timeout(300)
    shot2 = shoot(page, out / "journey_05_alert_resolved.png", full_page=False)
    return {
        "status": "pass",
        "goal_id": goal_id,
        "alert_id": target.get("alert_id"),
        "alert_reason": target.get("reason"),
        "alert_text_before": before_text[:300],
        "shots": [str(shot.relative_to(ROOT)), str(shot2.relative_to(ROOT))],
    }


# ---------------------------------------------------------------- J3 AA 同名
AA_PROMPTS = (
    "我们 AA 一下，300 元分摊给张三和李静，一人一半",
    "AA 分摊：总共 300 元，分给张三和李静各 150 元",
)


def _read_env_file() -> dict[str, str]:
    """只从仓库根的 .env 里取模型相关键，用于在沙箱里拉起真模型后端。"""
    env: dict[str, str] = {}
    path = ROOT / ".env"
    if not path.exists():
        return env
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        key = key.strip()
        if key.startswith("LLM_") or key == "CAPABILITY_SECRET":
            env[key] = val.strip().strip('"').strip("'")
    return env


def start_real_backend(base_url: str) -> tuple[Any, str]:
    """沙箱里起一个真模型后端（MOCK_MODE=false）。

    不能复用 bs.ensure_backend：它的 sandbox_env() 强制 MOCK_MODE=script，
    那是为了截图/冒烟的确定性；AA 分账需要模型真的吐 split_bill，只能走真模型。
    """
    env = dict(os.environ)
    env.update(_read_env_file())
    env.update({"MOCK_MODE": "false", "PYTHONPATH": str(SANDBOX)})
    port = base_url.rsplit(":", 1)[-1].rstrip("/")
    log_path = ROOT / "tmp" / "ui_journey_backend.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    handle = log_path.open("a", encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--port", port, "--log-level", "warning"],
        cwd=SANDBOX,
        env=env,
        stdout=handle,
        stderr=subprocess.STDOUT,
        text=True,
    )
    print(f"[后端] 真模型 uvicorn pid={proc.pid} -> {base_url}（日志 tmp/ui_journey_backend.log）")
    if not bs.wait_health(base_url, 40):
        proc.terminate()
        raise JourneyFail("[后端] 真模型后端 40 秒内没起来")
    return proc, base_url


def journey_aa_ambiguous(page: Any, base_url: str, token: str, out: Path) -> dict[str, Any]:
    """AA 分账遇到同名收款人：必须在动钱之前被拦下，不能默认挑第一个，也不能只扣一半。"""
    print("\n=== J3 AA 同名候选被阻止（张三 两位，李静 一位） ===")
    goto_view(page, "assistant", base_url)
    before = balance_fen(base_url, token)
    attempts: list[dict[str, Any]] = []
    for index, prompt in enumerate(AA_PROMPTS, start=1):
        send(page, prompt)
        try:
            page.wait_for_function(
                """() => {
                    const text = document.querySelector('#chat-log').innerText || '';
                    return text.indexOf('候选不唯一') >= 0
                        || text.indexOf('不能继续') >= 0
                        || text.indexOf('已转出') >= 0
                        || text.indexOf('已按 AA') >= 0
                        || document.querySelectorAll('.pick-list').length > 0;
                }""",
                timeout=90000,
            )
        except Exception as exc:  # noqa: BLE001 - 超时也要留下现场
            attempts.append({"prompt": prompt, "timeout": repr(exc)})
            continue
        page.wait_for_timeout(800)
        text = page.inner_text("#chat-log")
        sid = None
        try:
            sid = session_id_of(page)
        except JourneyFail:
            sid = None
        balance_after = balance_fen(base_url, token)
        blocked = "候选不唯一" in text or "不能继续" in text
        moved = balance_after != before or "已转出" in text or "已按 AA" in text
        picker = page.query_selector_all(".pick-list")
        nodes: dict[str, str] = {}
        kinds: set[str] = set()
        intents: list[str] = []
        if sid:
            nodes = node_states(session_view(base_url, token, sid))
            events = audit_events(base_url, token, sid)
            kinds = event_types(events)
            for row in events:
                if row.get("event_type") == "intent.parsed":
                    payload = row.get("payload") or {}
                    if isinstance(payload, dict) and payload.get("intent"):
                        intents.append(str(payload["intent"]))
        attempts.append(
            {
                "prompt": prompt,
                "blocked": blocked,
                "moved_money": moved,
                "picker_cards": len(picker),
                "balance_after_fen": balance_after,
                "node_states": nodes,
                "audit_events": sorted(kinds),
                "intents": intents,
            }
        )
        if moved:
            shot_fail = shoot(page, out / f"journey_03_aa_{index}_moved.png")
            raise JourneyFail(f"[J3] 同名候选竟然动钱了（第 {index} 次）：{balance_after} / {shot_fail}")
        if blocked or (picker and "split_bill" in intents):
            if nodes.get("split_transfer") == "done" or "transfer.committed" in kinds:
                raise JourneyFail("[J3] 页面说拦住了，但服务端 split_transfer 已经执行")
            page.evaluate(
                "() => { const n = Array.from(document.querySelectorAll('#chat-log .bubble-body'))"
                ".find(el => (el.textContent || '').indexOf('候选不唯一') >= 0);"
                " if (n) { n.scrollIntoView({block: 'center'}); } }"
            )
            page.wait_for_timeout(400)
            shot = shoot(page, out / "journey_03_aa_blocked.png")
            return {
                "status": "pass",
                "attempt": index,
                "prompt": prompt,
                "session_id": sid,
                "balance_unchanged_fen": before,
                "node_states": nodes,
                "audit_events": sorted(kinds),
                "intents": intents,
                "picker_cards": len(picker),
                "attempts": attempts,
                "shot": str(shot.relative_to(ROOT)),
            }
        # 走了消歧选择器或别的路径：换一句再试一次，两次都不行才算失败
        page.reload(wait_until="domcontentloaded")
        page.wait_for_selector('nav#nav button.nav-tab[data-view="assistant"]', timeout=15000)
        page.click('nav#nav button.nav-tab[data-view="assistant"]')
        page.wait_for_timeout(500)
    raise JourneyFail(f"[J3] 两句话都没走到「同名候选被阻止」：{attempts}")


# ---------------------------------------------------------------- run
def run(args: argparse.Namespace) -> int:
    from playwright.sync_api import sync_playwright  # noqa: PLC0415

    demo_db = ROOT / "data" / "bank.db"
    demo_before = bs.sha256_file(demo_db)
    bs.build_sandbox(SANDBOX)
    bs.seed_sandbox(SANDBOX)
    out = Path(args.out_dir)
    out = out if out.is_absolute() else ROOT / out
    out.mkdir(parents=True, exist_ok=True)
    wanted = {item.strip() for item in args.only.split(",") if item.strip()}
    report: dict[str, Any] = {
        "started_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "demo_db_sha256_before": demo_before,
        "sandbox": str(SANDBOX.relative_to(ROOT)),
        "journeys": {},
    }
    failures: list[str] = []
    proc, base_url = bs.ensure_backend(args.base_url, SANDBOX, False)
    token = bs.read_api_token(SANDBOX)
    page_url = f"{base_url}/#token={urllib.parse.quote(token)}" if token else base_url
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=not args.headed)
            page = browser.new_page(viewport={"width": 1440, "height": 960})
            errors: list[str] = []
            page.on("pageerror", lambda exc: errors.append(str(exc)))

            plan = [
                ("j1", journey_small_transfer),
                ("j2", journey_red_mfa),
                ("j4", journey_birthday_plan),
                ("j5", journey_savings_alert),
            ]
            for name, func in plan:
                if wanted and name not in wanted:
                    continue
                proc, base_url, token, page_url = restart_if_needed(proc, base_url, name)
                page.goto(page_url, wait_until="domcontentloaded")
                errors.clear()
                try:
                    report["journeys"][name] = func(page, base_url, token, out)
                except JourneyFail as exc:
                    report["journeys"][name] = {"status": "fail", "error": str(exc)}
                    failures.append(f"{name}: {exc}")
                    print(f"[失败] {name}: {exc}")
                if errors:
                    report["journeys"].setdefault(name, {})["page_errors"] = list(errors)
                    failures.append(f"{name}: 页面 JS 报错 {errors[:3]}")

            if not wanted or "j3" in wanted:
                # AA 需要真模型：换成 MOCK_MODE=false 的后端，再重播一次沙箱。
                proc = stop_backend(proc, base_url)
                bs.seed_sandbox(SANDBOX, force=True)
                proc, base_url = start_real_backend(f"http://127.0.0.1:{bs.free_port()}")
                token = bs.read_api_token(SANDBOX)
                page_url = f"{base_url}/#token={urllib.parse.quote(token)}" if token else base_url
                page.goto(page_url, wait_until="domcontentloaded")
                errors.clear()
                try:
                    report["journeys"]["j3"] = journey_aa_ambiguous(page, base_url, token, out)
                except JourneyFail as exc:
                    report["journeys"]["j3"] = {"status": "fail", "error": str(exc)}
                    failures.append(f"j3: {exc}")
                    print(f"[失败] j3: {exc}")
                if errors:
                    report["journeys"].setdefault("j3", {})["page_errors"] = list(errors)
                    failures.append(f"j3: 页面 JS 报错 {errors[:3]}")
            page.close()
            browser.close()
    finally:
        stop_backend(proc, base_url)
        demo_after = bs.sha256_file(demo_db)
        report["demo_db_sha256_after"] = demo_after
        report["demo_db_unchanged"] = demo_before == demo_after
        report["finished_at"] = dt.datetime.now().astimezone().isoformat(timespec="seconds")
        report["failures"] = failures
        report["status"] = "pass" if not failures else "fail"
        REPORT.parent.mkdir(parents=True, exist_ok=True)
        REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n[报告] {REPORT.relative_to(ROOT)}")
        if not report["demo_db_unchanged"]:
            print("[不变量] 演示库被改动了！")
            failures.append("演示库被改动")
        print(f"[汇总] {len(report['journeys'])} 条旅程，失败 {len(failures)} 条")
        for name, data in report["journeys"].items():
            print(f"  {name}: {data.get('status')}")
    return 0 if not failures else 1


def stop_backend(proc: Any, base_url: str) -> Any:
    if proc is not None:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except Exception:  # noqa: BLE001 - 停不下来就强杀，随后要重起
            proc.kill()
    deadline = time.time() + 20
    while time.time() < deadline:
        if not bs.wait_health(base_url, 0.4):
            break
        time.sleep(0.3)
    return None


def restart_if_needed(proc: Any, base_url: str, name: str) -> tuple[Any, str, str, str]:
    """J2 必须在「当天还没出过账」的库里跑，否则会被日累计拦下（那是另一条旅程）。

    所以 j1/j2/j4 各自从干净沙箱开始；j5 与 j4 共享同一份数据没有冲突。
    """
    if name == "j1":
        token = bs.read_api_token(SANDBOX)
        page_url = f"{base_url}/#token={urllib.parse.quote(token)}" if token else base_url
        return proc, base_url, token, page_url
    proc = stop_backend(proc, base_url)
    bs.seed_sandbox(SANDBOX, force=True)
    proc, base_url = bs.ensure_backend(base_url, SANDBOX, False)
    token = bs.read_api_token(SANDBOX)
    page_url = f"{base_url}/#token={urllib.parse.quote(token)}" if token else base_url
    return proc, base_url, token, page_url


def main() -> int:
    parser = argparse.ArgumentParser(description="五条高风险浏览器旅程（DOM + 服务端状态双证）")
    parser.add_argument("--base-url", default="http://127.0.0.1:8127")
    parser.add_argument("--out-dir", default=str(SHOT_DIR))
    parser.add_argument("--only", default="", help="只跑某几条，如 j1,j2")
    parser.add_argument("--headed", action="store_true")
    return run(parser.parse_args())


if __name__ == "__main__":
    raise SystemExit(main())