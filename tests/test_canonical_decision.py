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
            "lastClosedCandlePrice": 4655.2, "triggerLevel": 4650.08,
            "invalidationLevel": 4625.0,
        },
        "final_decision_state": final, "decision_assistant": {},
        "decision": {"signal_score": 80}, "position_management": {},
    }


def test_one_canonical_trigger_is_used_by_snapshot_and_telegram():
    data = payload()
    snapshot = build_decision_snapshot(data)
    canonical = snapshot["canonicalDecision"]
    assert canonical["canonicalNextTrigger"]["level"] == 4656.14
    assert snapshot["nextTrigger"] == "15M 收盤站上 4656.14"
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


def test_unknown_position_is_explicit_not_hypothetical():
    data = payload()
    position = build_canonical_decision(data, data["final_decision_state"])["positionManagement"]
    assert not position["positionKnown"]
    assert position["message"] == "未取得實際持倉資料"
