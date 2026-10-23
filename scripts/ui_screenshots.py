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
    """带令牌调一次后端，返回解析后的 JSON。"""
    url = base_url.rstrip("/") + path
    data = None
    headers = {"Authorization": "Bearer " + token} if token else {}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers, method="POST" if data else "GET")
    with urllib.request.urlopen(request, timeout=15) as response:  # noqa: S310 - 本机后端
        return json.loads(response.read().decode("utf-8"))


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


def shoot_views(page: Any, base_url: str, suffix: str, out_dir: Path) -> dict[str, Path]:
    out: dict[str, Path] = {}
    for view in VIEWS:
        goto_view(page, view, base_url)
        path = out_dir / f"{view}_{suffix}.png"
        page.screenshot(path=str(path), full_page=True)
        size = path.stat().st_size
        print(f"[截图] {path.relative_to(ROOT)} ({size} bytes)")
        if size < 12000:
            raise SystemExit(f"[截图] {path} 太小，页面可能是空白")
        # 键必须带 suffix：否则 desktop 的结果会被 mobile 覆盖，报告里只剩一半截图。
        out[f"{view}_{suffix}"] = path
    return out


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
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=not args.headed)
            if not args.payee_only:
                desk = browser.new_context(viewport=DESKTOP, locale="zh-CN")
                page = desk.new_page()
                errors: list[str] = []
                page.on("pageerror", lambda exc: errors.append(str(exc)))
                shots = shoot_views(page, page_url, "desktop", out_dir)
                mobile = browser.new_context(viewport=MOBILE, locale="zh-CN", is_mobile=True, device_scale_factor=2)
                mpage = mobile.new_page()
                mpage.on("pageerror", lambda exc: errors.append("mobile: " + str(exc)))
                shots.update(shoot_views(mpage, page_url, "mobile", out_dir))
                result["shots"] = {k: str(v.relative_to(ROOT)) for k, v in shots.items()}
                result["js_errors"] = errors
                if errors:
                    print("[FAIL] 页面有未捕获 JS 异常：", errors)
                    return 1
            if not args.views_only:
                desk2 = browser.new_context(viewport=DESKTOP, locale="zh-CN")
                page2 = desk2.new_page()
                result["payee"] = scenario_payee(page2, page_url, out_dir)
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
    return run(parser.parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
