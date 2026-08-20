from app.engines.breakout_alert_state import evaluate_breakout_alert


def payload(price, close, candle):
    return {
        "currentPrice": price,
        "lastClosedCandlePrice": close,
        "lastClosedCandleTimestamp": candle,
        "marketDataTimestamp": candle,
        "confirmationLevels": [
            {"kind": "resistance", "timeframe": "15M", "price": 4444, "buffer": 0}
        ],
    }


def test_2136_pending_then_closed_above_4444_confirms_without_weakness():
    pending, event = evaluate_breakout_alert(
        payload(4445, 4443.8, "2026-08-20T21:36:00+00:00")
    )
    assert pending.status == "PENDING_BREAKOUT"
    assert event["event_type"] == "PENDING_BREAKOUT"
    confirmed, event = evaluate_breakout_alert(
        payload(4447, 4446, "2026-08-20T21:45:00+00:00"), pending
    )
    assert confirmed.status == "BREAKOUT_CONFIRMED"
    assert event["event_type"] == "BREAKOUT_CONFIRMED"
    assert event["action"] == "等待回踩"
    assert "短線轉弱" not in event["message"]


def test_two_closed_bars_or_h1_close_confirms_bullish_continuation():
    first, _ = evaluate_breakout_alert(payload(4447, 4446, "2026-08-20T21:45:00+00:00"))
    second, event = evaluate_breakout_alert(
        payload(4448, 4447, "2026-08-20T22:00:00+00:00"), first
    )
    assert second.status == "BULLISH_CONTINUATION"
    assert event["event_type"] == "BULLISH_CONTINUATION"
    one_hour, event = evaluate_breakout_alert(
        payload(4447, 4446, "2026-08-20T22:15:00+00:00"), h1_close=4446
    )
    assert one_hour.status == "BULLISH_CONTINUATION"
    assert event["event_type"] == "BULLISH_CONTINUATION"


def test_overbought_context_cannot_mark_breakout_weak_without_structure_and_macd():
    confirmed, _ = evaluate_breakout_alert(
        payload(4447, 4446, "2026-08-20T21:45:00+00:00")
    )
    retest, event = evaluate_breakout_alert(
        payload(4442, 4443, "2026-08-20T22:00:00+00:00"),
        confirmed,
        higher_low_broken=False,
        macd_declining=False,
    )
    assert retest.status == "BREAKOUT_RETEST"
    assert event["event_type"] == "BREAKOUT_RETEST"
