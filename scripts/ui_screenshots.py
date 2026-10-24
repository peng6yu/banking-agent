#!/usr/bin/env python3.12
"""六区信息架构的浏览器截图：桌面 + 手机，全部打在沙箱后端上。

为什么单独写一个脚本：check_frontend.py 只扫源码，browser_smoke.py 只跑两条主链路。
答辩材料需要「每个区在真实数据下长什么样」，而且必须能重复生成 —— 所以截图也不能手工截。

用法：
    /home/peng/.local/bin/python3.12 scripts/ui_screenshots.py
    /home/peng/.local/bin/python3.12 scripts/ui_screenshots.py --payee-only
    /home/peng/.local/bin/python3.12 scripts/ui_screenshots.py --headed   # 现场看一遍

沙箱：和 browser_smoke.py 同一套隔离（app/ web/ scripts 复制到 tmp/，种子播在沙箱库里），
答辩现场的 data/bank.db 不会被碰；脚本末尾用 sha256 反证这一点。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import browser_smoke as bs  # noqa: E402  沙箱与后端拉起逻辑复用同一份，避免两套实现漂移

SANDBOX = ROOT / "tmp" / "ui_shot_sandbox"
SHOT_DIR = ROOT / "docs" / "交付" / "截图" / "信息架构"
VIEWS = ("overview", "assistant", "bill", "plans", "manage", "security")
DESKTOP = {"width": 1440, "height": 960}
MOBILE = {"width": 390, "height": 844}


def api(base_url: str, token: str, path: str, payload: dict[str, Any] | None = None) -> Any:
    """带令牌调一次后端，返回解析后的 JSON。

    后端刚重启时会有一两秒「进程还在收尾、连接能建但响应是空的」窗口；
    空响应重试一次，还不行就把状态码和响应头前缀打出来，不要只抛一个 JSONDecodeError。
    """
    url = base_url.rstrip("/") + path
    data = None
    headers = {"Authorization": "Bearer " + token} if token else {}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers, method="POST" if data else "GET")
    last_error: Exception | None = None
    for attempt in range(2):
        try:
            with urllib.request.urlopen(request, timeout=15) as response:  # noqa: S310 - 本机后端
                raw = response.read()
                if not raw:
                    last_error = RuntimeError(
                        f"{path} 返回空响应（HTTP {response.status}，"
                        f"content-type={response.headers.get('content-type')}）"
                    )
                    time.sleep(0.6)
                    continue
                return json.loads(raw.decode("utf-8"))
        except Exception as exc:  # noqa: BLE001 - 下面统一带上路径再抛
            last_error = exc
            time.sleep(0.6)
    raise SystemExit(f"[接口] {path} 调不通：{last_error}")


def wait_backend_down(base_url: str, timeout: float = 20.0) -> None:
    """等旧后端真的不响应了再重播库、再起新进程。

    只 proc.wait() 不够：uvicorn 收尾期间端口还开着，ensure_backend 的 wait_health
    会以为服务健在，于是新进程绑不上端口，后面的请求打到正在关闭的旧进程上。
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not bs.wait_health(base_url, 0.4):
            return
        time.sleep(0.3)
    print("[幂等] 警告：旧后端 20 秒内没停下来，继续重播（可能赌端口）")


def restart_backend(proc: Any, base_url: str) -> tuple[Any, str, str, str]:
    """停后端 → 重播沙箱库 → 重新起后端 → 重读令牌，返回 (proc, base_url, token, page_url)。"""
    if proc is not None:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except Exception:  # noqa: BLE001 - 停不下来就强杀，反正下面要重起
            proc.kill()
    wait_backend_down(base_url)
    bs.seed_sandbox(SANDBOX, force=True)
    proc, base_url = bs.ensure_backend(base_url, SANDBOX, False)
    token = bs.read_api_token(SANDBOX)
    page_url = f"{base_url}/#token={urllib.parse.quote(token)}" if token else base_url
    return proc, base_url, token, page_url


