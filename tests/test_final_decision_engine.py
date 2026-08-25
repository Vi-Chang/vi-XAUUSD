from copy import deepcopy

from app.engines.final_decision_engine import (
    collect_signal_candidates,
    evaluate_final_decision,
)


def market(*, status="BREAKOUT_ENTRY_READY", rr=2.0, can_enter=True,
           price=100.5, spread=.2, regime="TREND_BULLISH"):
    setup = {
        "setupId": "BO-v1", "scenarioVersion": 1, "lineageId": "BO",
        "type": "BREAKOUT", "direction": "LONG", "status": status,
        "breakoutTrigger": 100.0, "entryZoneLow": 100.0,
        "entryZoneHigh": 101.0, "stopPrice": 98.0, "tp1": 104.0,
        "tp2": 106.0, "tp3": 108.0, "riskReward": rr,
        "signalScore": 85, "atr15": 10.0, "expiresAt": "2026-08-21T16:00:00Z",
    }
    return {
        "symbol": "XAUUSD", "version": 8,
        "timestamp_utc": "2026-08-21T15:01:00Z",
        "current_price": {"mid": price, "spread": spread, "last_update": "", "provider": ""},
        "data_quality": {"status": "GOOD", "source_mismatch": False},
        "event_risk": {"event_lockout": False, "post_event_wait": False},
        "normalized_analysis": {
            "currentPrice": price, "marketDataStatus": "GOOD", "atr15": 10.0,
            "marketDataTimestamp": "2026-08-21T15:00:00Z",
            "lastClosedCandleTimestamp": "2026-08-21T15:00:00Z",
            "lastClosedCandlePrice": 100.4, "trendBias": "bullish",
            "shortTermMomentum": "stable", "consistencyValid": True,
            "confirmationLevels": [],
        },
        "decision_assistant": {
            "regime": regime, "canEnter": can_enter,
            "tradeState": "ENTRY_READY" if can_enter else "WAIT_BREAKOUT",
            "direction": "LONG", "entryQualityScore": 85,
            "rewardRiskRatio": rr, "distanceInAtr": abs(price - 100.5) / 10,
            "scenarioId": "BO-v1", "scenarioVersion": 1,
            "scenarioType": "BREAKOUT", "targets": [104, 106, 108],
        },
        "breakout_setup_manager": {"activeSetup": setup, "setups": [setup]},
        "trend_continuation_engine": {"candidates": []},
        "entry_engine": {}, "market_decision": {"signal_score": 85},
    }


def test_signal_candidates_have_lineage_lifecycle_and_level_sources():
    candidate = collect_signal_candidates(market())[0]
    assert candidate.scenario_id == "BO-v1" and candidate.lineage_id == "BO"
    assert candidate.lifecycle_state == "ENTRY_READY"
    assert candidate.level_sources["trigger"]["source_candle"]


def test_strong_breakout_with_good_rr_enters_long():
    decision, events = evaluate_final_decision(market())
    assert decision["finalAction"] == "ENTER_LONG" and decision["canEnter"]
    assert decision["riskGate"] == "ENTRY_READY"
    assert {event["event_type"] for event in events} == {
        "ENTRY_READY", "CANDLE_FINALIZED"}
    assert all(event["latestClosedCandlePrice"] == 100.4 for event in events)


def test_overextended_breakout_does_not_enter():
    decision, _ = evaluate_final_decision(market(price=110.0))
    assert decision["finalAction"] in {"WAIT", "NO_TRADE"}
    assert decision["primaryReason"] == "OVEREXTENDED"


def test_htf_bullish_m15_pullback_never_enters_short():
    data = market(can_enter=False, regime="HTF_BULLISH_LTF_WEAKENING")
    decision, _ = evaluate_final_decision(data)
    assert decision["finalAction"] != "ENTER_SHORT"
    assert "TIMEFRAME_CONFLICT" in decision["secondaryReasons"]


def test_low_rr_is_risk_veto_even_when_candidate_ready():
    decision, _ = evaluate_final_decision(market(rr=1.1))
    assert decision["finalAction"] == "NO_TRADE"
    assert decision["primaryReason"] == "RR_TOO_LOW"


def test_abnormal_spread_is_risk_veto():
    decision, _ = evaluate_final_decision(market(spread=20))
    assert decision["primaryReason"] == "SPREAD_TOO_HIGH"


