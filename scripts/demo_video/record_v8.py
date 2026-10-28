"""v8 录屏：43 条 cue，开头两拍先讲结构，其余每一条都做真实操作。

v8 相对 v7 的变化：
- 开头先讲结构（arch / arch_stack）：架构页从只滚 A 段改成 A+B、C+D 全覆盖；
- 计划类功能的主入口改成"跟助手说一句话"（真发消息、真看到目标被建出来），
  表单入口留作补充；
- 回到应用的 reload 会清空对话并换一个干净会话，避免续上种子会话把对话挡住。

用法：/home/peng/.local/bin/python3.12 scripts/demo_video/record_v8.py
产出：tmp/finalvideo8/*.webm + tmp/cues.json（含视频内时间轴）
"""
from __future__ import annotations

import json, os, socket, sqlite3, subprocess, sys, time
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from story_v8 import STORY  # noqa: E402

PORT = 8470
W, H = 1440, 900
DB = ROOT / "tmp/final8.db"
ARCH = ROOT / "scripts/demo_video/arch_layers.html"
VID = ROOT / "tmp/finalvideo8"
META = json.loads((ROOT / "tmp/voice6/meta.json").read_text())["items"]
SESSION = "demo_v8"
PAD = 0.30

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

TOP_OF = {"assistant": "assistant", "overview": "overview", "bill": "overview",
           "manage": "overview", "plans": "plans", "security": "security"}
