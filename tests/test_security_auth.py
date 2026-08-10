"""API 存取控制(Phase 1 + 稽核強化):env-based fail-closed / CSRF / 登入防暴力 /
session 上限與過期 / 不洩露 secret。"""
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
    saved = (s.admin_token, s.mock_data_mode, s.app_env,
             s.allow_unauthenticated_mutations, s.max_admin_sessions,
             s.admin_login_max_attempts, s.admin_session_ttl_minutes,
             s.min_admin_token_length)
    yield
    (s.admin_token, s.mock_data_mode, s.app_env,
     s.allow_unauthenticated_mutations, s.max_admin_sessions,
     s.admin_login_max_attempts, s.admin_session_ttl_minutes,
     s.min_admin_token_length) = saved
    security.reset_state_for_tests()
    # 清理:本檔的 /api/analysis/run 會寫入共享排程狀態,避免污染其他測試(如 test_tiered)
    from app.services.scheduler import state as _sched_state
    _sched_state.latest_result = None
    _sched_state.last_full_analysis = None


def _set(token="", env="test", allow_unauth=False):
    s = get_settings()
    s.admin_token, s.app_env, s.allow_unauthenticated_mutations = token, env, allow_unauth


def _run(coro):
    return asyncio.run(coro)


# ── constant-time token 比對 ────────────────────────────────

def test_token_matches_constant_time_and_correctness():
    import inspect
    assert "compare_digest" in inspect.getsource(security.token_matches)
    _set(token="s3cret-abc")
    assert security.token_matches("s3cret-abc") is True
    assert security.token_matches("wrong") is False
    assert security.token_matches("s3cret-ab") is False    # 長度不同也安全回 False
    assert security.token_matches("") is False
    assert security.token_matches(None) is False


# ── 環境判定:fail-closed 不靠 mock_data_mode ───────────────

def test_production_mock_true_no_token_denied():
    """production + mock=true + 無 token → 仍不得放行(不靠 mock 判斷)。"""
    _set(token="", env="production")
    get_settings().mock_data_mode = True         # 即使 mock 也不放行
    with pytest.raises(Exception) as ei:
        _run(security.require_admin(FakeReq()))
    assert ei.value.status_code == 503


def test_production_with_token_enforced_and_works():
    _set(token="tok", env="production")
    with pytest.raises(Exception) as ei:
        _run(security.require_admin(FakeReq()))          # 無憑證 → 401
    assert ei.value.status_code == 401
    _run(security.require_admin(FakeReq(headers={"X-Admin-Token": "tok"})))  # 正確 → 通過


def test_development_no_token_allows():
    _set(token="", env="development")
    _run(security.require_admin(FakeReq()))              # development 放行


def test_test_env_no_token_allows():
    _set(token="", env="test")
    _run(security.require_admin(FakeReq()))              # test 放行


def test_unknown_blank_misspelled_app_env_defaults_to_production():
    """未知/空/拼錯 APP_ENV → validator 正規化為 production(fail-closed)。"""
    from app.config import Settings
    for bad in ("garbage", "", "prod", "Production ", "production", "dev"):
        assert Settings(app_env=bad).app_env == "production", bad
    for good in ("development", "test", "production"):
        assert Settings(app_env=good).app_env == good


def test_allow_flag_does_NOT_open_production():
    """回歸:APP_ENV=production 時,ALLOW_UNAUTHENTICATED_MUTATIONS=true 也不得放行。"""
    _set(token="", env="production", allow_unauth=True)
    with pytest.raises(Exception) as ei:
        _run(security.require_admin(FakeReq()))          # 仍拒絕未授權
    assert ei.value.status_code == 503
    # 且視為組態錯誤
    bad, why = security.production_auth_misconfigured()
    assert bad and why == "allow_unauthenticated_mutations_set_in_production"


def test_allow_flag_opens_only_in_dev_test():
    for env in ("development", "test"):
        _set(token="", env=env, allow_unauth=True)
        _run(security.require_admin(FakeReq()))          # dev/test 放行(本就放行)
        assert security.production_auth_misconfigured()[0] is False


