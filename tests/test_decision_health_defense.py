from datetime import datetime, timezone

from app.engines.decision_health import (
    evaluate_decision_health,
    evaluate_defense_state,
    get_latest_valid_closed_15m,
    is_confirmed_break,
)
from app.engines.decision_presentation import format_decision_message
from app.engines.scenario_execution import can_execute_scenario
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


def test_deep_intrabar_cross_is_pending_close_not_testing_and_cannot_execute():
    defense = evaluate_defense_state(
        defense_level=4656.07, side="LONG", current_price=4646.70,
        atr15=2.0, closed_context=None,
        entry_confirmation="WAIT_15M_CLOSE", previous={})
    assert defense["defenseState"] == "BROKEN_PENDING_CLOSE"
    assert defense["entryConfirmation"] == "WAIT_15M_CLOSE"
    gate = can_execute_scenario(
        direction="LONG", current_price=4646.70, invalidation_price=4656.07,
        lifecycle_state="ENTRY_READY", data_health="DEGRADED_15M",
        entry_confirmation=defense["entryConfirmation"],
        closed_candle_confirmed=False, in_executable_zone=False,
        risk_valid=False, rr_valid=True, stop_valid=False)
    assert gate["executionAllowed"] is False
    assert gate["scenarioValidity"] == "BLOCKED_BY_DATA"


def test_pending_break_telegram_combines_degraded_data_and_close_outcomes():
    message = format_decision_message({
        "event_type": "DEFENSE_TEST", "currentState": "WAIT",
        "currentPrice": 4646.70, "marketBias": "BULLISH",
        "dataHealth": "DEGRADED_15M", "entryConfirmation": "WAIT_15M_CLOSE",
        "defenseState": "BROKEN_PENDING_CLOSE", "defenseLevel": 4656.07,
        "defenseSide": "LONG", "confirmationBuffer": .20,
    })
    assert "🟠 15M 資料延遲" in message
    assert "盤中已跌破多方防守 4656.07" in message
    assert "新進場：禁止" in message
    assert "收盤重新站回 4656.07" in message
    assert "收盤確認跌破 4655.87" in message
    assert "高週期方向保留" in message
    assert "正在測試原方向防守" not in message


def test_case_b_false_break_closes_back_above_defense_and_recovers():
    still_pending = evaluate_defense_state(
        defense_level=4667.16, side="LONG", current_price=4668.0,
        atr15=2.0, closed_context={
            "close": 4670.0, "closeTime": "2026-08-25T01:00:00+00:00"},
        entry_confirmation="READY",
        previous={"defenseState": "BROKEN_PENDING_CLOSE",
                  "defenseBasisCandleTime": "2026-08-25T01:00:00+00:00"})
    assert still_pending["defenseState"] == "BROKEN_PENDING_CLOSE"

    health = evaluate_decision_health(_data(close=4671.0), now=NOW)
    defense = evaluate_defense_state(
        defense_level=4667.16, side="LONG", current_price=4671.0,
        atr15=2.0, closed_context=health["latestClosed15m"],
        entry_confirmation=health["entryConfirmation"],
        previous={"defenseState": "BROKEN_PENDING_CLOSE",
                  "defenseBasisCandleTime": "2026-08-25T01:00:00+00:00"})
    assert defense["defenseState"] == "RECLAIMED"
    assert defense["falseBreakDetected"] is True
    assert defense["longScenarioInvalidated"] is False
    assert defense["entryConfirmation"] == "WAIT_NEW_STRUCTURE"

    later = datetime(2026, 8, 25, 1, 35, tzinfo=timezone.utc)
    later_health = evaluate_decision_health(
        _data(close=4672.0, close_time="2026-08-25T01:30:00+00:00"), now=later)
    held = evaluate_defense_state(
        defense_level=4667.16, side="LONG", current_price=4672.0,
        atr15=2.0, closed_context=later_health["latestClosed15m"],
        entry_confirmation=later_health["entryConfirmation"], previous=defense)
    assert held["defenseState"] == "HELD"
    assert held["falseBreakDetected"] is True
    assert held["entryConfirmation"] == "READY"


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
    assert defense["entryConfirmation"] == "WAIT_NEW_STRUCTURE"


def test_case_d_missing_latest_15m_uses_recent_context_without_erasing_htf_bias():
    data = _data(available=False)
    result = get_latest_valid_closed_15m(data, now=NOW)
    health = evaluate_decision_health(data, now=NOW)
    assert result["latestClosed15m"] is None
    assert result["contextClosed15m"]["close"] == 4671.0
    assert health["marketBias"] == "BULLISH"
    assert health["dataHealth"] == "DEGRADED_15M"
    assert health["entryConfirmation"] == "WAIT_15M_CLOSE"


def _event(*, price: float, defense_state="TESTING",
           candle_time="2026-08-25T01:15:00+00:00", event_type="DEFENSE_TEST"):
    return {
        "symbol": "XAUUSD", "event_type": event_type,
        "currentState": "WAIT", "canonicalState": "WAIT",
        "marketBias": "BULLISH", "entryConfirmation": "WAIT_15M_CLOSE",
        "defenseState": defense_state, "defenseLevel": 4667.16,
        "primaryTriggerId": "LONG-DEFENSE-4667",
        "decisionBasisCandleCloseTime": candle_time,
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


def test_wait_fingerprint_ignores_new_context_candle_but_confirmed_event_does_not():
    wait_0115 = _event(price=4667.08)
    wait_0130 = _event(price=4667.08, candle_time="2026-08-25T01:30:00+00:00")
    assert notification_fingerprint(wait_0115) == notification_fingerprint(wait_0130)

    confirmed_0115 = _event(
        price=4666.0, defense_state="BROKEN_CONFIRMED",
        event_type="DEFENSE_BROKEN_CONFIRMED")
    confirmed_0130 = _event(
        price=4666.0, defense_state="BROKEN_CONFIRMED",
        candle_time="2026-08-25T01:30:00+00:00",
        event_type="DEFENSE_BROKEN_CONFIRMED")
    assert notification_fingerprint(confirmed_0115) != notification_fingerprint(
        confirmed_0130)


def test_case_g_defense_break_invalidates_scenario_but_preserves_htf_bias():
    data = _data(close=4663.0)
    data["normalized_analysis"]["timeframeAssessments"].insert(
        0, {"timeframe": "1D", "trend": "bullish"})
    health = evaluate_decision_health(data, now=NOW)
    defense = evaluate_defense_state(
        defense_level=4667.16, side="LONG", current_price=4663.0,
        atr15=2.0, closed_context=health["latestClosed15m"],
        entry_confirmation=health["entryConfirmation"], previous={})
    result = {**health, **defense}

    assert result["defenseState"] == "BROKEN_CONFIRMED"
    assert result["activeLongScenario"] == "INVALIDATED"
    assert result["marketBias"] == "BULLISH"
    assert result["shortTermStructure"] == "CORRECTIVE"
    assert result["entryConfirmation"] == "WAIT_NEW_STRUCTURE"
    assert result["searchNextScenario"] is True
    assert result["nextScenarioCandidates"] == ["DEEP_PULLBACK", "BREAKDOWN_RETEST"]
    assert result["shortNow"] is False