SUB_OF = {"bill": "bill", "manage": "manage"}


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

    def glide_loc(self, loc, pause: float = 0.4) -> None:
        try:
            box = loc.bounding_box()
            if box:
                self.cursor(box["x"] + box["width"] / 2, box["y"] + min(box["height"] / 2, 26), pause)
        except Exception:
            pass

    def glide(self, selector: str, pause: float = 0.45) -> None:
        try:
            el = self.page.query_selector(selector)
            if not el:
                return
            box = el.bounding_box()
            if box:
                self.cursor(box["x"] + box["width"] / 2, box["y"] + min(box["height"] / 2, 26), pause)
        except Exception:
            pass

    def click(self, selector: str, pause: float = 0.4) -> None:
        self.glide(selector, 0.3)
        try:
            self.page.click(selector, timeout=15000)
        except Exception as err:
            print(f"!! click {selector}: {err}", flush=True)
            return
        self.page.wait_for_timeout(int(pause * 1000))

    def point_last(self, selector: str, pause: float = 0.4):
        """光标移到最后一个匹配元素上（演示库里的旧卡排在前面，要指最新的那张）。"""
        loc = self.page.locator(selector).last
        try:
            loc.scroll_into_view_if_needed(timeout=8000)
            box = loc.bounding_box()
            if box:
                self.cursor(box["x"] + box["width"] / 2, box["y"] + min(box["height"] / 2, 28), pause)
        except Exception:
            pass
        return loc

    def point(self, selector: str, pause: float = 0.8) -> bool:
        """把光标移到某个元素上（先滚进视口）。"""
        try:
            loc = self.page.locator(selector).first
            loc.scroll_into_view_if_needed(timeout=8000)
            box = loc.bounding_box()
            if not box:
                return False
            y = box["y"] + min(box["height"] / 2, 26)
            if -10 <= y <= H:
                self.cursor(box["x"] + box["width"] / 2, y, pause)
            else:
                self.page.wait_for_timeout(int(pause * 1000))
            return True
        except Exception as err:
            print(f"!! point {selector}: {err}", flush=True)
            return False

    def point_text(self, scope: str, phrase: str, pause: float = 0.9) -> bool:
        """指到某块区域里含指定文字的那个元素上（列表里认卡片用）。"""
        rect = self.page.evaluate(
            """([scope, phrase]) => {
                const root = document.querySelector(scope);
                if (!root) return null;
                const els = [...root.querySelectorAll('*')].filter(
                    e => e.children.length === 0 && (e.textContent || '').includes(phrase));
                if (!els.length) return null;
                const el = els[0];
                el.scrollIntoView({behavior: 'instant', block: 'center'});
                const r = el.getBoundingClientRect();
                return {x: r.x + Math.min(r.width / 2, 220), y: r.y + r.height / 2};
            }""",
            [scope, phrase],
        )
        if not rect:
            print(f"!! point_text 没找到 {phrase!r} in {scope}", flush=True)
            return False
        self.page.wait_for_timeout(300)
        self.cursor(rect["x"], rect["y"], pause)
        return True

    def fill(self, selector: str, value: str, pause: float = 0.3) -> bool:
        try:
            self.glide(selector, 0.22)
            self.page.click(selector, timeout=15000)
            typ = self.page.eval_on_selector(selector, "e => (e.type || 'text')")
            if typ in ("date", "datetime-local", "number", "time"):
                self.page.fill(selector, value, timeout=15000)
            else:
                self.page.fill(selector, "", timeout=15000)
                self.page.type(selector, value, delay=26)
            self.page.wait_for_timeout(int(pause * 1000))
            return True
        except Exception as err:
            print(f"!! fill {selector}: {err}", flush=True)
            return False

    def choose(self, selector: str, value: str, pause: float = 0.3) -> bool:
        try:
            self.glide(selector, 0.22)
            try:
                self.page.select_option(selector, value=value, timeout=15000)
            except Exception:
                self.page.select_option(selector, label=value, timeout=15000)
            self.page.wait_for_timeout(int(pause * 1000))
            return True
        except Exception as err:
            print(f"!! choose {selector}={value}: {err}", flush=True)
            return False

    def toggle(self, selector: str, on: bool = True, pause: float = 0.3) -> bool:
        try:
            el = self.page.query_selector(selector)
            if not el:
                print(f"!! toggle missing {selector}", flush=True)
                return False
            if bool(el.is_checked()) != on:
                self.page.click(selector, timeout=15000)
            self.page.wait_for_timeout(int(pause * 1000))
            return True
        except Exception as err:
            print(f"!! toggle {selector}: {err}", flush=True)
            return False

    def click_text(self, text: str, nth: int = 0, scope: str = ".view.active", pause: float = 0.6) -> bool:
        """按可见文字点按钮/链接。"""
        sel = f'{scope} button:has-text("{text}")'
        try:
            loc = self.page.locator(sel).nth(nth)
            loc.scroll_into_view_if_needed(timeout=8000)
            self.glide_loc(loc, 0.3)
            loc.click(timeout=15000)
            self.page.wait_for_timeout(int(pause * 1000))
            return True
        except Exception as err:
            print(f"!! click_text {text!r} nth={nth} scope={scope}: {err}", flush=True)
            return False

    def click_title(self, frag: str, scope: str = ".view.active", pause: float = 0.8) -> bool:
        """按 title 属性点（牌组按钮没有文字，只有 title 话术）。"""
        sel = f'{scope} button[title*="{frag}"]'
        try:
            loc = self.page.locator(sel).first
            loc.scroll_into_view_if_needed(timeout=8000)
            self.glide_loc(loc, 0.35)
            loc.click(timeout=15000)
            self.page.wait_for_timeout(int(pause * 1000))
            return True
        except Exception as err:
            print(f"!! click_title {frag!r}: {err}", flush=True)
            return False

    def open_details(self, text: str, pause: float = 0.9) -> bool:
        """展开标题含关键字的 details（页面里的折叠块默认都是关的）。"""
        try:
            loc = self.page.locator(f'.view.active summary:has-text("{text}")').first
            is_open = loc.evaluate("e => !!e.parentElement.open")
            if not is_open:
                loc.scroll_into_view_if_needed(timeout=8000)
                self.glide_loc(loc, 0.3)
                loc.click(timeout=15000)
            self.page.wait_for_timeout(int(pause * 1000))
            return True
        except Exception as err:
            print(f"!! open_details {text!r}: {err}", flush=True)
            return False

    def open_all_details(self, scope_text: str, pause: float = 0.8) -> bool:
        """把某个 summary 关键字对应的 details 内部所有折叠块都展开。"""
        try:
            ok = self.page.evaluate(
                """(kw) => {
                    const ds = [...document.querySelectorAll('.view.active details')];
                    const d = ds.find(x => (x.querySelector('summary') || {}).textContent?.includes(kw));
                    if (!d) return false;
                    d.querySelectorAll('details').forEach(x => {
                        if (!x.open) { x.open = true; x.dispatchEvent(new Event('toggle')); }
                    });
                    return true;
                }""",
                scope_text,
            )
            self.page.wait_for_timeout(int(pause * 1000))
            return bool(ok)
        except Exception as err:
            print(f"!! open_all_details {scope_text!r}: {err}", flush=True)
            return False

    def scroll_id(self, selector: str, block: str = "center", pause: float = 0.85) -> bool:
        try:
            ok = self.page.evaluate(
                """([sel, block]) => {
                    const el = document.querySelector(sel);
                    if (!el) return false;
                    el.scrollIntoView({behavior: 'smooth', block: block});
                    return true;
                }""",
                [selector, block],
            )
            if not ok:
                print(f"!! scroll_id missing {selector}", flush=True)
            self.page.wait_for_timeout(int(pause * 1000))
            return bool(ok)
        except Exception as err:
            print(f"!! scroll_id {selector}: {err}", flush=True)
            return False

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

    def scroll_top(self, pause: float = 0.8) -> None:
        self.page.evaluate("() => window.scrollTo({top: 0, behavior: 'smooth'})")
        self.page.wait_for_timeout(int(pause * 1000))

    # ---------- 页面切换 ----------
    def switch(self, view: str) -> None:
        self.glide(f"#nav .nav-tab[data-view={view}]", 0.25)
        try:
            self.page.click(f"#nav .nav-tab[data-view={view}]", timeout=15000)
        except Exception as err:
            print(f"!! switch {view}: {err}", flush=True)
            return
        self.page.wait_for_selector(f"#view-{view}.active", timeout=30000)
        self.page.wait_for_timeout(1200)

    def sub(self, view: str) -> None:
        self.glide(f".subnav-tab[data-view={view}]", 0.25)
        try:
            self.page.click(f".subnav-tab[data-view={view}]", timeout=15000)
        except Exception as err:
            print(f"!! sub {view}: {err}", flush=True)
            return
        self.page.wait_for_selector(f"#view-{view}.active", timeout=30000)
        self.page.wait_for_timeout(1200)

    def goto_view(self, name: str) -> None:
        """不管当前在哪个页签，切到目标页。"""
        if self.page.query_selector(f"#view-{name}.active"):
            self.page.wait_for_timeout(500)
            return
        top = TOP_OF.get(name, name)
        if not self.page.query_selector(f"#view-{top}.active"):
            self.switch(top)
        if name in SUB_OF:
            self.sub(SUB_OF[name])
        self.page.wait_for_timeout(400)

    # ---------- 一轮对话 ----------
    def turn_finished(self, n0: int, timeout: int = 120000) -> None:
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
        sent = False
        for attempt in range(3):
            self.glide("#chat-input", 0.22)
            self.page.click("#chat-input", timeout=15000)
            self.page.fill("#chat-input", "")
            self.page.type("#chat-input", text, delay=45)
            self.page.wait_for_timeout(350)
            val = ""
            try:
                val = self.page.input_value("#chat-input")
            except Exception:
                val = ""
            disabled = True
            try:
                disabled = bool(self.page.eval_on_selector("#chat-send", "e => !!e.disabled"))
            except Exception:
                disabled = True
            if val.strip() and not disabled:
                self.glide("#chat-send", 0.18)
                try:
                    self.page.click("#chat-send", timeout=8000)
                    sent = True
                    break
                except Exception as err:
                    print(f"!! 发送按钮点不动（第 {attempt + 1} 次）：{err}", flush=True)
            else:
                print(f"!! 输入框没就绪（值={val!r} disabled={disabled}），重试", flush=True)
                self.page.wait_for_timeout(700)
        if not sent:
            raise RuntimeError(f"消息没发出去：{text}")
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
        try:
            self.page.wait_for_selector(selector, timeout=timeout)
        except Exception:
            try:
                tail = self.page.inner_text("#chat-log")[-400:].replace("\n", " | ")
            except Exception:
                tail = ""
            print(f"!! 期望的界面没出现（{label}）：{selector}；对话末尾：{tail}", flush=True)
            raise RuntimeError(f"界面没出现：{label}")
        self.page.wait_for_timeout(500)

    def arch(self, y: int, pause: float = 1.0) -> None:
        self.page.goto(ARCH.as_uri(), wait_until="load")
        self.page.wait_for_timeout(350)
        self.page.evaluate("(y) => window.scrollTo({top: y, behavior: 'instant'})", y)
        self.page.wait_for_timeout(int(pause * 1000))

    def arch_at(self, y: int, pause: float = 1.0) -> None:
        self.page.evaluate("(y) => window.scrollTo({top: y, behavior: 'instant'})", y)
        self.page.wait_for_timeout(int(pause * 1000))

    def pan_timed(self, key: str, ys, slack: float = 0.4) -> None:
        total = float(META[key]["dur"]) + slack
        step = total / max(1, len(ys))
        for y in ys:
            self.page.evaluate("(y) => window.scrollTo({top: y, behavior: 'smooth'})", y)
            self.page.wait_for_timeout(int(step * 1000))

    def reset_chat(self, sid: str = SESSION) -> None:
        """清空对话气泡、丢掉记住的会话号，换一个干净会话。

        页面每次重载都会去续"上次那个会话"（可能是种子会话，那轮写操作没走完，
        会把对话挡住），所以回到应用后必须显式重置。
        """
        self.page.evaluate(
            """(sid) => {
                const log = document.querySelector('#chat-log');
                if (log) { Array.from(log.children).slice(1).forEach(k => k.remove()); }
                try { window.sessionStorage.removeItem('banking_session_id'); } catch (e) {}
                try { sessionId = sid; } catch (e) {}
            }""",
            sid,
        )
        self.page.wait_for_timeout(400)

    def back_app(self, view: str = "assistant", clear: bool = True) -> None:
        self.page.goto(f"http://127.0.0.1:{PORT}/", wait_until="domcontentloaded")
        self.page.wait_for_selector("#view-assistant.active", timeout=30000)
        self.page.wait_for_timeout(2600)   # 让异步的历史/续接先跑完，再清
        if clear:
            self.reset_chat()
        if view != "assistant":
            self.switch(view)


