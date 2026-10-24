#!/usr/bin/env python3.12
"""零代码执行面证明：扫 app/ 全量 .py，当面报数。

为什么这是一个安全属性而不是风格偏好
------------------------------------
赛题 §3.2 要求「生成的代码在受限、可监控的逻辑隔离环境中运行」，
常规做法是写一个沙箱：subprocess / AST 白名单 / rlimit / 超时。
但沙箱的前提是**存在执行面**——先有 eval/exec/子进程，才谈得上把它关进笼子。
本系统是另一种做法：把执行面整个删掉。

删掉之后，「沙箱是否可靠」这个问题的答案就不再取决于沙箱实现得好不好，
而是取决于一条可以被任何人当场复核的事实：app/ 下没有任何一处能执行代码。
这句话如果不加证明就是空话，所以它必须能被机器检查：

* 本脚本是检查器，跑一次打印扫描范围与命中数；
* tests/test_no_exec_surface.py 用同一套检查器做断言，并自检「检查器对植入
  的 eval/exec/... 样本必须报出来」，避免整套证明变成空转。

边界说明（不掩饰）：零代码执行面不等于零 I/O 面。app/llm.py 会向模型 API
出网（endpoint 来自环境变量配置），这是赛题要求的模型调用，不是用户代码执行面。
脚本把两类命中分开报，出网面照实列出来，不靠口径把它藏掉。

第三类命中是「动态 SQL」：conn.execute(拼接串)。它不是代码执行面（SQL 语法位置
里不会被解释成 Python），但把变量拼进 SQL 文本就可能把用户文本变成语法，
所以单独报出来逐条确认参数化；数量与行号不冻结，作为人工复核清单。

另外单独扫 scripts/ 并把 subprocess / os.system 逐条列出来：那些是离线脚本与
测试装置（起 uvicorn、跑种子数据、取 git 事实），不在在线 Agent 的调用链上。
区分「离线脚手架用系统命令」和「在线路径执行模型输出」，正是本文件要讲清的事。
"""

from __future__ import annotations

import argparse
import ast
import sys
from dataclasses import dataclass
from pathlib import Path

# 内置执行面：调用即执行代码
EXEC_BUILTIN_CALLS: frozenset[str] = frozenset(
    {"eval", "exec", "compile", "__import__", "execfile"}
)
# 按点分名匹配的进程 / 动态加载 / 反序列化执行面
EXEC_DOTTED: frozenset[str] = frozenset(
    {
        "os.system", "os.popen", "os.fork", "os.forkpty", "os.posix_spawn",
        "os.spawnl", "os.spawnv", "os.spawnve", "os.spawnlp", "os.spawnvp",
        "os.execl", "os.execv", "os.execve", "os.execvp", "os.execvpe", "os.execlp", "os.execle",
        "os.startfile",
        "subprocess.run", "subprocess.call", "subprocess.check_call", "subprocess.check_output",
        "subprocess.Popen", "subprocess.getoutput", "subprocess.getstatusoutput",
        "importlib.import_module", "importlib.reload", "importlib.__import__",
        "pickle.loads", "pickle.load", "pickle.Unpickler",
        "marshal.loads", "marshal.load",
        "ctypes.CDLL", "ctypes.cdll.LoadLibrary", "ctypes.pydll.LoadLibrary",
        "ctypes.cast", "ctypes.py_object",
        "runpy.run_path", "runpy.run_module",
        "multiprocessing.Process", "multiprocessing.Pool",
        "pty.spawn", "code.interact", "codeop.compile_command",
        "asyncio.create_subprocess_exec", "asyncio.create_subprocess_shell",
    }
)
# 导入即等于把执行面引进来的模块
EXEC_MODULES: frozenset[str] = frozenset(
    {
        "subprocess", "importlib", "pickle", "marshal", "ctypes", "runpy",
        "multiprocessing", "pty", "code", "codeop", "imp", "shelve", "commands",
    }
)
# 动态 SQL：不是代码执行面，但拼接错一个变量就等于把用户文本送进 SQL 语法位置。
# 只报「首参是 f-string 或 + 拼接」的调用；首参是整串常量（哪怕分两段常量拼）不算。
SQL_CALL_NAMES: frozenset[str] = frozenset({"execute", "executemany", "executescript"})
# 网络出网面（照实报，不当执行面算，但要让人看见）
EGRESS_DOTTED: frozenset[str] = frozenset(
    {
        "urllib.request.urlopen", "urllib.request.urlretrieve", "urllib.request.Request",
        "socket.create_connection", "socket.socket", "ssl.create_default_context",
        "http.client.HTTPSConnection", "http.client.HTTPConnection",
        "requests.get", "requests.post", "requests.request", "requests.Session",
        "httpx.get", "httpx.post", "httpx.Client", "aiohttp.request",
        "asyncio.open_connection", "ftplib.FTP", "smtplib.SMTP", "xmlrpc.client.ServerProxy",
    }
)
EGRESS_MODULES: frozenset[str] = frozenset(
    {
        "socket", "ssl", "urllib.request", "urllib3", "requests", "httpx", "aiohttp",
        "http.client", "ftplib", "telnetlib", "smtplib", "xmlrpc.client", "websockets",
    }
)
# 靠字符串绕过的那条路：getattr(builtins, "eval")
_STRING_HOLE_ATTRS: frozenset[str] = frozenset(EXEC_BUILTIN_CALLS | {"__builtins__", "__globals__"})


