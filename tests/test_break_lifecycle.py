import pandas as pd

from app.engines.break_lifecycle import evaluate_break_lifecycle


def frame(rows):
    return pd.DataFrame(rows, index=pd.date_range("2026-08-24 12:00", periods=len(rows), freq="15min", tz="UTC"))


def data(kind="support", level=100):
    return {"timestamp_utc": "2026-08-24T13:00:00+00:00",
            "normalized_analysis": {"atr15": 2.0, "confirmationLevels": [
                {"kind": kind, "timeframe": "15M", "price": level}]}}


def candle(o, h, l, c, v=100):
    return {"open": o, "high": h, "low": l, "close": c, "volume": v}


def test_case_a_wick_break_and_fast_reclaim_is_failed_breakdown_not_confirmed():
    bars = frame([candle(101, 102, 100.5, 101), candle(101, 102, 99.6, 101.4, 180)])
    result, _ = evaluate_break_lifecycle(bars, data=data())
    assert result["state"] == "FAILED_BREAKDOWN"
    assert result["break_type"] == "WICK_BREACH"


def test_case_b_close_break_then_follow_through_confirms():
    first = frame([candle(101, 102, 100.5, 101), candle(100.5, 100.7, 99.1, 99.4, 160)])
    pending, _ = evaluate_break_lifecycle(first, data=data())
    second = pd.concat([first, frame([candle(99.4, 99.5, 97.5, 98.0, 200)]).set_axis(
        [pd.Timestamp("2026-08-24 12:30", tz="UTC")])])
    confirmed, _ = evaluate_break_lifecycle(second, data=data(), previous=pending)
    assert pending["state"] == "BREAK_CONFIRMATION_PENDING"
    assert confirmed["state"] == "BREAK_CONFIRMED"
    assert confirmed["directionalState"] == "BEAR_BREAKOUT_CONFIRMED"


def test_case_c_close_break_then_fast_reclaim_does_not_invalidate_htf():
    first = frame([candle(101, 102, 100.5, 101), candle(100.5, 100.7, 99.1, 99.4, 160)])
    pending, _ = evaluate_break_lifecycle(first, data=data())
    second = pd.concat([first, frame([candle(99.4, 102, 99.0, 101.6, 190)]).set_axis(
        [pd.Timestamp("2026-08-24 12:30", tz="UTC")])])
    reclaimed, _ = evaluate_break_lifecycle(second, data=data(), previous=pending)
    assert pending["state"] == "BREAK_CONFIRMATION_PENDING"  # future result never rewrites snapshot
    assert reclaimed["state"] == "FAILED_BREAKDOWN"
    assert reclaimed["state"] != "BREAK_CONFIRMED"


def test_case_d_reclaim_then_second_confirmed_break_is_reclaim_failed():
    previous = {"state": "FAILED_BREAKDOWN", "level": 100.0, "direction": "DOWN",
                "bars_since_breach": 1, "recent_events": []}
    bars = frame([candle(100.8, 101, 100.2, 100.6), candle(100.4, 100.5, 97.5, 98.0, 220)])
    result, _ = evaluate_break_lifecycle(bars, data=data(), previous=previous)
    assert result["state"] == "RECLAIM_FAILED"
    assert result["break_confidence"] >= 65


def test_case_e_failed_breakout_is_mirrored_and_never_auto_sell():
    bars = frame([candle(99, 99.5, 98.5, 99), candle(99, 100.6, 98.8, 99.2, 180)])
    result, _ = evaluate_break_lifecycle(bars, data=data("resistance"))
    assert result["state"] == "FAILED_BREAKOUT"


def test_case_f_repeated_failed_breaks_enter_whipsaw():
    previous = {"state": "BREAK_CONFIRMATION_PENDING", "level": 100.0,
                "direction": "DOWN", "bars_since_breach": 1,
                "recent_events": [{"state": "FAILED_BREAKOUT"},
                                  {"state": "LIQUIDITY_SWEEP_CANDIDATE"}]}
    bars = frame([candle(99.5, 100, 99, 99.4), candle(99.4, 102, 99.1, 101.5, 180)])
    result, _ = evaluate_break_lifecycle(bars, data=data(), previous=previous)
    assert result["market_regime"] == "WHIPSAW"
    assert result["entry_confirmation_requirement"] == "CLOSE_PLUS_RETEST_HOLD"
    assert result["chase_breakout"] is False
    assert result["position_size_multiplier"] < 1