# ---------------- 43 条分镜动作 ----------------
def a_intro(t: Take) -> None:
    t.scroll_top(0.4)
    t.point("#nav .nav-tab[data-view=assistant]", 0.7)
    t.point("#panel-intent", 0.9)
    t.point("#chat-input", 0.9)


def a_panels(t: Take) -> None:
    t.scroll_top(0.4)
    for pid in ["#panel-intent", "#panel-plan", "#panel-policy", "#panel-factor", "#panel-audit"]:
        t.point(pid, 0.95)


def a_green(t: Take) -> None:
    t.ask("查一下我这个月花了多少")
    t.point("#panel-policy", 0.9)


def a_yellow(t: Take) -> None:
    n0 = t.page.evaluate("() => document.querySelectorAll('#chat-log .bubble-agent').length")
    t.glide("#chat-input", 0.22)
    t.page.click("#chat-input", timeout=15000)
    t.page.fill("#chat-input", "")
    t.page.type("#chat-input", "帮我给张三转 800 元", delay=52)
    t.page.wait_for_timeout(400)
    t.glide("#chat-send", 0.22)
    t.page.click("#chat-send", timeout=15000)
    t.require(".pick", "转账收款人澄清列表", 90000)
    t.point(".pick", 0.6)


def a_yellow_pick(t: Take) -> None:
    picks = t.page.query_selector_all(".pick")
    if picks:
        t.click(".pick", 0.6)
    t.require(".confirm-card", "确认卡片", 90000)
    t.point(".confirm-card", 0.9)


