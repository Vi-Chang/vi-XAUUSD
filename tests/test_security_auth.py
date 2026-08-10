"""API 存取控制(Phase 1):token / session / fail-closed / 不洩露 secret。"""
from __future__ import annotations

import asyncio
import logging

import pytest

from app.config import get_settings
from app.db.session import init_db
from app import security


class FakeReq:
    def __init__(self, headers=None, cookies=None):
        self.headers = headers or {}
        self.cookies = cookies or {}


@pytest.fixture(autouse=True)
def _reset():
    init_db()
    security.reset_state_for_tests()
    s = get_settings()
    orig_token, orig_mock = s.admin_token, s.mock_data_mode
    yield
    s.admin_token, s.mock_data_mode = orig_token, orig_mock
    security.reset_state_for_tests()


def _set(token="", mock=True):
    s = get_settings()
    s.admin_token, s.mock_data_mode = token, mock


# ── constant-time token 比對 ────────────────────────────────

def test_token_matches_uses_constant_time_and_correctness():
    import inspect
    src = inspect.getsource(security.token_matches)
    assert "compare_digest" in src, "必須使用 constant-time 比對"
    _set(token="s3cret-abc")
    assert security.token_matches("s3cret-abc") is True
    assert security.token_matches("wrong") is False
    assert security.token_matches("") is False
    assert security.token_matches(None) is False


def test_token_never_matches_when_unset():
    _set(token="")
    assert security.token_matches("anything") is False


# ── require_admin 分支 ──────────────────────────────────────

def _run(coro):
    return asyncio.run(coro)


def test_require_admin_configured_rejects_without_creds():
    _set(token="tok")
    with pytest.raises(Exception) as ei:
        _run(security.require_admin(FakeReq()))
    assert getattr(ei.value, "status_code", None) == 401


def test_require_admin_configured_rejects_wrong_header():
    _set(token="tok")
    with pytest.raises(Exception) as ei:
        _run(security.require_admin(FakeReq(headers={"X-Admin-Token": "nope"})))
    assert ei.value.status_code == 401


def test_require_admin_configured_accepts_correct_header():
    _set(token="tok")
    # 不應拋例外
    _run(security.require_admin(FakeReq(headers={"X-Admin-Token": "tok"})))


def test_require_admin_accepts_valid_session_cookie():
    _set(token="tok")
    sid, _ttl = security.create_session()
    _run(security.require_admin(
        FakeReq(cookies={get_settings().admin_session_cookie: sid})))


def test_require_admin_unset_dev_open_in_mock():
    _set(token="", mock=True)
    _run(security.require_admin(FakeReq()))   # 放行


def test_require_admin_unset_fail_closed_in_production():
    _set(token="", mock=False)
    with pytest.raises(Exception) as ei:
        _run(security.require_admin(FakeReq()))
    assert ei.value.status_code == 503        # 正式環境不默認放行


# ── session 生命週期 ────────────────────────────────────────

def test_session_lifecycle():
    _set(token="tok")
    sid, ttl = security.create_session()
    assert ttl > 0 and security.session_valid(sid) is True
    security.destroy_session(sid)
    assert security.session_valid(sid) is False
    assert security.session_valid("bogus") is False


# ── 節流 ────────────────────────────────────────────────────

def test_rate_limit_blocks_second_call():
    security.rate_limit("k", 60)
    with pytest.raises(Exception) as ei:
        security.rate_limit("k", 60)
    assert ei.value.status_code == 429
    assert "Retry-After" in ei.value.headers


# ── TestClient 整合:公開讀取 vs 受保護寫入 ─────────────────

@pytest.fixture()
def client():
    from fastapi.testclient import TestClient
    from app.main import app
    with TestClient(app) as c:
        yield c


def test_public_readonly_open_without_creds(client):
    _set(token="secret", mock=True)     # 即使設了 token,讀取端點仍公開
    assert client.get("/health").status_code == 200
    assert client.get("/health/live").status_code == 200
    assert client.get("/api/candles?timeframe=15M&limit=10").status_code == 200


def test_mutation_rejected_without_token(client):
    _set(token="secret", mock=True)
    r = client.post("/api/offset", json={"value": 0.0})
    assert r.status_code == 401
    # 回應不得洩露 token
    assert "secret" not in r.text


def test_mutation_accepts_correct_header(client):
    _set(token="secret", mock=True)
    r = client.post("/api/offset", json={"value": 0.0},
                    headers={"X-Admin-Token": "secret"})
    assert r.status_code == 200
    assert "secret" not in r.text        # 回應不回傳 token


def test_login_then_cookie_authorizes(client):
    _set(token="secret", mock=True)
    lr = client.post("/api/admin/login", json={"token": "secret"})
    assert lr.status_code == 200
    assert "secret" not in lr.text       # 不回傳 token 明文
    # 登入後帶 cookie(TestClient 會自動保留)→ 寫入放行
    r = client.post("/api/offset", json={"value": 0.0})
    assert r.status_code == 200


def test_login_wrong_token_rejected(client):
    _set(token="secret", mock=True)
    assert client.post("/api/admin/login", json={"token": "bad"}).status_code == 401


def test_failed_auth_does_not_log_token(client, caplog):
    _set(token="supersecrettoken", mock=True)
    with caplog.at_level(logging.DEBUG):
        client.post("/api/offset", json={"value": 0.0},
                    headers={"X-Admin-Token": "supersecrettoken-wrong"})
    joined = "\n".join(r.getMessage() for r in caplog.records)
    assert "supersecrettoken" not in joined      # log 不得含 token


def test_analysis_run_protected_and_rate_limited(client):
    _set(token="secret", mock=True)
    assert client.post("/api/analysis/run").status_code == 401   # 受保護
    security.reset_state_for_tests()
    r1 = client.post("/api/analysis/run", headers={"X-Admin-Token": "secret"})
    assert r1.status_code == 200
    r2 = client.post("/api/analysis/run", headers={"X-Admin-Token": "secret"})
    assert r2.status_code == 429                  # 冷卻期內第二次被擋
