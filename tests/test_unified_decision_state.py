from app.engines.unified_decision_state import (
    enforce_scenario_consistency,
    evaluate_unified_decision,
)
from app.schemas.analysis import Scenario


def payload(price, *, status="NO_SETUP", market_status="GOOD", tracker=None):
    return {
        "version": 7,
        "timestamp_utc": "2026-08-20T14:00:00+00:00",
        "market_decision": {"action": "WATCH", "reason": "等待結構確認"},
        "entry_engine": {"status": status, "direction": "NONE"},
        "virtual_profit_tracker": tracker or {},
        "normalized_analysis": {
            "currentPrice": price,
            "marketDataTimestamp": "2026-08-20T14:00:00+00:00",
            "lastClosedCandleTimestamp": "2026-08-20T13:45:00+00:00",
            "marketDataStatus": market_status,
            "consistencyValid": True,
            "atr15": 10,
            "confirmationLevels": [
                {"kind": "support", "timeframe": "15M", "price": 4480},
                {"kind": "resistance", "timeframe": "15M", "price": 4490},
            ],
        },
    }


def test_4450_to_4481_to_4495_emits_rebound_reclaim_and_wait_close():
    state, _ = evaluate_unified_decision(payload(4450))
    state, events = evaluate_unified_decision(payload(4481), state)
    kinds = [event["event_type"] for event in events]
    assert "PRICE_REBOUND" in kinds
    assert "KEY_LEVEL_RECLAIMED" in kinds
    state, events = evaluate_unified_decision(payload(4495), state)
    kinds = [event["event_type"] for event in events]
    assert "AWAIT_CLOSE_CONFIRMATION" in kinds
    assert "15 分鐘收盤站上 4490.00" in state["confirmation"]


def test_wait_forces_both_scenarios_out_of_executable_status():
    long, short = enforce_scenario_consistency(
        "WAIT", Scenario(status="PREPARE"), Scenario(status="TRIGGERED")
    )
    assert long.status == short.status == "WATCH"


def test_first_target_has_position_management_and_flat_no_chase():
    tracker = {"active": True, "direction": "LONG", "events": [{"event_type": "TP1"}]}
    state, events = evaluate_unified_decision(payload(4500, tracker=tracker))
    assert state["state"] == "LONG_MANAGE"
    assert "禁止追價" in state["flat_action"]
    assert any(event["event_type"] == "FIRST_TARGET_REACHED" for event in events)


def test_new_quote_updates_source_price_and_quote_metadata():
    state, _ = evaluate_unified_decision(payload(4450))
    newer = payload(4481)
    newer["normalized_analysis"]["marketDataTimestamp"] = "2026-08-20T14:01:00+00:00"
    state, _ = evaluate_unified_decision(newer, state)
    assert state["source_price"] == 4481
    assert state["quote_time"].endswith("14:01:00+00:00")


def test_stale_data_is_never_ready():
    state, events = evaluate_unified_decision(
        payload(4495, status="ENTRY_TRIGGERED", market_status="STALE")
    )
    assert state["state"] == "DATA_STALE"
    assert state["action"] == "暫停交易"
    assert any(event["event_type"] == "DATA_STALE" for event in events)


def test_rehydrated_same_state_and_price_does_not_repeat_notification():
    state, _ = evaluate_unified_decision(payload(4481))
    _same, events = evaluate_unified_decision(payload(4481), state)
    assert events == []


def test_ready_is_downgraded_when_cost_adjusted_rr_is_too_low():
    data = payload(100, status="ENTRY_TRIGGERED")
    data["entry_engine"].update({"direction": "LONG", "suggested_entry": 100,
                                  "stop_loss": 99, "take_profit_1": 101.6})
    data["current_price"] = {"spread": 0.5}
    data["normalized_analysis"]["eventDataStatus"] = "GOOD"
    state, events = evaluate_unified_decision(data)
    assert state["state"] == "LONG_WATCH"
    assert any(event["executionCosts"]["netRiskReward"] < 1.5 for event in events)


def test_failed_event_data_caps_confidence_without_erasing_direction():
    data = payload(100, status="SETUP_WATCH")
    data["entry_engine"].update({"direction": "LONG", "confidence_score": 90})
    data["normalized_analysis"]["eventDataStatus"] = "FAILED"
    state, _ = evaluate_unified_decision(data)
    assert state["state"] == "LONG_WATCH"
    assert state["confidence"] == 55