def a_yellow_confirm(t: Take) -> None:
    n0 = t.page.evaluate("() => document.querySelectorAll('#chat-log .bubble-agent').length")
    try:
        t.point_last(".confirm-card .btn-green", 0.45).click(timeout=15000)
    except Exception as err:
        print(f"!! yellow confirm: {err}", flush=True)
    t.page.wait_for_timeout(600)
    try:
        t.turn_finished(n0, timeout=30000)
    except Exception:
        pass
    t.page.wait_for_timeout(900)


def a_red(t: Take) -> None:
    n0 = t.page.evaluate("() => document.querySelectorAll('#chat-log .bubble-agent').length")
    t.glide("#chat-input", 0.22)
    t.page.click("#chat-input", timeout=15000)
    t.page.fill("#chat-input", "")
    t.page.type("#chat-input", "帮我把卡挂失", delay=52)
    t.page.wait_for_timeout(400)
    t.glide("#chat-send", 0.22)
    t.page.click("#chat-send", timeout=15000)
    t.require('button.cap-chip:has-text("尾号 1234")', "挂失：卡号选择按钮", 90000)
    t.point('button.cap-chip:has-text("尾号 1234")', 0.7)


def a_red_pick(t: Take) -> None:
    t.click('button.cap-chip:has-text("尾号 1234")', 0.6)
    t.page.wait_for_timeout(300)
    t.glide("#chat-send", 0.22)
    t.page.click("#chat-send", timeout=15000)
    t.require(".confirm-card", "确认卡片", 90000)
    t.point(".confirm-card", 0.8)


