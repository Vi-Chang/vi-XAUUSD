from datetime import datetime, timezone

from app.engines.decision_health import (
    evaluate_decision_health,
    evaluate_defense_state,
    get_latest_valid_closed_15m,
    is_allowed_scenario_transition,
    is_confirmed_break,
    resolve_market_context,
)
from app.engines.decision_presentation import format_decision_message
from app.engines.scenario_execution import can_execute_scenario
from app.services.alert_aggregator import (
    is_meaningful_change,
    notification_fingerprint,
    notification_state_regression,
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


def _confirmed_scenario() -> dict:
    return evaluate_defense_state(
        defense_level=4656.07, side="LONG", current_price=4650.0,
        atr15=2.0,
        closed_context={"close": 4650.0,
                        "closeTime": "2026-08-25T02:45:00+00:00"},
        entry_confirmation="READY", previous={}, scenario_id="LONG-A",
        scenario_version=3, structure_version="SV-17")


def test_state_transition_matrix_makes_confirmed_and_invalidated_terminal():
    assert is_allowed_scenario_transition("TESTING", "BROKEN_PENDING_CLOSE")
    assert is_allowed_scenario_transition("BROKEN_PENDING_CLOSE", "RECLAIMED")
    assert is_allowed_scenario_transition("BROKEN_PENDING_CLOSE", "BROKEN_CONFIRMED")
    assert not is_allowed_scenario_transition(
        "BROKEN_CONFIRMED", "BROKEN_PENDING_CLOSE")
    assert not is_allowed_scenario_transition(
        "BROKEN_CONFIRMED", "RECLAIMED", scenario_terminal=True)


def test_replay_case_a_confirmed_break_persists_monotonic_event():
    result = _confirmed_scenario()
    assert result["defenseState"] == "BROKEN_CONFIRMED"
    assert result["scenarioState"] == "INVALIDATED"
    assert result["scenarioTerminal"] is True
    assert result["canReopen"] is False
    assert len(result["confirmedStrategyEvents"]) == 1
    event = result["confirmedStrategyEvents"][0]
    assert event["scenarioId"] == "LONG-A"
    assert event["eventType"] == "DEFENSE_BROKEN_CONFIRMED"
    assert event["structureVersion"] == "SV-17"


def test_replay_case_b_stale_data_cannot_downgrade_confirmed_break():
    previous = _confirmed_scenario()
    result = evaluate_defense_state(
        defense_level=4656.07, side="LONG", current_price=4646.7,
        atr15=2.0, closed_context=None, entry_confirmation="BLOCKED_BY_DATA",
        previous=previous, scenario_id="LONG-A", scenario_version=3,
        structure_version="SV-17")
    assert result["defenseState"] == "BROKEN_CONFIRMED"
    assert result["scenarioState"] == "INVALIDATED"
    assert result["entryConfirmation"] == "BLOCKED_BY_DATA"
    assert len(result["confirmedStrategyEvents"]) == 1

    later_close = evaluate_defense_state(
        defense_level=4656.07, side="LONG", current_price=4648.0,
        atr15=2.0,
        closed_context={"close": 4648.0,
                        "closeTime": "2026-08-25T03:00:00+00:00"},
        entry_confirmation="READY", previous=result, scenario_id="LONG-A",
        scenario_version=3, structure_version="SV-17")
    assert later_close["defenseState"] == "BROKEN_CONFIRMED"
    assert len(later_close["confirmedStrategyEvents"]) == 1


def test_replay_case_c_reclaim_creates_new_identity_without_reviving_old_scenario():
    previous = _confirmed_scenario()
    result = evaluate_defense_state(
        defense_level=4656.07, side="LONG", current_price=4662.0,
        atr15=2.0,
        closed_context={"close": 4662.0,
                        "closeTime": "2026-08-25T03:00:00+00:00"},
        entry_confirmation="READY", previous=previous, scenario_id="LONG-A",
        scenario_version=3, structure_version="SV-17")
    assert result["defenseState"] == "BROKEN_CONFIRMED"
    assert result["scenarioState"] == "INVALIDATED"
    assert result["activeDefenseRole"] == "REFERENCE"
    assert result["historicalDefenseLevel"] == 4656.07
    assert result["reclaimEvent"]["previousScenarioId"] == "LONG-A"
    assert result["reclaimEvent"]["newScenarioId"] != "LONG-A"
    assert result["reclaimEvent"]["entry"] is None
    assert result["reclaimEvent"]["stopLoss"] is None
    assert result["reclaimEvent"]["targets"] == []
    assert result["reclaimEvent"]["requiresFullTradePlanRecalculation"] is True


def test_replay_case_d_new_scenario_id_starts_fresh_lifecycle():
    old = _confirmed_scenario()
    new_id = "LONG-B"
    result = evaluate_defense_state(
        defense_level=4660.0, side="LONG", current_price=4665.0,
        atr15=2.0,
        closed_context={"close": 4665.0,
                        "closeTime": "2026-08-25T03:15:00+00:00"},
        entry_confirmation="READY", previous=old, scenario_id=new_id,
        scenario_version=1, structure_version="SV-18")
    assert result["scenarioId"] == new_id
    assert result["scenarioState"] == "ACTIVE"
    assert result["scenarioTerminal"] is False
    assert result["defenseState"] != "BROKEN_CONFIRMED"
    assert old["confirmedStrategyEvents"][0]["scenarioId"] == "LONG-A"


def test_replay_case_e_htf_bias_and_local_correction_are_independent():
    data = _data()
    data["normalized_analysis"]["timeframeAssessments"] = [
        {"timeframe": "1D", "trend": "bullish"},
        {"timeframe": "4H", "trend": "bullish"},
        {"timeframe": "1H", "trend": "bearish"},
        {"timeframe": "15M", "trend": "bearish"},
    ]
    health = evaluate_decision_health(data, now=NOW)
    context = resolve_market_context(data, htf_bias=health["marketBias"])
    assert context == {
        "htfBias": "BULLISH", "structure1h": "BEARISH_CORRECTION",
        "structure15m": "BEARISH", "shortTermState": "CORRECTIVE_BEARISH",
        "activeScenarioDirection": "NONE",
    }


def test_replay_case_f_long_invalidated_does_not_create_short_entry():
    result = _confirmed_scenario()
    assert result["activeLongScenario"] == "INVALIDATED"
    assert result["activeShortScenario"] == "ACTIVE"
    assert result["shortNow"] is False
    assert result["entryConfirmation"] == "WAIT_NEW_STRUCTURE"


def test_replay_case_g_telegram_blocks_same_scenario_state_regression():
    previous = {
        "setupId": "LONG-A", "direction": "LONG",
        "currentState": "INVALIDATED", "scenarioState": "INVALIDATED",
        "scenarioValidity": "INVALIDATED", "defenseState": "BROKEN_CONFIRMED",
    }
    stale_snapshot = {
        "setupId": "LONG-A", "direction": "LONG",
        "currentState": "BROKEN_PENDING_CLOSE", "scenarioState": "ACTIVE",
        "scenarioValidity": "ACTIVE", "defenseState": "BROKEN_PENDING_CLOSE",
    }
    blocked, reason = notification_state_regression(previous, stale_snapshot)
    assert blocked is True
    assert reason == "STATE_REGRESSION_BLOCKED"

    fresh_scenario = {**stale_snapshot, "setupId": "LONG-B"}
    assert notification_state_regression(previous, fresh_scenario) == (
        False, "DIFFERENT_SCENARIO")


def test_invalidated_telegram_separates_htf_and_local_structures():
    message = format_decision_message({
        "event_type": "DEFENSE_BROKEN_CONFIRMED",
        "currentPrice": 4646.7, "marketBias": "BULLISH",
        "dataHealth": "STALE", "defenseState": "BROKEN_CONFIRMED",
        "defenseLevel": 4656.07, "defenseSide": "LONG",
        "marketContext": {
            "htfBias": "BULLISH", "structure1h": "BEARISH_CORRECTION",
            "structure15m": "BEARISH", "activeScenarioDirection": "LONG",
        },
    })
    assert "高週期方向：🟢 偏多" in message
    assert "1H 結構：🟠 空方修正" in message
    assert "15M 結構：🔴 偏空" in message
    assert "資料狀態：🔴 行情資料過期" in message
    assert "永久失效" in message
    assert "正在測試原防守" not in message


def test_data_stale_message_preserves_invalidated_scenario_fact():
    message = format_decision_message({
        "event_type": "DATA_STALE",
        "canonicalDecision": {
            "scenarioState": "INVALIDATED", "scenarioValidity": "INVALIDATED",
            "entryConfirmation": "BLOCKED_BY_DATA", "marketBias": "BULLISH",
        },
    })
    assert "原交易劇本：仍維持已確認失效" in message
    assert "不會讓策略狀態退回" in message
