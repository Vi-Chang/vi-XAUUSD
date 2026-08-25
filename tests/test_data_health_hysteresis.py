from datetime import datetime, timedelta, timezone

from app.engines.decision_health import evaluate_decision_health
from app.services.alert_aggregator import notification_fingerprint

BASE = datetime(2026, 8, 25, 3, 40, tzinfo=timezone.utc)


def _sample(now, *, available=True, market_time=None, candle_time=None, close=4637.03):
    market_time = market_time or now
    candle_time = candle_time or now.replace(minute=(now.minute // 15) * 15,
                                               second=0, microsecond=0)
    normalized = {
        "currentPrice": close, "marketDataTimestamp": market_time.isoformat(),
        "trendBias": "bullish", "timeframeAssessments": [],
    }
    return {
        "symbol": "XAUUSD", "timestamp_utc": now.isoformat(),
        "current_price": {"mid": close, "last_update": market_time.isoformat()},
        "closed_candles": {"15M": {
            "available": available,
            "close_price": close if available else None,
            "close_time": candle_time.isoformat() if available else None,
            "source": "test",
        }},
        "normalized_analysis": normalized,
    }


def _step(previous, minute, **kwargs):
    now = BASE + timedelta(minutes=minute)
    return evaluate_decision_health(_sample(now, **kwargs), previous=previous, now=now)


def test_case_a_one_timeout_does_not_flap_or_notify():
    healthy = _step({}, 0)
    one_failure = _step(healthy, 1, available=False)
    normal = _step(one_failure, 2)
    assert one_failure["dataHealth"] == "HEALTHY"
    assert one_failure["dataHealthEvent"] is None
    assert normal["dataHealth"] == "HEALTHY"
    assert normal["dataHealthEvent"] is None


def test_case_b_consecutive_failures_open_one_incident():
    healthy = _step({}, 0)
    first = _step(healthy, 1, available=False)
    degraded = _step(first, 2, available=False)
    assert degraded["dataHealth"] == "DEGRADED"
    assert degraded["dataHealthEvent"]["event_type"] == "DATA_DELAYED"
    assert degraded["dataHealthEvent"]["dataHealthEventKey"].startswith(
        "DATA_DELAYED:DATA-")


def test_case_c_success_with_unchanged_timestamp_is_not_recovery():
    healthy = _step({}, 0)
    first = _step(healthy, 1, available=False)
    degraded = _step(first, 2, available=False)
    unchanged = BASE + timedelta(minutes=2)
    success = _step(degraded, 3, market_time=unchanged,
                    candle_time=BASE.replace(minute=30))
    assert success["apiOk"] is True
    assert success["marketTimestampAdvanced"] is False
    assert success["dataHealth"] == "DEGRADED"
    assert success["dataHealthEvent"] is None


def test_case_d_two_fresh_advancing_polls_recover_once():
    healthy = _step({}, 0)
    degraded = _step(_step(healthy, 1, available=False), 2, available=False)
    first = _step(degraded, 3)
    recovered = _step(first, 4)
    assert first["dataHealth"] == "DEGRADED"
    assert recovered["dataHealth"] == "HEALTHY"
    assert recovered["dataHealthEvent"]["event_type"] == "DATA_RECOVERED"
    assert recovered["dataHealthEvent"]["dataHealthEventKey"] == (
        f"DATA_RECOVERED:{degraded['dataIncidentId']}")


def test_case_e_healthy_polling_never_emits_recovery_for_same_candle_price():
    state = _step({}, 0)
    events = []
    same_candle = BASE.replace(minute=45)
    for minute in range(1, 11):
        state = _step(state, minute, candle_time=same_candle, close=4637.03)
        events.append(state.get("dataHealthEvent"))
    assert state["dataHealth"] == "HEALTHY"
    assert all(event is None for event in events)


def test_case_f_same_recovery_incident_has_one_persistent_fingerprint():
    incident = "DATA-20260825-1150"
    event = {
        "symbol": "XAUUSD", "event_type": "DATA_RECOVERED",
        "dataIncidentId": incident,
        "dataHealthEventKey": f"DATA_RECOVERED:{incident}",
        "currentState": "HEALTHY", "dataHealth": "HEALTHY",
    }
    assert len({notification_fingerprint(dict(event)) for _ in range(3)}) == 1


def test_case_g_same_closed_candle_after_recovery_does_not_recover_again():
    healthy = _step({}, 0)
    degraded = _step(_step(healthy, 1, available=False), 2, available=False)
    recovered = _step(_step(degraded, 3), 4)
    same_candle = BASE.replace(minute=45)
    at_1157 = _step(recovered, 17, candle_time=same_candle)
    at_1200 = _step(at_1157, 20, candle_time=same_candle)
    assert at_1157["dataHealthEvent"] is None
    assert at_1200["dataHealthEvent"] is None