def a_red_factor(t: Take) -> None:
    n0 = t.page.evaluate("() => document.querySelectorAll('#chat-log .bubble-agent').length")
    # 因子面板由服务端挑战渲染：先等它出来（演示环境验证码由服务端下发、自动填好）
    sel = '#panel-factor button:has-text("提交验证并执行")'
    try:
        t.page.wait_for_selector(sel, timeout=45000)
    except Exception as err:
        print(f"!! 因子面板没出来：{err}", flush=True)
    t.point("#panel-factor", 0.45)
    try:
        loc = t.page.locator(sel).last
        loc.scroll_into_view_if_needed(timeout=5000)
        t.glide_loc(loc, 0.3)
        loc.click(timeout=12000)
    except Exception as err:
        print(f"!! 提交验证并执行点不动：{err}", flush=True)
    t.page.wait_for_timeout(1500)
    try:
        t.turn_finished(n0, timeout=20000)
    except Exception:
        pass
    t.scroll_to("确认这笔操作", "center", 0.8)


def a_scope(t: Take) -> None:
    t.ask("今天天气怎么样")
    t.page.wait_for_timeout(500)


def a_overview(t: Take) -> None:
    t.goto_view("overview")
    t.scroll_top(0.6)
    t.point("#ov-balance", 0.9)
    t.scroll_id("#ov-inbox", "center", 1.0)


def a_inbox(t: Take) -> None:
    t.scroll_id("#ov-inbox", "center", 0.8)
    t.point("#ov-inbox", 0.8)
    t.click_text("去账单分析看依据", 0, "#ov-inbox", 1.4)
    t.page.wait_for_timeout(1200)
    if not t.page.query_selector("#view-bill.active"):
        t.goto_view("bill")
    t.scroll_id("#bill-anomalies", "center", 1.0)


def a_bill_summary(t: Take) -> None:
    t.goto_view("bill")
    t.scroll_id("#bill-summary", "center", 0.8)
    t.point("#bill-cats", 1.0)
    t.scroll_id("#bill-trend", "center", 1.1)


def a_bill_anomaly(t: Take) -> None:
    t.scroll_id("#bill-anomalies", "center", 1.0)
    t.point("#bill-anomalies", 0.9)
    t.scroll_to("判定规则", "center", 1.1)
    t.point("#bill-note", 0.7)


def a_bill_ack(t: Take) -> None:
    t.scroll_id("#bill-anomalies", "center", 0.9)
    t.click_text("是我自己刷的", 1, "#bill-anomalies", 1.3)
    t.scroll_id("#bill-acked", "center", 1.0)
    t.point("#bill-acked", 0.8)
    t.click_text("恢复成待核实", 0, "#bill-acked", 1.0)


def a_bill_ledger(t: Take) -> None:
    t.scroll_id("#ledger-tx", "center", 0.9)
    t.click("#ledger-more", 1.1)
    t.choose("#ledger-direction", "out", 0.5)
    t.fill("#ledger-q", "爱奇艺", 0.7)
    rows = t.page.evaluate("() => document.querySelectorAll('#ledger-tx tr.tx-row').length")
    t.toggle("#ledger-flagged", True, 0.7)
    rows2 = t.page.evaluate("() => document.querySelectorAll('#ledger-tx tr.tx-row').length")
    if rows2 == 0 and rows:
        t.toggle("#ledger-flagged", False, 0.5)
    t.point("#ledger-tx", 0.8)


def a_bill_manual(t: Take) -> None:
    t.scroll_id("#bill-manual-form", "center", 0.9)
    t.fill("#bill-manual-merchant", "楼下便利店", 0.45)
    t.fill("#bill-manual-amount", "23.50", 0.35)
    t.fill("#bill-manual-category", "餐饮", 0.35)
    t.fill("#bill-manual-note", "现金消费，刷卡账单还没进本行", 0.45)
    t.click_text("记到本行流水", 0, "#bill-manual-form", 1.5)
    t.point("#bill-manual-result", 0.8)


def a_plans_map(t: Take) -> None:
    t.goto_view("plans")
    t.scroll_top(0.5)
    for sel in ["#plan-goals", "#plan-persist", "#plan-upcoming", "#plan-reminders"]:
        t.scroll_id(sel, "center", 0.85)


