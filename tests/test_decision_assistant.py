import pytest

from app.engines.decision_assistant import (
    breakout_quality,
    evaluate_decision_assistant,
    pullback_depth,
)


def data(*, price=100.5, trend="bullish", momentum="stable", regime="bullish",
         status="BREAKOUT_ENTRY_READY", rr=2.0, score=85, market="GOOD"):
    setup = {
        "setupId": "BO-v3", "scenarioVersion": 2, "type": "BREAKOUT",
        "direction": "LONG", "status": status, "breakoutTrigger": 100,
        "entryZoneLow": 100, "entryZoneHigh": 101, "maxChasePrice": 103,
        "stopPrice": 98, "tp1": 104, "tp2": 106, "tp3": 109,
        "riskReward": rr, "signalScore": score, "atr15": 10,
        "passedReasons": ["4H／1H 同向"],
    }
    return {
        "symbol": "XAUUSD", "timestamp_utc": "2026-08-21T15:01:00Z",
        "normalized_analysis": {
            "currentPrice": price, "marketDataStatus": market, "trendBias": trend,
            "shortTermMomentum": momentum, "marketRegime": regime,
            "trendScore": score, "entryQualityScore": score, "entryTiming": "favorable",
            "breakoutState": "confirmed", "supportState": "none", "atr15": 10,
            "consistencyValid": True, "lastClosedCandleTimestamp": "2026-08-21T15:00:00Z",
            "timeframeAssessments": [
                {"timeframe": "4H", "trend": trend}, {"timeframe": "1H", "trend": trend},
                {"timeframe": "15M", "trend": trend}],
        },
        "timeframes": {"m15": {"rsi": 60}},
        "breakout_setup_manager": {"activeSetup": setup, "setups": [setup]},
        "trend_continuation_engine": {"candidates": []},
    }


def candle(*, close=101, opened=99, high=101.2, low=98.8):
    return {"open": opened, "high": high, "low": low, "close": close}


def test_strong_bullish_breakout_is_actionable_with_quality_and_rr():
    result, events = evaluate_decision_assistant(data(), latest_candle=candle())
    assert result["regime"] == "TREND_BULLISH"
    assert result["canEnter"] is True and result["actionSummary"] == "現在可以進"
    assert result["entryQualityGrade"] in {"A", "B", "C"}
    assert result["rrPassed"] is True and events[0]["event_type"] == "ENTRY_READY"


def test_weak_breakout_waits_for_next_close():
    result, _ = evaluate_decision_assistant(
        data(), latest_candle=candle(close=100.2, opened=99.9, high=102, low=99.8))
    assert result["breakoutQuality"]["state"] == "WEAK_BREAKOUT"
    assert result["tradeState"] == "WEAK_BREAKOUT" and not result["canEnter"]


def test_fake_breakout_and_reversal_risk_are_not_entries():
    value = data(); value["normalized_analysis"].update(
        breakoutState="failed", shortTermMomentum="reversal_risk")
    result, _ = evaluate_decision_assistant(value, latest_candle=candle(close=99))
    assert result["regime"] in {"HTF_BULLISH_LTF_WEAKENING", "REVERSAL_RISK"}
    assert result["canEnter"] is False


def test_breakout_retest_is_separate_setup_type():
    value = data(status="PULLBACK_ENTRY_READY")
    value["breakout_setup_manager"]["activeSetup"]["type"] = "BREAKOUT_RETEST"
    result, _ = evaluate_decision_assistant(value, latest_candle=candle())
    assert result["scenarioType"] == "BREAKOUT_RETEST"


@pytest.mark.parametrize(("distance", "expected"), [
    (2, "SHALLOW"), (5, "NORMAL"), (9, "DEEP"), (13, "STRUCTURE_BREAK")])
def test_pullback_depth_classes(distance, expected):
    assert pullback_depth(distance, 10, False) == expected


