from datetime import datetime, timezone

import pandas as pd

from app.engines.entry_engine import evaluate_entry_engine

NOW = datetime(2026, 8, 19, 1, tzinfo=timezone.utc)


def frame(previous, current):
    return pd.DataFrame([
        {"open": previous[0], "high": previous[1], "low": previous[2],
         "close": previous[3], "is_closed": True},
        {"open": current[0], "high": current[1], "low": current[2],
         "close": current[3], "is_closed": True},
    ])


def scenario(direction, *, tp1=None, tp2=None):
    if direction == "SHORT":
        tp1, tp2 = tp1 or 97, tp2 or 95
    else:
        tp1, tp2 = tp1 or 103, tp2 or 105
    return {
        "target_ids": ["T1", "T2"],
        "resolved_prices": {
            "T1": {"price_low": tp1, "price_high": tp1},
            "T2": {"price_low": tp2, "price_high": tp2},
        },
    }


def data(support_state, *, direction="SHORT", price=98.5, tp1=None, tp2=None,
         closed_price=99.0):
    return {
        "symbol": "XAUUSD",
        "normalized_analysis": {
            "supportState": support_state,
            "currentPrice": price,
            "lastClosedCandlePrice": closed_price,
            "lastClosedCandleTimestamp": NOW.isoformat(),
            "atr15": 10,
            "invalidationLevel": 100.2,
            "marketDataStatus": "GOOD",
            "consistencyValid": True,
            "entryQualityScore": 70,
            "confirmationLevels": [
                {"kind": "support", "timeframe": "15M", "price": 100, "buffer": .2}
            ],
            "tradingDecision": {"marketAssessment": {
                "reversalState": "reversal_confirmed" if direction == "LONG" else "none"}},
        },
        "short_scenario": scenario("SHORT", tp1=tp1, tp2=tp2),
        "long_scenario": scenario("LONG", tp1=tp1, tp2=tp2),
    }


def test_short_entry_watch_ready_and_triggered_keep_same_setup_id():
    watch = evaluate_entry_engine(data("confirmed_breakdown"), now=NOW)
    assert watch.plan.status == "SETUP_WATCH"
    assert watch.plan.risk_reward >= 1.5
    no_signal = frame((100, 100.5, 99.5, 100), (100.05, 100.2, 99.8, 100.1))
    ready = evaluate_entry_engine(data("confirmed_breakdown", price=100), watch.plan,
                                  m5_closed=no_signal, now=NOW)
    assert ready.plan.status == "ENTRY_READY"
    bearish = frame((100, 100.5, 99.5, 100), (100.2, 100.3, 99.7, 99.8))
    triggered = evaluate_entry_engine(data("confirmed_breakdown"), ready.plan,
                                      m5_closed=bearish, now=NOW)
    assert triggered.plan.status == "ENTRY_TRIGGERED"
    assert triggered.plan.setup_id == watch.plan.setup_id
    assert triggered.plan.trigger_timeframe == "5M"
    assert all(x is not None for x in (triggered.plan.suggested_entry,
        triggered.plan.stop_loss, triggered.plan.take_profit_1,
        triggered.plan.take_profit_2))
    assert "【可進場方向】做空" in triggered.message


def test_long_entry_is_symmetric_and_requires_closed_bullish_trigger():
    watch = evaluate_entry_engine(
        data("failed_breakdown", direction="LONG", price=100), now=NOW)
    assert watch.plan.direction == "LONG"
    bullish = frame((100, 100.5, 99.5, 100), (99.8, 100.3, 99.7, 100.2))
    triggered = evaluate_entry_engine(
        data("failed_breakdown", direction="LONG", price=100.2, closed_price=100.2),
        watch.plan, m15_closed=bullish, now=NOW)
    assert triggered.plan.status == "ENTRY_TRIGGERED"
    assert triggered.plan.risk_reward >= 1.5
    assert "【可進場方向】做多" in triggered.message


def test_insufficient_rr_never_creates_setup_or_signal():
    result = evaluate_entry_engine(
        data("confirmed_breakdown", tp1=99.0, tp2=98.5), now=NOW)
    assert result.plan.status == "NO_SETUP"
    assert result.should_notify is False


def test_closed_candle_beyond_stop_invalidates_before_entry():
    watch = evaluate_entry_engine(data("confirmed_breakdown"), now=NOW).plan
    invalid = evaluate_entry_engine(
        data("confirmed_breakdown", price=102, closed_price=102), watch, now=NOW)
    assert invalid.plan.status == "INVALIDATED"
    assert invalid.should_notify is True


def test_touch_without_reversal_does_not_enter_and_duplicate_is_suppressed():
    watch = evaluate_entry_engine(data("confirmed_breakdown"), now=NOW).plan
    no_signal = frame((100, 100.5, 99.5, 100), (100.05, 100.2, 99.8, 100.1))
    ready = evaluate_entry_engine(data("confirmed_breakdown", price=100), watch,
                                  m5_closed=no_signal, now=NOW)
    duplicate = evaluate_entry_engine(data("confirmed_breakdown", price=100), ready.plan,
                                      m5_closed=no_signal, now=NOW)
    assert ready.plan.status == "ENTRY_READY"
    assert ready.plan.setup_id == watch.setup_id
    assert duplicate.plan.status == "ENTRY_READY"
    assert duplicate.should_notify is False