def a_ai_goal(t: Take) -> None:
    """跟助手说一句话建储蓄目标（走 app/planner/assist.py 的规则快路）。"""
    t.goto_view("assistant")
    t.scroll_top(0.4)
    t.ask("帮我建一个每月存 500 元、一年内攒够 6500 元的储蓄计划，为了换投影仪", pause=1.2)
    t.point_last("#chat-log .bubble-agent", 1.0)


def a_ai_goal_calc(t: Take) -> None:
    """助手把目标算好了：金额、每月要存多少、还差多少，还给可点的下一步。"""
    t.point_last("#chat-log .bubble-agent", 1.0)
    t.point_last("#chat-log .bubble-agent .cap-chip", 0.9)


def a_goal_persist(t: Take) -> None:
    """助手建的目标落到计划页的目标列表；跨场景计划也能在这推进或取消。"""
    t.goto_view("plans")
    t.open_details("监测与目标明细", 1.4)
    # 监测面板是首屏加载的，助手刚建的目标得点一下刷新才出现在列表里。
    t.click("#btn-pf-refresh", 2.0)
    t.scroll_id("#pf-goal-list", "center", 1.0)
    if not t.point_text("#pf-goal-list", "换投影仪", 1.1):
        t.point("#pf-goal-list", 0.8)
    t.scroll_id("#plan-persist", "center", 1.0)
    if not t.point_text("#plan-persist", "推进这个计划", 1.0):
        t.point("#plan-persist", 1.0)


def a_ai_reminder(t: Take) -> None:
    """提醒同样一句话建：助手认得这是还款日，定好下一次时间和重复规则。"""
    t.goto_view("assistant")
    t.scroll_top(0.4)
    t.ask("提醒我每月 25 号还信用卡", pause=1.2)
    t.point_last("#chat-log .bubble-agent", 1.0)


def a_manual_plan(t: Take) -> None:
    """手动入口：填一次表也能建储蓄计划、授权每月划款。"""
    t.goto_view("plans")
    t.scroll_id("#panel-new-savings", "center", 0.8)
    t.open_details("新建储蓄计划", 1.2)
    t.fill("#plan-savings-name", "装修备用金", 0.45)
    t.fill("#plan-savings-target", "9000", 0.35)
    t.fill("#plan-savings-date", "2027-03-31", 0.35)
    t.choose("#plan-savings-cadence", "monthly", 0.35)
    t.fill("#plan-savings-auto", "900", 0.35)
    t.toggle("#plan-savings-auth", True, 0.35)
    t.click_text("建这个计划", 0, "#panel-new-savings", 1.8)
    t.point("#plan-savings-hint", 0.9)


def a_goal_sim(t: Take) -> None:
    t.scroll_id("#pf-sim-form", "center", 0.8)
    t.fill("#pf-sim-amount", "1800", 0.35)
    t.fill("#pf-sim-merchant", "数码城", 0.35)
    t.choose("#pf-sim-category", "购物", 0.35)
    t.fill("#pf-sim-note", "换镜头进度支出", 0.35)
    t.click_text("注入模拟支出", 0, "#pf-sim-form", 1.6)
    t.point("#pf-sim-result", 0.7)


def a_goal_alert(t: Take) -> None:
    t.scroll_id("#pf-alert-list", "center", 1.0)
    t.point("#pf-alert-list", 1.0)
    t.click_text("标记已解决", 0, "#pf-alert-list", 1.3)
    t.point("#pf-alert-list", 0.6)


def a_monitor(t: Take) -> None:
    t.scroll_id("#btn-pf-monitor", "center", 0.8)
    t.click("#btn-pf-monitor", 1.8)
    t.point("#pf-run", 1.0)


def a_upcoming(t: Take) -> None:
    t.scroll_id("#plan-upcoming", "center", 1.0)
    t.point("#plan-upcoming", 0.9)
    t.scroll_id("#plan-reminders", "center", 0.8)
    t.scroll_id("#plan-goals", "center", 0.8)


def a_mg_cards(t: Take) -> None:
    t.goto_view("manage")
    t.scroll_id("#mg-cards", "center", 0.9)
    t.open_details("冻结 / 调限额 / 挂失", 1.0)
    t.point("#mg-cards", 0.9)
    t.click_title("限额", "#mg-cards", 1.4)
    t.page.wait_for_timeout(1400)