def test_production_token_too_short_is_misconfigured():
    s = get_settings()
    s.app_env, s.allow_unauthenticated_mutations = "production", False
    s.min_admin_token_length = 32
    s.admin_token = "short"                              # < 32 → 弱憑證
    bad, why = security.production_auth_misconfigured()
    assert bad and why == "admin_token_too_short"
    s.admin_token = "x" * 32                             # 達門檻 → OK
    assert security.production_auth_misconfigured()[0] is False


def test_production_token_missing_flag():
    _set(token="", env="production")
    assert security.production_token_missing() is True
    _set(token="y" * 40, env="production")
    assert security.production_token_missing() is False
    _set(token="", env="development")
    assert security.production_token_missing() is False


def test_misconfig_reason_does_not_leak_token_length_or_content():
    s = get_settings()
    s.app_env, s.admin_token, s.min_admin_token_length = "production", "abc123secret", 32
    _bad, why = security.production_auth_misconfigured()
    assert "abc123secret" not in why and "12" not in why and "len" not in why.lower()


# ── header vs session + CSRF/Origin ─────────────────────────

def test_session_cookie_requires_same_origin():
    _set(token="tok", env="production")
    sid, _ = security.create_session()
    cookie = {get_settings().admin_session_cookie: sid}
    # 跨站 Origin + session cookie → 403(CSRF 阻擋)
    with pytest.raises(Exception) as ei:
        _run(security.require_admin(FakeReq(
            headers={"host": "vi-xauusd.zeabur.app", "origin": "https://evil.com"},
            cookies=cookie)))
    assert ei.value.status_code == 403
    # 同源 Origin → 通過
    _run(security.require_admin(FakeReq(
        headers={"host": "vi-xauusd.zeabur.app", "origin": "https://vi-xauusd.zeabur.app"},
        cookies=cookie)))


def test_missing_origin_with_session_rejected_but_header_token_ok():
    _set(token="tok", env="production")
    sid, _ = security.create_session()
    # session cookie 但完全無 Origin/Referer → 拒絕(瀏覽器 POST 必送 Origin)
    with pytest.raises(Exception) as ei:
        _run(security.require_admin(FakeReq(
            headers={"host": "h"}, cookies={get_settings().admin_session_cookie: sid})))
    assert ei.value.status_code == 403
    # 但 header-token(curl,無 Origin)仍可用 → 通過
    _run(security.require_admin(FakeReq(headers={"host": "h", "X-Admin-Token": "tok"})))


def test_header_token_not_subject_to_origin_check():
    _set(token="tok", env="production")
    # 即使帶跨站 Origin,header-token 路徑不受 CSRF 檢查(非 cookie,無法被 CSRF 濫用)
    _run(security.require_admin(FakeReq(
        headers={"host": "h", "origin": "https://evil.com", "X-Admin-Token": "tok"})))


# ── session 生命週期 / 上限 / 過期 ─────────────────────────

def test_session_lifecycle_and_destroy():
    _set(token="tok")
    sid, ttl = security.create_session()
    assert ttl > 0 and security.session_valid(sid) is True
    security.destroy_session(sid)
    assert security.session_valid(sid) is False
    assert security.session_valid("bogus") is False


def test_session_expiry():
    _set(token="tok")
    get_settings().admin_session_ttl_minutes = 0     # → ttl 秒數最小,立即過期
    sid, _ = security.create_session()
    # ttl=max(1,0)*60=60s 其實不會馬上過期;改為手動塞過期時間驗證清理
    import datetime as dt
    with security._sessions_lock:
        security._sessions[sid] = dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=1)
    assert security.session_valid(sid) is False       # 過期 → 清掉


def test_session_store_capacity_evicts_oldest():
    _set(token="tok")
    get_settings().max_admin_sessions = 5
    sids = [security.create_session()[0] for _ in range(8)]
    assert security.session_count() <= 5              # 不超過上限(防記憶體 DoS)
    # 最舊的已被淘汰,最新的仍有效
    assert security.session_valid(sids[-1]) is True


def test_session_id_is_high_entropy():
    _set(token="tok")
    sid, _ = security.create_session()
    assert len(sid) >= 32                              # secrets.token_urlsafe(32)


