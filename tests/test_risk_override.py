import pytest

from app.engines.normalized_analysis import validate_api_payload
from app.engines.risk_override import apply_risk_priority, detect_short_term_weakness
from app.schemas.analysis import NormalizedAnalysisState


def assess(indicators, support_state):
    weakness = detect_short_term_weakness(
        indicators=indicators, support_state=support_state)
    policy = apply_risk_priority(
        weakness=weakness, market_status="GOOD", event_status="FAILED",
        event_lockout=False, market_regime="bullish",
        entry_readiness="wait_confirmation", support_state=support_state, levels=[])
    return weakness, policy


def test_case_a_4413_weakness_protects_existing_long():
    weakness, policy = assess({
        "15M": {"macd_hist": -0.35, "macd_hist_prev": 0.12,
                "rsi6": 40.0, "rsi6_prev": 55.0, "rsi12": 45.0,
                "stoch_k": 30.0, "stoch_d": 40.0, "stoch_k_prev": 45.0},
        "1H": {"macd_hist": 0.27, "macd_hist_prev": 0.50,
               "macd_hist_prev2": 0.80, "rsi14": 52.0},
    }, "confirmed_breakdown")
    assert weakness.state == "confirmed"
    assert policy["riskOverride"] == "protect_existing_long"
    assert policy["positionRisk"] == "elevated"
    assert policy["entryReadiness"] == "wait_confirmation"
    assert policy["longEntryAllowed"] is False


def test_case_b_4406_accelerating_weakness_is_no_trade():
    weakness, policy = assess({
        "15M": {"macd_hist": -2.51, "macd_hist_prev": -1.20,
                "rsi6": 24.9, "rsi6_prev": 30.0, "rsi12": 38.08,
                "stoch_k": 12.0, "stoch_d": 20.0, "stoch_k_prev": 18.0},
        "1H": {"macd_hist": 0.20, "macd_hist_prev": 0.45,
               "macd_hist_prev2": 0.70, "rsi14": 48.0},
    }, "retest_rejected")
    assert weakness.state == "accelerating"
    assert weakness.oversold is True
    assert policy["riskOverride"] == "protect_existing_long"
    assert policy["positionRisk"] == "elevated"
    assert policy["entryReadiness"] == "no_trade"
    assert policy["longEntryAllowed"] is False
    assert "不代表已產生立即平倉訊號" in policy["existingLongGuidance"]
    assert "優先降低曝險" not in policy["existingLongGuidance"]


def test_case_c_oversold_recovery_candidate_still_waits():
    weakness, policy = assess({
        "15M": {"macd_hist": -0.50, "macd_hist_prev": -1.00,
                "rsi6": 27.0, "rsi6_prev": 24.0, "rsi12": 39.0,
                "stoch_k": 22.0, "stoch_d": 18.0, "stoch_k_prev": 14.0},
        "1H": {"macd_hist": 0.30, "macd_hist_prev": 0.35,
               "macd_hist_prev2": 0.40, "rsi14": 52.0},
    }, "failed_breakdown")
    assert weakness.recovery_candidate is True
    assert policy["riskOverride"] == "block_new_long"
    assert policy["entryReadiness"] == "wait_confirmation"
    assert policy["longEntryAllowed"] is False


def test_event_lockout_has_priority_over_technical_direction():
    weakness = detect_short_term_weakness(indicators={}, support_state="none")
    policy = apply_risk_priority(
        weakness=weakness, market_status="GOOD", event_status="GOOD",
        event_lockout=True, market_regime="strong_bullish", entry_readiness="ready",
        support_state="none", levels=[])
    assert policy["riskOverride"] == "suspend_all_entries"
    assert policy["entryReadiness"] == "no_trade"
    assert not policy["longEntryAllowed"] and not policy["shortEntryAllowed"]


def test_api_validator_throws_in_strict_mode_and_downgrades_in_production():
    timestamp = "2026-08-13T01:00:00+00:00"
    state = NormalizedAnalysisState(
        marketDataTimestamp=timestamp, currentPrice=4406.0,
        marketDataStatus="GOOD", eventDataStatus="FAILED",
        sourceTimestamps={"analysis": timestamp}, sourcePrices={"analysis": 4406.0},
        shortTermWeakness="confirmed", longEntryAllowed=True,
        entryReadiness="wait_confirmation")
    payload = {"normalized_analysis": state.model_dump(), "snapshot_ts": timestamp,
               "current_price": {"mid": 4406.0}}
    with pytest.raises(ValueError, match="ANALYSIS_CONSISTENCY_ERROR"):
        validate_api_payload(payload, strict=True)
    safe = validate_api_payload(payload, strict=False)["normalized_analysis"]
    assert safe["entryReadiness"] == "no_trade"
    assert safe["riskOverride"] == "suspend_all_entries"
    assert safe["longEntryAllowed"] is False
