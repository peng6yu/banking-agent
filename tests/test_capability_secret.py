"""能力令牌密钥的三段取值：显式传入 / 环境变量 / 本地文件（首次运行自动生成）。

README「已知限制 1」原来写的是「CAPABILITY_SECRET 必须存在，否则确认后直接
MISSING_CAPABILITY」。那对首次上手太苛刻：拷贝仓库、装依赖、make run，第一次
点确认就撞墙。现在的语义是：

- 变量**设置了**（哪怕是空串）：只认 64 位十六进制，不合格立刻 fail closed ——
  「显式关掉」必须能被表达；
- 变量**没设置**：读 CAPABILITY_SECRET_FILE（默认 data/token_secret），没有就
  生成一把 0600 的随机密钥落盘，下次启动复用；删文件 = 轮换。
- 任何一条路都走不到硬编码默认密钥。
"""

from __future__ import annotations

import os
import stat

import pytest

from app.clock import MissingCapability
from app.security import token as tk

SECRET = "a" * 64


def _isolate(monkeypatch: pytest.MonkeyPatch, tmp_path) -> "os.PathLike[str]":
    """每个用例一套：清掉环境变量，把密钥文件指到临时目录。"""
    monkeypatch.delenv(tk.SECRET_ENV, raising=False)
    path = tmp_path / "token_secret"
    monkeypatch.setenv(tk.SECRET_FILE_ENV, str(path))
    return path


def test_first_run_generates_a_secret_file(monkeypatch, tmp_path) -> None:
    path = _isolate(monkeypatch, tmp_path)
    secret = tk.load_secret()
    assert len(secret) == tk.SECRET_HEX_LEN
    assert all(char in "0123456789abcdef" for char in secret)
    assert path.exists(), "首次运行应该把密钥落盘"
    assert path.read_text(encoding="utf-8").strip() == secret


def test_generated_secret_file_is_owner_only(monkeypatch, tmp_path) -> None:
    path = _isolate(monkeypatch, tmp_path)
    tk.load_secret()
    mode = stat.S_IMODE(path.stat().st_mode)
    assert mode == tk.SECRET_FILE_MODE, oct(mode)


def test_second_run_reuses_the_same_secret(monkeypatch, tmp_path) -> None:
    path = _isolate(monkeypatch, tmp_path)
    first = tk.load_secret()
    second = tk.load_secret()
    assert first == second
    assert path.read_text(encoding="utf-8").strip() == first


def test_env_wins_over_the_file(monkeypatch, tmp_path) -> None:
    path = _isolate(monkeypatch, tmp_path)
    path.write_text("b" * 64 + "\n", encoding="utf-8")
    monkeypatch.setenv(tk.SECRET_ENV, SECRET)
    assert tk.load_secret() == SECRET


def test_explicitly_empty_env_fails_closed(monkeypatch, tmp_path) -> None:
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setenv(tk.SECRET_ENV, "")
    with pytest.raises(MissingCapability):
        tk.load_secret()


def test_corrupt_secret_file_is_regenerated(monkeypatch, tmp_path) -> None:
    path = _isolate(monkeypatch, tmp_path)
    path.write_text("not-a-secret\n", encoding="utf-8")
    secret = tk.load_secret()
    assert secret != "not-a-secret"
    assert path.read_text(encoding="utf-8").strip() == secret


def test_unreadable_secret_file_fails_closed(monkeypatch, tmp_path) -> None:
    path = _isolate(monkeypatch, tmp_path)
    path.mkdir()  # 目录不是文件：read_text 抛 IsADirectoryError → 不许静默兜底
    with pytest.raises(MissingCapability) as excinfo:
        tk.load_secret()
    assert excinfo.value.fields.get("reason") == "CAPABILITY_SECRET_FILE_UNREADABLE"


def test_signed_token_round_trips_on_the_generated_secret(monkeypatch, tmp_path, conn) -> None:
    """自动生成的密钥能真的签发与校验：不是只写了个文件。"""
    _isolate(monkeypatch, tmp_path)
    from app.models import Action

    action = Action(tool="transfer", params={"amount_fen": 100}, account_id="a_1", amount_fen=100)
    token = tk.issue(action, level="yellow")
    assert tk.decode(token).tool == "transfer"
