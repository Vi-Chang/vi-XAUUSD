from copy import deepcopy

from app.engines.canonical_decision import build_canonical_decision
from app.engines.decision_presentation import format_decision_message
from app.engines.decision_snapshot import build_decision_snapshot


def payload(rr=1.8, *, trigger=4656.14, setup_type="BREAKOUT", price=4655.0):
    tp1 = round(price + rr * (price - 4640.0), 2)
    candidate = {
        "source": setup_type, "timeframe": "15M", "direction": "LONG",
        "strength": 80, "confidence": 80, "reason_codes": [],
        "trigger_price": trigger, "invalidation_price": 4640.0,
        "entry_zone": (4654.0, 4658.0), "chase_limit": 4660.0,
        "targets": (tp1, tp1 + 10, tp1 + 20), "scenario_id": "LONG-1",
        "scenario_version": 1, "setup_type": setup_type,
        "risk_reward": rr, "lifecycle_state": "ENTRY_READY",
    }
    final = {
        "state": "LONG_READY", "finalAction": "ENTER_LONG", "canEnter": True,
        "direction": "LONG", "selectedScenarioId": "LONG-1",
        "selectedScenarioVersion": 1, "signalCandidates": [candidate],
        "humanSummary": "進場條件已成立", "effectiveRR": rr,
        "entryZone": {"low": 4654.0, "high": 4658.0},
        "invalidationPrice": 4640.0, "targets": [tp1],
    }
    return {
        "symbol": "XAUUSD", "timestamp_utc": "2026-08-24T13:16:00Z",
        "current_price": {"mid": price, "last_update": "2026-08-24T13:16:00Z"},
        "data_quality": {"status": "GOOD"},
        "normalized_analysis": {
            "currentPrice": price, "marketDataStatus": "GOOD",
            "marketDataTimestamp": "2026-08-24T13:15:00Z",
            "lastClosedCandleTimestamp": "2026-08-24T13:15:00Z",
            "lastClosedCandlePrice": 4657.2, "triggerLevel": 4650.08,
            "invalidationLevel": 4625.0,
        },
        "final_decision_state": final, "decision_assistant": {},
        "decision": {"signal_score": 80}, "position_management": {},
    }


def test_one_canonical_trigger_is_used_by_snapshot_and_telegram():
    data = payload()
    data["final_decision_state"].update(
        state="WAIT", finalAction="WAIT", canEnter=False,
        humanSummary="等待收盤確認")
    data["normalized_analysis"]["lastClosedCandlePrice"] = 4650.0
    snapshot = build_decision_snapshot(data)
    canonical = snapshot["canonicalDecision"]
    assert canonical["canonicalNextTrigger"]["level"] == 4656.14
    assert snapshot["nextTrigger"].startswith("15M 收盤站上 4656.14")
    event = {"event_type": "ENTRY_READY", "currentPrice": 4655.0,
             "canonicalDecision": canonical}
    message = format_decision_message(event)
    assert "4656.14" in message and "4650.08" not in message


def test_early_strength_and_entry_confirmation_have_distinct_semantics():
    data = payload()
    canonical = build_canonical_decision(data, data["final_decision_state"])
    assert canonical["earlyStrengthLevel"] == {"level": 4650.08, "label": "初步轉強價"}
    assert canonical["entryConfirmationLevel"] == 4656.14


def test_rr_below_gate_is_no_entry_and_never_best_zone():
    data = payload(rr=.87)
    canonical = build_canonical_decision(data, data["final_decision_state"])
    entry = canonical["newEntryDecision"]
    assert entry["action"] == "WAIT" and entry["tradeStatus"] == "NO_ENTRY_RR"
    assert entry["selectedSetup"]["entryZoneLabel"] == "不建議進場區"
    assert entry["selectedSetup"]["requiredEntryPriceForMinRR"] == 4651.22


def test_failed_breakout_bias_setup_and_entry_permission_are_separate():
    data = payload()
    data["failed_breakout_rejection_engine"] = {
        "side": "LONG", "state": "REPEATED_REJECTION",
        "biasState": "NEUTRAL_BULLISH", "marketBias": "NEUTRAL",
        "biasConfidence": 56, "setupQuality": "POOR", "entryEligibility": "NO",
        "supportState": "SAFE", "positionRiskState": "POSITION_WARNING",
    }
    data["decision_health_state"] = {
        "dataHealth": "HEALTHY", "entryConfirmation": "READY",
        "marketBias": "NEUTRAL", "marketBiasState": "NEUTRAL_BULLISH",
        "biasConfidence": 56,
    }
    canonical = build_canonical_decision(data, data["final_decision_state"])
    assert canonical["marketBiasState"] == "NEUTRAL_BULLISH"
    assert canonical["setupQuality"] == "POOR"
    assert canonical["entryEligibility"] == "NO"
    assert canonical["newEntryDecision"]["action"] == "WAIT"


