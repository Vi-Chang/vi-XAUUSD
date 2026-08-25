from app.db.session import init_db
from app.engines.decision_presentation import format_decision_message
from app.engines.early_entry_candidate import (
    apply_canonical_entry_result,
    evaluate_early_entry_candidate,
    is_chasing_entry,
)
from app.services.decision_outbox import persist_decision_events


def _data(*, side="LONG", price=100.0, break_state="FAILED_BREAKDOWN",
          break_direction="DOWN", health="HEALTHY", behavior="RANGE"):
    bullish = side == "LONG"
    return {
        "symbol": "XAUUSD-EARLY-TEST",
        "timestamp_utc": "2026-08-25T04:00:00+00:00",
        "normalized_analysis": {"currentPrice": price, "atr15": 2.0,
                                "trendBias": "bullish" if bullish else "bearish",
                                "marketDataStatus": health},
        "decision_health_state": {"marketBias": "BULLISH" if bullish else "BEARISH",
                                  "dataHealth": health},
        "entry_opportunity_engine": {
            "primaryOpportunityId": "OP-LONG" if bullish else "OP-SHORT",
            "opportunities": [{
                "opportunity_id": "OP-LONG" if bullish else "OP-SHORT",
                "setup_id": "BASE", "side": side, "type": "SHALLOW_PULLBACK",
                "state": "WAIT_CONFIRMATION", "primary_eligible": True,
                "entry_zone": {"lower": 99.0, "upper": 101.0},
                "tactical_stop": 97.0 if bullish else 103.0,
                "target1": 107.0 if bullish else 93.0,
                "estimated_rr": 2.0, "opportunity_score": 80,
                "distance_from_current": 0.0,
            }],
        },
        "break_lifecycle_engine": {"state": break_state, "direction": break_direction},
        "wick_rejection_engine": {"wick_rejection_state": "NO_SIGNIFICANT_REJECTION",
                                   "wick_rejection_strength": "NONE"},
        "market_behavior_engine": {"state": behavior},
    }


def test_runtime_support_that_keeps_breaking_does_not_prepare():
    state, events = evaluate_early_entry_candidate(
        _data(break_state="BREAK_CONFIRMED"), {"state": "IDLE"})
    assert state["state"] == "IDLE"
    assert events == []
    assert "NO_REACTION_CONFIRMATION" in state["evaluationLog"][-1]["rejection_reasons"]


def test_sweep_reclaim_creates_one_immutable_prepare_long():
    state, events = evaluate_early_entry_candidate(_data(), {"state": "IDLE"})
    assert state["state"] == "PREPARE_LONG"
    assert state["candidatePrice"] == 100.0
    assert state["candidateZone"] == {"low": 99.0, "high": 101.0}
    assert {"FAILED_BREAKDOWN", "SWEEP_RECLAIM"}.issubset(state["candidateReasons"])
    assert events[0]["eventKey"] == f"{state['setup_id']}:PREPARE"
    repeated, repeated_events = evaluate_early_entry_candidate(
        _data(price=100.2), state)
    assert repeated["candidatePrice"] == 100.0
    assert repeated_events == []


def test_formal_ready_is_only_copied_from_canonical_gate():
    state, _ = evaluate_early_entry_candidate(_data(), {"state": "IDLE"})
    still_prepare = apply_canonical_entry_result(
        state, {"canEnter": False, "primaryAction": "WAIT", "dataHealth": "HEALTHY"},
        evaluated_at="2026-08-25T04:15:00+00:00")
    assert still_prepare["state"] == "PREPARE_LONG"
    ready = apply_canonical_entry_result(
        state, {"canEnter": True, "primaryAction": "BUY", "dataHealth": "HEALTHY"},
        evaluated_at="2026-08-25T04:15:00+00:00")
    assert ready["state"] == "LONG_READY"


def test_prepare_invalidates_after_confirmed_directional_support_break():
    state, _ = evaluate_early_entry_candidate(_data(), {"state": "IDLE"})
    invalid, events = evaluate_early_entry_candidate(
        _data(price=96.8, break_state="BREAK_CONFIRMED", break_direction="DOWN"), state)
    assert invalid["state"] == "INVALIDATED_LONG"
    assert invalid["transitionReason"] == "STRUCTURE_INVALIDATED"
    assert events[0]["eventKey"].endswith(":INVALIDATED")


def test_prepare_becomes_missed_when_remaining_rr_is_not_safe():
    state, _ = evaluate_early_entry_candidate(_data(), {"state": "IDLE"})
    state = apply_canonical_entry_result(
        state, {"canEnter": True, "primaryAction": "BUY", "dataHealth": "HEALTHY"},
        evaluated_at="2026-08-25T04:05:00+00:00")
    missed, events = evaluate_early_entry_candidate(
        _data(price=104.0, break_state="LEVEL_TEST"), state)
    assert missed["state"] == "MISSED_LONG"
    assert missed["transitionReason"] in {"PRICE_TOO_EXTENDED", "RR_TOO_LOW"}
    assert events[0]["eventKey"].endswith(":MISSED")


def test_price_that_never_touched_or_became_actionable_is_not_missed():
    watching, _ = evaluate_early_entry_candidate(
        _data(price=102.0, break_state="LEVEL_TEST"), {"state": "IDLE"})
    assert watching["state"] == "WATCH_LONG"
    assert watching["zone_touched"] is False
    moved, events = evaluate_early_entry_candidate(
        _data(price=104.0, break_state="LEVEL_TEST"), watching)
    assert moved["state"] == "WATCH_LONG"
    assert moved["missedGateBlockedReason"] == "ENTRY_WAS_NEVER_ACTIONABLE"
    assert events == []