# ── 節流:登入防暴力 + 分析冷卻(不同 bucket)──────────────

def test_login_rate_limit_window():
    security.reset_state_for_tests()
    for _ in range(5):
        security.rate_limit_window("admin_login", 5, 60)
    with pytest.raises(Exception) as ei:
        security.rate_limit_window("admin_login", 5, 60)   # 第 6 次被擋
    assert ei.value.status_code == 429 and "Retry-After" in ei.value.headers


def test_cooldown_and_login_use_different_buckets():
    security.reset_state_for_tests()
    security.rate_limit("analysis_run", 60)
    # 登入 bucket 獨立,不受 analysis cooldown 影響
    security.rate_limit_window("admin_login", 5, 60)


# ── TestClient 整合 ─────────────────────────────────────────

@pytest.fixture()
def client():
    from fastapi.testclient import TestClient
    from app.main import app
    with TestClient(app) as c:
        yield c


def test_public_readonly_open_without_creds(client):
    _set(token="secret", env="test")
    assert client.get("/health").status_code == 200
    assert client.get("/health/live").status_code == 200
    assert client.get("/api/candles?timeframe=15M&limit=10").status_code == 200


def test_mutation_rejected_without_token(client):
    _set(token="secret", env="test")
    r = client.post("/api/offset", json={"value": 0.0})
    assert r.status_code == 401 and "secret" not in r.text


def test_mutation_accepts_correct_header(client):
    _set(token="secret", env="test")
    r = client.post("/api/offset", json={"value": 0.0}, headers={"X-Admin-Token": "secret"})
    assert r.status_code == 200 and "secret" not in r.text


def test_login_sets_hardened_cookie_and_authorizes(client):
    _set(token="secret", env="test")
    lr = client.post("/api/admin/login", json={"token": "secret"})
    assert lr.status_code == 200 and "secret" not in lr.text
    setc = lr.headers.get("set-cookie", "").lower()
    assert "httponly" in setc and "samesite=strict" in setc and "path=/" in setc
    assert "max-age" in setc                           # 合理有效期
    assert "secret" not in setc                        # cookie 不含永久 token
    # 登入後(TestClient 保留 cookie）→ 帶 Origin 同源的 mutation 放行
    r = client.post("/api/offset", json={"value": 0.0},
                    headers={"Origin": "http://testserver"})
    assert r.status_code == 200


def test_login_wrong_token_rejected_same_message(client):
    _set(token="secret", env="test")
    r = client.post("/api/admin/login", json={"token": "bad"})
    assert r.status_code == 401
    assert "secret" not in r.text and "length" not in r.text.lower()


def test_login_brute_force_locked(client):
    _set(token="secret", env="test")
    get_settings().admin_login_max_attempts = 3
    codes = [client.post("/api/admin/login", json={"token": "x"}).status_code for _ in range(5)]
    assert 429 in codes                                # 超過嘗試次數 → 429


def test_failed_auth_does_not_log_token(client, caplog):
    _set(token="supersecrettoken", env="test")
    with caplog.at_level(logging.DEBUG):
        client.post("/api/offset", json={"value": 0.0},
                    headers={"X-Admin-Token": "supersecrettoken-wrong"})
    joined = "\n".join(r.getMessage() for r in caplog.records)
    assert "supersecrettoken" not in joined


def test_analysis_run_protected_and_rate_limited(client):
    _set(token="secret", env="test")
    assert client.post("/api/analysis/run").status_code == 401
    security.reset_state_for_tests()
    r1 = client.post("/api/analysis/run", headers={"X-Admin-Token": "secret"})
    assert r1.status_code == 200
    r2 = client.post("/api/analysis/run", headers={"X-Admin-Token": "secret"})
    assert r2.status_code == 429


def test_docs_available_in_non_production():
    """test/development:/openapi.json 可用(production 才關閉,見部署說明)。"""
    from fastapi.testclient import TestClient
    from app.main import app
    with TestClient(app) as c:
        # app 在 import 時依 app_env 決定;測試環境為 test → 文件開啟
        assert c.get("/openapi.json").status_code == 200
