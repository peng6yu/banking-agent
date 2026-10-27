"""连接卫生：每个 HTTP 请求都必须把自己开的连接关掉。

为什么单独钉这一层：改造前 app/api 里有三个各自实现的 `_conn()`，其中
handoffs_api 用 `with db.connect() as conn`——sqlite3 的 Connection 虽然是上下文
管理器，但 `__exit__` 只提交事务、**不关连接**，于是 /api/handoffs 每个请求都漏
一条连接，进程跑久了就是文件句柄耗尽。现在所有端点只走 `db.open_conn()`：

- 形状门：app/api 下不许出现 `db.connect(`，也不许再定义局部 `_conn`；
- 行为门：真发一个请求，请求结束时它开的每一条连接都必须是关的。
"""

from __future__ import annotations

import pathlib
import re
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import db
from scripts.seed import seed

API_DIR: Path = pathlib.Path(__file__).resolve().parent.parent / "app" / "api"

#: 这些端点全是只读，不需要令牌（conftest 默认关掉校验），适合当探针。
PROBE_PATHS: tuple[str, ...] = ("/api/handoffs", "/api/memory", "/api/business/overview")


class _Tracked:
    """把连接包一层，只为了记住「它到底有没有被关」。

    sqlite3.Connection 的 close 是只读属性，没法直接替换成计数版，所以走代理；
    除 close 外的所有属性透传给真连接，业务代码无感。
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        object.__setattr__(self, "_conn", conn)
        object.__setattr__(self, "closed", False)

    def __getattr__(self, name: str):
        return getattr(object.__getattribute__(self, "_conn"), name)

    def __setattr__(self, name: str, value: object) -> None:
        setattr(object.__getattribute__(self, "_conn"), name, value)

    def close(self) -> None:
        object.__setattr__(self, "closed", True)
        object.__getattribute__(self, "_conn").close()

    def __enter__(self) -> _Tracked:
        return self

    def __exit__(self, *exc: object) -> object:
        return object.__getattribute__(self, "_conn").__exit__(*exc)


@pytest.fixture()
def tracked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[list[_Tracked]]:
    """临时库 + 连接探针；种子数据先写好，避免段子里出现「没账户」的空态分支。"""
    monkeypatch.setattr(db, "DEFAULT_DB_PATH", tmp_path / "bank.db")
    seed_conn = db.connect()
    db.init_db(seed_conn)
    seed(seed_conn)
    seed_conn.close()

    opened: list[_Tracked] = []
    real_connect = db.connect

    def spy(path: object = None) -> _Tracked:
        conn = _Tracked(real_connect(path))
        opened.append(conn)
        return conn

    monkeypatch.setattr(db, "connect", spy)
    try:
        yield opened
    finally:
        for item in opened:
            if not item.closed:
                item.close()


def test_open_conn_closes_even_when_the_block_raises(tracked: list[_Tracked]) -> None:
    """报错路径也必须关：关连接是 finally，不是成功分支的福利。"""
    with pytest.raises(RuntimeError):
        with db.open_conn():
            raise RuntimeError("boom")
    assert [item.closed for item in tracked] == [True]


@pytest.mark.parametrize("path", PROBE_PATHS)
def test_read_only_requests_close_every_connection_they_open(
    tracked: list[_Tracked], path: str
) -> None:
    client = TestClient(db_app())
    client.get(path)  # 预热：启动钩子/缓存第一次跑会多开几条连接，不在测量范围内
    del tracked[:]

    response = client.get(path)

    assert response.status_code == 200, response.text
    assert tracked, f"{path} 一条连接都没开，探针可能没生效"
    leaked = [item for item in tracked if not item.closed]
    assert leaked == [], f"{path} 漏了 {len(leaked)} 条连接（请求结束仍未关）"


def test_api_modules_never_open_connections_by_hand() -> None:
    """形状门：连接只能由 db.open_conn() 提供。**

    出现 `db.connect(` 就说明有人绕过了统一入口；出现 `def _conn(` 说明又长出了
    一份「各自实现」——这两条正是这次改造前漏连接的成因。
    """
    offenders: list[str] = []
    for path in sorted(API_DIR.glob("*.py")):
        source = path.read_text(encoding="utf-8")
        if re.search(r"\bdb\.connect\(", source):
            offenders.append(f"{path.name}: 直接调用 db.connect(")
        if re.search(r"^def _conn\(", source, re.MULTILINE):
            offenders.append(f"{path.name}: 又定义了局部 _conn()")
    assert offenders == [], "端点请统一用 db.open_conn()：\n" + "\n".join(offenders)


def db_app():
    """延迟导入：app.main 会连带初始化一堆模块，放在用例里导入更好定位失败。"""
    from app.main import app

    return app
