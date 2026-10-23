#!/usr/bin/env python3.12
"""web/index.html 静态合规检查（不依赖任何第三方库）。

用法：
    python3.12 scripts/check_frontend.py
    python3.12 scripts/check_frontend.py path/to/other.html   # 自测用：校验任意一份文件

校验项来自 docs/07_规划器_API与前端规格.md 第 7.7/7.8 节与 docs/08_编码规范.md 第 8.9 节：
DOM 结构、SSE 解析方式、零外链、三色常量、转义规范、确认卡片的金额与收款人展示。
全部通过退出码 0，任何一项不过退出码 1。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "web" / "index.html"

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

FORBIDDEN_TOKENS: tuple[str, ...] = (
    ("EventSource", "源码里不允许出现浏览器内置的事件流对象，它不支持 POST"),
    ("http://", "不允许出现外链资源引用"),
    ("https://", "不允许出现外链资源引用"),
    ("<script src", "不允许外链脚本"),
    ("<link ", "不允许外链样式表"),
    ("@import", "不允许 CSS 外链导入"),
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


def check_structure(res: Result, src: str) -> None:
    """七个双栏 DOM id 必须真的在页面里存在。"""
    for element_id in REQUIRED_IDS:
        pattern = rf'id\s*=\s*["\']{re.escape(element_id)}["\']'
        res.add(
            f"DOM id 存在：{element_id}",
            re.search(pattern, src) is not None,
            f"未找到 id=\"{element_id}\"",
        )


def check_forbidden(res: Result, src: str) -> None:
    """禁止词：外链、内置事件流对象。"""
    for token, reason in FORBIDDEN_TOKENS:
        found = token in src
        res.add(
            f"源码不含 {token!r}（{reason}）",
            not found,
            f"第 {src.find(token) + 1} 行出现" if found else "",
        )

    external = re.findall(r"""(?:src|href)\s*=\s*["']\s*//[^"']+""", src)
    res.add("没有协议相对外链资源", not external, ", ".join(external[:3]))

    css_url = re.findall(r"url\(\s*['\"]?(?!data:)", src)
    res.add("CSS 里没有 url() 引用外部资源", not css_url, f"发现 {len(css_url)} 处")


def check_sse(res: Result, src: str) -> None:
    """必须用 fetch + ReadableStream 手工解析 SSE。"""
    res.add("使用 fetch 发起请求", re.search(r"\bfetch\s*\(", src) is not None, "未找到 fetch(")
    res.add("fetch 使用 POST 方法", re.search(r"method\s*:\s*['\"]POST['\"]", src) is not None, "未找到 POST")
    res.add("使用 ReadableStream 响应体", "ReadableStream" in src, "未找到 ReadableStream")
    res.add("调用 getReader() 读取流", re.search(r"\.getReader\s*\(\s*\)", src) is not None, "未找到 getReader()")
    res.add("按 \\n\\n 切分 SSE 块", "indexOf('\\n\\n')" in src, "未找到空行切包逻辑")

    for name in SSE_EVENTS:
        res.add(
            f"处理 SSE 事件 {name}",
            re.search(rf"case\s+['\"]{name}['\"]\s*:", src) is not None,
            f"未找到 case '{name}'",
        )


def check_colors(res: Result, src: str) -> None:
    """三色常量与三色面板都要落到代码里。"""
    for color in COLORS:
        res.add(f"颜色常量 #{color} 存在", f"#{color}".lower() in src.lower(), f"缺少 #{color}")
    for css_var in COLOR_VARS:
        res.add(f"颜色变量 {css_var} 被引用", f"var({css_var})" in src, f"未出现 var({css_var})")

    for css_class in ("lv-green", "lv-amber", "lv-red"):
        res.add(f"三色面板样式 .{css_class} 定义", f".{css_class}" in src, f"缺少 .{css_class}")
    res.add(
        "颜色等级映射 green/yellow/red 齐全",
        all(f"{k}:" in src for k in ("green", "yellow", "red")),
        "LEVEL_CLASS 缺少等级键",
    )


def check_escape(res: Result, src: str) -> None:
    """escapeHtml 必须定义并被真实调用；innerHTML 路径逐条带转义。"""
    res.add("escapeHtml 已定义", re.search(r"function\s+escapeHtml\s*\(", src) is not None, "未找到定义")

    definition = re.search(r"function\s+escapeHtml\s*\(", src)
    calls = len(re.findall(r"escapeHtml\s*\(", src))
    if definition:
        calls -= 1
    res.add("escapeHtml 至少被调用一次", calls >= 1, f"实际调用 {calls} 次")

    assignments = list(re.finditer(r"\.innerHTML\s*=", src))
    res.add(
        "存在 innerHTML 模板渲染路径",
        len(assignments) >= 1,
        "一条 innerHTML 赋值都没有，这条规则无从谈起",
    )

    bare: list[str] = []
    for match in assignments:
        statement = statement_after(src, match.end())
        line_no = src.count("\n", 0, match.start()) + 1
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
        any("escapeHtml(" in statement_after(src, m.end()) for m in assignments),
        "没有任何一处 innerHTML 使用 escapeHtml",
    )

    res.add("DOM 更新使用 textContent", ".textContent" in src, "未找到 textContent")
    res.add("所有脚本包在 IIFE 里", re.search(r"\(function\s*\(\s*\)\s*\{", src) is not None, "未找到 IIFE 包裹")
    res.add("没有 var 声明（const 优先）", re.search(r"\bvar\s+[A-Za-z_$]", src) is None, "发现 var 声明")