def seed_savings_goal(base_url: str, token: str) -> dict[str, Any]:
    """用一个真实持久化的目标把「计划与提醒」区填满，而不是编一段示例数据。

    6000 元 / 6 个月正好是任务书里那个储蓄目标场景，计划里的每期金额由后端算。
    """
    payload = {
        "name": "半年存 6000 元",
        "target_fen": 600_000,
        "target_date": "2027-04-30",
        "saved_fen": 50_000,
        "period": "monthly",
    }
    created = api(base_url, token, "/api/savings/goals", payload)
    goal_id = created.get("goal_id") or (created.get("goal") or {}).get("goal_id")
    print(f"[数据] 已创建储蓄目标 {goal_id}（6000 元 / 2027-04-30，已存 500 元）")
    api(base_url, token, "/api/proactive/monitor/run", {})
    print("[数据] 已跑一次手动监测（不是后台持续监测，见 plans 区说明）")
    return created


def seed_plan(base_url: str, token: str) -> dict[str, Any]:
    """建一个真的落库的跨场景计划，让「计划与提醒」区显示真实状态而不是空卡片。

    三步：检查余额 → 提醒转存 → 资金动作。最后那步故意也设成到点，
    推进时后端会把它标成「已交回你确认」并把计划挂起 —— 页面正好演示这条边界。
    """
    now_ms = int(__import__("time").time() * 1000)
    payload = {
        "title": "生日支出计划（演示）",
        "steps": [
            {
                "kind": "check",
                "title": "先看余额够不够",
                "payload": {"check": "balance_at_least", "amount_fen": 100_000, "repeat_ms": 86_400_000},
                "due_ms": now_ms - 1000,
            },
            {
                "kind": "reminder",
                "title": "生日前提醒留出 500 元",
                "payload": {"text": "下周三妈妈生日，提前留出 500 元"},
                "due_ms": now_ms - 1000,
            },
            {
                "kind": "action",
                "title": "到期转 300 元给李静",
                "payload": {
                    "tool": "transfer",
                    "params": {"payee_id": "p_1d6f92b0", "amount_fen": 30_000},
                    "prompt": "到期了：要转 300 元给李静，请回来看确认卡片",
                },
                "due_ms": now_ms - 1000,
            },
        ],
    }
    created = api(base_url, token, "/api/plans", payload)
    plan_id = (created.get("plan") or {}).get("plan_id")
    ran = api(base_url, token, "/api/plans/run", {})
    print(f"[数据] 已创建跨场景计划 {plan_id}，并手动推进：{ran.get('count')} 步")
    return created


def wait_view(page: Any, view: str, timeout: float = 20000) -> None:
    """等到这个区不再是骨架屏。"""
    page.wait_for_function(
        """(name) => {
            const section = document.querySelector('#view-' + name);
            if (!section || !section.classList.contains('active')) { return false; }
            const skeletons = section.querySelectorAll('.skeleton');
            const text = section.innerText || '';
            return skeletons.length === 0 && text.indexOf('加载中') === -1;
        }""",
        arg=view,
        timeout=timeout,
    )


def goto_view(page: Any, view: str, base_url: str) -> None:
    page.goto(base_url, wait_until="domcontentloaded")
    tab = page.wait_for_selector('nav#nav button.nav-tab[data-view="%s"]' % view, timeout=15000)
    tab.click()
    wait_view(page, view)
    page.wait_for_timeout(400)


#: 每个区必须出现的「真实数据」锚点：看不到就说明接口没接上或渲染崩了。
VIEW_ANCHORS: dict[str, tuple[str, ...]] = {
    "overview": ("余额", "本月支出", "需要核实的异常"),
    "assistant": ("常见操作与工具清单", "发送"),
    "bill": ("分类支出", "月度趋势"),
    "plans": ("储蓄目标", "跨场景计划"),
    "manage": ("卡片", "订阅"),
    "security": ("执行记录", "审计"),
}


def check_no_hscroll(page: Any, view: str, suffix: str) -> None:
    """手机上横向溢出是最常见的布局回归：整页宽度不许超过视口。"""
    overflow = page.evaluate(
        "() => Math.round((document.documentElement.scrollWidth || 0) - (window.innerWidth || 0))"
    )
    if overflow > 1:
        raise SystemExit(f"[响应式] {view}/{suffix} 横向溢出 {overflow}px")