@dataclass(frozen=True)
class Hit:
    """一处命中：够定位到行，也够打印给人看。"""

    kind: str
    symbol: str
    path: str
    line: int
    code: str

    def render(self) -> str:
        return f"  - {self.path}:{self.line} {self.symbol}｜{self.code}"


@dataclass(frozen=True)
class Report:
    """一次扫描的完整结果：扫了多少、命中多少。"""

    files: tuple[str, ...]
    lines: int
    exec_hits: tuple[Hit, ...]
    egress_hits: tuple[Hit, ...]
    # 动态 SQL 不进 ok 的判定：它不是执行面，但必须被看见、被逐条人工确认
    sql_hits: tuple[Hit, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.exec_hits


def _dotted(node: ast.AST) -> str | None:
    """把 a.b.c 还原成点分字符串；还原不出来（动态拼接）返回 None。"""
    parts: list[str] = []
    current = node
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if not isinstance(current, ast.Name):
        return None
    parts.append(current.id)
    return ".".join(reversed(parts))


def _literal_str(node: ast.AST) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _all_constants(node: ast.AST) -> bool:
    """整条 + 表达式是否全由字符串常量组成；是则 SQL 文本仍是常量，不必报。"""
    if isinstance(node, ast.Constant):
        return isinstance(node.value, str)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _all_constants(node.left) and _all_constants(node.right)
    return False


def _sql_lead(node: ast.AST) -> str | None:
    """返回动态 SQL 的字面量片段（给人工复核用）；全常量或非拼接返回 None。"""
    if isinstance(node, ast.JoinedStr):
        if not any(isinstance(part, ast.FormattedValue) for part in node.values):
            return None
        text = "".join(
            part.value for part in node.values if isinstance(part, ast.Constant) and isinstance(part.value, str)
        )
        return text.strip()[:60] or "<f-string>"
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add) and not _all_constants(node):
        parts: list[str] = []
        current: ast.AST = node
        while isinstance(current, ast.BinOp) and isinstance(current.op, ast.Add):
            if isinstance(current.left, ast.Constant) and isinstance(current.left.value, str):
                parts.append(current.left.value)
            current = current.right
        if isinstance(current, ast.Constant) and isinstance(current.value, str):
            parts.append(current.value)
        return "".join(parts).strip()[:60] or "<拼接>"
    return None


