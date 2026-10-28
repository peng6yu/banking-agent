"""一次连续录制整条演示（不再分段拼接，字幕天然不会重复）。

用法：/home/peng/.local/bin/python3.12 scripts/demo_video/record_final.py
产出：tmp/finalvideo/*.webm + tmp/cues.json（含视频内时间轴）
"""
from __future__ import annotations

import json, os, socket, sqlite3, subprocess, sys, time
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from story2 import STORY  # noqa: E402

PORT = 8470
W, H = 1440, 900
DB = ROOT / "tmp/final.db"
ARCH = ROOT / "scripts/demo_video/arch_layers.html"
VID = ROOT / "tmp/finalvideo"
META = json.loads((ROOT / "tmp/voice4/meta.json").read_text())["items"]

CURSOR_JS = """
() => {
  if (document.getElementById('__cur')) return;
  const d = document.createElement('div'); d.id = '__cur';
  d.style.cssText = 'position:fixed;left:12px;top:12px;width:22px;height:22px;z-index:2147483647;pointer-events:none;transition:left .4s cubic-bezier(.22,.61,.36,1),top .4s cubic-bezier(.22,.61,.36,1)';
  d.innerHTML = '<svg viewBox="0 0 24 24" width="22" height="22"><path d="M4 2 L20 12 L12.6 13.4 L16 21 L13 22.3 L9.6 14.6 L4 19 Z" fill="#111" stroke="#fff" stroke-width="1.6"/></svg>';
  document.documentElement.appendChild(d);
  window.__curTo = (x, y) => { const c = document.getElementById('__cur'); c.style.left = x + 'px'; c.style.top = y + 'px'; };
}
"""


def wait_port(port: int, tries: int = 150) -> bool:
    for _ in range(tries):
        s = socket.socket(); s.settimeout(0.3)
        try:
            s.connect(("127.0.0.1", port)); s.close(); return True
        except OSError:
            time.sleep(0.3)
    return False