def check_api_token(res: Result, src: str) -> None:
    """入口访问令牌：从 ?token= 读一次、存 sessionStorage、抹掉地址栏、请求自动带上。"""
    res.add(
        "从 ?token= 读取入口令牌",
        re.search(r"URLSearchParams[^\n]*\.get\(\s*['\"]token['\"]\s*\)", src) is not None,
        "未找到 new URLSearchParams(...).get('token')",
    )
    res.add("令牌存进 sessionStorage", "sessionStorage" in src, "未找到 sessionStorage")
    res.add(
        "读完后抹掉地址栏里的 token",
        "replaceState" in src,
        "未找到 history.replaceState",
    )
    res.add(
        "fetch 自动带 Authorization",
        "Authorization" in src and "Bearer " in src,
        "未找到 Authorization: Bearer",
    )


def check_contract(res: Result, src: str) -> None:
    """docs/07 第 7.7 节的函数约定与确认卡片硬性要求。"""
    for func in REQUIRED_FUNCS:
        res.add(
            f"函数 {func}() 存在",
            re.search(rf"function\s+{re.escape(func)}\s*\(", src) is not None,
            f"未找到 function {func}(",
        )

    res.add("确认卡片显式展示金额", re.search(r"金额", src) is not None, "未出现「金额」")
    res.add("确认卡片显式展示收款人", re.search(r"收款人", src) is not None, "未出现「收款人」")
    res.add(
        "确认按钮文案带金额与收款人",
        re.search(r"'确认：'\s*\+\s*amountText\s*\+\s*'\s*→\s*'\s*\+\s*payeeText", src) is not None
        or ("确认：" in src and "amountText" in src and "payeeText" in src),
        "确认按钮没有拼金额与收款人",
    )
    res.add("确认请求字段齐全", all(k in src for k in ("session_id", "confirmation_id", "approve", "factor")),
            "ConfirmRequest 字段不全")
    res.add("审计面板带 side-actions 按钮", "btn-audit-verify" in src, "缺少审计校验入口")


def main() -> int:
    res = Result()
    target = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else TARGET

    if not target.exists():
        print(f"[FAIL] 前端文件不存在：{target}", file=sys.stderr)
        return 1

    src = target.read_text(encoding="utf-8")
    res.add("web/index.html 存在且非空", len(src.strip()) > 0, f"{len(src)} 字符")
    res.add("文档声明为 HTML5", src.lstrip().lower().startswith("<!doctype html>"), "缺少 <!DOCTYPE html>")

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