def test_pullback_with_better_rr_is_preferred_over_breakout():
    data = payload(rr=.87)
    pullback = deepcopy(data["final_decision_state"]["signalCandidates"][0])
    pullback.update({"scenario_id": "PB-1", "setup_type": "PULLBACK_LONG",
                     "risk_reward": 1.9, "entry_zone": (4644.0, 4648.0),
                     "lifecycle_state": "ARMED"})
    data["final_decision_state"]["signalCandidates"].append(pullback)
    canonical = build_canonical_decision(data, data["final_decision_state"])
    assert canonical["newEntryDecision"]["preferredRoute"] == "PULLBACK"
    assert (canonical["newEntryDecision"]["pullbackLong"]["riskReward"] >
            canonical["newEntryDecision"]["breakoutLong"]["riskReward"])


def test_actual_position_entry_and_size_are_not_candidate_values():
    data = payload()
    data["position_management"] = {
        "has_position": True, "position_side": "LONG", "entry_price": 4642.87,
        "position_size": .2, "recommended_action": "HOLD", "unrealized_pnl": 24.5,
    }
    position = build_canonical_decision(data, data["final_decision_state"])["positionManagement"]
    assert position["actualEntryPrice"] == 4642.87
    assert position["actualSize"] == .2 and position["positionKnown"]
    assert position["riskRewardFromActualEntry"] != 1.8


def test_forming_price_cannot_become_close_confirmation():
    data = payload(price=4665.0)
    data["normalized_analysis"]["lastClosedCandlePrice"] = 4650.0
    canonical = build_canonical_decision(data, data["final_decision_state"])
    assert canonical["confirmationSource"] == "CLOSED_CANDLE"
    assert canonical["lastClosedCandlePrice"] == 4650.0


def test_stale_data_forces_wait_even_when_engine_says_enter():
    data = payload()
    data["normalized_analysis"]["marketDataStatus"] = "STALE"
    canonical = build_canonical_decision(data, data["final_decision_state"])
    assert canonical["dataStale"]
    assert canonical["primaryAction"] == "WAIT"
    assert canonical["newEntryDecision"]["tradeStatus"] == "WAIT_DATA_CONFIRMATION"


def test_degraded_data_keeps_prices_as_reference_without_invalidating_setup():
    data = payload()
    snapshot = {"scenarioId": "LONG-OLD", "entryZone": [4639, 4643]}
    data["decision_health_state"] = {
        "dataHealth": "DEGRADED", "canonicalDataHealth": "DEGRADED",
        "entryConfirmation": "WAIT_15M_CLOSE", "marketBias": "BULLISH",
        "lastValidStrategySnapshot": snapshot,
        "strategySnapshotMode": "REFERENCE_ONLY",
    }
    canonical = build_canonical_decision(data, data["final_decision_state"])
    entry = canonical["newEntryDecision"]

    assert canonical["dataHealth"] == "DEGRADED"
    assert canonical["scenarioValidity"] == "BLOCKED_BY_DATA"
    assert canonical["scenarioInvalidated"] is False
    assert canonical["lastValidStrategySnapshot"] == snapshot
    assert entry["canEnter"] is False
    assert entry["levelsExecutable"] is False
    assert entry["priceStatus"] == "REFERENCE_ONLY"
    assert entry["tradeStatus"] == "WAIT_DATA_CONFIRMATION"
    assert "已失效" not in canonical["primaryReason"]


def test_latest_final_health_overrides_stale_embedded_observation_in_snapshot():
    data = payload()
    data["decision_health_state"] = {
        "dataHealth": "STALE",
        "canonicalDataHealth": "STALE",
        "entryConfirmation": "BLOCKED_BY_DATA",
        "marketBias": "BULLISH",
    }
    data["final_decision_state"].update({
        "dataHealth": "HEALTHY",
        "canonicalDataHealth": "HEALTHY",
        "entryConfirmation": "READY",
        "marketBias": "BULLISH",
        "scenarioValidity": "PENDING_CONFIRMATION",
    })

    snapshot = build_decision_snapshot(data)
    canonical = snapshot["canonicalDecision"]

    assert canonical["dataHealth"] == "HEALTHY"
    assert canonical["dataStale"] is False
    assert canonical["scenarioValidity"] != "BLOCKED_BY_DATA"
    assert snapshot["dataHealth"]["healthy"] is True


