"""隱私邊界(A+B):私人 GET 保護、公開投影 allowlist、GET 無副作用、WS 分流。"""
from __future__ import annotations

import pytest

from app.config import get_settings
from app.db.session import init_db
from app import security
from app.services import public_view as pv


PRIVATE_GETS = [
    "/api/accounts", "/api/accounts/comparison", "/api/positions",
    "/api/behavior/flags", "/api/mentor/history", "/api/mentor/signals",
]


@pytest.fixture(autouse=True)
def _reset():
    init_db()
    security.reset_state_for_tests()
    s = get_settings()
    saved = (s.admin_token, s.app_env, s.allow_unauthenticated_mutations)
    yield
    (s.admin_token, s.app_env, s.allow_unauthenticated_mutations) = saved
    security.reset_state_for_tests()
    from app.services.scheduler import state as st
    st.latest_result = None
    st.last_full_analysis = None


@pytest.fixture()
def client():
    from fastapi.testclient import TestClient
    from app.main import app
    with TestClient(app) as c:
        yield c


def _protect():
    get_settings().admin_token = "secret-token-32-chars-xxxxxxxxxxxx"


# ── 1. 私人 GET 未登入 → 401,且無私人欄位 ──────────────────

def test_private_gets_require_auth(client):
    _protect()
    for url in PRIVATE_GETS:
        r = client.get(url)
        assert r.status_code == 401, url
        body = r.text
        for leak in ("lot_size", "pnl", "account_name", "corrective_action"):
            assert leak not in body, f"{url} 洩露 {leak}"


# ── 2/3. header token / session → 私人 GET 成功 ─────────────

def test_private_get_with_header_token(client):
    _protect()
    r = client.get("/api/positions", headers={"X-Admin-Token": "secret-token-32-chars-xxxxxxxxxxxx"})
    assert r.status_code == 200


def test_private_get_with_session(client):
    _protect()
    assert client.post("/api/admin/login",
                       json={"token": "secret-token-32-chars-xxxxxxxxxxxx"}).status_code == 200
    # GET 同源(TestClient host=testserver);session cookie 自動帶上
    r = client.get("/api/positions")
    assert r.status_code == 200


# ── 4/5/6. 公開投影 allowlist + 遞迴無私人 key + 新欄位不自動公開 ──

def _full_result_with_private() -> dict:
    from app.schemas.analysis import (
        AnalysisResult, Decision, PositionManagement, MentorComparison,
        MentorSignalView, TradingCoachView,
    )
    r = AnalysisResult()
    r.decision = Decision(action="MANAGE", reason="你手上已經有單了,先顧好這張單")
    r.market_decision = Decision(action="WATCH", reason="區間盤整,先看著")
    r.position_management = PositionManagement(
        has_position=True, position_side="LONG", entry_price=4000.0,
        current_r_multiple=1.2, recommended_action="續抱")
    r.mentor_comparison = MentorComparison(
        has_signals=True, signals=[MentorSignalView(
            id=1, direction="LONG", entry_price=4000.0, note="老師私人筆記:加碼")])
    r.trading_coach = TradingCoachView(
        behavior_flags=["STOP_WIDENING"], corrective_action="別凹單")
    d = r.model_dump()
    d["some_future_unknown_field"] = {"lot_size": 0.5, "secret": "x"}   # 未來新欄位
    return d


def test_public_projection_strips_all_private():
    pub = pv.public_analysis(_full_result_with_private())
    leaked = pv.assert_no_private_keys(pub)
    assert leaked == [], f"公開投影仍含私人 key:{leaked}"
    # 決策用市場層(非 MANAGE),不洩露持倉
    assert pub["decision"]["action"] == "WATCH"
    assert "手上已經有單" not in pub["decision"]["reason"]


def test_public_projection_excludes_unknown_new_field():
    pub = pv.public_analysis(_full_result_with_private())
    assert "some_future_unknown_field" not in pub          # 未在 allowlist → 不公開
    assert "position_management" not in pub
    assert "mentor_comparison" not in pub
    assert "trading_coach" not in pub
    assert "offset_info" not in pub


def test_public_projection_keeps_market_analysis():
    pub = pv.public_analysis(_full_result_with_private())
    for k in ("market_state", "timeframes", "key_levels", "bias_analysis",
              "ai_strategy", "long_scenario", "short_scenario", "decision"):
        assert k in pub, f"公開分析應保留 {k}"
    assert pub.get("public") is True