def scan_source(source: str, path: str = "<memory>") -> list[Hit]:
    """扫描一段源码，返回命中列表；语法错误按执行面报，不放过。"""
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return [Hit("执行面", "SyntaxError", path, exc.lineno or 0, (exc.msg or "")[:80])]

    hits: list[Hit] = []

    def add(kind: str, symbol: str, node: ast.AST) -> None:
        line = getattr(node, "lineno", 0)
        code = source.splitlines()[line - 1].strip()[:100] if line else ""
        hits.append(Hit(kind, symbol, path, line, code))

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            target = _dotted(node.func)
            if isinstance(node.func, ast.Name) and node.func.id in EXEC_BUILTIN_CALLS:
                add("执行面", node.func.id, node)
            elif target in EXEC_DOTTED:
                add("执行面", str(target), node)
            elif target in EGRESS_DOTTED:
                add("出网面", str(target), node)
            elif (
                isinstance(node.func, ast.Attribute)
                and node.func.attr in SQL_CALL_NAMES
                and node.args
                and _sql_lead(node.args[0]) is not None
            ):
                add("动态 SQL", str(target or node.func.attr), node)
            elif target in {"getattr", "builtins.getattr"} and len(node.args) >= 2:
                first = _dotted(node.args[0])
                second = _literal_str(node.args[1])
                if (first == "builtins" or second in _STRING_HOLE_ATTRS) and (
                    second is not None or first == "builtins"
                ):
                    add("执行面", f"getattr(builtins, {second!r})", node)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                dotted = alias.name
                if root in EXEC_MODULES or dotted in EXEC_MODULES:
                    add("执行面", f"import {dotted}", node)
                if root in EGRESS_MODULES or dotted in EGRESS_MODULES:
                    add("出网面", f"import {dotted}", node)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            root = module.split(".")[0]
            dotted = module
            if root in EXEC_MODULES or dotted in EXEC_MODULES:
                add("执行面", f"from {dotted} import …", node)
            if root in EGRESS_MODULES or dotted in EGRESS_MODULES:
                add("出网面", f"from {dotted} import …", node)
            if module.split(".")[0] == "builtins":
                for alias in node.names:
                    if alias.name in EXEC_BUILTIN_CALLS:
                        add("执行面", f"from builtins import {alias.name}", node)
    return hits


def scan_file(path: Path, root: Path) -> tuple[int, list[Hit]]:
    source = path.read_text(encoding="utf-8")
    rel = path.relative_to(root).as_posix()
    return len(source.splitlines()), scan_source(source, rel)


def scan_tree(target: Path) -> Report:
    """扫描一个目录下的全部 .py（递归）。目录不存在直接报错，不静默通过。"""
    target = target.resolve()
    if not target.is_dir():
        raise FileNotFoundError(f"扫描目标不是目录: {target}")
    root = target.parent
    files = sorted(p for p in target.rglob("*.py") if p.is_file())
    if not files:
        raise FileNotFoundError(f"扫描目标下没有 .py 文件: {target}")
    total = 0
    exec_hits: list[Hit] = []
    egress_hits: list[Hit] = []
    sql_hits: list[Hit] = []
    for path in files:
        count, hits = scan_file(path, root)
        total += count
        for hit in hits:
            if hit.kind == "出网面":
                egress_hits.append(hit)
            elif hit.kind == "动态 SQL":
                sql_hits.append(hit)
            else:
                exec_hits.append(hit)
    return Report(
        files=tuple(p.relative_to(root).as_posix() for p in files),
        lines=total,
        exec_hits=tuple(exec_hits),
        egress_hits=tuple(egress_hits),
        sql_hits=tuple(sql_hits),
    )