def shoot_views(
    page: Any, base_url: str, suffix: str, out_dir: Path
) -> tuple[dict[str, Path], list[dict[str, Any]]]:
    out: dict[str, Path] = {}
    checks: list[dict[str, Any]] = []
    for view in VIEWS:
        goto_view(page, view, base_url)
        text = page.inner_text("#view-" + view)
        missing = [anchor for anchor in VIEW_ANCHORS[view] if anchor not in text]
        if missing:
            raise SystemExit(f"[内容] {view}/{suffix} 缺少关键文案：{missing}")
        check_no_hscroll(page, view, suffix)
        path = out_dir / f"{view}_{suffix}.png"
        page.screenshot(path=str(path), full_page=True)
        size = path.stat().st_size
        print(f"[截图] {path.relative_to(ROOT)} ({size} bytes)")
        if size < 12000:
            raise SystemExit(f"[截图] {path} 太小，页面可能是空白")
        checks.append({"view": view, "suffix": suffix, "anchors": list(VIEW_ANCHORS[view]),
                       "no_hscroll": True, "bytes": size})
        # 键必须带 suffix：否则 desktop 的结果会被 mobile 覆盖，报告里只剩一半截图。
        out[f"{view}_{suffix}"] = path
    return out, checks


def scroll_chat_to_bottom(page: Any, target: str = "#chat-log") -> None:
    """对话在独立滚动容器里，整页截图只拍容器顶部。

    页面结构改过两次（外层 .col-left 与 #chat-log 都可能成为滚动容器），
    所以这里不写死某一个 id：从目标元素往上走，把所有真正在滚的祖先都滚到底。
    """
    page.evaluate(
        """(sel) => {
            const start = document.querySelector(sel) || document.getElementById('chat-log');
            let el = start;
            while (el) {
                if (el.scrollHeight > el.clientHeight + 8) { el.scrollTop = el.scrollHeight; }
                el = el.parentElement;
            }
            window.scrollTo(0, document.body.scrollHeight);
            if (start && start.lastElementChild) { start.lastElementChild.scrollIntoView({block: 'end'}); }
        }""",
        target,
    )
    page.wait_for_timeout(350)


def scenario_payee(page: Any, base_url: str, out_dir: Path) -> dict[str, Any]:
    """主线演示：一句话里出现两个同名收款人，系统必须停下来问，不能自己挑。"""
    print("\n=== 主线：转 500 元给张三（通讯录里有两个张三） ===")
    goto_view(page, "assistant", base_url)
    page.fill("#chat-input", "帮我转 500 元给张三")
    page.click("#chat-send")
    page.wait_for_selector("#chat-log .pick-list", timeout=30000)
    page.wait_for_timeout(300)
    pick_text = page.inner_text("#chat-log .pick-list").replace("\n", " / ")
    print("[消歧]", pick_text[:220])
    picks = page.query_selector_all("#chat-log .pick-list .pick")
    if len(picks) != 2:
        raise SystemExit(f"[消歧] 期望 2 个候选，实际 {len(picks)} 个")
    scroll_chat_to_bottom(page)
    shot1 = out_dir / "main_01_payee_picker.png"
    page.screenshot(path=str(shot1), full_page=True)
    print(f"[截图] {shot1.relative_to(ROOT)} ({shot1.stat().st_size} bytes)")

    # 选「民生 6620」那一位：按卡尾号点，而不是按顺序点第一个
    target = None
    for pick in picks:
        if "6620" in pick.inner_text():
            target = pick
            break
    if target is None:
        raise SystemExit("[消歧] 没找到尾号 6620 的候选")
    target.click()

    page.wait_for_selector(".confirm-card", timeout=30000)
    page.wait_for_timeout(400)
    card_text = page.inner_text(".confirm-card").replace("\n", " / ")
    print("[确认卡]", card_text[:320])
    if "500.00" not in card_text:
        raise SystemExit("[确认卡] 金额没有回显 500.00")
    if "6620" not in card_text:
        raise SystemExit("[确认卡] 收款人没有换成 6620 那一位 —— 计划里的唯一标识没跟着更新")
    for needle in ("转出账户", "手续费", "权限等级", "验证步骤"):
        if needle not in card_text:
            raise SystemExit(f"[确认卡] 缺字段：{needle}")
    page.evaluate(
        "() => { const el = document.querySelector('.confirm-card'); if (el) el.scrollIntoView({block: 'center'}); }"
    )
    page.wait_for_timeout(250)
    scroll_chat_to_bottom(page)
    shot2 = out_dir / "main_02_confirm_card.png"
    page.screenshot(path=str(shot2), full_page=True)
    print(f"[截图] {shot2.relative_to(ROOT)} ({shot2.stat().st_size} bytes)")

    page.click(".confirm-card .btn-green")
    page.wait_for_function(
        "() => document.querySelector('#chat-log').innerText.includes('已转出 500.00 元')",
        timeout=30000,
    )
    page.wait_for_timeout(600)
    scroll_chat_to_bottom(page)
    shot3 = out_dir / "main_03_executed.png"
    page.screenshot(path=str(shot3), full_page=True)
    print(f"[截图] {shot3.relative_to(ROOT)} ({shot3.stat().st_size} bytes)")
    return {"picker": str(shot1), "card": str(shot2), "done": str(shot3), "card_text": card_text}