def test_active_candidate_zone_is_immutable_without_transition_reason():
    state, _ = evaluate_early_entry_candidate(_data(), {"state": "IDLE"})
    changed = _data(price=100.1)
    changed["entry_opportunity_engine"]["opportunities"][0]["entry_zone"] = {
        "lower": 93.0, "upper": 95.0}
    repeated, events = evaluate_early_entry_candidate(changed, state)
    assert repeated["candidateZone"] == {"low": 99.0, "high": 101.0}
    assert repeated["setup_id"] == state["setup_id"]
    assert repeated["zoneTransitionReason"] == "ACTIVE_ZONE_LOCKED_NO_CONFIRMED_TRANSITION"
    assert repeated["pendingZoneCandidate"] == {"low": 93.0, "high": 95.0}
    assert events == []


def test_invalidated_prepare_immediately_rescans_new_micro_structure():
    state, _ = evaluate_early_entry_candidate(_data(), {"state": "IDLE"})
    changed = _data(price=96.8, break_state="LEVEL_TEST", behavior="RECOVERING")
    changed["decision_health_state"].update({
        "defenseState": "BROKEN_CONFIRMED", "scenarioId": state["setup_id"]})
    changed["entry_opportunity_engine"]["opportunities"].append({
        "opportunity_id": "OP-LONG-NEW-HL", "setup_id": "BASE-NEW-HL",
        "side": "LONG", "type": "SHALLOW_PULLBACK", "state": "WAIT_CONFIRMATION",
        "primary_eligible": True, "entry_zone": {"lower": 96.0, "upper": 98.0},
        "tactical_stop": 94.0, "target1": 104.0, "estimated_rr": 2.0,
        "opportunity_score": 85, "distance_from_current": 0.0,
    })
    replacement, events = evaluate_early_entry_candidate(changed, state)
    assert replacement["setup_id"] != state["setup_id"]
    assert replacement["state"] == "PREPARE_LONG"
    assert replacement["zoneTransitionReason"] == "OLD_SETUP_INVALIDATED_NEW_MICRO_STRUCTURE"
    assert len(events) == 1
    assert events[0]["event_type"] == "EARLY_ENTRY_REPLACED"
    message = format_decision_message(events[0])
    assert "新的做多機會形成" in message
    assert "舊候選區已失效" in message
    assert "新觀察區：96.00–98.00" in message


def test_intrabar_stop_cross_does_not_replace_candidate_before_confirmation():
    state, _ = evaluate_early_entry_candidate(_data(), {"state": "IDLE"})
    pending, events = evaluate_early_entry_candidate(
        _data(price=96.8, break_state="LEVEL_TEST"), state)
    assert pending["state"] == "PREPARE_LONG"
    assert pending["setup_id"] == state["setup_id"]
    assert pending["intrabarStopCrossed"] is True
    assert pending["zoneTransitionReason"] == "ACTIVE_ZONE_LOCKED_NO_CONFIRMED_TRANSITION"
    assert events == []


def test_short_is_a_true_mirror_and_above_zone_is_not_short_chasing():
    state, events = evaluate_early_entry_candidate(
        _data(side="SHORT", break_state="FAILED_BREAKOUT", break_direction="UP"),
        {"state": "IDLE"})
    assert state["state"] == "PREPARE_SHORT"
    assert events[0]["candidateSide"] == "SHORT"
    chasing, _, _ = is_chasing_entry(
        side="SHORT", current_price=104, zone=(99, 101), stop=106, target=93,
        atr=2, minimum_rr=1.5)
    assert chasing is False
    chasing, _, _ = is_chasing_entry(
        side="SHORT", current_price=96, zone=(99, 101), stop=103, target=93,
        atr=2, minimum_rr=1.5)
    assert chasing is True


def test_degraded_data_preserves_bias_but_never_promotes_entry():
    state, _ = evaluate_early_entry_candidate(
        _data(health="DEGRADED_15M"), {"state": "IDLE"})
    assert state["state"] == "PREPARE_LONG"
    assert state["canonicalBias"] == "BULLISH"
    gated = apply_canonical_entry_result(
        state, {"canEnter": True, "primaryAction": "BUY", "dataHealth": "DEGRADED_15M"},
        evaluated_at="2026-08-25T04:15:00+00:00")
    assert gated["state"] == "PREPARE_LONG"


def test_late_momentum_does_not_create_a_post_hoc_prepare():
    state, events = evaluate_early_entry_candidate(
        _data(price=106, break_state="LEVEL_TEST", behavior="RECOVERING"),
        {"state": "IDLE"})
    assert state["state"] == "IDLE"
    assert events == []
    assert "PRICE_OUTSIDE_CANDIDATE_NEIGHBORHOOD" in state["evaluationLog"][-1]["rejection_reasons"]


def test_prepare_telegram_is_plain_and_not_an_entry_instruction():
    state, events = evaluate_early_entry_candidate(_data(), {"state": "IDLE"})
    message = format_decision_message({**events[0], "currentPrice": 100.0})
    assert "🟡【準備做多】" in message
    assert "不是正式進場訊號" in message
    assert "99.00–101.00" in message
    assert state["state"] == "PREPARE_LONG"


def test_prepare_stage_uses_atomic_outbox_dedupe_exactly_once():
    init_db()
    _state, events = evaluate_early_entry_candidate(_data(), {"state": "IDLE"})
    first = persist_decision_events("XAUUSD-EARLY-ATOMIC", events)
    assert len(first) == 1
    assert persist_decision_events("XAUUSD-EARLY-ATOMIC", events) == []