def test_stale_data_is_highest_priority():
    data = market()
    data["normalized_analysis"]["marketDataStatus"] = "STALE"
    data["event_risk"]["event_lockout"] = True
    decision, _ = evaluate_final_decision(data)
    assert decision["primaryReason"] == "DATA_STALE"
    assert decision["riskGate"] == "DATA_INVALID"


def test_data_recovery_emits_a_distinct_canonical_event_once():
    stale_data = market()
    stale_data["normalized_analysis"]["marketDataStatus"] = "STALE"
    stale, _ = evaluate_final_decision(stale_data)
    recovered_data = market()
    recovered_data["signal_facts"] = [{
        "event_type": "DATA_RECOVERED",
        "dataIncidentId": "DATA-20260821-1500",
        "dataHealthEventKey": "DATA_RECOVERED:DATA-20260821-1500",
    }]
    recovered, events = evaluate_final_decision(recovered_data, previous=stale)
    assert recovered["primaryReason"] != "DATA_STALE"
    assert "DATA_RECOVERED" in {event["event_type"] for event in events}
    _same, repeated = evaluate_final_decision(market(), previous=recovered)
    assert "DATA_RECOVERED" not in {event["event_type"] for event in repeated}


def test_broken_scenario_waits_for_new_structure_without_stale_data_label():
    data = market(can_enter=False)
    data["decision_health_state"] = {
        "marketBias": "BULLISH", "dataHealth": "HEALTHY",
        "entryConfirmation": "WAIT_NEW_STRUCTURE",
        "defenseState": "BROKEN_CONFIRMED", "defenseLevel": 99.0,
        "activeLongScenario": "INVALIDATED", "activeShortScenario": "ACTIVE",
        "shortTermStructure": "CORRECTIVE", "searchNextScenario": True,
        "nextScenarioCandidates": ["DEEP_PULLBACK", "BREAKDOWN_RETEST"],
    }
    decision, events = evaluate_final_decision(data)
    assert decision["primaryReason"] == "WAIT_NEW_STRUCTURE"
    assert decision["state"] == "WAIT_NEW_STRUCTURE"
    assert decision["entrySignal"] == "WAIT"
    assert decision["marketBias"] == "BULLISH"
    assert "DATA_STALE" not in {event["event_type"] for event in events}


def test_event_blackout_blocks_entry():
    data = market(); data["event_risk"]["event_lockout"] = True
    decision, _ = evaluate_final_decision(data)
    assert decision["primaryReason"] == "EVENT_BLACKOUT"


def test_range_market_is_no_trade():
    data = market(can_enter=False, regime="RANGE")
    decision, _ = evaluate_final_decision(data)
    assert decision["finalAction"] == "NO_TRADE"


def test_position_management_precedes_new_entry():
    data = market(); data["position_management"] = {"has_position": True}
    decision, _ = evaluate_final_decision(data)
    assert decision["finalAction"] == "MANAGE_POSITION"


def test_micro_price_change_does_not_create_new_decision_version():
    first, _ = evaluate_final_decision(market(price=100.50))
    second, events = evaluate_final_decision(market(price=100.55), previous=first)
    assert second["decisionVersion"] == first["decisionVersion"]
    assert second["decisionChanged"] is False and events == []


def test_position_risk_transition_emits_even_when_high_level_action_is_unchanged():
    first, _ = evaluate_final_decision(market())
    unchanged = market()
    unchanged["signal_facts"] = [{
        "event_type": "POSITION_WARNING", "setupId": "BO-v1",
        "warningLevel": 99.0, "currentState": "WARNING",
        "tradeThesis": {"thesisDescription": "固定交易論點"},
    }]
    second, events = evaluate_final_decision(unchanged, previous=first)
    assert second["decisionChanged"] is False
    warning = next(event for event in events
                   if event["event_type"] == "POSITION_WARNING")
    assert warning["warningLevel"] == 99.0


def test_scenario_version_change_creates_new_decision_version():
    first, _ = evaluate_final_decision(market())
    changed = market()
    changed["breakout_setup_manager"]["activeSetup"]["scenarioVersion"] = 2
    changed["breakout_setup_manager"]["setups"][0]["scenarioVersion"] = 2
    second, _ = evaluate_final_decision(changed, previous=first)
    assert second["decisionVersion"] == first["decisionVersion"] + 1