RED_PROMPT = "帮我给妈妈转 2000 元"  # 妈妈在种子通讯录里只有一位，金额 2000 > 1000 必进红灯


def scenario_red_factor(page: Any, base_url: str, out_dir: Path) -> dict[str, Any]:
    """红灯主线：>1000 元转账必须由服务端下发验证码、服务端校验，前端不能自己声明验过了。"""
    print("\n=== 红灯：转 2000 元给妈妈（单笔超 1000 元赛题分级） ===")
    goto_view(page, "assistant", base_url)
    page.fill("#chat-input", RED_PROMPT)
    page.click("#chat-send")
    try:
        page.wait_for_selector(".confirm-card", timeout=30000)
    except Exception:  # noqa: BLE001 - 失败时把聊天区打出来，比只看到一个 Timeout 有用
        print("[红灯] 30 秒内没出现确认卡，聊天区内容是：")
        print(page.inner_text("#chat-log")[:1500])
        raise
    page.wait_for_timeout(300)
    card_text = page.inner_text(".confirm-card").replace("\n", " / ")
    print("[确认卡]", card_text[:260])
    if "2,000.00" not in card_text:
        raise SystemExit("[红灯] 确认卡金额不是 2,000.00")
    if "高" not in card_text:
        raise SystemExit("[红灯] 确认卡没有标出高权限等级")

    page.click(".confirm-card .btn-green")
    # 面板里应该出现服务端下发的挑战（演示回显会把验证码自动填好）
    page.wait_for_selector("#panel-factor input[data-challenge]", timeout=30000)
    page.wait_for_timeout(400)
    rows = page.query_selector_all("#panel-factor .factor-code-row")
    print(f"[多因子] 服务端下发 {len(rows)} 个挑战")
    if not rows:
        raise SystemExit("[多因子] 面板里没有挑战行")
    codes = [row.query_selector("input").input_value() for row in rows]
    print("[多因子] 已填入的验证码：", codes)
    if any(not c for c in codes):
        raise SystemExit(f"[多因子] 有挑战没有拿到验证码：{codes}")
    shot1 = out_dir / "main_04_red_mfa.png"
    page.screenshot(path=str(shot1), full_page=True)
    print(f"[截图] {shot1.relative_to(ROOT)} ({shot1.stat().st_size} bytes)")

    page.click("#panel-factor .btn-red")
    page.wait_for_function(
        "() => document.querySelector('#chat-log').innerText.includes('已转出 2,000.00 元')",
        timeout=40000,
    )
    page.wait_for_timeout(700)
    scroll_chat_to_bottom(page)
    shot2 = out_dir / "main_05_red_executed.png"
    page.screenshot(path=str(shot2), full_page=True)
    print(f"[截图] {shot2.relative_to(ROOT)} ({shot2.stat().st_size} bytes)")
    return {"mfa": str(shot1), "done": str(shot2), "card_text": card_text, "challenges": len(rows)}


DAILY_PROMPT = "帮我给房东转 300 元"  # 房东在种子通讯录里只有一位，金额小，只可能撞日累计


