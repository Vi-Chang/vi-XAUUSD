from copy import deepcopy

from app.engines.decision_presentation import format_decision_message
from app.engines.early_entry_candidate import evaluate_early_entry_candidate
from app.engines.entry_opportunity import evaluate_entry_opportunities
from app.engines.final_decision_engine import evaluate_final_decision
from app.engines.multi_timeframe_bias import derive_multi_timeframe_bias
from app.engines.scalp_decision import (
    build_scalp_decision_snapshot,
    derive_scalp_bias,
    preferred_scalp_side,
    scalp_opportunity_coverage,
    scalp_setup_ttl_bars,
)
from app.services.notification_policy import eligibility
from tests.test_early_entry_candidate import _data as early_data
from tests.test_entry_opportunity import by_type
from tests.test_entry_opportunity import payload as opportunity_payload
from tests.test_final_decision_engine import market


def assessments(m15="bearish", h1="bearish", h4="bullish", d1="bullish"):
    return [{"timeframe": tf, "trend": trend} for tf, trend in
            (("15M", m15), ("1H", h1), ("4H", h4), ("1D", d1))]


def test_1_15m_1h_bearish_prefers_short_despite_daily_bullish():
    multi = derive_multi_timeframe_bias(
        {"timeframeAssessments": assessments()}, canonical_bias="BULLISH")
    scalp = derive_scalp_bias(multi)
    assert scalp == "SCALP_BEARISH"
    assert preferred_scalp_side(scalp) == "SHORT"


def test_2_daily_bullish_does_not_veto_closed_15m_short_entry():
    data = market(price=100.0, regime="TREND_BEARISH")
    setup = data["breakout_setup_manager"]["activeSetup"]
    setup.update({"direction": "SHORT", "entryZoneLow": 99.0,
                  "entryZoneHigh": 101.0, "stopPrice": 103.0,
                  "tp1": 95.0, "tp2": 93.0, "tp3": 90.0})
    data["decision_assistant"].update({"direction": "SHORT", "regime": "TREND_BEARISH",
                                        "targets": [95, 93, 90]})
    data["decision_health_state"] = {
        "marketBias": "BULLISH", "dataHealth": "HEALTHY",
        "entryConfirmation": "READY", "defenseState": "INACTIVE"}
    data["normalized_analysis"]["timeframeAssessments"] = assessments()
    decision, _ = evaluate_final_decision(data)
    assert decision["finalAction"] == "ENTER_SHORT"
    assert decision["canEnter"] is True


def test_3_counter_higher_timeframe_long_is_evaluated_not_vetoed():
    multi = derive_multi_timeframe_bias(
        {"timeframeAssessments": assessments("bullish", "bullish", "bearish", "bearish")},
        canonical_bias="BEARISH")
    assert derive_scalp_bias(multi) == "SCALP_BULLISH"
    snapshot = build_scalp_decision_snapshot({
        "timestamp_utc": "2026-08-25T05:00:00Z",
        "normalized_analysis": {"currentPrice": 100, "atr15": 2,
                                "timeframeAssessments": assessments(
                                    "bullish", "bullish", "bearish", "bearish")}},
        {"marketBias": "BEARISH", "multiTimeframeBias": multi})
    assert snapshot["preferredSide"] == "LONG"
    assert snapshot["counterHigherTimeframe"] is True
    assert snapshot["managementMode"] == "SCALP_ONLY"


def test_4_new_confirmed_15m_structure_reanchors_primary_zone():
    first_data = opportunity_payload()
    first, _ = evaluate_entry_opportunities(first_data)
    old = by_type(first, "SHALLOW_PULLBACK")
    changed = opportunity_payload(price=99.0)
    changed["timestamp_utc"] = "2026-08-24T06:15:00+00:00"
    changed["normalized_analysis"]["lastClosedCandleTimestamp"] = "2026-08-24T06:00:00+00:00"
    changed["normalized_analysis"]["confirmationLevels"][0]["price"] = 99.0
    second, _ = evaluate_entry_opportunities(changed, first)
    current = by_type(second, "SHALLOW_PULLBACK")
    assert current["zone_transition_reason"] == "CONFIRMED_TACTICAL_STRUCTURE_REANCHOR"
    assert current["old_zone"] == old["entry_zone"]
    assert current["new_zone"] == current["entry_zone"]