def test_recursive_key_scan_catches_nested_private():
    """遞迴針對 schema key(非字串搜尋),不會誤判市場用語。"""
    # 市場分析中的一般欄位(如 scenario 的 entry_zone_id)不得被誤判
    safe = {"market_state": "RANGE", "long_scenario": {"entry_zone_id": "SUP_ZONE_01"}}
    assert pv.assert_no_private_keys(safe) == []
    # 真正的私人 key(巢狀)會被抓到
    bad = {"a": {"b": {"lot_size": 1}}, "c": [{"pnl_usd": 5}]}
    assert set(pv.assert_no_private_keys(bad)) == {"lot_size", "pnl_usd"}


def test_public_endpoint_latest_has_no_private(client):
    """公開 /api/analysis/latest(匿名)不含任何私人欄位。"""
    from app.services.scheduler import state as st
    st.latest_result = _full_result_with_private()
    r = client.get("/api/analysis/latest")   # 匿名
    assert r.status_code == 200
    leaked = pv.assert_no_private_keys(r.json())
    assert leaked == [], f"公開端點洩露:{leaked}"
    assert r.json().get("public") is True


# ── 7. 匿名 latest GET 無副作用 ─────────────────────────────

def test_anonymous_latest_no_analysis_side_effect(client, monkeypatch):
    """空快取時匿名 GET latest:不觸發 run_analysis / LLM / provider / 新 AnalysisRun。"""
    import app.services.analysis_service as asvc
    from app.services.scheduler import state as st
    st.latest_result = None
    calls = {"n": 0}

    async def boom(*a, **k):
        calls["n"] += 1
        raise AssertionError("run_analysis 不應被匿名 GET 觸發")

    monkeypatch.setattr(asvc, "run_analysis", boom)
    # DB 也清空 → 應回安全「尚無分析」狀態
    from app.db.models import AnalysisRun
    from app.db.session import db_session
    with db_session() as db:
        db.query(AnalysisRun).delete()

    for _ in range(3):
        r = client.get("/api/analysis/latest")
        assert r.status_code == 200
        assert r.json().get("available") is False
    assert calls["n"] == 0                    # core analysis 呼叫次數 = 0


def test_anonymous_latest_reads_last_db_analysis(client, monkeypatch):
    """有既有分析時,匿名 GET 從 DB 讀取(唯讀),仍不觸發新分析。"""
    import app.services.analysis_service as asvc
    from app.services.scheduler import state as st
    from datetime import datetime, timezone
    st.latest_result = None
    calls = {"n": 0}

    async def boom(*a, **k):
        calls["n"] += 1
        raise AssertionError("不應觸發")
    monkeypatch.setattr(asvc, "run_analysis", boom)

    from app.db.models import AnalysisRun
    from app.db.session import db_session
    with db_session() as db:
        db.query(AnalysisRun).delete()
        db.add(AnalysisRun(
            run_time=datetime.now(timezone.utc), trigger="test", market_state="RANGE",
            decision_action="WATCH", confidence_grade="C", evidence_score=0,
            data_quality_status="GOOD", result_json=_full_result_with_private(),
            prompt_version="v", strategy_version="v", model_version="m"))

    r = client.get("/api/analysis/latest")
    assert r.status_code == 200 and calls["n"] == 0
    assert pv.assert_no_private_keys(r.json()) == []   # 從 DB 讀的也走公開投影


# ── 8-12. WebSocket 分流 ────────────────────────────────────

def _seed_latest():
    from app.services.scheduler import state as st
    st.latest_result = _full_result_with_private()


def test_public_ws_only_public_projection(client):
    _seed_latest()
    with client.websocket_connect("/ws") as ws:
        msg = ws.receive_json()
        assert msg["type"] == "analysis"
        assert pv.assert_no_private_keys(msg["data"]) == []
        assert msg["data"].get("public") is True


def test_private_ws_without_session_rejected(client):
    _protect()
    _seed_latest()
    from starlette.websockets import WebSocketDisconnect
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws/private") as ws:
            ws.receive_json()


def test_private_ws_with_session_gets_full(client):
    _protect()
    _seed_latest()
    client.post("/api/admin/login", json={"token": "secret-token-32-chars-xxxxxxxxxxxx"})
    with client.websocket_connect("/ws/private",
                                  headers={"Origin": "http://testserver"}) as ws:
        msg = ws.receive_json()
        assert msg["type"] == "analysis"
        # 私人頻道應含完整私人欄位
        assert "position_management" in msg["data"]


def test_private_ws_cross_origin_rejected(client):
    _protect()
    _seed_latest()
    client.post("/api/admin/login", json={"token": "secret-token-32-chars-xxxxxxxxxxxx"})
    from starlette.websockets import WebSocketDisconnect
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws/private",
                                      headers={"Origin": "https://evil.com"}) as ws:
            ws.receive_json()
