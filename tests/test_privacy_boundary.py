"""隱私邊界(A+B):私人 GET 保護、公開投影 allowlist、GET 無副作用、WS 分流。"""
from __future__ import annotations

import pytest

from app.config import get_settings
from app.db.session import init_db
from app import security
from app.services import public_view as pv


PRIVATE_GETS = [
    "/api/accounts", "/api/accounts/comparison", "/api/positions",
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

def _full_result_with_private(stamped: bool = True) -> dict:
    from app.schemas.analysis import (
        AnalysisResult, Decision, PositionManagement,
    )
    from app.services.public_view import PRIVACY_BOUNDARY_VERSION
    r = AnalysisResult()
    r.decision = Decision(action="MANAGE", reason="你手上已經有單了,先顧好這張單")
    r.market_decision = Decision(action="WATCH", reason="區間盤整,先看著")
    r.market_state = "RANGE"
    if stamped:
        r.privacy_boundary_version = PRIVACY_BOUNDARY_VERSION
    r.position_management = PositionManagement(
        has_position=True, position_side="LONG", entry_price=4000.0,
        current_r_multiple=1.2, recommended_action="續抱")
    d = r.model_dump()
    d["some_future_unknown_field"] = {"lot_size": 0.5, "secret": "x"}   # 未來新欄位
    return d


def test_public_projection_strips_all_private():
    pub = pv.public_analysis(_full_result_with_private(stamped=True))
    leaked = pv.assert_no_private_keys(pub)
    assert leaked == [], f"公開投影仍含私人 key:{leaked}"
    # 決策用市場層(非 MANAGE),不洩露持倉
    assert pub["decision"]["action"] == "WATCH"
    assert "手上已經有單" not in pub["decision"]["reason"]


def test_public_projection_excludes_unknown_new_field():
    pub = pv.public_analysis(_full_result_with_private(stamped=True))
    assert "some_future_unknown_field" not in pub          # 未在 allowlist → 不公開
    assert "position_management" not in pub
    assert "offset_info" not in pub


def test_public_projection_keeps_market_analysis():
    pub = pv.public_analysis(_full_result_with_private(stamped=True))
    for k in ("market_state", "timeframes", "key_levels", "bias_analysis",
              "ai_strategy", "long_scenario", "short_scenario", "decision"):
        assert k in pub, f"公開分析應保留 {k}"
    assert pub.get("public") is True and pub.get("available") is True


# ── legacy 資料:缺版本戳記 → 不得公開任何自由文字 ─────────

def _legacy_payload_with_text_leaks() -> dict:
    """模擬部署前產生的舊資料:自由文字內含私人內容,且無版本戳記/market_decision。"""
    return {
        "version": 99,
        # 無 privacy_boundary_version、無 market_decision(舊 pipeline)
        "market_state": "STRONG_BULL_TREND",
        "decision": {"action": "MANAGE", "confidence_grade": "A", "evidence_score": 60,
                     "reason": "你手上已經有多單,建議續抱,停損移到 4000,分批平倉 30%"},
        "summary_zh_tw": "【強多】帳戶B 老師帶單,0.5 手,浮動 +1200 USD",
        "most_likely_user_mistake_now": "你最近有凹單(STOP_WIDENING)紀錄,別再擴大停損",
        "ai_strategy": {"available": True,
                        "rationale": "考量您目前持有 0.5 手多單、成本 4000,建議加碼",
                        "one_liner": "續抱手上多單", "risk_warning": "留意您的浮動損益"},
        "bias_analysis": {"bull_pct": 60, "bear_pct": 40},
    }


def test_legacy_payload_not_published_no_text_leak():
    """舊資料(無戳記)→ 公開投影一律 unavailable,任何私人自由文字不得外洩。"""
    pub = pv.public_analysis(_legacy_payload_with_text_leaks())
    assert pub == {"public": True, "available": False, "reason": "analysis_refresh_required"}
    blob = str(pub)
    for leak in ("多單", "0.5 手", "1200", "凹單", "STOP_WIDENING", "帳戶B",
                 "成本 4000", "續抱", "浮動"):
        assert leak not in blob, f"legacy 自由文字外洩:{leak}"


def test_missing_market_decision_fails_closed():
    """有戳記但缺 market_decision → 不得 fallback 用舊 decision,回 unavailable。"""
    from app.services.public_view import PRIVACY_BOUNDARY_VERSION
    payload = _legacy_payload_with_text_leaks()
    payload["privacy_boundary_version"] = PRIVACY_BOUNDARY_VERSION  # 有戳記
    # 但無 market_decision
    pub = pv.public_analysis(payload)
    assert pub["available"] is False
    assert "你手上已經有多單" not in str(pub)   # 不得用舊 decision.reason


def test_projection_exception_fails_closed(monkeypatch):
    """投影過程拋例外 → 回安全 unavailable,不得外洩原始 payload。"""
    payload = _full_result_with_private(stamped=True)
    payload["_secret_marker"] = "PRIVATE-RAW-XYZ"
    # 讓內部 allowlist 取值時拋例外
    def boom(_p):
        raise RuntimeError("boom")
    monkeypatch.setattr(pv, "assert_no_private_keys", boom)
    pub = pv.public_analysis(payload)
    assert pub["available"] is False
    assert "PRIVATE-RAW-XYZ" not in str(pub)     # 原始資料不外洩


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


# ── 私人/公開決策不得互換 ──────────────────────────────────

def test_private_vs_public_decision_not_swapped(client):
    _protect()
    from app.services.scheduler import state as st
    st.latest_result = _full_result_with_private(stamped=True)
    # 匿名 → 公開:市場層 market_decision(WATCH),不含持倉 MANAGE
    anon = client.get("/api/analysis/latest").json()
    assert anon["decision"]["action"] == "WATCH"
    assert "手上已經有單" not in str(anon)
    # 已登入 → 完整:私人 decision(MANAGE 覆寫)只在授權回應出現
    client.post("/api/admin/login", json={"token": "secret-token-32-chars-xxxxxxxxxxxx"})
    full = client.get("/api/analysis/latest").json()
    assert full["decision"]["action"] == "MANAGE"
    assert full.get("position_management", {}).get("has_position") is True


# ── 首次部署:legacy 記憶體/WS → 等待刷新 ──────────────────

def test_legacy_latest_endpoint_returns_unavailable(client):
    from app.services.scheduler import state as st
    st.latest_result = _full_result_with_private(stamped=False)   # 舊資料在記憶體
    r = client.get("/api/analysis/latest")
    assert r.status_code == 200
    assert r.json().get("available") is False
    assert r.json().get("reason") == "analysis_refresh_required"
    assert "手上已經有單" not in r.text and "老師私人筆記" not in r.text


def test_public_ws_legacy_unavailable(client):
    from app.services.scheduler import state as st
    st.latest_result = _full_result_with_private(stamped=False)
    with client.websocket_connect("/ws") as ws:
        msg = ws.receive_json()
        assert msg["data"].get("available") is False
        assert "老師私人筆記" not in str(msg)


# ── privacy_boundary_version invariant(單一真實來源)──────────

def test_privacy_version_single_source_of_truth():
    """常數、pipeline 蓋章、schema 欄位同源;不得硬編碼三份 magic number。"""
    import inspect
    import re
    import app.services.analysis_service as asvc
    from app.schemas.analysis import AnalysisResult

    src = inspect.getsource(asvc)
    # pipeline 蓋章必須 import 並使用常數,不得硬編碼數字賦值(如 = 1)
    assert "from app.services.public_view import PRIVACY_BOUNDARY_VERSION" in src
    assert "result.privacy_boundary_version = PRIVACY_BOUNDARY_VERSION" in src
    assert not re.search(r"privacy_boundary_version\s*=\s*\d", src), "不得硬編碼版本數字"
    # schema 預設為 0(legacy sentinel),與 current 常數不同義
    assert AnalysisResult().privacy_boundary_version == 0
    assert pv.PRIVACY_BOUNDARY_VERSION >= 1
    # 只有 current 戳記能公開;current± 或 0 皆被閘門擋下
    base = _full_result_with_private(stamped=True)
    assert pv.public_analysis(base)["available"] is True
    for bad in (0, pv.PRIVACY_BOUNDARY_VERSION + 1):
        base["privacy_boundary_version"] = bad
        assert pv.public_analysis(base)["available"] is False, bad


def test_privacy_version_stamped_end_to_end():
    """實際 pipeline 產生的分析,戳記 == public_view 常數(端到端同源)。"""
    import asyncio
    from app.providers.mock import MockProvider
    from app.services.analysis_service import run_analysis
    r = asyncio.run(run_analysis(MockProvider(), trigger="manual"))
    assert r.privacy_boundary_version == pv.PRIVACY_BOUNDARY_VERSION
    # 且經公開投影可正常公開(available:True)
    assert pv.public_analysis(r.model_dump())["available"] is True


def test_decision_assistant_is_in_position_free_public_allowlist():
    from app.services import public_view as pv

    assert "decision_assistant" in pv.PUBLIC_ALLOWLIST
    assert not ({"position_management", "account", "pnl", "lot_size"}
                & {"regime", "scenarioId", "actionSummary", "entryQualityScore",
                   "rewardRiskRatio", "why"})


# ── AI 送出的 user_payload 遞迴無私人 key(position-free 完整性)──

def test_ai_user_payload_has_no_private_keys(monkeypatch):
    import asyncio
    from types import SimpleNamespace

    import app.llm.agents as agents
    from app.llm.client import set_client_for_tests
    from app.llm.service import generate_ai_strategy
    from app.engines.key_levels import CandidateLevel
    from app.schemas.analysis import BiasAnalysis

    captured = []

    async def fake_call_json(*, system, user_payload, schema, max_tokens=2000):
        captured.append(user_payload)
        if "bias" in schema.get("properties", {}):
            return {"bias": "NEUTRAL", "strength": 50, "key_points": [], "one_line": "x"}, 0.0
        return ({
            "market_structure": {"label": "Range", "reason": "x"},
            "win_rates": {"long_pct": 50, "short_pct": 50},
            "action": {"type": "Wait", "wait_condition": "等", "next_trigger": "等收盤站上前高"},
            "entry_id": None, "stop_loss_id": None, "tp1_id": None, "tp2_id": None, "tp3_id": None,
            "invalidation": "x", "rationale": "x", "risk_warning": "x", "one_liner": "x",
            "scenarios": [{"name": "a", "probability_pct": 34, "trigger": "t", "plan": "p"},
                          {"name": "b", "probability_pct": 33, "trigger": "t", "plan": "p"},
                          {"name": "c", "probability_pct": 33, "trigger": "t", "plan": "p"}],
            "confidence": {"score": 50, "factors": []}}, 0.0)

    monkeypatch.setattr(agents, "call_json", fake_call_json)
    set_client_for_tests(SimpleNamespace())   # llm_available → True(_test_client 非 None)
    try:
        ev = SimpleNamespace(event_impact="LOW", time_risk="LOW", event_lockout=False,
                             next_event="", minutes_remaining=None)
        levels = [CandidateLevel("SUP_ZONE_01", "SUP_ZONE", 3990.0, 3992.0, "STRONG", ["t"])]
        asyncio.run(generate_ai_strategy(
            price=4000.0, atr15=5.0, state="RANGE", quality_status="GOOD", ev=ev,
            ind={"15M": {"atr14": 5.0}}, structures={}, levels=levels, dfs_closed={},
            bias=BiasAnalysis(), position=None, no_signal=False))
    finally:
        set_client_for_tests(None)

    assert len(captured) == 4, "應攔截到 3 分析師 + 1 決策的 user_payload"
    ai_private = {"position", "account", "account_id", "lot_size", "pnl", "pnl_usd",
                  "mentor", "note", "mentor_comparison", "behavior_flags", "trading_coach",
                  "corrective_action", "stop_modification_history", "partial_exit_history"}
    for payload in captured:
        leaked = pv.collect_keys(payload) & ai_private
        assert not leaked, f"AI user_payload 含私人 key:{leaked}"