def test_wait_is_dashboard_only_not_periodic_telegram():
    data = market(status="WAIT_BREAKOUT_CONFIRMATION", can_enter=False)
    first, events1 = evaluate_final_decision(data)
    _second, events2 = evaluate_final_decision(data, previous=first)
    assert first["finalAction"] == "WAIT"
    assert [event["event_type"] for event in events1] == ["CANDLE_FINALIZED"]
    assert events2 == []


def test_historical_calibration_requires_enough_samples():
    data = market()
    data["historical_calibration"] = {"buckets": [
        {"low": 80, "high": 89, "sampleSize": 50, "observedSuccessRate": .56}]}
    decision, _ = evaluate_final_decision(data)
    assert decision["rawScore"] == 85
    assert decision["calibratedProbability"] == .56


def test_fake_breakout_is_no_trade():
    data = market(can_enter=False, regime="REVERSAL_RISK")
    data["decision_assistant"]["noTradeReasons"] = ["假突破風險尚未解除"]
    decision, _ = evaluate_final_decision(data)
    assert decision["finalAction"] == "NO_TRADE"


def test_retest_ready_is_distinct_executable_setup():
    data = market(status="BREAKOUT_RETEST_READY")
    data["breakout_setup_manager"]["activeSetup"]["type"] = "BREAKOUT_RETEST"
    data["breakout_setup_manager"]["setups"][0]["type"] = "BREAKOUT_RETEST"
    decision, _ = evaluate_final_decision(data)
    assert decision["finalAction"] == "ENTER_LONG"
    assert decision["selectedSetupType"] == "BREAKOUT_RETEST"


def test_same_decision_has_stable_id_after_restart_like_recalculation():
    first, _ = evaluate_final_decision(market())
    recreated, _ = evaluate_final_decision(deepcopy(market()), previous={
        "decisionSignature": first["decisionSignature"],
        "decisionVersion": first["decisionVersion"], "state": first["state"],
    })
    assert recreated["decisionId"] == first["decisionId"]
    assert not recreated["decisionChanged"]


def test_confirmed_opposite_setup_still_requires_and_can_pass_canonical_risk_gates():
    data = market()
    data["fake_breakout_recovery"] = {
        "active": True,
        "state": "LONG_SETUP_CONFIRMED",
        "sourceFailureId": "failed-short-1",
        "invalidatedBreakoutDirection": "SHORT",
        "oppositeDirection": "LONG",
        "oppositeBiasBoost": 12,
        "nextAction": {
            "triggerLevel": 100.0,
            "invalidationLevel": 98.0,
            "targets": [104.0, 106.0, 108.0],
        },
    }
    decision, _ = evaluate_final_decision(data)
    assert decision["finalAction"] == "ENTER_LONG"
    assert decision["canEnter"] is True
    assert decision["entrySignal"] == "READY"


def test_scalp_recovery_confirmation_is_not_vetoed_by_old_trend_wait_state():
    data = market()
    data["breakout_setup_manager"] = {"activeSetup": {}, "setups": []}
    data["decision_health_state"] = {
        "marketBias": "BULLISH", "dataHealth": "HEALTHY",
        "entryConfirmation": "WAIT_NEW_STRUCTURE", "defenseState": "INACTIVE",
    }
    data["fake_breakout_recovery"] = {
        "active": True,
        "state": "LONG_SETUP_CONFIRMED",
        "scalpEntryReady": True,
        "executableRR": 2.0,
        "sourceFailureId": "failed-short-scalp",
        "invalidatedBreakoutDirection": "SHORT",
        "oppositeDirection": "LONG",
        "oppositeBiasBoost": 20,
        "expiresAt": "2026-08-21T16:00:00Z",
        "nextAction": {
            "triggerLevel": 100.0,
            "entryZoneLow": 100.0,
            "entryZoneHigh": 101.0,
            "invalidationLevel": 98.0,
            "targets": [104.0, 106.0, 108.0],
            "estimatedRR": 2.0,
        },
    }
    decision, _ = evaluate_final_decision(data)
    assert decision["selectedSetupType"] == "FAKE_BREAKOUT_RECOVERY"
    assert decision["entryConfirmation"] == "READY"
    assert decision["finalAction"] == "ENTER_LONG"
    assert decision["canEnter"] is True
