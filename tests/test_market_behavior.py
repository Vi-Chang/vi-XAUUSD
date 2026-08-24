from datetime import datetime, timedelta, timezone

import pandas as pd

from app.engines.canonical_decision import build_canonical_decision
from app.engines.final_decision_engine import evaluate_final_decision
from app.engines.market_behavior import evaluate_market_behavior
from tests.test_final_decision_engine import market


def candles(closes, *, volumes=None, wick=.3):
    start = datetime(2026, 8, 24, tzinfo=timezone.utc)
    rows = []
    for index, close in enumerate(closes):
        opened = closes[index - 1] if index else close - .1
        rows.append({"open": opened, "high": max(opened, close) + wick,
                     "low": min(opened, close) - wick, "close": close,
                     "volume": (volumes[index] if volumes else 100)})
    return pd.DataFrame(rows, index=[start + timedelta(minutes=15 * index)
                                    for index in range(len(rows))])


def data(*, bias="bullish", support="none", momentum="stable", htf=False):
    return {"timestamp_utc": "2026-08-24T02:00:00Z", "normalized_analysis": {
        "trendBias": bias, "supportState": support,
        "shortTermMomentum": momentum,
        "lastClosedCandleTimestamp": "2026-08-24T01:45:00Z",
        "higherTimeframeReversalConfirmed": htf,
    }}


def evaluate(frame, context, previous=None):
    result, events = evaluate_market_behavior(
        m15=frame, h1=frame, h4=frame, data=context, previous=previous)
    return result, events


def test_bullish_bias_can_have_slow_bearish_drift_without_sell():
    frame = candles([110, 109.8, 109.5, 109.2, 109.0, 108.7, 108.5, 108.2])
    result, _ = evaluate(frame, data())
    assert result["market_bias"] == "BULLISH"
    assert result["market_behavior"] == "SLOW_BEARISH_DRIFT"


def test_bullish_support_test_is_pullback_with_drift_secondary():
    frame = candles([110, 109.8, 109.5, 109.2, 109.0, 108.7, 108.5, 108.2])
    result, _ = evaluate(frame, data(support="testing_support"))
    assert result["market_behavior"] == "PULLBACK"
    assert result["secondary_behavior"] == "SLOW_BEARISH_DRIFT"


def test_large_bearish_body_volume_spike_and_swing_break_is_strong_decline():
    frame = candles([110, 110.2, 110.1, 110.0, 109.9, 109.8, 109.6, 105.0],
                    volumes=[100, 100, 100, 100, 100, 100, 100, 350], wick=.1)
    result, _ = evaluate(frame, data())
    assert result["market_behavior"] == "STRONG_DECLINE"


def test_bearish_bias_with_higher_lows_is_rebound_not_bullish_flip():
    frame = candles([100, 100.2, 100.5, 100.8, 101.0, 101.3, 101.5, 101.8])
    result, _ = evaluate(frame, data(bias="bearish"))
    assert result["market_bias"] == "BEARISH"
    assert result["market_behavior"] == "REBOUND"


def test_tactical_break_without_htf_confirmation_is_warning():
    frame = candles([110, 109.8, 109.5, 109.2, 109.0, 108.7, 108.5, 108.2])
    previous = {"market_behavior": "PULLBACK", "pending_behavior": "REVERSAL_WARNING",
                "pending_count": 1, "last_evaluated_candle": "older",
                "behavior_since": "2026-08-24T00:00:00Z"}
    result, _ = evaluate(frame, data(support="confirmed_breakdown"), previous)
    assert result["market_behavior"] == "REVERSAL_WARNING"
    assert result["market_bias"] == "BULLISH"


def test_htf_closed_break_confirms_reversal():
    frame = candles([110, 109.8, 109.5, 109.2, 109.0, 108.7, 108.5, 108.2])
    result, _ = evaluate(frame, data(support="retest_rejected", htf=True))
    assert result["market_behavior"] == "REVERSAL_CONFIRMED"


def test_negative_momentum_alone_cannot_turn_flat_price_into_drift():
    frame = candles([100, 100.05, 99.98, 100.04, 99.97, 100.03, 99.99, 100.0])
    result, _ = evaluate(frame, data())
    assert result["market_behavior"] == "RANGE"


def test_hysteresis_does_not_count_same_closed_candle_twice(monkeypatch):
    sequence = iter(["2026-08-24 01:00:00+00:00"] * 3
                    + ["2026-08-24 01:00:00+00:00"] * 3
                    + ["2026-08-24 01:15:00+00:00"] * 3)

    def fake_classify(*_args, **_kwargs):
        return {"behavior": "SLOW_RISE", "confidence": 70,
                "secondaryBehavior": None, "scores": {"SLOW_RISE": 70},
                "metrics": {"candleTime": next(sequence)}}

    monkeypatch.setattr("app.engines.market_behavior._classify", fake_classify)
    frame = candles([100, 100, 100, 100])
    first, _ = evaluate(frame, data())
    second, _ = evaluate(frame, data(), first)
    third, _ = evaluate(frame, data(), second)
    assert first["market_behavior"] == second["market_behavior"] == "RANGE"
    assert third["market_behavior"] == "SLOW_RISE"


def test_slow_bearish_drift_blocks_new_long_but_never_creates_sell():
    snapshot = market()
    snapshot["market_behavior_engine"] = {"market_behavior": "SLOW_BEARISH_DRIFT"}
    decision, _ = evaluate_final_decision(snapshot)
    assert decision["finalAction"] == "WAIT"
    assert decision["primaryReason"] == "BEHAVIOR_WAIT_PULLBACK"
    assert decision["finalAction"] != "ENTER_SHORT"


def test_long_position_management_uses_behavior_without_changing_bias():
    snapshot = market()
    snapshot["market_behavior_engine"] = {
        "market_bias": "BULLISH", "market_behavior": "SLOW_BEARISH_DRIFT",
        "behavior_confidence": 82, "behavior_15m": "SLOW_BEARISH_DRIFT",
        "behavior_1h": "PULLBACK", "behavior_4h": "SLOW_RISE",
        "structure_status": "BULLISH_INTACT", "momentum_status": "SHORT_TERM_BEARISH",
    }
    snapshot["position_management"] = {
        "has_position": True, "position_side": "LONG", "entry_price": 99.0,
        "position_size": .1, "recommended_action": "HOLD"}
    canonical = build_canonical_decision(snapshot, snapshot["final_decision_state"]
                                         if "final_decision_state" in snapshot else
                                         evaluate_final_decision(snapshot)[0])
    assert canonical["marketBias"] == "BULLISH"
    assert canonical["marketBehavior"] == "SLOW_BEARISH_DRIFT"
    assert canonical["newEntryDecision"]["action"] == "WAIT"
    assert canonical["positionManagement"]["managementMode"] == "HOLD_WITH_CAUTION"