def a_mg_subs(t: Take) -> None:
    t.goto_view("manage")
    t.scroll_id("#mg-subs", "center", 0.9)
    t.point("#mg-subs", 0.9)
    t.scroll_id("#mg-subs-inferred", "center", 1.0)
    t.point("#mg-subs-inferred", 0.9)


def a_mg_verdict(t: Take) -> None:
    t.scroll_id("#mg-subs-inferred", "center", 0.8)
    t.open_details("这条怎么定性", 1.0)
    t.click_text("确实是我订的", 0, "#mg-subs-inferred", 1.5)
    t.scroll_id("#mg-subs-inferred", "center", 0.9)
    t.point("#mg-subs-inferred", 0.9)


def a_mg_memory(t: Take) -> None:
    t.scroll_id("#mg-memory", "center", 0.9)
    t.fill("#mg-memory-key", "咖啡偏好", 0.4)
    t.fill("#mg-memory-value", "美式，不加糖", 0.4)
    t.choose("#mg-memory-kind", "preference", 0.35)
    t.click_text("记住这条", 0, "#mg-memory", 1.5)
    t.point("#mg-memory", 0.9)


def a_mg_fit(t: Take) -> None:
    t.scroll_id("#mg-suit", "center", 0.9)
    t.open_details("重新做一次风险测评", 1.3)
    t.choose("#suit-q_horizon", "1y_3y", 0.28)
    t.choose("#suit-q_experience", "mixed", 0.28)
    t.choose("#suit-q_loss", "sell_part", 0.28)
    t.choose("#suit-q_goal", "steady", 0.28)
    t.choose("#suit-q_share", "30_50", 0.28)
    t.click_text("提交测评", 0, ".view.active", 1.8)
    t.scroll_id("#mg-products", "center", 1.0)
    t.point("#mg-products", 0.9)


def a_mg_tools(t: Take) -> None:
    t.scroll_id("#mg-caps", "center", 1.0)
    t.point("#mg-caps", 1.0)
    t.scroll_id("#mg-fit", "center", 0.8)
    t.point("#mg-fit", 0.7)


def a_sec_sessions(t: Take) -> None:
    t.goto_view("security")
    t.scroll_top(0.6)
    t.scroll_id("#sec-sessions", "center", 1.0)
    t.point("#sec-sessions", 1.0)


def a_sec_denials(t: Take) -> None:
    t.scroll_id("#sec-denials", "center", 1.0)
    t.point("#sec-denials", 1.0)


def a_handoff(t: Take) -> None:
    t.scroll_id("#sec-handoff-list", "center", 1.0)
    t.click_text("看这张单的详情", 0, "#sec-handoff-list", 1.5)
    t.scroll_id("#sec-handoff-detail", "center", 1.0)
    t.click_text("我来处理", 0, "#sec-handoff-list", 1.3)


def a_audit_ok(t: Take) -> None:
    t.goto_view("assistant")
    t.scroll_id("#btn-audit-pull", "center", 0.8)
    t.click("#btn-audit-pull", 1.6)
    t.click("#btn-audit-verify", 1.8)
    t.point("#panel-audit", 1.0)


def a_tamper(t: Take) -> None:
    conn = sqlite3.connect(str(DB))
    with conn:
        conn.execute("DROP TRIGGER IF EXISTS audit_no_update")
        conn.execute(
            "UPDATE audit_log SET payload_json = payload_json || ' ' "
            "WHERE seq = (SELECT MIN(seq) FROM audit_log)"
        )
    conn.close()
    t.page.wait_for_timeout(600)


def a_audit_bad(t: Take) -> None:
    t.click("#btn-audit-verify", 2.2)
    t.point("#panel-audit", 1.0)


def a_arch(t: Take) -> None:
    """结构页第一段：运行时五层 + 三条线（A/B 段）。"""
    t.arch(0, 0.4)
    t.pan_timed("arch", [140, 420, 700, 860])
    t.page.wait_for_timeout(300)


def a_arch_stack(t: Take) -> None:
    """结构页第二段：界面五块对应哪一层 + 代码分层（C/D 段），然后回应用。"""
    if not t.page.url.startswith("file://"):
        t.arch(0, 0.4)
    t.pan_timed("arch_stack", [1040, 1250, 1430, 1600])
    t.page.wait_for_timeout(300)
    t.back_app("assistant")