def test_structure_break_is_never_called_pullback():
    assert pullback_depth(2, 10, True) == "STRUCTURE_BREAK"


def test_range_disables_breakout_entry():
    value = data(regime="range", trend="neutral")
    result, _ = evaluate_decision_assistant(value, latest_candle=candle())
    assert result["regime"] == "RANGE" and not result["canEnter"]
    assert result["tradeState"] == "NO_TRADE"


def test_overheated_bullish_does_not_chase():
    value = data(price=110); value["timeframes"]["m15"]["rsi"] = 84
    value["normalized_analysis"]["entryTiming"] = "chase"
    result, _ = evaluate_decision_assistant(value, latest_candle=candle(close=110, high=111))
    assert result["regime"] == "OVERHEATED_BULLISH"
    assert result["actionSummary"] in {"不要追價", "沒有好機會"}


def test_htf_bullish_15m_pullback_is_neutral_not_bearish():
    value = data(momentum="pullback")
    result, _ = evaluate_decision_assistant(value, latest_candle=candle())
    assert result["regime"] == "HTF_BULLISH_LTF_WEAKENING"
    assert result["direction"] != "SHORT"


def test_rr_gate_blocks_ready_signal():
    result, _ = evaluate_decision_assistant(data(rr=1.2), latest_candle=candle())
    assert not result["rrPassed"] and not result["canEnter"]
    assert "賺賠比" in " ".join(result["noTradeReasons"])


def test_chase_penalty_lowers_quality_and_marks_missed():
    near, _ = evaluate_decision_assistant(data(price=101), latest_candle=candle())
    far, _ = evaluate_decision_assistant(data(price=110), latest_candle=candle(close=110, high=111))
    assert far["entryQualityScore"] < near["entryQualityScore"]
    assert far["tradeState"] in {"MISSED_ENTRY", "NO_TRADE"}


def test_entry_approaching_is_event_not_wait_spam():
    value = data(price=102.4, status="WAIT_BREAKOUT_OR_PULLBACK")
    result, events = evaluate_decision_assistant(value, latest_candle=candle())
    assert result["tradeState"] == "ENTRY_APPROACHING"
    assert events and events[0]["event_type"] == "APPROACH_ENTRY"


def test_same_state_has_no_repeat_event():
    first, events = evaluate_decision_assistant(data(), latest_candle=candle())
    assert events
    second, repeated = evaluate_decision_assistant(data(price=100.6), latest_candle=candle(), previous=first)
    assert second["tradeState"] == first["tradeState"] and repeated == []


def test_new_scenario_version_is_a_real_event():
    first, _ = evaluate_decision_assistant(data(), latest_candle=candle())
    value = data(); setup = value["breakout_setup_manager"]["activeSetup"]
    setup["setupId"], setup["scenarioVersion"] = "BO-v4", 3
    result, events = evaluate_decision_assistant(value, latest_candle=candle(), previous=first)
    assert result["scenarioVersion"] == 3 and events


def test_data_stale_is_no_edge_and_no_new_entry():
    result, events = evaluate_decision_assistant(data(market="STALE"), latest_candle=candle())
    assert result["regime"] == "NO_EDGE" and not result["canEnter"]
    assert events == []


def test_breakout_quality_uses_body_wick_and_atr():
    strong = breakout_quality(candle(), 100, 10, "LONG")
    weak = breakout_quality(candle(close=100.1, opened=100, high=102, low=99.9), 100, 10, "LONG")
    assert strong["score"] > weak["score"]
    assert weak["state"] == "WEAK_BREAKOUT"


def test_action_output_contains_auditable_matrix_fields():
    result, events = evaluate_decision_assistant(data(), latest_candle=candle())
    assert {"regime", "scenarioType", "actionSummary", "entryQualityScore",
            "rewardRiskRatio", "eventType", "shouldNotify", "why"} <= result.keys()
    assert events[0]["decisionAssistant"] == result