class Take:
    def __init__(self, pw):
        self.ctx = pw.chromium.launch().new_context(
            viewport={"width": W, "height": H},
            record_video_dir=str(VID),
            record_video_size={"width": W, "height": H},
        )
        self.page = self.ctx.new_page()
        self.page.set_default_timeout(60000)
        self.t0 = 0.0
        self.cue_start = 0.0
        self.cues: list[dict] = []

    def t(self) -> float:
        return time.monotonic() - self.t0

    # ---------- 视觉小工具 ----------
    def cursor(self, x: float, y: float, pause: float = 0.35) -> None:
        try:
            self.page.evaluate(CURSOR_JS)
            self.page.mouse.move(x, y, steps=16)
            self.page.evaluate("([x, y]) => window.__curTo(x, y)", [x, y])
            self.page.wait_for_timeout(int(pause * 1000))
        except Exception:
            pass

    def glide(self, selector: str, pause: float = 0.45) -> None:
        try:
            el = self.page.query_selector(selector)
            if not el:
                return
            box = el.bounding_box()
            if box:
                self.cursor(box["x"] + box["width"] / 2, box["y"] + min(box["height"] / 2, 28), pause)
        except Exception:
            pass

    def click(self, selector: str, pause: float = 0.4) -> None:
        self.glide(selector, 0.35)
        self.page.click(selector)
        self.page.wait_for_timeout(int(pause * 1000))

    def point_last(self, selector: str, pause: float = 0.4):
        """光标移到最后一个匹配元素上；演示库里预置的旧卡会排在前面，别指着它。"""
        loc = self.page.locator(selector).last
        try:
            box = loc.bounding_box()
            if box:
                self.cursor(box["x"] + box["width"] / 2, box["y"] + min(box["height"] / 2, 28), pause)
        except Exception:
            pass
        return loc

    def scroll_to(self, phrase: str, block: str = "center", pause: float = 1.0) -> bool:
        ok = self.page.evaluate(
            """([phrase, block]) => {
                const nodes = [...document.querySelectorAll('.view.active *')];
                const el = nodes.find(e => e.children.length === 0 && (e.textContent || '').trim().startsWith(phrase));
                if (!el) return false;
                el.scrollIntoView({behavior: 'smooth', block: block});
                return true;
            }""",
            [phrase, block],
        )
        self.page.wait_for_timeout(int(pause * 1000))
        return bool(ok)

    def scroll_top(self, pause: float = 0.9) -> None:
        self.page.evaluate("() => window.scrollTo({top: 0, behavior: 'smooth'})")
        self.page.wait_for_timeout(int(pause * 1000))

    # ---------- 一轮对话 ----------
    def turn_finished(self, n0: int, timeout: int = 150000) -> None:
        self.page.wait_for_function(
            """(n) => {
                const bs = document.querySelectorAll('#chat-log .bubble-agent');
                const send = document.querySelector('#chat-send');
                return bs.length > n && send && !send.disabled;
            }""",
            arg=n0,
            timeout=timeout,
        )

    def ask(self, text: str, pause: float = 0.8) -> None:
        n0 = self.page.evaluate("() => document.querySelectorAll('#chat-log .bubble-agent').length")
        self.glide("#chat-input", 0.25)
        self.page.click("#chat-input")
        self.page.fill("#chat-input", "")
        self.page.type("#chat-input", text, delay=55)
        self.page.wait_for_timeout(500)
        self.glide("#chat-send", 0.25)
        self.page.click("#chat-send")
        self.turn_finished(n0)
        self.page.wait_for_timeout(int(pause * 1000))

    def wait_selector(self, selector: str, pause: float = 0.8, timeout: int = 120000) -> bool:
        try:
            self.page.wait_for_selector(selector, timeout=timeout)
            self.page.wait_for_timeout(int(pause * 1000))
            return True
        except Exception as err:
            print(f"!! wait_selector failed: {selector}: {err}", flush=True)
            return False

    def require(self, selector: str, label: str, timeout: int = 60000) -> None:
        """等关键元素出现；没等到就把对话末尾打出来并立刻中止本次录制。"""
        try:
            self.page.wait_for_selector(selector, timeout=timeout)
        except Exception:
            tail = self.page.inner_text("#chat-log")[-400:].replace("\n", " | ")
            print(f"!! 期望的界面没出现（{label}）：{selector}；对话末尾：{tail}", flush=True)
            raise SystemExit(2)
        self.page.wait_for_timeout(600)

    def switch(self, view: str) -> None:
        """顶层页签：AI 助手 / 账务 / 计划 / 治理。"""
        self.glide(f"#nav .nav-tab[data-view={view}]", 0.3)
        self.page.click(f"#nav .nav-tab[data-view={view}]")
        self.page.wait_for_selector(f"#view-{view}.active", timeout=30000)
        self.page.wait_for_timeout(1500)

    def sub(self, view: str) -> None:
        """账务组里的二级页签：总览 / 账单 / 业务管理。"""
        self.glide(f".subnav-tab[data-view={view}]", 0.3)
        self.page.click(f".subnav-tab[data-view={view}]")
        self.page.wait_for_selector(f"#view-{view}.active", timeout=30000)
        self.page.wait_for_timeout(1600)

    def arch(self, y: int, pause: float = 1.0) -> None:
        """切到架构分层页（本地 HTML），滚到指定位置。"""
        self.page.goto(ARCH.as_uri(), wait_until="load")
        self.page.wait_for_timeout(350)
        self.page.evaluate("(y) => window.scrollTo({top: y, behavior: 'instant'})", y)
        self.page.wait_for_timeout(int(pause * 1000))

    def arch_at(self, y: int, pause: float = 1.0) -> None:
        """架构页上直接滚到某处（不重新加载）。"""
        self.page.evaluate("(y) => window.scrollTo({top: y, behavior: 'instant'})", y)
        self.page.wait_for_timeout(int(pause * 1000))

    def pan_timed(self, key: str, ys, slack: float = 0.4) -> None:
        """按旁白长度把镜头匀速扫过若干位置，画面跟旁白同步。"""
        total = float(META[key]["dur"]) + slack
        step = total / max(1, len(ys))
        for y in ys:
            self.page.evaluate("(y) => window.scrollTo({top: y, behavior: 'smooth'})", y)
            self.page.wait_for_timeout(int(step * 1000))

    def pan(self, ys, pause: float = 1.2) -> None:
        """在架构页上平滑地来回移动，像镜头慢慢扫过这一层。"""
        for y in ys:
            self.page.evaluate("(y) => window.scrollTo({top: y, behavior: 'smooth'})", y)
            self.page.wait_for_timeout(int(pause * 1000))

    def back_app(self, view: str = "assistant") -> None:
        """回到应用页（同一标签页，sessionStorage 里的令牌还在）。"""
        self.page.goto(f"http://127.0.0.1:{PORT}/", wait_until="domcontentloaded")
        self.page.wait_for_selector("#view-assistant.active", timeout=30000)
        self.page.wait_for_timeout(1800)
        if view != "assistant":
            self.switch(view)

    def wait_audio(self, key: str, slack: float = 0.5) -> None:
        """把这一段画面撑到旁白放完，字幕才会对得上。"""
        left = float(META[key]["dur"]) + slack - (self.t() - self.cue_start)
        if left > 0:
            self.page.wait_for_timeout(int(left * 1000))


# ---------------- 分镜动作 ----------------
def a_intro(t: Take) -> None:
    t.page.wait_for_timeout(400)


def a_green(t: Take) -> None:
    t.ask("查一下我这个月花了多少")


