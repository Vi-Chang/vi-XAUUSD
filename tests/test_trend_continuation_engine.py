from datetime import datetime, timedelta, timezone

import pandas as pd

from app.engines.trend_continuation_engine import (
    classify_market_type,
    evaluate_trend_continuation,
)

NOW = datetime(2026, 8, 21, 10, tzinfo=timezone.utc)


def frame(closes, minutes=15):
    rows = []
    for index, close in enumerate(closes):
        open_ = close - 0.25
        rows.append({"open_time": NOW + timedelta(minutes=minutes * index),
                     "close_time": NOW + timedelta(minutes=minutes * (index + 1)),
                     "open": open_, "high": max(open_, close) + .35,
                     "low": min(open_, close) - .35, "close": close,
                     "volume": 100 + index, "is_closed": True})
    return pd.DataFrame(rows)


def higher_tf():
    return frame([100 + i * .8 for i in range(60)], 60)


def data(rsi=60, ledger=None):
    return {"symbol": "XAUUSD", "timestamp_utc": "2026-08-21T20:00:00+00:00",
            "normalized_analysis": {"marketDataStatus": "GOOD"},
            "timeframes": {"m15": {"rsi": rsi}},
            "breakout_setup_manager": ledger or {}}


def test_market_type_uses_h4_and_h1_weighted_trend():
    market_type, score, dimensions = classify_market_type(higher_tf(), higher_tf())
    assert market_type == "TREND_CONTINUATION_LONG"
    assert score >= 80 and dimensions["h4Structure"] == 100


def test_overbought_does_not_veto_valid_shallow_pullback():
    m15 = frame([100 + i * .5 for i in range(40)])
    waiting, _ = evaluate_trend_continuation(
        data(rsi=85), m15=m15, h1=higher_tf(), h4=higher_tf())
    fixed = next(c for c in waiting["candidates"] if c["type"] == "SHALLOW_PULLBACK_LONG")
    close = (fixed["entryZoneLow"] + fixed["entryZoneHigh"]) / 2
    m15.loc[m15.index[-1], ["open", "high", "low", "close"]] = [
        close - .2, close, close - .8, close - .5]
    next_bar = pd.DataFrame([{**m15.iloc[-1].to_dict(),
        "open_time": m15.iloc[-1]["open_time"] + timedelta(minutes=15),
        "close_time": m15.iloc[-1]["close_time"] + timedelta(minutes=15),
        "open": close - .3, "low": close - .35, "high": close + .4,
        "close": close, "is_closed": True}])
    result, _ = evaluate_trend_continuation(
        data(rsi=85), m15=pd.concat((m15, next_bar), ignore_index=True),
        h1=higher_tf(), h4=higher_tf(), previous=waiting)
    shallow = next(c for c in result["candidates"] if c["type"] == "SHALLOW_PULLBACK_LONG")
    assert result["overbought"] is True
    assert shallow["status"] == "ENTRY_READY_SHALLOW_PULLBACK"
    assert shallow["signalScore"] < result["trendScore"]


def test_far_from_support_outputs_exact_no_chase_numbers():
    m15 = frame([100 + i * .7 for i in range(40)])
    result, _ = evaluate_trend_continuation(data(), m15=m15, h1=higher_tf(), h4=higher_tf())
    shallow = next(c for c in result["candidates"] if c["type"] == "SHALLOW_PULLBACK_LONG")
    assert shallow["status"] == "WAIT_SHALLOW_PULLBACK"
    assert shallow["entryZoneLow"] < shallow["entryZoneHigh"]
    assert any("距回踩區" in reason for reason in shallow["missingConditions"])


