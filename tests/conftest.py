"""pytest 全局配置：把仓库根目录放进 sys.path，并提供临时库 fixture。"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import db  # noqa: E402  （必须在 sys.path 调整之后导入）


@pytest.fixture(autouse=True)
def _http_auth_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """HTTP 访问令牌的强制校验在 tests/test_api_auth.py 里单独覆盖。

    其余用例关心的是业务行为（端点、SSE、异常映射），不是每个请求都补一遍
    令牌头；所以默认关掉校验，并清掉可能从开发机继承来的 API_TOKEN。
    test_api_auth.py 的 autouse fixture 会把这个开关删掉，照常跑真校验。
    """
    monkeypatch.delenv("API_TOKEN", raising=False)
    monkeypatch.delenv("API_TOKEN_FILE", raising=False)
    monkeypatch.setenv("API_AUTH_DISABLED", "1")


@pytest.fixture()
def conn(tmp_path: Path) -> sqlite3.Connection:
    """每个用例一份干净的临时库，init_db 幂等所以重复调用无副作用。"""
    connection = db.connect(tmp_path / "test.db")
    db.init_db(connection)
    try:
        yield connection
    finally:
        connection.close()