def a_evidence(t: Take) -> None:
    t.arch(0, 0.3)
    t.arch_at(1590, 0.4)
    t.page.wait_for_timeout(3800)
    t.arch_at(1720, 0.4)
    t.page.wait_for_timeout(3600)


def a_outro(t: Take) -> None:
    t.back_app("assistant")
    t.page.wait_for_timeout(1500)
    t.arch(0, 0.3)
    t.arch_at(1500, 0.4)
    t.page.wait_for_timeout(3600)


ACTIONS = {
    "intro": a_intro, "arch": a_arch, "arch_stack": a_arch_stack, "panels": a_panels,
    "green": a_green, "yellow": a_yellow, "yellow_pick": a_yellow_pick,
    "yellow_confirm": a_yellow_confirm, "red": a_red, "red_pick": a_red_pick,
    "red_factor": a_red_factor, "scope": a_scope, "overview": a_overview,
    "inbox": a_inbox, "bill_summary": a_bill_summary, "bill_anomaly": a_bill_anomaly,
    "bill_ack": a_bill_ack, "bill_ledger": a_bill_ledger, "bill_manual": a_bill_manual,
    "plans_map": a_plans_map, "ai_goal": a_ai_goal, "ai_goal_calc": a_ai_goal_calc,
    "goal_persist": a_goal_persist, "ai_reminder": a_ai_reminder,
    "manual_plan": a_manual_plan, "goal_sim": a_goal_sim, "monitor": a_monitor,
    "goal_alert": a_goal_alert, "upcoming": a_upcoming, "mg_cards": a_mg_cards,
    "mg_subs": a_mg_subs, "mg_verdict": a_mg_verdict, "mg_memory": a_mg_memory,
    "mg_fit": a_mg_fit, "mg_tools": a_mg_tools, "sec_sessions": a_sec_sessions,
    "sec_denials": a_sec_denials, "handoff": a_handoff, "audit_ok": a_audit_ok,
    "tamper": a_tamper, "audit_bad": a_audit_bad, "evidence": a_evidence,
    "outro": a_outro,
}


def main() -> None:
    missing = [k for k, _ in STORY if k not in ACTIONS]
    if missing:
        raise SystemExit(f"ACTIONS 缺分镜: {missing}")
    for f in VID.glob("*.webm"):
        f.unlink()
    VID.mkdir(parents=True, exist_ok=True)
    subprocess.run([str(ROOT / ".venv/bin/python"), "scripts/make_demo_db.py", "--target", str(DB), "--force"],
                   cwd=str(ROOT), stdout=subprocess.DEVNULL, check=True)

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
            pg.wait_for_timeout(3200)
            pg.evaluate(CURSOR_JS)
            # 开新会话：首屏别续种子会话（那轮没走完的写操作会把对话挡住），
            # 也把回放出来的历史气泡清掉，只留欢迎语。
            take.reset_chat(SESSION)
            print("新会话:", SESSION, flush=True)
            take.t0 = time.monotonic()
            print("recording start", flush=True)
            for i, (key, text) in enumerate(STORY):
                need = META[key]["dur"] + PAD
                start = take.t()
                take.cue_start = start
                try:
                    ACTIONS[key](take)
                except Exception as err:
                    print(f"!! cue {key} 出错: {err}", flush=True)
                used = take.t() - start
                if used < need:
                    pg.wait_for_timeout(int((need - used) * 1000))
                end = take.t()
                take.cues.append({"i": i, "key": key, "text": text, "start": round(start, 3), "end": round(end, 3)})
                print(f"[{start:6.1f} → {end:6.1f}] ({used:5.1f}/{need:5.1f}) {key}", flush=True)
            t_close = take.t()
            video = pg.video.path() if pg.video else None
            take.ctx.close()
        dur = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", video],
                                   capture_output=True, text=True).stdout.strip())
        shift = round(dur - t_close, 3)
        (ROOT / "tmp/cues.json").write_text(json.dumps(
            {"video": video, "duration": dur, "shift": shift, "cues": take.cues}, ensure_ascii=False, indent=1))
        print(f"video={video} dur={dur:.2f}s content={t_close:.2f}s shift={shift:.2f}s", flush=True)
    finally:
        srv.terminate()
        try:
            srv.wait(timeout=15)
        except Exception:
            srv.kill()


if __name__ == "__main__":
    main()