def test_healthy_wait_for_15m_close_is_wait_not_data_invalid():
    data = payload()
    data["decision_health_state"] = {
        "dataHealth": "HEALTHY",
        "canonicalDataHealth": "HEALTHY",
        "entryConfirmation": "WAIT_15M_CLOSE",
        "marketBias": "BULLISH",
    }
    data["final_decision_state"].update({
        "dataHealth": "HEALTHY",
        "canonicalDataHealth": "HEALTHY",
        "entryConfirmation": "WAIT_15M_CLOSE",
        "marketBias": "BULLISH",
        "scenarioValidity": "PENDING_CONFIRMATION",
        "canEnter": False,
        "finalAction": "WAIT",
    })

    canonical = build_decision_snapshot(data)["canonicalDecision"]

    assert canonical["dataHealth"] == "HEALTHY"
    assert canonical["dataStale"] is False
    assert canonical["scenarioValidity"] == "PENDING_CONFIRMATION"
    assert canonical["primaryAction"] == "WAIT"
    assert canonical["newEntryDecision"]["canEnter"] is False


def test_unknown_position_is_explicit_not_hypothetical():
    data = payload()
    data["position_management"] = {
        "has_position": False, "entry_price": 4611.0, "position_size": 9,
        "recommended_action": "EXIT", "stop_loss": 4600.0,
    }
    position = build_canonical_decision(data, data["final_decision_state"])["positionManagement"]
    assert not position["positionKnown"]
    assert position["message"] == "未取得實際持倉資料"
    assert position["actualEntryPrice"] is None
    assert position["actualSize"] is None
    assert position["actualSide"] is None
    assert position["action"] is None
    assert position["tacticalDefense"] is None
    assert position["targets"] == []


def test_engine_selected_setup_does_not_change_between_wait_cards():
    data = payload()
    final = data["final_decision_state"]
    final.update({"state": "WAIT", "finalAction": "WAIT", "canEnter": False})
    alternative = deepcopy(final["signalCandidates"][0])
    alternative.update({
        "scenario_id": "OTHER-2", "strength": 99,
        "entry_zone": (4630.0, 4632.0), "trigger_price": 4633.0,
    })
    final["signalCandidates"].append(alternative)
    canonical = build_canonical_decision(data, final)
    assert canonical["activeSetupId"] == "LONG-1"
    assert canonical["newEntryDecision"]["selectedSetup"]["setupId"] == "LONG-1"
    assert canonical["canonicalNextTrigger"]["setupId"] == "LONG-1"


def test_snapshot_cannot_republish_raw_ready_when_canonical_rr_blocks():
    data = payload(rr=.87)
    snapshot = build_decision_snapshot(data)
    assert snapshot["canEnter"] is False
    assert snapshot["action"] == "WAIT"
    assert snapshot["tradeStatus"] == "NO_ENTRY_RR"
    assert snapshot["finalDecision"]["action"] == "WAIT"


def test_actual_position_management_does_not_borrow_candidate_targets():
    data = payload()
    data["position_management"] = {
        "has_position": True, "position_side": "LONG", "entry_price": 4642.87,
        "position_size": .2, "recommended_action": "HOLD", "stop_loss": 4630.0,
    }
    position = build_canonical_decision(data, data["final_decision_state"])["positionManagement"]
    assert position["tacticalDefense"] == 4630.0
    assert position["targets"] == []
    assert position["riskRewardFromActualEntry"] is None


def test_stale_nested_setup_mismatch_is_recomputed_without_false_engine_conflict():
    data = payload()
    data["final_decision_state"]["selectedScenarioId"] = "MISSING"
    canonical = build_canonical_decision(data, data["final_decision_state"])
    assert canonical["primaryAction"] == "WAIT"
    assert canonical["newEntryDecision"]["canEnter"] is False
    assert canonical["newEntryDecision"]["tradeStatus"] != "SYSTEM_CONFLICT"
    assert canonical["conflictType"] != "TRUE_ENGINE_CONFLICT"
    assert canonical["engineSelectedSetupId"] == canonical["activeSetupId"]