def test_confirmed_breakout_retest_can_become_ready():
    closes = [100 + i * .4 for i in range(38)] + [114.8, 115.2]
    m15 = frame(closes)
    ledger = {"setups": [{"setupId": "BO-FIXED", "direction": "LONG",
        "breakoutTrigger": 115.0, "breakoutConfirmedAt": "2026-08-21T18:00:00+00:00",
        "createdFromCandleTime": "2026-08-21T18:00:00+00:00",
        "retestZoneLow": 114.5, "retestZoneHigh": 115.5, "stopPrice": 112.0}]}
    result, _ = evaluate_trend_continuation(data(ledger=ledger), m15=m15,
                                             h1=higher_tf(), h4=higher_tf())
    candidate = next(c for c in result["candidates"] if c["type"] == "BREAKOUT_RETEST_LONG")
    assert candidate["status"] == "ENTRY_READY_BREAKOUT_RETEST"


def test_bull_flag_breakout_is_detected():
    closes = [100 + i * .3 for i in range(31)]
    closes += [110.0, 111.5, 113.0, 114.5, 114.2, 114.4, 114.3, 114.35, 115.0]
    result, _ = evaluate_trend_continuation(data(), m15=frame(closes),
                                             h1=higher_tf(), h4=higher_tf())
    flag = next(c for c in result["candidates"] if c["type"] == "BULL_FLAG_CONTINUATION")
    assert flag["status"] == "ENTRY_READY_BULL_FLAG"


def test_strong_aligned_trend_can_form_strict_momentum_continuation():
    result, _ = evaluate_trend_continuation(
        data(), m15=frame([100 + i * .5 for i in range(40)]),
        h1=higher_tf(), h4=higher_tf())
    momentum = next(c for c in result["candidates"] if c["type"] == "MOMENTUM_CONTINUATION")
    assert momentum["status"] == "ENTRY_READY_MOMENTUM_CONTINUATION"
    assert momentum["riskWeight"] == .5


def test_waiting_setup_keeps_immutable_prices_on_next_candle():
    first = frame([100 + i * .7 for i in range(40)])
    state, _ = evaluate_trend_continuation(data(), m15=first,
                                            h1=higher_tf(), h4=higher_tf())
    old = next(c for c in state["candidates"] if c["type"] == "SHALLOW_PULLBACK_LONG")
    second = frame([100 + i * .7 for i in range(40)] + [128.0])
    updated, _ = evaluate_trend_continuation(data(), m15=second,
                                              h1=higher_tf(), h4=higher_tf(), previous=state)
    new = next(c for c in updated["candidates"] if c["type"] == "SHALLOW_PULLBACK_LONG")
    for key in ("setupId", "entryZoneLow", "entryZoneHigh", "stopPrice", "tp1", "tp2", "tp3"):
        assert new[key] == old[key]


def test_all_failed_routes_report_numeric_reasons_and_wait_dedupes():
    m15 = frame([100 + i * .05 for i in range(40)])
    state, first = evaluate_trend_continuation(data(), m15=m15,
                                                h1=higher_tf(), h4=higher_tf())
    assert state["status"] == "WAIT"
    assert all(candidate.get("missingConditions") for candidate in state["candidates"])
    _, second = evaluate_trend_continuation(data(), m15=m15,
                                             h1=higher_tf(), h4=higher_tf(), previous=state)
    assert first == second == []


def test_synthetic_4508_to_4602_trend_does_not_raise_trigger_forever():
    closes = [4508.90 + i * 2.5 for i in range(31)]
    closes += [4587.0, 4592.0, 4597.0, 4600.0, 4599.4, 4599.8, 4599.6, 4599.9, 4602.18]
    m15 = frame(closes)
    m15.loc[m15.index[-2], "high"] = 4602.0
    h1 = frame([4450 + i * 3 for i in range(60)], 60)
    h4 = frame([4300 + i * 6 for i in range(60)], 240)
    result, _ = evaluate_trend_continuation(data(), m15=m15, h1=h1, h4=h4)
    assert result["marketType"] == "TREND_CONTINUATION_LONG"
    assert any(str(candidate["status"]).startswith("ENTRY_READY_")
               for candidate in result["candidates"])
