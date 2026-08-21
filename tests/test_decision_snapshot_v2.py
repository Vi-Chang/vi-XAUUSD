from app.engines.data_health_gate import evaluate_data_health
from app.engines.decision_snapshot import build_decision_snapshot
from app.services.public_view import PRIVACY_BOUNDARY_VERSION, public_analysis


def _base(score=75):
    return {
        "symbol": "XAUUSD", "snapshot_ts": "2026-08-21T10:00:00+00:00",
        "timestamp_utc": "2026-08-21T10:00:01+00:00",
        "current_price": {"mid": 4600.0, "last_update": "2026-08-21T10:00:01+00:00", "provider": "test"},
        "data_quality": {"status": "GOOD"},
        "normalized_analysis": {"marketDataStatus": "GOOD", "marketDataTimestamp": "2026-08-21T10:00:00+00:00"},
        "decision": {"signal_score": score, "can_enter": False, "trade_status": "WAIT_CONFIRMATION", "blocked_reason": "等待收盤"},
        "final_decision_state": {"state": "LONG_WATCH", "direction": "LONG", "confirmation": "15M 收盤站上 4601"},
        "trend_continuation_engine": {"marketType": "TREND_CONTINUATION_LONG", "selected": None},
        "event_risk": {"data_status": "FAILED", "status": "UNKNOWN"},
    }


def test_snapshot_separates_confidence_from_permission():
    snap = build_decision_snapshot(_base())
    assert snap["confidenceGrade"] == "B"
    assert snap["signalScore"] == 75
    assert snap["canEnter"] is False
    assert snap["action"] == "WAIT_CONFIRMATION"
    assert snap["marketSession"]["name"] == "LONDON"
    assert snap["executionCost"]["estimatedRoundTripCost"] == 0.15


def test_data_health_fail_closed():
    data = _base(90)
    data["current_price"]["mid"] = None
    snap = build_decision_snapshot(data)
    assert snap["dataHealth"]["status"] == "INVALID_PRICE"
    assert snap["state"] == "DATA_STALE"
    assert snap["canEnter"] is False
    assert snap["action"] == "DATA_UNAVAILABLE"


def test_source_divergence_is_not_tradable():
    data = _base()
    data["data_quality"]["source_mismatch"] = True
    assert evaluate_data_health(data)["status"] == "SOURCE_DIVERGENCE"


def test_quote_and_closed_candle_large_gap_waits_for_sync():
    data = _base(90)
    data["normalized_analysis"].update({
        "lastClosedCandlePrice": 4602.83,
        "atr15": 11.97,
    })
    data["current_price"]["mid"] = 4582.84
    health = evaluate_data_health(data)
    assert health["status"] == "QUOTE_CANDLE_DIVERGENCE"
    assert health["healthy"] is False
    assert "等待下一根K棒更新" in health["reasons"][0]
    snapshot = build_decision_snapshot(data)
    assert snapshot["state"] == "DATA_STALE"
    assert snapshot["canEnter"] is False


def test_normal_intrabar_move_does_not_trigger_sync_guard():
    data = _base()
    data["normalized_analysis"].update({"lastClosedCandlePrice": 4600, "atr15": 12})
    data["current_price"]["mid"] = 4592
    assert evaluate_data_health(data)["status"] == "HEALTHY"


def test_snapshot_is_stable_for_quote_only_refresh():
    first = build_decision_snapshot(_base())
    data = _base()
    data["current_price"]["mid"] = 4600.8
    data["current_price"]["last_update"] = "2026-08-21T10:00:08+00:00"
    second = build_decision_snapshot(data)
    assert first["decisionId"] == second["decisionId"]


def test_snapshot_uses_exact_final_decision_identity():
    data = _base()
    data["final_decision_state"].update({
        "decisionId": "canonical-123", "decisionVersion": 7,
        "finalAction": "WAIT", "humanSummary": "等待新結構",
    })
    snapshot = build_decision_snapshot(data)
    assert snapshot["decisionId"] == "canonical-123"
    assert snapshot["decisionVersion"] == 7


def test_public_projection_exposes_same_canonical_snapshot():
    data = _base()
    snapshot = build_decision_snapshot(data)
    data.update({"privacy_boundary_version": PRIVACY_BOUNDARY_VERSION,
                 "version": 1, "market_decision": data["decision"],
                 "decision_snapshot": snapshot})
    public = public_analysis(data)
    assert public["decision_snapshot"] == snapshot