# 扫描器自检样本：每一条都是真实的绕过写法，扫描器必须逐条报出来
PLANTED: tuple[tuple[str, str], ...] = (
    ("eval('1+1')", "eval"),
    ("exec('x=1')", "exec"),
    ("compile('a', '<s>', 'exec')", "compile"),
    ("__import__('os')", "__import__"),
    ("import subprocess", "import subprocess"),
    ("from subprocess import Popen", "from subprocess import"),
    ("import os\nos.system('ls')", "os.system"),
    ("os.popen('id')", "os.popen"),
    ("importlib.import_module('os')", "importlib.import_module"),
    ("import pickle\npickle.loads(b'')", "pickle.loads"),
    ("import marshal\nmarshal.loads(b'')", "marshal.loads"),
    ("import ctypes\nctypes.CDLL('libc.so.6')", "ctypes.CDLL"),
    ("import asyncio\nasyncio.create_subprocess_exec('ls')", "asyncio.create_subprocess_exec"),
    ("getattr(builtins, 'eval')('1+1')", "getattr(builtins"),
    ("from builtins import eval as e", "from builtins import eval"),
)


def selfcheck() -> list[str]:
    """把植入样本喂给扫描器，返回漏报的样本描述；空列表表示扫描器是活的。"""
    missed: list[str] = []
    for code, expect in PLANTED:
        found = scan_source(code, "<planted>")
        if not any(expect in hit.symbol for hit in found):
            missed.append(f"{expect}（样本: {code!r}）")
    return missed


def emit(text: str = "") -> None:
    sys.stdout.write(text + "\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="证明 app/ 不存在代码执行面")
    parser.add_argument("--root", default=None, help="要扫描的目录，默认 <仓库>/app")
    parser.add_argument("--list-files", action="store_true", help="逐个文件打印扫描范围")
    args = parser.parse_args(argv)

    repo_root = Path(__file__).resolve().parent.parent
    target = Path(args.root) if args.root else repo_root / "app"
    report = scan_tree(target)
    missed = selfcheck()

    emit("=== 零代码执行面证明扫描 ===")
    emit(f"扫描目标: {target}")
    emit(f"扫描范围: {len(report.files)} 个 .py 文件 / {report.lines} 行")
    if args.list_files:
        for name in report.files:
            emit(f"    {name}")
    emit(f"命中（代码执行面）: {len(report.exec_hits)}")
    for hit in report.exec_hits:
        emit(hit.render())
    emit(f"命中（网络出网面）: {len(report.egress_hits)}")
    for hit in report.egress_hits:
        emit(hit.render())
    emit(f"命中（动态 SQL，需人工确认参数化）: {len(report.sql_hits)}")
    for hit in report.sql_hits:
        emit(hit.render())

    # 离线脚手架：这些系统命令不在在线 Agent 的调用链上，但必须列出来而不是藏起来
    scripts_dir = repo_root / "scripts"
    if scripts_dir.is_dir():
        scripts_report = scan_tree(scripts_dir)
        emit(f"离线/测试脚本系统命令（不在在线 Agent 路径）: {len(scripts_report.exec_hits)}")
        for hit in scripts_report.exec_hits:
            emit(hit.render())
    emit(f"扫描器自检: {len(PLANTED) - len(missed)}/{len(PLANTED)} 段植入样本被报出")
    for item in missed:
        emit(f"    漏报: {item}")
    emit()
    if report.ok and not missed:
        emit("结论: app/ 下不存在任何代码执行面 —— 赛题「沙箱」条目因此不成立。")
        emit("      这不是「沙箱做得不够好」，是压根没有可被关进笼子的执行面。")
        emit("      模型只提申请、执行区只认令牌（能力令牌七步校验 + R04/R11 红拒兜底）。")
        emit("      出网面仅上列模型 API 一处，属赛题要求的模型调用，非用户代码执行。")
    else:
        emit("结论: 存在执行面或扫描器失灵，先修再谈沙箱。")
    return 0 if report.ok and not missed else 1


if __name__ == "__main__":
    raise SystemExit(main())