def scenario_daily_block(page: Any, base_url: str, out_dir: Path) -> dict[str, Any]:
    """日累计硬线：红灯刚转出 2000 元之后，再转 300 元必须被直接拦下，而且不给验证面板。

    这条不是多因子场景：限额是额度问题，验证码再多也变不出额度。
    """
    print("\n=== 日累计硬线：再转 300 元（当天已转 2000 元，超过 1000 元赛题日累计） ===")
    page.fill("#chat-input", DAILY_PROMPT)
    page.click("#chat-send")
    page.wait_for_function(
        "() => document.querySelector('#chat-log').innerText.includes('日累计')",
        timeout=40000,
    )
    page.wait_for_timeout(600)
    log_text = page.inner_text("#chat-log")
    cards = page.query_selector_all(".confirm-card")
    factor_rows = page.query_selector_all("#panel-factor .factor-code-row")
    if factor_rows:
        raise SystemExit("[日累计] 撞到限额却下发了多因子挑战：额度问题不该靠验证码解决")
    shot = out_dir / "main_06_daily_limit.png"
    scroll_chat_to_bottom(page)
    page.screenshot(path=str(shot), full_page=True)
    print(f"[截图] {shot.relative_to(ROOT)} ({shot.stat().st_size} bytes)")
    print("[日累计] 拦截文案：", [ln for ln in log_text.splitlines() if "日累计" in ln][:1])
    return {"shot": str(shot), "cards": len(cards), "factor_rows": len(factor_rows)}


IDEMPOTENT_PROMPT = "帮我转 300 元给李静"  # 李静唯一；300 元走黄灯，一次确认就够
REFRESH_PROMPT = "帮我再转 200 元给李静"  # 第二笔只为了造一张「刷新时还挂着」的确认卡


def _balance_fen(base_url: str, token: str) -> int:
    data = api(base_url, token, "/api/business/overview")
    return int(data["account"]["balance_fen"])


def scenario_double_click_and_refresh(
    page: Any, base_url: str, token: str, out_dir: Path
) -> dict[str, Any]:
    """重复点击不重复扣款 + 刷新后确认卡还在。

    这两条最容易被「前端提示」糊过去：
    1. 点一次确认 != 成功；点两次也只能扣一次。幂等必须落在后端，
       不能只靠按钮 disabled —— 双击、重放请求、两条标签页都能绕过 disabled。
    2. 刷新等于 SSE 断流。页面必须从 /api/session 把还挂着的确认卡拿回来，
       否则用户一按 F5 就以为操作丢了，只好重新发一句、可能重复下单。
    """
    print("\n=== 幂等与刷新：连点两次确认 / 刷新后确认卡还在 ===")
    # 传进来的 base_url 可能带 #token=…（页面要用），接口地址必须把 fragment 切掉，
    # 否则请求会打到 "/" 上，拿回 index.html，json.loads 直接炸。
    api_base = base_url.split("#", 1)[0]
    goto_view(page, "assistant", base_url)
    before = _balance_fen(api_base, token)
    page.fill("#chat-input", IDEMPOTENT_PROMPT)
    page.click("#chat-send")
    page.wait_for_selector(".confirm-card", timeout=30000)
    card = page.inner_text(".confirm-card")
    if "300.00" not in card:
        raise SystemExit("[幂等] 确认卡金额不是 300.00")
    # 同一个按钮同步点两次：不依赖前端 disabled，交给后端幂等裁决
    page.evaluate(
        "() => { const b = document.querySelector('.confirm-card .btn-green');"
        " if (b) { b.click(); b.click(); } }"
    )
    page.wait_for_function(
        "() => document.querySelector('#chat-log').innerText.includes('已转出 300.00 元')",
        timeout=30000,
    )
    page.wait_for_timeout(900)
    after = _balance_fen(api_base, token)
    # 只数「工具结果气泡」里精确等于这句的条数：
    # done 汇总气泡会把金额再复述一遍，按子串数会把一次执行数成两三次。
    executed = page.evaluate(
        "() => Array.from(document.querySelectorAll('#chat-log .bubble-agent .bubble-body'))"
        ".filter(n => n.textContent.trim() === '已转出 300.00 元').length"
    )
    if after != before - 30_000:
        raise SystemExit(f"[幂等] 余额应该只少 300.00 元，实际 {before} → {after}（重复扣款了）")
    if executed != 1:
        raise SystemExit(f"[幂等] 成功气泡出现 {executed} 次，重复执行了")
    print(f"[幂等] 连点两次：成功气泡 1 次，余额 {before} → {after}（只扣 300.00 元）")

    # 第二笔：确认卡挂起时刷新整页
    page.fill("#chat-input", REFRESH_PROMPT)
    page.click("#chat-send")
    page.wait_for_selector(".confirm-card", timeout=30000)
    pending_text = page.inner_text(".confirm-card").replace("\n", " / ")
    page.reload(wait_until="domcontentloaded")
    try:
        # 用 wait_for_function 数节点，而不是 wait_for_selector(state="visible")：
        # 卡片在会话列表里重建时可能先挂上 DOM、后套样式，按「可见」等会白等 12 秒，
        # 结果打印出来时卡其实已经在了（上一次就是这么误报的）。
        page.wait_for_function(
            "() => document.querySelectorAll('.confirm-card').length > 0", timeout=12000
        )
        recovered = True
    except Exception as exc:  # noqa: BLE001 - 恢复不了就是缺陷，下面统一判定
        print("[刷新] 等待异常：", repr(exc))
        recovered = False
    card_after = page.inner_text(".confirm-card").replace("\n", " / ") if recovered else ""
    if not recovered:
        # 失败时先把页面上的真实内容与浏览器报错打出来，否则只看到一句「没回来」，
        # 分不清是后端没给卡、还是前端没画卡。
        print("[刷新] 页面内容：", page.inner_text("#chat-log")[:600].replace("\n", " / "))
        print("[刷新] 确认卡数量：", page.evaluate("() => document.querySelectorAll('.confirm-card').length"))
        raise SystemExit("[刷新] 刷新后确认卡没回来：SSE 一断，业务状态就找不到了")
    if "200.00" not in card_after:
        raise SystemExit("[刷新] 恢复出来的卡不是刚才那笔 200.00 元")
    # 恢复出来的卡必须落在「AI 助手」视图里：刷新后默认是财务总览，
    # 若卡被画在隐藏视图，用户点进总览的「待处理的授权」也看不到金额与收款人。
    active = page.evaluate(
        "() => { const v = document.querySelector('main.views > .view.active');"
        " return v ? v.id : ''; }"
    )
    if active != "view-assistant":
        raise SystemExit(f"[刷新] 重建的确认卡不在 AI 助手视图（当前 {active}），用户看不见")
    print("[刷新] 确认卡已恢复：", card_after[:160])
    page.evaluate(
        "() => { const c = document.querySelector('.confirm-card');"
        " if (c) { c.scrollIntoView({block: 'center'}); } }"
    )
    page.wait_for_timeout(400)
    shot = out_dir / "main_07_idempotent_refresh.png"
    # 用视口截图而不是 full_page：卡片是「当场恢复」的证据，整页截图里它会被挤到折叠线以下。
    page.screenshot(path=str(shot), full_page=False)
    print(f"[截图] {shot.relative_to(ROOT)} ({shot.stat().st_size} bytes)")
    return {
        "shot": str(shot),
        "executed_bubbles": executed,
        "balance_before_fen": before,
        "balance_after_fen": after,
        "refresh_recovered": recovered,
        "pending_card_before_reload": pending_text[:300],
        "card_after_reload": card_after[:300],
    }