def test_5_scalp_setup_ttl_is_volatility_aware_and_expires():
    assert scalp_setup_ttl_bars(atr15=2.0, price=100.0) == 4
    data = opportunity_payload()
    data["timestamp_utc"] = "2026-08-24T10:00:00+00:00"
    state, _ = evaluate_entry_opportunities(data)
    assert all(item["state"] == "EXPIRED" for item in state["opportunities"])


def test_6_telegram_uses_short_term_priority_not_daily_headline():
    data = early_data(side="SHORT", break_state="FAILED_BREAKOUT", break_direction="UP")
    data["normalized_analysis"]["timeframeAssessments"] = assessments()
    data["decision_health_state"]["marketBias"] = "BULLISH"
    state, events = evaluate_early_entry_candidate(data, {"state": "IDLE"})
    assert state["preferredScalpSide"] == "SHORT"
    assert events and events[0]["preferredScalpSide"] == "SHORT"
    message = format_decision_message(events[0])
    assert "短線：🔴 偏空" in message
    assert "目前策略：優先找空" in message
    assert "市場方向：🟢 偏多" not in message


def test_7_daily_conflict_marks_risk_but_does_not_block_prepare():
    data = early_data(side="SHORT", break_state="FAILED_BREAKOUT", break_direction="UP")
    data["normalized_analysis"]["timeframeAssessments"] = assessments()
    data["decision_health_state"]["marketBias"] = "BULLISH"
    state, _ = evaluate_early_entry_candidate(data, {"state": "IDLE"})
    assert state["state"] == "PREPARE_SHORT"
    assert state["counterHigherTimeframe"] is True


def test_8_ordinary_wait_remains_log_only():
    assert eligibility({"event_type": "WAIT", "currentState": "WAIT"})["eligible"] is False


def test_9_scalp_snapshot_contains_no_required_null_direction_fields():
    multi = derive_multi_timeframe_bias(
        {"timeframeAssessments": assessments()}, canonical_bias="BULLISH")
    snapshot = build_scalp_decision_snapshot(
        {"timestamp_utc": "2026-08-25T05:00:00Z",
         "normalized_analysis": {"currentPrice": 100, "atr15": 2}},
        {"multiTimeframeBias": multi, "marketBias": "BULLISH"})
    assert snapshot["scalpBias"] and snapshot["preferredSide"]
    assert snapshot["tradingHorizon"] == "SCALP_INTRADAY"


def test_10_missing_closed_15m_allows_watch_prepare_but_not_entry():
    data = early_data(side="LONG", health="DEGRADED_15M")
    data["normalized_analysis"]["timeframeAssessments"] = assessments(
        "bullish", "bullish", "bullish", "bearish")
    state, _ = evaluate_early_entry_candidate(data, {"state": "IDLE"})
    assert state["state"] in {"WATCH_LONG", "PREPARE_LONG"}
    assert state["capabilities"]["entryAllowed"] is False


def test_11_countertrend_profile_takes_profit_quickly():
    multi = derive_multi_timeframe_bias(
        {"timeframeAssessments": assessments()}, canonical_bias="BULLISH")
    snapshot = build_scalp_decision_snapshot(
        {"timestamp_utc": "2026-08-25T05:00:00Z",
         "normalized_analysis": {"currentPrice": 100, "atr15": 2}},
        {"multiTimeframeBias": multi, "marketBias": "BULLISH"})
    assert snapshot["managementMode"] == "SCALP_ONLY"
    assert "COUNTER_MACRO_TREND_RISK" in snapshot["riskFlags"]


def test_12_never_touched_candidate_is_not_missed():
    data = early_data(side="LONG", price=102.0, break_state="LEVEL_TEST")
    data["normalized_analysis"]["timeframeAssessments"] = assessments(
        "bullish", "bullish", "bullish", "bullish")
    watching, _ = evaluate_early_entry_candidate(data, {"state": "IDLE"})
    moved = deepcopy(data)
    moved["normalized_analysis"]["currentPrice"] = 104.0
    next_state, _ = evaluate_early_entry_candidate(moved, watching)
    assert "MISSED" not in next_state["state"]


def test_scalp_opportunity_coverage_gap_is_auditable():
    metric = scalp_opportunity_coverage({
        "zoneEntered": True, "validReaction": True, "favorableExcursionR": 1.2,
        "watchRecorded": False, "prepareRecorded": False, "entryRecorded": False})
    assert metric["coverageGap"] is True
    assert metric["eventType"] == "SCALP_OPPORTUNITY_COVERAGE_GAP"
