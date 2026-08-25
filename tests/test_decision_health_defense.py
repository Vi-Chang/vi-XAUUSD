from datetime import datetime, timezone

from app.engines.decision_health import (
    evaluate_decision_health,
    evaluate_defense_state,
    get_latest_valid_closed_15m,
    is_confirmed_break,
)
from app.services.alert_aggregator import (
    is_meaningful_change,
    notification_fingerprint,
)

NOW = datetime(2026, 8, 25, 1, 20, tzinfo=timezone.utc)


def _data(*, available=True, close=4671.0, close_time="2026-08-25T01:15:00+00:00"):
    return {
        "symbol": "XAUUSD", "timestamp_utc": NOW.isoformat(),
        "current_price": {"mid": 4667.83, "last_update": NOW.isoformat()},
        "closed_candles": {"15M": {
            "available": available, "close_price": close if available else None,
            "close_time": close_time if available else None, "source": "test",
        }},
        "normalized_analysis": {
            "currentPrice": 4667.83, "atr15": 2.0, "trendBias": "bullish",
            "lastClosedCandlePrice": close,
            "lastClosedCandleTimestamp": close_time,
            "timeframeAssessments": [
                {"timeframe": "4H", "trend": "bullish"},
                {"timeframe": "1H", "trend": "bullish"},
            ],
        },
    }


def test_case_a_intrabar_defense_touch_preserves_bias_and_waits_for_close():
    health = evaluate_decision_health(_data(), now=NOW)
    defense = evaluate_defense_state(
        defense_level=4667.16, side="LONG", current_price=4667.08,
        atr15=2.0, closed_context=health["latestClosed15m"],
        entry_confirmation=health["entryConfirmation"], previous={})
    assert health["marketBias"] == "BULLISH"
    assert defense["defenseState"] in {"TESTING", "BROKEN_PENDING_CLOSE"}
    assert defense["entryConfirmation"] == "WAIT_15M_CLOSE"
    assert defense["shortNow"] is False


def test_case_b_false_break_closes_back_above_defense_and_recovers():
    health = evaluate_decision_health(_data(close=4671.0), now=NOW)
    defense = evaluate_defense_state(
        defense_level=4667.16, side="LONG", current_price=4671.0,
        atr15=2.0, closed_context=health["latestClosed15m"],
        entry_confirmation=health["entryConfirmation"],
        previous={"defenseState": "BROKEN_PENDING_CLOSE",
                  "defenseBasisCandleTime": "2026-08-25T01:00:00+00:00"})
    assert defense["defenseState"] == "HELD"
    assert defense["falseBreakDetected"] is True
    assert defense["longScenarioInvalidated"] is False


def test_case_c_confirmed_defense_break_invalidates_long_but_never_short_now():
    health = evaluate_decision_health(_data(close=4666.8), now=NOW)
    defense = evaluate_defense_state(
        defense_level=4667.16, side="LONG", current_price=4666.7,
        atr15=2.0, closed_context=health["latestClosed15m"],
        entry_confirmation=health["entryConfirmation"], previous={})
    assert is_confirmed_break(4667.16, "LONG", health["latestClosed15m"], buffer=.2)
    assert defense["defenseState"] == "BROKEN_CONFIRMED"
    assert defense["longScenarioInvalidated"] is True
    assert defense["shortNow"] is False


def test_case_d_missing_latest_15m_uses_recent_context_without_erasing_htf_bias():
    data = _data(available=False)
    result = get_latest_valid_closed_15m(data, now=NOW)
    health = evaluate_decision_health(data, now=NOW)
    assert result["latestClosed15m"] is None
    assert result["contextClosed15m"]["close"] == 4671.0
    assert health["marketBias"] == "BULLISH"
    assert health["dataHealth"] == "DEGRADED_15M"
    assert health["entryConfirmation"] == "WAIT_15M_CLOSE"


def _event(*, price: float, defense_state="TESTING"):
    return {
        "symbol": "XAUUSD", "event_type": "DEFENSE_TEST",
        "currentState": "WAIT", "canonicalState": "WAIT",
        "marketBias": "BULLISH", "entryConfirmation": "WAIT_15M_CLOSE",
        "defenseState": defense_state, "defenseLevel": 4667.16,
        "primaryTriggerId": "LONG-DEFENSE-4667",
        "decisionBasisCandleCloseTime": "2026-08-25T01:15:00+00:00",
        "currentPrice": price,
    }


def test_case_e_minor_live_price_change_has_same_telegram_identity():
    first, second = _event(price=4667.83), _event(price=4667.08)
    assert notification_fingerprint(first) == notification_fingerprint(second)
    assert is_meaningful_change(first, second) == (
        False, "NO_MEANINGFUL_DECISION_CHANGE")


def test_case_f_defense_testing_to_held_is_meaningful_transition():
    testing = _event(price=4667.08)
    held = _event(price=4671.0, defense_state="HELD")
    assert notification_fingerprint(testing) != notification_fingerprint(held)
    assert is_meaningful_change(testing, held) == (True, "DEFENSESTATE_CHANGED")
