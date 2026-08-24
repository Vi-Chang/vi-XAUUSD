from datetime import datetime, timedelta, timezone

import pandas as pd

from app.engines.wick_rejection import evaluate_wick_rejection


def make_frame(rows):
    start = datetime(2026, 8, 24, tzinfo=timezone.utc)
    return pd.DataFrame(rows, index=[start + timedelta(minutes=15 * i)
                                     for i in range(len(rows))])


def context(level=110, kind="resistance", timeframe="4H"):
    return {"normalized_analysis": {"confirmationLevels": [
        {"price": level, "kind": kind, "timeframe": timeframe}]}}


def upper_rejections():
    return make_frame([
        {"open": 106, "high": 107, "low": 105.5, "close": 106.8, "volume": 100},
        {"open": 106.8, "high": 109.9, "low": 106.7, "close": 107.2, "volume": 130},
        {"open": 107.2, "high": 110.1, "low": 107.0, "close": 107.6, "volume": 140},
        {"open": 107.6, "high": 110.0, "low": 107.4, "close": 108.0, "volume": 150},
    ])


def test_repeated_upper_wick_is_clustered_and_not_confirmed():
    result, _ = evaluate_wick_rejection(upper_rejections(), data=context())
    assert result["wick_rejection_state"] == "REPEATED_UPPER_WICK_REJECTION"
    assert result["wick_rejection_count"] >= 2
    assert result["wick_rejection_zone"]["low"] >= 109.8
    assert result["breakout_state"] in {"BREAKOUT_ATTEMPT", "BREAKOUT_FAILED"}


def test_two_closed_bodies_above_zone_confirm_breakout():
    base = upper_rejections()
    extra = make_frame([
        {"open": 110.0, "high": 111.2, "low": 109.9, "close": 110.8, "volume": 150},
        {"open": 110.8, "high": 111.5, "low": 110.5, "close": 111.1, "volume": 150},
    ])
    extra.index = [base.index[-1] + timedelta(minutes=15),
                   base.index[-1] + timedelta(minutes=30)]
    result, _ = evaluate_wick_rejection(pd.concat([base, extra]), data=context())
    assert result["breakout_state"] == "BREAKOUT_CONFIRMED"


def test_htf_location_scores_higher_than_range_middle():
    high, _ = evaluate_wick_rejection(upper_rejections(), data=context())
    low, _ = evaluate_wick_rejection(upper_rejections(), data=context(level=120))
    assert high["wick_rejection_score"] > low["wick_rejection_score"]
    assert high["rejection_location_quality"] == "HIGH"


def test_lower_wick_is_symmetric_support_rejection():
    rows = [{"open": 220-row["open"], "high": 220-row["low"],
             "low": 220-row["high"], "close": 220-row["close"],
             "volume": row["volume"]}
            for row in upper_rejections().iloc[::-1].to_dict("records")]
    result, _ = evaluate_wick_rejection(
        make_frame(rows), data=context(level=110, kind="support"))
    assert result["wick_rejection_state"] == "REPEATED_LOWER_WICK_REJECTION"


def test_state_transition_notifies_once():
    first, events = evaluate_wick_rejection(upper_rejections(), data=context())
    _, repeated = evaluate_wick_rejection(
        upper_rejections(), data=context(), previous=first)
    assert len(events) == 1
    assert repeated == []