def a_yellow(t: Take) -> None:
    n0 = t.page.evaluate("() => document.querySelectorAll('#chat-log .bubble-agent').length")
    t.glide("#chat-input", 0.25)
    t.page.click("#chat-input")
    t.page.fill("#chat-input", "")
    t.page.type("#chat-input", "帮我给张三转 800 元", delay=55)
    t.page.wait_for_timeout(400)
    t.glide("#chat-send", 0.25)
    t.page.click("#chat-send")
    t.require(".pick", "转账收款人澄清列表")
    t.glide(".pick", 0.5)


def a_yellow_pick(t: Take) -> None:
    picks = t.page.query_selector_all(".pick")
    if picks:
        t.click(".pick", 0.5)
    t.require(".confirm-card", "确认卡片", 90000)
    info = t.page.evaluate("""() => [...document.querySelectorAll('.confirm-card')].map(c => ({
        disabled: !!(c.querySelector('.btn-green') || {}).disabled,
        text: (c.innerText || '').slice(0, 40).replace(/\\n/g, ' ')
    }))""")
    print('   confirm cards:', info, flush=True)


def a_yellow_confirm(t: Take) -> None:
    n0 = t.page.evaluate("() => document.querySelectorAll('#chat-log .bubble-agent').length")
    t.point_last(".confirm-card .btn-green", 0.45).click()
    t.page.wait_for_timeout(600)
    try:
        t.turn_finished(n0, timeout=60000)
    except Exception:
        pass
    t.page.wait_for_timeout(800)


def a_red_intro(t: Take) -> None:
    n0 = t.page.evaluate("() => document.querySelectorAll('#chat-log .bubble-agent').length")
    t.glide("#chat-input", 0.25)
    t.page.click("#chat-input")
    t.page.fill("#chat-input", "")
    t.page.type("#chat-input", "帮我把卡挂失", delay=55)
    t.page.wait_for_timeout(400)
    t.glide("#chat-send", 0.25)
    t.page.click("#chat-send")
    t.require('button.cap-chip:has-text("尾号 1234")', "挂失：卡号选择按钮", 90000)
    t.glide('button.cap-chip:has-text("尾号 1234")', 0.5)


def a_red_pick(t: Take) -> None:
    t.click('button.cap-chip:has-text("尾号 1234")', 0.5)
    t.page.wait_for_timeout(300)
    t.glide("#chat-send", 0.25)
    t.page.click("#chat-send")
    t.require(".confirm-card", "确认卡片", 90000)


def a_red_confirm(t: Take) -> None:
    t.point_last(".confirm-card .btn-green", 0.45).click()
    t.require("#panel-factor input[data-challenge]", "多因子验证输入", 90000)
    t.scroll_to("多因子验证", "center", 1.1)


def a_red_factor(t: Take) -> None:
    btn = t.page.query_selector('#panel-factor button:has-text("提交验证并执行")')
    if btn:
        box = btn.bounding_box()
        if box:
            t.cursor(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2, 0.4)
        btn.click()
    t.page.wait_for_timeout(2200)
    t.scroll_to("确认这笔操作", "center", 0.8)
    t.page.wait_for_timeout(600)


def a_scope(t: Take) -> None:
    t.ask("今天天气怎么样")
    t.page.wait_for_timeout(400)


def a_overview(t: Take) -> None:
    t.switch("overview")
    t.sub("overview")
    t.scroll_top(0.8)


def a_bill(t: Take) -> None:
    t.sub("bill")
    t.scroll_top(0.7)
    t.scroll_to("异常与依据", "center", 1.2)


def a_bill_ack(t: Take) -> None:
    t.scroll_to("已核实", "center", 1.2)

def a_manage(t: Take) -> None:
    t.sub("manage")
    t.scroll_to("订阅与代扣", "center", 1.2)

def a_plans(t: Take) -> None:
    t.switch("plans")
    t.scroll_top(0.8)
    t.scroll_to("你的计划", "center", 1.0)

def a_plans2(t: Take) -> None:
    t.scroll_to("助手提醒", "center", 1.2)

def a_security(t: Take) -> None:
    t.switch("security")
    # 治理页顶部 = 最近会话 / 被拒绝的操作 / 审计链 + 最近的执行，正对旁白
    t.scroll_top(1.0)

def a_handoff(t: Take) -> None:
    t.scroll_to("人工接管队列", "center", 1.2)

def a_audit_ok(t: Take) -> None:
    t.switch("assistant")
    t.scroll_top(0.6)
    t.scroll_to("审计链", "center", 0.9)
    t.click("#btn-audit-pull", 1.4)
    t.click("#btn-audit-verify", 1.6)

def a_tamper(t: Take) -> None:
    conn = sqlite3.connect(str(DB))
    with conn:
        conn.execute("DROP TRIGGER IF EXISTS audit_no_update")
        conn.execute(
            "UPDATE audit_log SET payload_json = payload_json || ' ' "
            "WHERE seq = (SELECT MIN(seq) FROM audit_log)"
        )
    conn.close()
    t.page.wait_for_timeout(500)

