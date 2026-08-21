from app.engines.data_health_gate import evaluate_data_health
from app.engines.decision_snapshot import build_decision_snapshot


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


def test_snapshot_is_stable_for_quote_only_refresh():
    first = build_decision_snapshot(_base())
    data = _base()
    data["current_price"]["mid"] = 4600.8
    data["current_price"]["last_update"] = "2026-08-21T10:00:08+00:00"
    second = build_decision_snapshot(data)
    assert first["decisionId"] == second["decisionId"]
