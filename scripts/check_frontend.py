#!/usr/bin/env python3.12
"""web/ 前端静态合规检查（不依赖任何第三方库）。

用法：
    python3.12 scripts/check_frontend.py
    python3.12 scripts/check_frontend.py path/to/other.html   # 自测用：只校验一份 HTML（单文件模式）

校验项来自 docs/07_规划器_API与前端规格.md 第 7.7/7.8 节与 docs/08_编码规范.md 第 8.9 节：
DOM 结构、SSE 解析方式、零外链、三色常量、转义规范、确认卡片的金额与收款人展示。

前端从「单文件」拆成「壳 + 样式 + 模块脚本」之后（W5），检查对象是四类文件：
    web/index.html      结构（只放 DOM，不许内联 <style> / <script>）
    web/app.css         样式（全站唯一一份，含三色变量）
    web/js/*.js         17 个经典脚本，按文件名序加载，共享一个全局作用域
所以这里先把它们读成三份文本（html / css / js），再按项检查；同一份规则不再重复写。
全部通过退出码 0，任何一项不过退出码 1。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WEB_DIR = ROOT / "web"
TARGET = WEB_DIR / "index.html"
CSS = WEB_DIR / "app.css"
JS_DIR = WEB_DIR / "js"

#: 每个文件的物理行数上限（与 scripts/arch_report.py 的 FILE_LINE_LIMIT 同口径）。
FILE_LINE_LIMIT = 600

REQUIRED_IDS: tuple[str, ...] = (
    "chat-log",
    "chat-input",
    "panel-intent",
    "panel-plan",
    "panel-policy",
    "panel-factor",
    "panel-audit",
)

COLORS: tuple[str, ...] = ("16a34a", "d97706", "dc2626")

COLOR_VARS: tuple[str, ...] = ("--green", "--amber", "--red")

SSE_EVENTS: tuple[str, ...] = (
    "start",
    "intent",
    "trace",
    "confirm",
    "factor",
    "tool",
    "done",
    "error",
)

REQUIRED_FUNCS: tuple[str, ...] = (
    "sendMessage",
    "handleEvent",
    "renderIntent",
    "renderPlan",
    "renderPolicy",
    "renderFactor",
    "renderAudit",
    "confirmAction",
    "escapeHtml",
)

#: 禁止词：外链与浏览器内置事件流对象。同源 /static 的 <script src>、<link> 另有专项检查，
#: 所以不在这张表里——但任何带协议或协议相对的地址仍然一律禁止。
FORBIDDEN_TOKENS: tuple[str, ...] = (
    ("EventSource", "源码里不允许出现浏览器内置的事件流对象，它不支持 POST"),
    ("http://", "不允许出现外链资源引用"),
    ("https://", "不允许出现外链资源引用"),
    ("@import", "不允许 CSS 外链导入"),
    ("//cdn", "不允许 CDN 资源"),
)


class Result:
    """收集每条校验的结果，最后统一汇报。"""

    def __init__(self) -> None:
        self.rows: list[tuple[str, bool, str]] = []

    def add(self, name: str, ok: bool, detail: str = "") -> bool:
        self.rows.append((name, bool(ok), detail))
        return bool(ok)

    @property
    def failed(self) -> list[tuple[str, bool, str]]:
        return [row for row in self.rows if not row[1]]

    def report(self) -> int:
        for name, ok, detail in self.rows:
            print(f"[PASS] {name}" if ok else f"[FAIL] {name} — {detail}")
        if self.failed:
            print(f"\n前端检查未通过：{len(self.failed)}/{len(self.rows)} 项失败", file=sys.stderr)
            return 1
        print(f"\n前端检查通过：{len(self.rows)}/{len(self.rows)} 项全部达标")
        return 0


class Sources:
    """三份源文本 + 文件清单；单文件模式下 js/css 为空。"""

    def __init__(self, html: str, css: str, js: str, files: list[Path]) -> None:
        self.html = html
        self.css = css
        self.js = js
        self.files = files


def load_sources(target: Path) -> Sources:
    """读入四类文件；target 不是默认壳文件时退回单文件模式（只校验这一份）。"""
    if target != TARGET or not CSS.exists():
        return Sources(target.read_text(encoding="utf-8"), "", "", [target])
    js_files = sorted(JS_DIR.glob("*.js"))
    css = CSS.read_text(encoding="utf-8")
    js = "\n".join(path.read_text(encoding="utf-8") for path in js_files)
    return Sources(
        target.read_text(encoding="utf-8"),
        css,
        js,
        [target, *js_files, CSS],
    )


def statement_after(src: str, start: int, limit: int = 4000) -> str:
    """取出 .innerHTML = 之后的那条语句（按括号深度和引号状态找分号）。"""
    depth = 0
    quote = ""
    i = start
    end = min(len(src), start + limit)
    while i < end:
        ch = src[i]
        if quote:
            if ch == "\\":
                i += 2
                continue
            if ch == quote:
                quote = ""
        elif ch in "'\"":
            quote = ch
        elif ch in "([{":
            depth += 1
        elif ch in ")]}":
            if depth == 0:
                break
            depth -= 1
        elif ch == ";" and depth == 0:
            break
        i += 1
    return src[start:i]


def unsafe_tokens(statement: str) -> list[str]:
    """innerHTML 右侧允许出现的只有：字符串字面量、escapeHtml(...) 调用与字符串拼接。

    去掉字面量和 escapeHtml(...) 之后，残留里再出现标识符或别的运算符就是没转义的后端值。
    """
    chars: list[str] = []
    quote = ""
    i = 0
    while i < len(statement):
        ch = statement[i]
        if quote:
            if ch == "\\":
                i += 2
                continue
            if ch == quote:
                quote = ""
        elif ch in "'\"":
            quote = ch
        else:
            chars.append(ch)
        i += 1
    cleaned = "".join(chars)

    while True:
        at = cleaned.find("escapeHtml(")
        if at == -1:
            break
        depth = 0
        j = at + len("escapeHtml")
        while j < len(cleaned):
            if cleaned[j] == "(":
                depth += 1
            elif cleaned[j] == ")":
                depth -= 1
                if depth == 0:
                    break
            j += 1
        cleaned = cleaned[:at] + cleaned[j + 1:]

    residue = re.sub(r"[\s+()]", "", cleaned)
    return re.findall(r"[A-Za-z_$][\w$]*|[^+\s()]", residue)


def check_files(res: Result, src: Sources) -> None:
    """文件层：每个文件都不超限，壳里不许再内联样式与脚本。"""
    for path in src.files:
        lines = len(path.read_text(encoding="utf-8").splitlines())
        res.add(
            f"{path.relative_to(ROOT).as_posix()} ≤ {FILE_LINE_LIMIT} 行",
            lines <= FILE_LINE_LIMIT,
            f"实际 {lines} 行",
        )

    res.add("web/index.html 存在且非空", len(src.html.strip()) > 0, f"{len(src.html)} 字符")
    res.add("文档声明为 HTML5", src.html.lstrip().lower().startswith("<!doctype html>"), "缺少 <!DOCTYPE html>")
    res.add("壳里没有内联 <style> 块", "<style" not in src.html.lower(), "样式必须放 web/app.css")
    res.add(
        "壳里没有内联脚本",
        re.search(r"<script(?![^>]*\bsrc=)", src.html, re.I) is None,
        "脚本必须放 web/js/*.js",
    )
    res.add("web/app.css 存在且非空", len(src.css.strip()) > 0, "缺少样式表")


def check_static_refs(res: Result, src: Sources) -> None:
    """引用层：所有 src/href 都指向同源 /static，引到的文件真实存在，脚本按序加载。"""
    refs = re.findall(r"""(?:src|href)\s*=\s*["'](?P<url>[^"']+)["']""", src.html)
    outside = [url for url in refs if not url.startswith("/static/") and not url.startswith("#")]
    res.add(
        "所有 src/href 都指向同源 /static（没有 CDN 与协议相对地址）",
        not outside,
        f"越界引用：{outside[:3]}",
    )

    missing = [
        url for url in refs
        if url.startswith("/static/") and not (WEB_DIR / url[len("/static/"):]).is_file()
    ]
    res.add("引用的静态文件都真实存在", not missing, f"缺文件：{missing[:3]}")

    tokens = [token for token, _ in FORBIDDEN_TOKENS]
    hits = [token for token in tokens if token in src.html]
    res.add("壳里没有外链资源引用", not hits, f"出现：{hits[:3]}")

    scripts = re.findall(r"""<script\s+src\s*=\s*["']([^"']+)["']\s*>\s*</script>""", src.html)
    expected = sorted(f"/static/js/{path.name}" for path in JS_DIR.glob("*.js"))
    res.add(
        f"{len(scripts)} 个脚本按文件名序加载（顺序即依赖顺序）",
        scripts == expected,
        f"页面里的顺序：{[Path(s).name for s in scripts][:5]}…",
    )


def check_structure(res: Result, src: Sources) -> None:
    """七个双栏 DOM id 必须真的在页面里存在。"""
    for element_id in REQUIRED_IDS:
        pattern = rf'id\s*=\s*["\']{re.escape(element_id)}["\']'
        res.add(
            f"DOM id 存在：{element_id}",
            re.search(pattern, src.html) is not None,
            f'未找到 id="{element_id}"',
        )


def check_forbidden(res: Result, src: Sources) -> None:
    """禁止词：外链、内置事件流对象。逐份文件扫，能定位到具体文件。"""
    for token, reason in FORBIDDEN_TOKENS:
        spots = [
            path.relative_to(ROOT).as_posix()
            for path in src.files
            if token in path.read_text(encoding="utf-8")
        ]
        res.add(f"源码不含 {token!r}（{reason}）", not spots, f"出现在 {spots[:3]}")

    external = re.findall(r"""(?:src|href)\s*=\s*["']\s*//[^"']+""", src.html)
    res.add("没有协议相对外链资源", not external, ", ".join(external[:3]))

    css_url = re.findall(r"url\(\s*['\"]?(?!data:)", src.css)
    res.add("CSS 里没有 url() 引用外部资源", not css_url, f"发现 {len(css_url)} 处")


def check_sse(res: Result, src: Sources) -> None:
    """必须用 fetch + ReadableStream 手工解析 SSE。"""
    js = src.js
    res.add("使用 fetch 发起请求", re.search(r"\bfetch\s*\(", js) is not None, "未找到 fetch(")
    res.add("fetch 使用 POST 方法", re.search(r"method\s*:\s*['\"]POST['\"]", js) is not None, "未找到 POST")
    res.add("使用 ReadableStream 响应体", "ReadableStream" in js, "未找到 ReadableStream")
    res.add("调用 getReader() 读取流", re.search(r"\.getReader\s*\(\s*\)", js) is not None, "未找到 getReader()")
    res.add("按 \\n\\n 切分 SSE 块", "indexOf('\\n\\n')" in js, "未找到空行切包逻辑")

    for name in SSE_EVENTS:
        res.add(
            f"处理 SSE 事件 {name}",
            re.search(rf"case\s+['\"]{name}['\"]\s*:", js) is not None,
            f"未找到 case '{name}'",
        )


def check_colors(res: Result, src: Sources) -> None:
    """三色常量与三色面板都要落到代码里（变量在 CSS，等级映射在 JS）。"""
    for color in COLORS:
        res.add(f"颜色常量 #{color} 存在", f"#{color}".lower() in src.css.lower(), f"缺少 #{color}")
    for css_var in COLOR_VARS:
        res.add(f"颜色变量 {css_var} 被引用", f"var({css_var})" in src.css, f"未出现 var({css_var})")

    for css_class in ("lv-green", "lv-amber", "lv-red"):
        res.add(f"三色面板样式 .{css_class} 定义", f".{css_class}" in src.css, f"缺少 .{css_class}")
    res.add(
        "颜色等级映射 green/yellow/red 齐全",
        all(f"{k}:" in src.js for k in ("green", "yellow", "red")),
        "LEVEL_CLASS 缺少等级键",
    )


def check_escape(res: Result, src: Sources) -> None:
    """escapeHtml 必须定义并被真实调用；innerHTML 路径逐条带转义。"""
    js = src.js
    res.add("escapeHtml 已定义", re.search(r"function\s+escapeHtml\s*\(", js) is not None, "未找到定义")

    calls = len(re.findall(r"escapeHtml\s*\(", js))
    definition = re.search(r"function\s+escapeHtml\s*\(", js)
    if definition:
        calls -= 1
    res.add("escapeHtml 至少被调用一次", calls >= 1, f"实际调用 {calls} 次")

    assignments = list(re.finditer(r"\.innerHTML\s*=", js))
    res.add(
        "存在 innerHTML 模板渲染路径",
        len(assignments) >= 1,
        "一条 innerHTML 赋值都没有，这条规则无从谈起",
    )

    bare: list[str] = []
    for match in assignments:
        statement = statement_after(js, match.end())
        line_no = js.count("\n", 0, match.start()) + 1
        leftovers = unsafe_tokens(statement)
        if leftovers:
            bare.append(f"第 {line_no} 行残留 {leftovers[:3]}")
    res.add(
        "innerHTML 赋值只拼 escapeHtml 后的字符串",
        not bare,
        f"共 {len(assignments)} 处赋值，违规：{bare}",
    )
    res.add(
        "innerHTML 赋值确实调用了 escapeHtml",
        any("escapeHtml(" in statement_after(js, m.end()) for m in assignments),
        "没有任何一处 innerHTML 使用 escapeHtml",
    )

    res.add("DOM 更新使用 textContent", ".textContent" in js, "未找到 textContent")
    res.add("没有 var 声明（const 优先）", re.search(r"\bvar\s+[A-Za-z_$]", js) is None, "发现 var 声明")

    js_files = [path for path in src.files if path.suffix == ".js"]
    bad = [
        path.name
        for path in js_files
        if not path.read_text(encoding="utf-8").lstrip().startswith("/*")
    ]
    res.add("每个 js 文件都有块注释开头（说明这块负责什么）", not bad, f"缺注释：{bad[:3]}")


def check_api_token(res: Result, src: Sources) -> None:
    """入口访问令牌：从 #token= 读一次、存 sessionStorage、抹掉地址栏、请求自动带上。"""
    js = src.js
    res.add(
        "从 #token= 读取入口令牌（fragment 不进服务端日志）",
        re.search(r"location\.hash[^\n]*#token=", js) is not None,
        "未找到 location.hash 里的 #token=",
    )
    res.add("令牌存进 sessionStorage", "sessionStorage" in js, "未找到 sessionStorage")
    res.add("读完后抹掉地址栏里的 token", "replaceState" in js, "未找到 history.replaceState")
    res.add(
        "fetch 自动带 Authorization",
        "Authorization" in js and "Bearer " in js,
        "未找到 Authorization: Bearer",
    )


def check_contract(res: Result, src: Sources) -> None:
    """docs/07 第 7.7 节的函数约定与确认卡片硬性要求。"""
    js = src.js
    for func in REQUIRED_FUNCS:
        res.add(
            f"函数 {func}() 存在",
            re.search(rf"function\s+{re.escape(func)}\s*\(", js) is not None,
            f"未找到 function {func}(",
        )

    res.add("确认卡片显式展示金额", re.search(r"金额", js) is not None, "未出现「金额」")
    res.add("确认卡片显式展示收款人", re.search(r"收款人", js) is not None, "未出现「收款人」")
    res.add(
        "确认按钮文案带金额与收款人",
        re.search(r"'确认：'\s*\+\s*amountText\s*\+\s*'\s*→\s*'\s*\+\s*payeeText", js) is not None
        or ("确认：" in js and "amountText" in js and "payeeText" in js),
        "确认按钮没有拼金额与收款人",
    )
    res.add(
        "确认请求字段齐全",
        all(k in js for k in ("session_id", "confirmation_id", "approve", "factor")),
        "ConfirmRequest 字段不全",
    )
    res.add("审计面板带 side-actions 按钮", "btn-audit-verify" in js, "缺少审计校验入口")


def main() -> int:
    res = Result()
    target = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else TARGET

    if not target.exists():
        print(f"[FAIL] 前端文件不存在：{target}", file=sys.stderr)
        return 1

    src = load_sources(target)

    check_files(res, src)
    check_static_refs(res, src)
    check_structure(res, src)
    check_forbidden(res, src)
    check_sse(res, src)
    check_colors(res, src)
    check_escape(res, src)
    check_api_token(res, src)
    check_contract(res, src)

    return res.report()


if __name__ == "__main__":
    sys.exit(main())
