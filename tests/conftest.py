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


@pytest.fixture()
def conn(tmp_path: Path) -> sqlite3.Connection:
    """每个用例一份干净的临时库，init_db 幂等所以重复调用无副作用。"""
    connection = db.connect(tmp_path / "test.db")
    db.init_db(connection)
    try:
        yield connection
    finally:
        connection.close()
