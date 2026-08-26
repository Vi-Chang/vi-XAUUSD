from app.engines.decision_health import evaluate_decision_health
from app.engines.failed_breakout_rejection import (
    evaluate_failed_breakout,
    evaluate_intrabar_support_pressure,
)
from app.services.semantic_decision import detect_meaningful_transition


def candle(close: float, *, high: float | None = None, low: float | None = None,
           time: str = "2026-08-25T06:00:00Z") -> list[dict]:
    return [{"open": close - .5, "high": high if high is not None else close + 1,
             "low": low if low is not None else close - 1, "close": close,
             "time": time}]


def evaluate(**overrides):
    values = {
        "side": "LONG", "resistance_zone": {"low": 4643, "high": 4645},
        "support_zone": {"low": 4638, "high": 4639},
        "attempt_count": 1, "closed_candles": candle(4644, high=4646),
        "current_price": 4644, "confirmation_buffer": .2,
    }
    values.update(overrides)
    return evaluate_failed_breakout(**values)


def test_market_sequence_downgrades_faster_than_it_reverses():
    first, _ = evaluate()
    assert first["state"] == "FIRST_REJECTION"
    assert first["biasState"] == "BULLISH_WITH_RESISTANCE"

    second, _ = evaluate(
        attempt_count=2,
        momentum={"macd_histogram_shrinking": True}, previous=first)
    assert second["state"] == "REPEATED_REJECTION"
    assert second["biasState"] == "NEUTRAL_BULLISH"

    third, _ = evaluate(
        attempt_count=3,
        momentum={"macd_histogram_shrinking": True, "kd_rollover": True},
        follow_through={"distance_decreasing": True}, previous=second)
    assert third["state"] == "FAILED_BREAKOUT"
    assert third["biasState"] == "NEUTRAL"

    broken, events = evaluate(
        attempt_count=3, closed_candles=candle(4637, high=4644, low=4635),
        current_price=4637, previous=third)
    assert broken["supportState"] == "BROKEN_CONFIRMED"
    assert broken["supportRole"] == "RESISTANCE_CANDIDATE"
    assert broken["biasState"] in {"NEUTRAL", "NEUTRAL_BEARISH"}
    assert "SUPPORT_BROKEN" in {event["event_type"] for event in events}


def test_intrabar_break_then_closed_reclaim_does_not_create_bearish_entry():
    pending, _ = evaluate(
        closed_candles=candle(4639.5, high=4641, low=4638.5),
        current_price=4637.5, attempt_count=0)
    assert pending["supportState"] == "BROKEN_PENDING_CLOSE"
    assert pending["biasState"] == "BULLISH_WEAKENING"
    assert pending["entryEligibility"] == "NO"

    reclaimed, events = evaluate(
        closed_candles=candle(4639.7, high=4641, low=4637.2,
                              time="2026-08-25T06:15:00Z"),
        current_price=4640, attempt_count=0, previous=pending)
    assert reclaimed["supportState"] == "RECLAIMED"
    assert reclaimed["biasState"] == "BULLISH_RECLAIM"
    assert reclaimed["marketBias"] == "BULLISH"
    assert "SUPPORT_RECLAIMED" in {event["event_type"] for event in events}


def test_broken_support_stays_resistance_candidate_until_new_closed_evidence():
    broken, _ = evaluate(
        closed_candles=candle(4637, high=4640, low=4636), current_price=4637,
        attempt_count=0)
    assert broken["supportRole"] == "RESISTANCE_CANDIDATE"

    testing, _ = evaluate(
        closed_candles=candle(4637.8, high=4639.2, low=4637), current_price=4638.5,
        attempt_count=0, previous=broken)
    assert testing["supportState"] == "BROKEN_CONFIRMED"
    assert testing["supportRole"] == "RESISTANCE_CANDIDATE"

    reclaimed, _ = evaluate(
        closed_candles=candle(4639.5, high=4640, low=4637.5), current_price=4639.5,
        attempt_count=0, previous=broken)
    assert reclaimed["supportState"] == "RECLAIMED"
    assert reclaimed["supportRole"] == "RECLAIM_CANDIDATE"


def test_long_position_risk_is_independent_from_still_bullish_context():
    warning, warning_events = evaluate(
        attempt_count=2, position_side="LONG",
        momentum={"macd_histogram_shrinking": True})
    assert warning["biasState"] == "NEUTRAL_BULLISH"
    assert warning["positionRiskState"] == "POSITION_WARNING"
    assert "POSITION_WARNING" in {event["event_type"] for event in warning_events}

    defensive, defensive_events = evaluate(
        attempt_count=2, position_side="LONG", previous=warning,
        closed_candles=candle(4639, high=4641, low=4638.5), current_price=4637.5)
    assert defensive["positionRiskState"] == "POSITION_DEFENSIVE"
    assert defensive["positionAction"] == "REDUCE"
    assert "POSITION_DEFENSIVE" in {event["event_type"] for event in defensive_events}

    live, live_events = evaluate_intrabar_support_pressure(
        {**warning, "supportState": "SAFE"}, current_price=4637)
    assert live["biasState"] == "BULLISH_WEAKENING"
    assert live["positionRiskState"] == "POSITION_DEFENSIVE"
    assert {event["event_type"] for event in live_events} == {
        "SUPPORT_BREAK_PENDING_CLOSE", "POSITION_DEFENSIVE"}


def test_same_failed_breakout_semantics_suppress_small_price_changes():
    base = {"symbol": "XAUUSD", "event_type": "REPEATED_REJECTION",
            "marketBiasState": "NEUTRAL_BULLISH", "failedBreakoutState": "REPEATED_REJECTION",
            "supportState": "SAFE", "currentPrice": 4640.1}
    changed_price = {**base, "currentPrice": 4640.2}
    assert detect_meaningful_transition(base, changed_price) is None


def test_short_side_is_symmetric():
    result, _ = evaluate_failed_breakout(
        side="SHORT", resistance_zone={"low": 4627, "high": 4629},
        support_zone={"low": 4643, "high": 4645}, attempt_count=2,
        closed_candles=candle(4628, high=4630, low=4626), current_price=4628,
        wick_rejection={"wick_rejection_state": "REPEATED_LOWER_WICK_REJECTION",
                        "wick_rejection_score": 70})
    assert result["state"] == "REPEATED_REJECTION"
    assert result["biasState"] == "NEUTRAL_BEARISH"


def test_decision_health_uses_current_failed_breakout_evidence_not_sticky_bias():
    data = {
        "timestamp_utc": "2026-08-25T06:16:00Z",
        "normalized_analysis": {
            "trendBias": "bullish", "currentPrice": 4637,
            "lastClosedCandleTimestamp": "2026-08-25T06:15:00Z",
            "lastClosedCandlePrice": 4637,
        },
        "failed_breakout_rejection_engine": {
            "biasState": "NEUTRAL", "biasConfidence": 45,
        },
    }
    health = evaluate_decision_health(data, now="2026-08-25T06:16:00Z")
    assert health["marketBiasState"] == "NEUTRAL"
    assert health["marketBias"] == "NEUTRAL"
    assert health["biasConfidence"] == 45
