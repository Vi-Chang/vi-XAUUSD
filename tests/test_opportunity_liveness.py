from app.engines.early_entry_candidate import (
    apply_canonical_entry_result,
    evaluate_early_entry_candidate,
    opportunity_capabilities,
)
from app.engines.opportunity_coverage_watchdog import evaluate_opportunity_coverage
from app.services.notification_coordinator import coordinate_notification_intents


def _opportunity(side: str, *, low: float, high: float, rr: float = 2.0) -> dict:
    return {
        "opportunity_id": f"OP-{side}", "setup_id": f"BASE-{side}",
        "side": side, "type": "SHALLOW_PULLBACK", "state": "WAIT_CONFIRMATION",
        "primary_eligible": True, "entry_zone": {"lower": low, "upper": high},
        "tactical_stop": low - 2 if side == "LONG" else high + 2,
        "target1": high + 6 if side == "LONG" else low - 6,
        "estimated_rr": rr, "opportunity_score": 80,
        "distance_from_current": 0.0,
    }


def _data(*, price: float = 100, health: str = "HEALTHY",
          behavior: str = "RANGE", break_state: str = "LEVEL_TEST") -> dict:
    return {
        "symbol": "XAUUSD-LIVENESS", "timestamp_utc": "2026-08-25T05:00:00+00:00",
        "normalized_analysis": {"currentPrice": price, "atr15": 2.0,
                                "trendBias": "bullish", "marketDataStatus": health},
        "decision_health_state": {"marketBias": "BULLISH", "dataHealth": health},
        "entry_opportunity_engine": {
            "primaryOpportunityId": "OP-LONG",
            "opportunities": [_opportunity("LONG", low=99, high=101),
                              _opportunity("SHORT", low=99, high=101)],
        },
        "break_lifecycle_engine": {"state": break_state, "direction": "NONE"},
        "wick_rejection_engine": {"wick_rejection_state": "NONE",
                                   "wick_rejection_strength": "NONE"},
        "market_behavior_engine": {"state": behavior},
    }


def test_a_both_sides_are_evaluated_and_watch_is_not_entry():
    state, events = evaluate_early_entry_candidate(_data(), {"state": "IDLE"})
    assert state["candidates"]["LONG"]["state"] == "WATCH_LONG"
    assert state["candidates"]["SHORT"]["state"] == "WATCH_SHORT"
    assert {event["event_type"] for event in events} == {"EARLY_ENTRY_WATCH"}
    assert state["capabilities"]["entryAllowed"] is True


def test_b_location_reaction_and_structure_promote_prepare():
    state, _ = evaluate_early_entry_candidate(
        _data(behavior="RECOVERING", break_state="FAILED_BREAKDOWN"), {"state": "IDLE"})
    assert state["candidates"]["LONG"]["state"] == "PREPARE_LONG"
    assert state["candidates"]["LONG"]["nextAction"]


def test_c_degraded_15m_keeps_watch_prepare_but_blocks_ready():
    state, _ = evaluate_early_entry_candidate(
        _data(health="DEGRADED_15M", behavior="RECOVERING",
              break_state="FAILED_BREAKDOWN"), {"state": "IDLE"})
    assert state["capabilities"] == {
        "status": "DEGRADED_15M", "watchAllowed": True, "prepareAllowed": True,
        "entryAllowed": False, "reasons": ["CLOSED_15M_DEGRADED"],
    }
    gated = apply_canonical_entry_result(
        state, {"canEnter": True, "primaryAction": "BUY", "dataHealth": "DEGRADED_15M"},
        evaluated_at="2026-08-25T05:15:00+00:00")
    assert gated["candidates"]["LONG"]["state"] == "PREPARE_LONG"


def test_d_critical_data_blocks_all_opportunities():
    state, events = evaluate_early_entry_candidate(_data(health="FAILED"), {"state": "IDLE"})
    assert state["capabilities"]["watchAllowed"] is False
    assert all(item["state"] == "IDLE" for item in state["candidates"].values())
    assert events == []


def test_e_countertrend_is_observed_but_never_promoted_by_long_canonical_action():
    state, _ = evaluate_early_entry_candidate(_data(), {"state": "IDLE"})
    short = state["candidates"]["SHORT"]
    assert short["countertrend"] is True
    gated = apply_canonical_entry_result(
        state, {"canEnter": True, "primaryAction": "BUY", "dataHealth": "HEALTHY"},
        evaluated_at="2026-08-25T05:15:00+00:00")
    assert gated["candidates"]["SHORT"]["state"] == "WATCH_SHORT"


def test_f_coverage_gap_is_audit_only_and_never_retroactive_entry():
    early = {"candidates": {"LONG": {"state": "IDLE", "rejectionReasons": ["NO_REACTION"]},
                             "SHORT": {"state": "IDLE"}}}
    previous = {"sides": {"LONG": {"touchPrice": 100.0,
                                      "touchAt": "2026-08-25T04:45:00+00:00",
                                      "coveredAtTouch": False,
                                      "zone": {"low": 99, "high": 101}}}}
    state, events = evaluate_opportunity_coverage(_data(price=102), early, previous)
    assert events[0]["event_type"] == "OPPORTUNITY_COVERAGE_GAP"
    assert events[0]["notificationEligible"] is False
    assert state["sides"]["LONG"]["gapLogged"] is True


def _event(event_type: str, *, state: str, snapshot: str = "S1") -> dict:
    return {"eventId": f"{event_type}-{state}", "eventVersion": 1,
            "snapshotId": snapshot, "event_type": event_type,
            "currentState": state, "calculatedAt": "2026-08-25T05:00:00+00:00",
            "dataVersion": 7, "direction": "LONG", "setupId": "SETUP-1",
            "marketBias": "BULLISH", "dataHealth": "HEALTHY"}


def test_g_one_snapshot_produces_one_user_facing_notification():
    intents = coordinate_notification_intents("XAUUSD", [
        _event("EARLY_ENTRY_WATCH", state="WATCH_LONG"),
        _event("EARLY_ENTRY_PREPARE", state="PREPARE_LONG"),
        _event("MISSED_ENTRY", state="MISSED_ENTRY"),
    ])
    assert len(intents) == 1
    assert intents[0]["factCount"] == 3
    assert intents[0]["notificationCoordinator"] == "single-snapshot-v1"


def test_h_entry_ready_wins_notification_priority():
    intents = coordinate_notification_intents("XAUUSD", [
        _event("EARLY_ENTRY_PREPARE", state="PREPARE_LONG"),
        _event("ENTRY_READY", state="LONG_READY"),
    ])
    assert len(intents) == 1
    assert intents[0]["event_type"] == "ENTRY_READY"


def test_i_delivery_unknown_is_log_only():
    intents = coordinate_notification_intents("XAUUSD", [
        _event("DELIVERY_UNKNOWN", state="WAIT"),
    ])
    assert intents[0]["notificationRoute"] == "LOG_ONLY"
    assert intents[0]["notificationEligible"] is False


def test_j_every_rejection_has_a_reason_and_next_action():
    state, events = evaluate_early_entry_candidate(_data(price=110), {"state": "IDLE"})
    assert events == []
    for candidate in state["candidates"].values():
        assert candidate["rejectionReasons"]
        assert candidate["nextAction"]


def test_capabilities_do_not_treat_api_success_as_fresh_closed_candle():
    data = _data(health="DEGRADED_15M")
    data["api_ok"] = True
    assert opportunity_capabilities(data)["entryAllowed"] is False
