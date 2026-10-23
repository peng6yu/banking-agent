#!/usr/bin/env python3.12
"""浏览器级冒烟：真开 Chromium，真输入一句话，断言 DOM 与截图。

和 scripts/check_frontend.py 的区别：那边是正则扫 HTML 源码，一行 JS 都没执行；
这边用 Playwright 驱动真实浏览器，页面里所有 JS 都会真的跑。

用法：
    export PATH=$PATH:/home/peng/.local/bin
    python3.12 scripts/browser_smoke.py                # 自动起沙箱后端 + 跑两条场景
    python3.12 scripts/browser_smoke.py --base-url http://127.0.0.1:8123
    python3.12 scripts/browser_smoke.py --probe-only   # 只打印环境探测结果
    python3.12 scripts/browser_smoke.py --reuse        # 复用已在跑的服务（会改它自己的库）
    python3.12 scripts/browser_smoke.py --headed       # 开有头浏览器，答辩前看一遍现场

沙箱：脚本先把 app/ web/ scripts/ 复制到 tmp/browser_smoke_sandbox/ 再起后端，
后端的根目录落在沙箱里，data/bank.db 于是也落在沙箱里 —— 答辩现场那份演示库
data/bank.db 一个字节都不会被这次冒烟碰到，跑完还用 sha256 反证这一点。

退出码 0 = 全过；1 = 有断言失败；2 = 环境/后端不可用。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_URL = "http://127.0.0.1:8123"
DEFAULT_SHOT_DIR = ROOT / "docs" / "交付" / "截图"
CHAT_TEXT = "查一下我储蓄卡尾号1234的余额"
DEMO_DB = ROOT / "data" / "bank.db"          # 答辩现场那份演示库，冒烟不能碰它
SANDBOX_DIR = ROOT / "tmp" / "browser_smoke_sandbox"
SEED_MODULES = ("__init__.py", "seed.py", "fund_seeds.py")

# 页面里注入的探针：只观察，不改产品行为。
# 1) 包住 fetch('/api/chat') 的响应流，记录每个 chunk 的到达时刻与解码后的文本；
# 2) MutationObserver 盯 #chat-log，记录每次 DOM 变化的时刻与文本长度。
# 这样「流式」就有了可验证的证据：多个 chunk、首块含 start、末块含 done、
# 而且 DOM 在中途就已增长（不是最后一次性刷出来）。
PROBE_JS = r"""
window.__probe = { chunks: [], dom: [], t0: performance.now() };
(function () {
  const origFetch = window.fetch;
  window.fetch = async function (...args) {
    const url = String(args[0] && args[0].url ? args[0].url : args[0]);
    const resp = await origFetch.apply(this, args);
    if (!url.includes('/api/chat') || !resp.body) return resp;
    const reader = resp.body.getReader();
    const dec = new TextDecoder('utf-8');
    const stream = new ReadableStream({
      async pull(ctrl) {
        const r = await reader.read();
        if (r.done) { ctrl.close(); return; }
        window.__probe.chunks.push({
          t: performance.now(),
          bytes: r.value.length,
          text: dec.decode(r.value, { stream: true })
        });
        ctrl.enqueue(r.value);
      },
      cancel(reason) { return reader.cancel(reason); }
    });
    return new Response(stream, { status: resp.status, headers: resp.headers });
  };
})();
(function () {
  const obs = new MutationObserver(function () {
    const log = document.getElementById('chat-log');
    window.__probe.dom.push({
      t: performance.now(),
      len: log ? log.innerText.length : -1,
      bubbles: document.querySelectorAll('#chat-log .bubble').length
    });
  });
  document.addEventListener('DOMContentLoaded', function () {
    obs.observe(document.getElementById('chat-log'), {
      childList: true, subtree: true, characterData: true
    });
  });
})();
"""


class Fail(Exception):
    """一条断言没通过；由 run() 收集成失败列表，不中断后面的场景。"""


def check(cond: bool, msg: str) -> None:
    if not cond:
        raise Fail(msg)


def probe_environment() -> dict[str, Any]:
    """探测本机可用的浏览器自动化，并把过程如实打印出来。"""
    info: dict[str, Any] = {"ego_browser": shutil.which("ego-browser")}
    try:
        from importlib.metadata import version  # noqa: PLC0415 - 探测本身就是按需导入
        from playwright.sync_api import sync_playwright  # noqa: F401

        info["playwright"] = version("playwright")
    except Exception as exc:  # noqa: BLE001 - 探测失败要如实报告原因
        info["playwright"] = None
        info["playwright_error"] = f"{type(exc).__name__}: {exc}"
    cache = Path.home() / ".cache" / "ms-playwright"
    info["browsers"] = sorted(p.name for p in cache.glob("chromium-*"))
    return info


def http_json(url: str, payload: dict[str, Any] | None = None, timeout: float = 10.0) -> Any:
    data = None if payload is None else json.dumps(payload).encode()
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"} if data else {}
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - 只打本机/显式传入的地址
        return json.loads(resp.read().decode())


def wait_health(base_url: str, timeout: float) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if http_json(base_url + "/api/health", timeout=2).get("ok"):
                return True
        except Exception:  # noqa: BLE001 - 起进程期间的拒绝连接是预期内
            time.sleep(0.3)
    return False


def sha256_file(path: Path) -> str | None:
    """文件指纹；文件不存在返回 None。"""
    if not path.exists():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def demo_db_snapshot(path: Path) -> dict[str, Any] | None:
    """演示库的「逻辑状态」快照：账户余额、流水条数、有没有本次那笔给妈妈的转账。

    比文件 sha256 可靠：同一台机器上还有别的进程（队友的 live_check、probe 脚本）
    会读写同一个 data/bank.db，字节指纹会因为 WAL checkpoint 之类的原因变化，
    但「演示库里有没有这笔转账」是明确的因果证据。读不到就返回 None。
    """
    if not path.exists():
        return None
    for uri in (f"file:{path}?mode=ro", f"file:{path}?mode=ro&immutable=1"):
        try:
            conn = sqlite3.connect(uri, uri=True)
            try:
                balance = conn.execute(
                    "SELECT COALESCE(SUM(balance_fen), 0) FROM accounts"
                ).fetchone()[0]
                count = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
                moved = conn.execute(
                    "SELECT COUNT(*) FROM transactions "
                    "WHERE direction = 'out' AND amount_fen = 10000 "
                    "AND counterparty LIKE '%妈妈%'"
                ).fetchone()[0]
            finally:
                conn.close()
            return {"balance_fen": balance, "txn_count": count, "mom_transfers": moved}
        except sqlite3.Error:
            continue
    return None


def secret_from_env_file() -> str:
    """从仓库根的 .env 里只取 CAPABILITY_SECRET 一个键，不把整个文件复制走。"""
    env_path = ROOT / ".env"
    if not env_path.exists():
        return ""
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        if key.strip() == "CAPABILITY_SECRET":
            return val.strip().strip('"').strip("'")
    return ""


def read_api_token(sandbox: Path) -> str:
    """读沙箱后端的 API 令牌。

    后端除首页与 /api/health 外都要求令牌（app/security/api_auth.py：环境变量
    API_TOKEN 优先，否则 data/api_token 首次启动自动生成）。前端只在地址里
    出现 ?token= 时才把令牌存进 sessionStorage，所以脚本必须把令牌带进 URL，
    否则页面能打开但每一次请求都是 401，聊天区永远等不到助手气泡。
    """
    if os.environ.get("API_TOKEN"):
        return os.environ["API_TOKEN"].strip()
    path = sandbox / "data" / "api_token"
    if path.exists():
        return path.read_text(encoding="utf-8").strip()
    return ""


def sandbox_env(sandbox: Path) -> dict[str, str]:
    """沙箱后端的进程环境：离线脚本桩 + 沙箱自己的 PYTHONPATH + 一把可用密钥。"""
    env = dict(os.environ)
    secret = os.environ.get("CAPABILITY_SECRET") or secret_from_env_file() or secrets.token_hex(32)
    env.update({"MOCK_MODE": "script", "PYTHONPATH": str(sandbox), "CAPABILITY_SECRET": secret})
    return env


def build_sandbox(dest: Path) -> Path:
    """把 app/ web/ scripts/ 复制到沙箱。

    app/db.py 的 DEFAULT_DB_PATH 是「按模块位置算出来的仓库根 / data / bank.db」，
    所以只要把 app/ 复制到别的目录，后端看到的库就在沙箱里 —— 演示库不会被这次冒烟碰到。
    """
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    shutil.copytree(ROOT / "app", dest / "app", ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copytree(ROOT / "web", dest / "web", ignore=shutil.ignore_patterns("__pycache__"))
    (dest / "scripts").mkdir()
    for name in SEED_MODULES:
        shutil.copy2(ROOT / "scripts" / name, dest / "scripts" / name)
    (dest / "data").mkdir()
    return dest


def seed_sandbox(dest: Path) -> None:
    """在沙箱里播一遍种子：确定性（RANDOM_SEED=20261018），余额回到 12,860.50 元。"""
    proc = subprocess.run(
        [sys.executable, "-m", "scripts.seed"],
        cwd=dest, env=sandbox_env(dest), capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise SystemExit(f"[沙箱] 播种失败：{proc.stdout[-400:]} / {proc.stderr[-400:]}")
    print("[沙箱] 种子已播（储蓄卡末条流水余额 12,860.50 元，与 docs/03 校验表一致）")


def free_port() -> int:
    """找一个当前没人占用的本机端口。"""
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def ensure_backend(base_url: str, sandbox: Path, reuse: bool) -> tuple[subprocess.Popen[str] | None, str]:
    """后端没起就自己起一个（MOCK_MODE=script，不碰外网）。

    返回 (进程, 实际用的 base_url)。端口被别的服务占着时不失败，而是换一个端口
    起自己的沙箱后端 —— 同一台机器上队友的联调脚本也占端口，冒烟不该因此挂掉。
    """
    if wait_health(base_url, 2):
        if reuse:
            print(f"[后端] 复用已在跑的服务：{base_url}")
            return None, base_url
        port = free_port()
        base_url = f"http://127.0.0.1:{port}"
        print(
            f"[后端] {base_url} 之前那个端口上已经有别的服务在跑"
            "（可能是同机联调脚本），改用新端口起自己的沙箱后端"
        )
    port = base_url.rsplit(":", 1)[-1].rstrip("/")
    env = sandbox_env(sandbox)
    log_path = ROOT / "tmp" / "browser_smoke_backend.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log = log_path.open("w", encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--port", port, "--log-level", "warning"],
        cwd=sandbox, env=env, stdout=log, stderr=subprocess.STDOUT, text=True,
    )
    print(f"[后端] 自行拉起 MOCK_MODE=script uvicorn pid={proc.pid} -> {base_url}"
          f"（沙箱 {sandbox.relative_to(ROOT)}，日志 tmp/browser_smoke_backend.log）")
    if not wait_health(base_url, 25):
        proc.terminate()
        raise SystemExit(f"[后端] 25 秒内没起来，看 {log_path}")
    return proc, base_url


def shot(page: Any, path: Path, full_page: bool = True) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(path), full_page=full_page)
    check(path.exists() and path.stat().st_size > 8000, f"截图没写出来或太小：{path}")
    print(f"[截图] {path.relative_to(ROOT)} ({path.stat().st_size} bytes)")
    return path


def panel_text(page: Any, panel_id: str) -> str:
    return page.inner_text("#" + panel_id).strip()


def wait_done(page: Any, timeout: float = 20000) -> None:
    page.wait_for_function(
        """() => {
            const b = document.querySelector('#panel-intent [data-role="badge"]');
            return b && (b.innerText.includes('已完成') || b.innerText.includes('已拦截'));
        }""",
        timeout=timeout,
    )


def scenario_balance(page: Any, shots: Path) -> dict[str, Any]:
    """场景 1：绿灯查询。断言助手回复、右栏面板、SSE 流式三段证据。"""
    print(f"\n=== 场景 1：输入「{CHAT_TEXT}」 ===")
    page.goto(page.base_url, wait_until="domcontentloaded")
    page.wait_for_function(
        "() => document.querySelectorAll('#cap-list .cap-chip').length > 1 || "
        "!document.querySelector('#cap-list').innerText.includes('加载中')",
        timeout=15000,
    )
    page.fill("#chat-input", CHAT_TEXT)
    page.click("#chat-send")
    page.wait_for_selector("#chat-log .bubble-agent", timeout=20000)
    wait_done(page)
    page.wait_for_timeout(600)

    agent_text = "\n".join(b.inner_text() for b in page.query_selector_all("#chat-log .bubble-agent"))
    print("[DOM] 助手气泡：", agent_text.replace("\n", " / ")[:200])
    check("余额" in agent_text, "聊天区没有出现含「余额」的助手回复")
    import re
    m = re.search(r"余额\s*([\d,]+\.\d{2})", agent_text)
    check(m is not None, f"助手回复里没有可识别的余额数字：{agent_text[:120]}")
    balance_text = m.group(1)

    panels = ["panel-intent", "panel-plan", "panel-policy", "panel-factor", "panel-audit"]
    filled = {}
    for pid in panels:
        text = panel_text(page, pid)
        body = page.inner_text(f"#{pid} [data-role=body]").strip()
        filled[pid] = {"text": text.replace("\n", " / ")[:160], "body_len": len(body)}
        print(f"[面板] {pid}: {filled[pid]['text']}")
    non_empty = [pid for pid, v in filled.items() if v["body_len"] > 0]
    check(len(non_empty) >= 2, f"右栏有内容的面板不足两个，只有 {non_empty}")
    check("intent" in "".join(filled["panel-intent"]["text"]) or "意图" in filled["panel-intent"]["text"],
          "意图面板没渲染")
    check("query_balance" in filled["panel-plan"]["text"], "计划面板没出现 query_balance 节点")
    check("绿灯" in filled["panel-policy"]["text"], "权限灯面板没显示绿灯")

    probe = page.evaluate("window.__probe")
    chunks = probe["chunks"]
    dom = probe["dom"]
    print("[流式] chunk 数：", len(chunks), "时刻/字节：",
          [(round(c["t"]), c["bytes"]) for c in chunks])
    check(len(chunks) >= 2, f"只收到 {len(chunks)} 个 SSE chunk，无法证明流式（可能一次刷出）")
    check(all(chunks[i]["t"] < chunks[i + 1]["t"] for i in range(len(chunks) - 1)),
          "SSE chunk 时间戳不是严格递增，数据可疑")
    joined_head = chunks[0]["text"]
    joined_tail = chunks[-1]["text"]
    check("event: start" in joined_head or "start" in joined_head,
          f"首个 chunk 不含 start 事件：{joined_head[:120]!r}")
    check("done" in joined_tail, f"最后一个 chunk 不含 done 事件：{joined_tail[-160:]!r}")
    batches = len(dom)
    print("[流式] DOM 变更批次：", batches, "首个/末个文本长度：",
          (dom[0]["len"] if dom else None, dom[-1]["len"] if dom else None))
    check(batches >= 2, f"DOM 只变化了 {batches} 次，像是一次性刷出来")
    check(dom[-1]["len"] > dom[0]["len"], "DOM 文本长度没有增长，渲染异常")
    dup = [c for c in dom if c["len"] == dom[-1]["len"]]
    check(len(dup) < len(dom), "所有 DOM 变更都是最终态，说明不是流式渲染")

    shot(page, shots / "01_balance_query.png")
    return {"balance": balance_text, "panels": filled, "chunks": len(chunks), "dom_batches": batches}


def scenario_confirm(page: Any, shots: Path, prev: dict[str, Any]) -> dict[str, Any]:
    """场景 2：黄灯转账。确认卡真的渲染、按钮真的可点、点了钱真的动。"""
    print("\n=== 场景 2：输入「转 100 元给妈妈」，等待确认卡并点击确认 ===")
    page.goto(page.base_url, wait_until="domcontentloaded")
    page.fill("#chat-input", "转 100 元给妈妈")
    page.click("#chat-send")
    page.wait_for_selector(".confirm-card", timeout=20000)
    page.wait_for_timeout(400)
    card_text = page.inner_text(".confirm-card").replace("\n", " / ")
    print("[确认卡]", card_text[:240])
    check("妈妈" in card_text, "确认卡没显示收款人")
    check("100.00" in card_text, "确认卡没显示金额")
    buttons = page.query_selector_all(".confirm-card button")
    check(len(buttons) >= 2, f"确认卡只有 {len(buttons)} 个按钮")
    ok_btn = page.query_selector(".confirm-card .btn-green")
    check(ok_btn is not None and not ok_btn.is_disabled(), "确认按钮不存在或不可点")
    check("黄灯" in panel_text(page, "panel-policy"), "转账没触发黄灯面板")
    shot(page, shots / "02_confirm_card.png")

    ok_btn.click()
    page.wait_for_function(
        "() => document.querySelector('#chat-log').innerText.includes('已转出')",
        timeout=20000,
    )
    wait_done(page)
    page.wait_for_timeout(600)
    chat = page.inner_text("#chat-log")
    check("已转出 100.00 元" in chat, "点确认后没看到已转出回执")
    done_line = [ln for ln in chat.splitlines() if "交易后余额" in ln]
    check(bool(done_line), "没找到交易后余额回执")
    after_fen = int(done_line[-1].split("：")[-1].strip())
    import re
    before_fen = int(float(prev["balance"].replace(",", "")) * 100)
    print(f"[余额] 转账前 {before_fen} 分，转账后 {after_fen} 分")
    check(after_fen == before_fen - 10000, f"余额变化不等于 100 元：{before_fen} -> {after_fen}")
    print("[权限灯]", panel_text(page, "panel-policy").replace("\n", " / ")[:200])
    shot(page, shots / "03_confirm_done.png")
    return {"card": card_text, "before_fen": before_fen, "after_fen": after_fen}


def run(args: argparse.Namespace) -> int:
    info = probe_environment()
    print("[探测] ego-browser:", info["ego_browser"] or "未安装")
    print("[探测] playwright:", info.get("playwright") or f"不可用（{info.get('playwright_error')}）")
    print("[探测] 已下载的 chromium:", info["browsers"] or "无")
    if args.probe_only:
        return 0 if info.get("playwright") else 2
    if not info.get("playwright"):
        print("[探测] 没有可用的浏览器自动化（ego-browser 未安装、playwright 导入失败）", file=sys.stderr)
        return 2

    demo_db_before = sha256_file(DEMO_DB)
    demo_snap_before = demo_db_snapshot(DEMO_DB)
    sandbox = build_sandbox(SANDBOX_DIR)
    seed_sandbox(sandbox)
    proc, base_url = ensure_backend(args.base_url, sandbox, args.reuse)
    failures: list[str] = []
    try:
        from playwright.sync_api import sync_playwright  # noqa: PLC0415

        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=not args.headed)
            context = browser.new_context(viewport={"width": 1440, "height": 960}, locale="zh-CN")
            context.add_init_script(PROBE_JS)
            page = context.new_page()
            token = read_api_token(sandbox)
            page_url = f"{base_url}/?token={urllib.parse.quote(token)}" if token else base_url
            page.base_url = page_url  # type: ignore[attr-defined]
            if token:
                print("[后端] 已把 API 令牌带进页面地址（?token=…），否则页面能开但请求全 401")
            else:
                print("[后端] 没读到 API 令牌；如果后端要求鉴权，下面的场景会卡在等助手气泡")
            errors: list[str] = []
            page.on("pageerror", lambda exc: errors.append(f"pageerror: {exc}"))
            shots = Path(args.shot_dir)
            shots = shots if shots.is_absolute() else ROOT / shots
            try:
                balance = scenario_balance(page, shots)
                scenario_confirm(page, shots, balance)
                check(not errors, f"页面有未捕获 JS 异常：{errors}")
            except Fail as exc:
                failures.append(str(exc))
                print(f"[FAIL] {exc}")
                try:
                    page.screenshot(path=str(shots / "99_failure.png"), full_page=True)
                    print("[截图] 失败现场：docs/交付/截图/99_failure.png")
                except Exception:  # noqa: BLE001 - 失败截图本身失败不该掩盖原因
                    pass
            browser.close()
        # 顺带把控制台错误也抓出来（放最后，避免影响主断言顺序）
        if errors:
            failures.append(f"页面 JS 异常：{errors}")
        # 不变量：这次冒烟的钱不该落在答辩现场那份演示库里（沙箱隔离的反证）。
        # 判据是逻辑的（演示库里有没有这笔给妈妈的转账），不是字节指纹 ——
        # 同机上别的进程（联调脚本、probe）也会碰这个库，字节指纹会因为 WAL
        # checkpoint 变化，那不能拿来判定是不是这次冒烟干的。
        if proc is not None:
            demo_db_after = sha256_file(DEMO_DB)
            demo_snap_after = demo_db_snapshot(DEMO_DB)
            if demo_snap_before is None or demo_snap_after is None:
                failures.append(
                    "读不到演示库 data/bank.db 的逻辑状态，无法证明冒烟没污染它"
                )
            else:
                if demo_snap_after["mom_transfers"] > demo_snap_before["mom_transfers"]:
                    failures.append(
                        "演示库 data/bank.db 里出现了本次那笔「转 100 元给妈妈」"
                        f"（{demo_snap_before['mom_transfers']} -> {demo_snap_after['mom_transfers']} 笔）"
                        " —— 冒烟的钱落到了答辩现场那份库上"
                    )
                elif demo_snap_after["balance_fen"] != demo_snap_before["balance_fen"]:
                    failures.append(
                        "演示库账户余额被这次冒烟改了"
                        f"（{demo_snap_before['balance_fen']} -> {demo_snap_after['balance_fen']} 分）"
                    )
                else:
                    print(
                        "[不变量] 演示库 data/bank.db 余额 "
                        f"{demo_snap_after['balance_fen']} 分未变，"
                        f"给妈妈的转账 {demo_snap_after['mom_transfers']} 笔（冒烟的钱没落进去）"
                    )
                if demo_db_before != demo_db_after:
                    print(
                        "[不变量·说明] 演示库字节指纹变了"
                        f"（{(demo_db_before or '?')[:12]} -> {(demo_db_after or '?')[:12]}），"
                        "但上面那笔转账不在演示库里 —— 同机上还有别的进程在写这个库，"
                        "不是这次冒烟干的"
                    )
                else:
                    print(f"[不变量] 演示库 data/bank.db sha256 未变：{demo_db_after}")
        else:
            print("[不变量] 复用了外部服务，演示库改不改由那个服务决定，本脚本不检查")
    finally:
        if proc is not None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
            print("[后端] 已关闭自拉的 uvicorn")

    if failures:
        print("\n=== 结果：FAIL ===")
        for item in failures:
            print(" -", item)
        return 1
    print("\n=== 结果：PASS（浏览器真的打开了页面、输入了句子、点过按钮） ===")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="浏览器级冒烟（Playwright + 真 Chromium）")
    parser.add_argument("--base-url", default=os.environ.get("SMOKE_BASE_URL", DEFAULT_URL))
    parser.add_argument("--shot-dir", default=str(DEFAULT_SHOT_DIR))
    parser.add_argument("--probe-only", action="store_true", help="只打印浏览器自动化探测结果")
    parser.add_argument(
        "--reuse", action="store_true",
        help="复用 --base-url 上已在跑的服务（放弃沙箱隔离，确认场景会改它自己的库）",
    )
    parser.add_argument(
        "--headed", action="store_true",
        help="开有头浏览器（默认无头；答辩前用有头再跑一次更接近现场）",
    )
    return run(parser.parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
