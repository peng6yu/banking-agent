"""HTTP 访问令牌：把「谁能调这台 API」从部署假设变成真校验。

覆盖两件事：
1. 纯函数层——取值优先级、缺配置时 fail closed、生成文件的权限与复用、常数时间比较；
2. HTTP 层——没带令牌 401 且响应体只有 code 与人话；请求头三种带法能过，
   查询参数 ?token= 一律 401（长期令牌不该出现在访问日志里）。

HTTP 用例只走不需要数据库的端点（/api/health、/、/api/tools），
因为 TestClient 不进入 lifespan，也就不该假设库已经建好。
"""

from __future__ import annotations

import stat

import pytest
from fastapi.testclient import TestClient

from app.security import api_auth


@pytest.fixture(autouse=True)
def _isolated_token(tmp_path, monkeypatch):
    """每个用例一份干净环境：三个变量都清掉，令牌文件指到临时目录。"""
    for name in (api_auth.TOKEN_ENV, api_auth.TOKEN_FILE_ENV, api_auth.DISABLED_ENV):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(api_auth.TOKEN_FILE_ENV, str(tmp_path / "api_token"))
    yield


@pytest.fixture()
def client() -> TestClient:
    from app.main import app

    return TestClient(app)


# --------------------------------------------------------------------- 纯函数


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on", "  Yes  "])
def test_disabled_env_truthy_values(monkeypatch, value: str) -> None:
    monkeypatch.setenv(api_auth.DISABLED_ENV, value)
    assert api_auth.auth_disabled() is True


@pytest.mark.parametrize("value", ["", "0", "false", "no", "off", "之类"])
def test_disabled_env_falsy_values(monkeypatch, value: str) -> None:
    monkeypatch.setenv(api_auth.DISABLED_ENV, value)
    assert api_auth.auth_disabled() is False


def test_env_token_wins_over_file(monkeypatch) -> None:
    path = api_auth.token_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("from-file\n", encoding="utf-8")
    monkeypatch.setenv(api_auth.TOKEN_ENV, "from-env")
    assert api_auth.load_token() == "from-env"
    assert api_auth.ensure_token() == "from-env"


def test_missing_file_means_empty_token() -> None:
    assert api_auth.load_token() == ""
    assert api_auth.is_authorized("任何东西") is False


def test_ensure_token_generates_private_file_and_reuses_it() -> None:
    token = api_auth.ensure_token()
    path = api_auth.token_path()
    assert len(token) >= 32
    assert path.read_text(encoding="utf-8").strip() == token
    mode = stat.S_IMODE(path.stat().st_mode)
    assert mode == api_auth.TOKEN_FILE_MODE == 0o600
    assert api_auth.ensure_token() == token  # 第二次读文件，不重新生成


def test_ensure_token_env_does_not_write_file(monkeypatch) -> None:
    monkeypatch.setenv(api_auth.TOKEN_ENV, "explicit-token-value")
    assert api_auth.ensure_token() == "explicit-token-value"
    assert not api_auth.token_path().exists()


@pytest.mark.parametrize(
    ("presented", "expected"),
    [(None, False), ("", False), ("   ", False), ("wrong", False), ("right-token", True)],
)
def test_is_authorized_compares_exactly(presented, expected: bool, monkeypatch) -> None:
    monkeypatch.setenv(api_auth.TOKEN_ENV, "right-token")
    assert api_auth.is_authorized(presented) is expected


def test_is_authorized_fail_closed_without_config() -> None:
    assert api_auth.is_authorized("guess") is False
    assert api_auth.is_authorized("guess", token="") is False


def test_is_authorized_short_circuits_when_disabled(monkeypatch) -> None:
    monkeypatch.setenv(api_auth.DISABLED_ENV, "1")
    assert api_auth.is_authorized(None) is True
    assert api_auth.is_authorized("随便") is True


def test_mask_keeps_only_head_and_tail() -> None:
    token = "abcdefghijklmnop"
    masked = api_auth.mask(token)
    assert masked == "abcd…op"
    assert "efghijklmn" not in masked
    assert api_auth.mask("short") == "***"
    assert api_auth.mask("") == "***"


def test_bootstrap_url_carries_token_in_fragment() -> None:
    # fragment 不会随 HTTP 请求发出去，避免令牌进服务端/代理日志。
    assert api_auth.bootstrap_url("abc") == "http://127.0.0.1:8000/#token=abc"


# ------------------------------------------------------------------ HTTP 层


def test_health_and_index_stay_open(client: TestClient, monkeypatch) -> None:
    monkeypatch.setenv(api_auth.TOKEN_ENV, "right-token")
    assert client.get("/api/health").status_code == 200
    assert client.get("/").status_code == 200


def test_tools_without_token_is_401_with_plain_body(client: TestClient, monkeypatch) -> None:
    monkeypatch.setenv(api_auth.TOKEN_ENV, "right-token")
    response = client.get("/api/tools")
    assert response.status_code == 401
    body = response.json()
    assert set(body) == {"code", "user_text"}
    assert body["code"] == "UNAUTHORIZED"
    assert "令牌" in body["user_text"]


def test_bearer_works_but_query_token_is_rejected(client: TestClient, monkeypatch) -> None:
    monkeypatch.setenv(api_auth.TOKEN_ENV, "right-token")
    by_header = client.get("/api/tools", headers={"Authorization": "Bearer right-token"})
    by_query = client.get("/api/tools?token=right-token")
    assert by_header.status_code == 200
    assert by_query.status_code == 401
    assert by_query.json()["code"] == "UNAUTHORIZED"


def test_custom_header_also_accepted(client: TestClient, monkeypatch) -> None:
    monkeypatch.setenv(api_auth.TOKEN_ENV, "right-token")
    assert client.get("/api/tools", headers={"X-API-Token": "right-token"}).status_code == 200


def test_wrong_token_is_401(client: TestClient, monkeypatch) -> None:
    monkeypatch.setenv(api_auth.TOKEN_ENV, "right-token")
    response = client.post("/api/demo/reset", headers={"Authorization": "Bearer wrong"})
    assert response.status_code == 401
    assert response.json()["code"] == "UNAUTHORIZED"


def test_demo_reset_requires_token(client: TestClient, monkeypatch) -> None:
    monkeypatch.setenv(api_auth.TOKEN_ENV, "right-token")
    assert client.post("/api/demo/reset").status_code == 401


def test_disabled_flag_bypasses_token(client: TestClient, monkeypatch) -> None:
    monkeypatch.setenv(api_auth.DISABLED_ENV, "1")
    assert client.get("/api/tools").status_code == 200