def run(args: argparse.Namespace) -> int:
    from playwright.sync_api import sync_playwright  # noqa: PLC0415

    demo_db = ROOT / "data" / "bank.db"
    before = bs.sha256_file(demo_db)
    bs.build_sandbox(SANDBOX)
    bs.seed_sandbox(SANDBOX)
    proc, base_url = bs.ensure_backend(args.base_url, SANDBOX, False)
    token = bs.read_api_token(SANDBOX)
    page_url = f"{base_url}/#token={urllib.parse.quote(token)}" if token else base_url
    out_dir = Path(args.out_dir)
    out_dir = out_dir if out_dir.is_absolute() else ROOT / out_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    result: dict[str, Any] = {"base_url": base_url, "shots": {}}
    try:
        if not args.payee_only:
            seed_savings_goal(base_url, token)
            seed_plan(base_url, token)
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=not args.headed)
            if args.idem_only:
                # 只跑幂等/刷新这一段，改脚本时不用每次等前面 18 张图重拍
                ctx = browser.new_context(viewport=DESKTOP, locale="zh-CN")
                only = ctx.new_page()
                errors_only: list[str] = []
                only.on("pageerror", lambda exc: errors_only.append("idem: " + str(exc)))
                result["idempotent_refresh"] = scenario_double_click_and_refresh(
                    only, page_url, token, out_dir
                )
                browser.close()
                if errors_only:
                    print("[FAIL] 幂等/刷新流程有未捕获 JS 异常：", errors_only)
                    return 1
                print(json.dumps(result["idempotent_refresh"], ensure_ascii=False, indent=2))
                return 0
            if not args.payee_only:
                desk = browser.new_context(viewport=DESKTOP, locale="zh-CN")
                page = desk.new_page()
                errors: list[str] = []
                page.on("pageerror", lambda exc: errors.append(str(exc)))
                shots, desk_checks = shoot_views(page, page_url, "desktop", out_dir)
                mobile = browser.new_context(viewport=MOBILE, locale="zh-CN", is_mobile=True, device_scale_factor=2)
                mpage = mobile.new_page()
                mpage.on("pageerror", lambda exc: errors.append("mobile: " + str(exc)))
                mobile_shots, mobile_checks = shoot_views(mpage, page_url, "mobile", out_dir)
                shots.update(mobile_shots)
                result["shots"] = {k: str(v.relative_to(ROOT)) for k, v in shots.items()}
                result["checks"] = desk_checks + mobile_checks
                result["js_errors"] = errors
                if errors:
                    print("[FAIL] 页面有未捕获 JS 异常：", errors)
                    return 1
            if not args.views_only and not args.red_only:
                desk2 = browser.new_context(viewport=DESKTOP, locale="zh-CN")
                page2 = desk2.new_page()
                result["payee"] = scenario_payee(page2, page_url, out_dir)
            if not args.views_only:
                if not args.red_only:
                    # 收款人主线已经真转出 500 元，日累计只剩 500 元额度；
                    # 紧接着演示 2000 元转账会先撞上 R07 日累计硬线（那是正确行为，但拍不到多因子）。
                    # 所以红灯段先用一份干净沙箱重来，五张主线截图才都是各自场景的真实结果。
                    proc, base_url, token, page_url = restart_backend(proc, base_url)
                    print("[红灯] 沙箱重播 + 后端重启：日累计清零，余额回到初始值")
                desk3 = browser.new_context(viewport=DESKTOP, locale="zh-CN")
                page3 = desk3.new_page()
                errors3: list[str] = []
                page3.on("pageerror", lambda exc: errors3.append("red: " + str(exc)))
                result["red_flow"] = scenario_red_factor(page3, page_url, out_dir)
                result["daily_block"] = scenario_daily_block(page3, page_url, out_dir)
                if errors3:
                    print("[FAIL] 红灯流程有未捕获 JS 异常：", errors3)
                    return 1
                # 幂等/刷新要干净的日累计：2000 元那笔清了才好验「只扣一次」。
                # 注意顺序：先停后端再重播库。边跑边 purge 会让正在跑的 uvicorn 拿着旧文件句柄，
                # 接口直接 500（上一次就是这么挂的），重启后重读 token 才不会用到期权。
                proc, base_url, token, page_url = restart_backend(proc, base_url)
                print("[幂等] 沙箱重播 + 后端重启：日累计清零，接着验重复点击与刷新恢复")
                desk4 = browser.new_context(viewport=DESKTOP, locale="zh-CN")
                page4 = desk4.new_page()
                errors4: list[str] = []
                page4.on("pageerror", lambda exc: errors4.append("idem: " + str(exc)))
                result["idempotent_refresh"] = scenario_double_click_and_refresh(
                    page4, page_url, token, out_dir
                )
                if errors4:
                    print("[FAIL] 幂等/刷新流程有未捕获 JS 异常：", errors4)
                    return 1
            browser.close()
    finally:
        if proc is not None:
            proc.terminate()
            print("[后端] 已关闭自拉的 uvicorn")
    after = bs.sha256_file(demo_db)
    if before != after:
        print("[FAIL] 演示库 data/bank.db 被改动了！", file=sys.stderr)
        return 1
    print(f"[不变量] 演示库未被改动（sha256 {after[:16]}…）")
    report = out_dir / "browser_report.json"
    report.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[报告] {report.relative_to(ROOT)}：{len(result.get('checks') or [])} 项页面检查")
    print(json.dumps(result, ensure_ascii=False, indent=2)[:1200])
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="六区信息架构截图（桌面 + 手机 + 主线流程）")
    parser.add_argument("--base-url", default="http://127.0.0.1:8124")
    parser.add_argument("--out-dir", default=str(SHOT_DIR))
    parser.add_argument("--headed", action="store_true")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--views-only", action="store_true")
    group.add_argument("--payee-only", action="store_true")
    group.add_argument("--red-only", action="store_true")
    group.add_argument("--idem-only", action="store_true")
    return run(parser.parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
