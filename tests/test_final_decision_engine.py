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
    assert decision["riskGate"] == "ENTRY_READY" and len(events) == 1


def test_overextended_breakout_does_not_enter():
    decision, _ = evaluate_final_decision(market(price=110.0))
    assert decision["finalAction"] in {"WAIT", "NO_TRADE"}
    assert decision["primaryReason"] == "OVEREXTENDED"


def test_htf_bullish_m15_pullback_never_enters_short():
    data = market(can_enter=False, regime="SHORT_WEAK_HTF_BULLISH")
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
    assert events1 == [] and events2 == []


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
