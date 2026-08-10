"""Health/readiness/liveness(Phase 1):正常 / stale / 排程停用 / provider 失敗 / 週末休市。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.config import get_settings
from app.services import heartbeat


def _state(**over):
    now = datetime.now(timezone.utc)
    st = SimpleNamespace(
        last_job_run={}, started_at=now, scheduler_started=True,
        last_quote_ok_at=now, last_full_analysis=now,
        provider=SimpleNamespace(name="mock"), fast_provider=None,
        secondary=None, l1_fail_count=0, quote_cache=None, latest_result=None,
    )
    for k, v in over.items():
        setattr(st, k, v)
    return st


@pytest.fixture(autouse=True)
def _settings():
    s = get_settings()
    orig = (s.disable_scheduler, s.data_lag_warn_minutes, s.app_env,
            s.admin_token, s.api_only_mode, s.allow_unauthenticated_mutations)
    s.disable_scheduler = False        # 預設當作有排程,個別測試再覆寫
    s.api_only_mode = False
    s.app_env = "test"                 # 預設非 production,不觸發 admin_token_missing
    yield
    (s.disable_scheduler, s.data_lag_warn_minutes, s.app_env,
     s.admin_token, s.api_only_mode, s.allow_unauthenticated_mutations) = orig


def _fresh_candle(minutes_old):
    dt = datetime.now(timezone.utc) - timedelta(minutes=minutes_old)
    return dt, float(minutes_old)


# ── readiness ───────────────────────────────────────────────

def test_ready_normal_market_open_fresh_data(monkeypatch):
    monkeypatch.setattr(heartbeat, "market_is_open", lambda: True)
    monkeypatch.setattr(heartbeat, "_last_15m_candle", lambda: _fresh_candle(2))
    out = heartbeat.compute_readiness(_state())
    assert out["ready"] is True and out["reason"] == "ok"


def test_not_ready_when_data_stale(monkeypatch):
    monkeypatch.setattr(heartbeat, "market_is_open", lambda: True)
    monkeypatch.setattr(heartbeat, "_last_15m_candle", lambda: _fresh_candle(180))  # 180 分鐘前
    out = heartbeat.compute_readiness(_state())
    assert out["ready"] is False and out["reason"] == "data_stale"


def test_weekend_closed_is_ready_not_failure(monkeypatch):
    """週末休市:視為就緒(market_closed),不得誤判成 stale failure。"""
    monkeypatch.setattr(heartbeat, "market_is_open", lambda: False)
    # 即使沒有新資料,休市仍應 ready
    monkeypatch.setattr(heartbeat, "_last_15m_candle", lambda: (None, None))
    out = heartbeat.compute_readiness(_state(last_full_analysis=None, last_quote_ok_at=None))
    assert out["ready"] is True and out["reason"] == "market_closed"


def test_scheduler_disabled_reason(monkeypatch):
    monkeypatch.setattr(heartbeat, "market_is_open", lambda: True)
    monkeypatch.setattr(heartbeat, "_last_15m_candle", lambda: _fresh_candle(2))
    get_settings().disable_scheduler = True
    out = heartbeat.compute_readiness(_state(scheduler_started=False))
    assert out["ready"] is False and out["reason"] == "scheduler_disabled"


def test_api_only_mode_is_ready(monkeypatch):
    """刻意 API-only(關排程但明確設定)→ 就緒,reason=api_only(區分誤設)。"""
    monkeypatch.setattr(heartbeat, "market_is_open", lambda: True)
    monkeypatch.setattr(heartbeat, "_last_15m_candle", lambda: (None, None))
    s = get_settings()
    s.disable_scheduler = True
    s.api_only_mode = True
    out = heartbeat.compute_readiness(_state(scheduler_started=False))
    assert out["ready"] is True and out["reason"] == "api_only"


def test_production_missing_token_not_ready_even_on_weekend(monkeypatch):
    """production 缺 ADMIN_TOKEN → not-ready,且不被 market_closed 掩蓋。"""
    monkeypatch.setattr(heartbeat, "market_is_open", lambda: False)   # 週末休市
    monkeypatch.setattr(heartbeat, "_last_15m_candle", lambda: (None, None))
    s = get_settings()
    s.app_env = "production"
    s.admin_token = ""
    out = heartbeat.compute_readiness(_state())
    assert out["ready"] is False and out["reason"] == "admin_token_missing"


def test_provider_failure_no_data_past_grace(monkeypatch):
    """provider 一直失敗 → 無新資料且過了開機寬限 → not ready(no_data)。"""
    monkeypatch.setattr(heartbeat, "market_is_open", lambda: True)
    monkeypatch.setattr(heartbeat, "_last_15m_candle", lambda: (None, None))
    old_start = datetime.now(timezone.utc) - timedelta(hours=2)   # 早已過寬限
    out = heartbeat.compute_readiness(_state(started_at=old_start, last_quote_ok_at=None))
    assert out["ready"] is False and out["reason"] == "no_data"


def test_warming_up_within_startup_grace(monkeypatch):
    monkeypatch.setattr(heartbeat, "market_is_open", lambda: True)
    monkeypatch.setattr(heartbeat, "_last_15m_candle", lambda: (None, None))
    out = heartbeat.compute_readiness(_state(started_at=datetime.now(timezone.utc)))
    assert out["ready"] is False and out["reason"] == "warming_up"


# ── liveness:provider 失敗不影響存活 ───────────────────────

def test_liveness_alive_even_on_provider_failure(monkeypatch):
    out = heartbeat.liveness_payload(_state(l1_fail_count=99))
    assert out["status"] == "alive"
    assert out["scheduler_started"] is True
    assert out["uptime_seconds"] is not None


# ── health payload:不昂貴、不洩露敏感值 ───────────────────

def test_health_payload_no_sensitive_and_has_monitoring_fields(monkeypatch):
    monkeypatch.setattr(heartbeat, "market_is_open", lambda: True)
    monkeypatch.setattr(heartbeat, "_last_15m_candle", lambda: _fresh_candle(3))
    s = get_settings()
    s.admin_token = "supersecret-xyz"
    try:
        out = heartbeat.health_payload(_state())
    finally:
        s.admin_token = ""
    import json
    blob = json.dumps(out, default=str)
    # 不得洩露 token / DB URL / 內部檔案路徑
    assert "supersecret-xyz" not in blob
    assert "sqlite" not in blob and "postgresql" not in blob
    assert "/srv/" not in blob and "\\Users\\" not in blob
    # 監控欄位齊備
    assert "ready" in out and "readiness_reason" in out
    assert "provider_consecutive_failures" in out
    assert "started_at" in out and "scheduler_started" in out
    assert "llm" in out and set(out["llm"]) >= {
        "last_success_at", "last_error_at", "last_error_type"}
    assert out["tiered"]["quote_age_minutes"] is not None