def a_audit_bad(t: Take) -> None:
    t.click("#btn-audit-verify", 2.2)

def a_evidence(t: Take) -> None:
    # 架构总览页 E 段：系统自检 + 数字，正对旁白里的自检口径
    t.arch(0, 0.3)
    t.arch_at(1590, 0.4)
    t.page.wait_for_timeout(3800)
    t.arch_at(1720, 0.4)
    t.page.wait_for_timeout(3600)

def a_outro(t: Take) -> None:
    # 收尾两拍：先回应用（能开口/能办事/能留痕），再切架构页（代码层号 + 数字）
    t.switch("assistant")
    t.page.wait_for_timeout(2000)
    t.arch(0, 0.3)
    t.arch_at(1500, 0.4)
    t.page.wait_for_timeout(500)


def a_arch_layers(t: Take) -> None:
    t.arch(0, 0.4)
    t.pan_timed("arch_layers", [120, 300, 470, 620, 330])
    t.page.wait_for_timeout(250)
    t.back_app("assistant")


def a_arch_flow(t: Take) -> None:
    t.arch(0, 0.3)
    t.arch_at(760, 0.4)
    t.pan_timed("arch_flow", [660, 600, 540, 500])
    t.page.wait_for_timeout(250)
    t.back_app("assistant")


def a_arch_map(t: Take) -> None:
    t.arch(0, 0.3)
    t.arch_at(1050, 0.4)
    t.pan_timed("arch_map", [1060, 1180, 1300, 1420])
    t.page.wait_for_timeout(250)
    t.back_app("assistant")


ACTIONS = {
    "intro": a_intro, "arch_layers": a_arch_layers, "green": a_green, "yellow": a_yellow, "yellow_pick": a_yellow_pick,
    "yellow_confirm": a_yellow_confirm, "red_intro": a_red_intro, "red_pick": a_red_pick,
    "red_confirm": a_red_confirm, "red_factor": a_red_factor, "scope": a_scope,
    "overview": a_overview, "bill": a_bill, "bill_ack": a_bill_ack, "manage": a_manage,
    "plans": a_plans, "plans2": a_plans2, "security": a_security, "handoff": a_handoff,
    "audit_ok": a_audit_ok, "tamper": a_tamper, "audit_bad": a_audit_bad,
    "evidence": a_evidence, "arch_flow": a_arch_flow, "arch_map": a_arch_map,
    "outro": a_outro,
}


def main() -> None:
    for f in VID.glob("*.webm"):
        f.unlink()
    VID.mkdir(parents=True, exist_ok=True)
    subprocess.run([str(ROOT / ".venv/bin/python"), "scripts/make_demo_db.py", "--target", str(DB), "--force"], cwd=str(ROOT), stdout=subprocess.DEVNULL, check=True)

    env = dict(os.environ, BANK_DB_PATH=str(DB), API_TOKEN="rec", PYTHONUNBUFFERED="1")
    srv = subprocess.Popen(
        [str(ROOT / ".venv/bin/python"), "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(PORT)],
        cwd=str(ROOT), env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        if not wait_port(PORT):
            raise SystemExit("server not up")
        with sync_playwright() as pw:
            take = Take(pw)
            pg = take.page
            pg.goto(f"http://127.0.0.1:{PORT}/#token=rec", wait_until="domcontentloaded")
            pg.wait_for_timeout(2000)
            pg.evaluate(CURSOR_JS)
            take.t0 = time.monotonic()
            print("recording start", flush=True)
            for i, (key, text) in enumerate(STORY):
                need = META[key]["dur"] + 0.4
                start = take.t()
                take.cue_start = start
                ACTIONS[key](take)
                used = take.t() - start
                if used < need:
                    pg.wait_for_timeout(int((need - used) * 1000))
                end = take.t()
                take.cues.append({"i": i, "key": key, "text": text, "start": round(start, 3), "end": round(end, 3)})
                print(f"[{start:6.1f} → {end:6.1f}] {key}", flush=True)
            t_close = take.t()
            video = pg.video.path() if pg.video else None
            take.ctx.close()
        dur = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", video], capture_output=True, text=True).stdout.strip())
        shift = round(dur - t_close, 3)
        (ROOT / "tmp/cues.json").write_text(json.dumps({"video": video, "duration": dur, "shift": shift, "cues": take.cues}, ensure_ascii=False, indent=1))
        print(f"video={video} dur={dur:.2f}s content={t_close:.2f}s shift={shift:.2f}s", flush=True)
    finally:
        srv.terminate()
        try:
            srv.wait(timeout=15)
        except Exception:
            srv.kill()


if __name__ == "__main__":
    main()
