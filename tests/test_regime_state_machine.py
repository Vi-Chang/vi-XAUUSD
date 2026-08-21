from app.engines.regime_state_machine import evaluate_regime_state


def payload(*, candle="2026-08-21T23:00:00+08:00", close=99.0,
            live=99.0, h4="bullish", h1="bullish", m15="bullish",
            momentum="weakening", support="none"):
    return {
        "symbol": "XAUUSD", "version": 10,
        "timestamp_utc": "2026-08-21T23:01:00+08:00",
        "normalized_analysis": {
            "trendBias": "bullish" if h4 == "bullish" else "bearish",
            "shortTermMomentum": momentum,
            "currentPrice": live,
            "lastClosedCandlePrice": close,
            "lastClosedCandleTimestamp": candle,
            "supportState": support,
            "timeframeAssessments": [
                {"timeframe": "4H", "trend": h4},
                {"timeframe": "1H", "trend": h1},
                {"timeframe": "15M", "trend": m15},
            ],
            "confirmationLevels": [
                {"timeframe": "15M", "kind": "resistance", "price": 100.0},
                {"timeframe": "15M", "kind": "support", "price": 95.0},
            ],
        },
    }


def test_htf_bullish_with_m15_falling_is_weak_not_bearish():
    state, _ = evaluate_regime_state(payload())
    assert state["confirmedCandleState"] == "WEAKENING"
    assert state["compositeRegime"] == "HTF_BULLISH_LTF_WEAKENING"


def test_rsi_and_macd_improvement_near_reclaim_is_recovering():
    indicators = {"15M": {"rsi14": 52, "rsi14_prev": 46,
                           "macd_hist": -.2, "macd_hist_prev": -.8}}
    state, _ = evaluate_regime_state(payload(close=99.7), indicators=indicators)
    assert state["confirmedCandleState"] == "RECOVERING"


def test_closed_reclaim_restores_bullish_and_emits_once():
    previous, _ = evaluate_regime_state(payload(candle="2026-08-21T22:45:00+08:00"))
    state, events = evaluate_regime_state(payload(close=100.2), previous=previous)
    assert state["confirmedCandleState"] == "BULLISH_RESTORED"
    assert events[0]["event_type"] == "BULLISH_RESTORED"


def test_live_reclaim_without_closed_reclaim_is_only_testing():
    state, _ = evaluate_regime_state(payload(close=99.4, live=100.4))
    assert state["livePriceState"] == "LIVE_TESTING_RECLAIM"
    assert state["confirmedCandleState"] != "BULLISH_RESTORED"


def test_m15_break_cannot_flip_bullish_h1_and_h4_to_bearish():
    state, _ = evaluate_regime_state(payload(
        m15="bearish", support="confirmed_breakdown"))
    assert state["confirmedCandleState"] == "WEAKENING"
    assert state["compositeRegime"] != "BEARISH_CONFIRMED"


def test_m15_and_h1_structure_break_confirms_bearish():
    state, events = evaluate_regime_state(payload(
        h1="bearish", m15="bearish", support="confirmed_breakdown"))
    assert state["confirmedCandleState"] == "BEARISH_CONFIRMED"
    assert events[0]["event_type"] == "BEARISH_CONFIRMED"


def test_new_closed_candle_recomputes_and_supersedes_old_weak_state():
    weak, _ = evaluate_regime_state(payload(candle="2026-08-21T22:30:00+08:00"))
    restored, _ = evaluate_regime_state(
        payload(candle="2026-08-21T23:00:00+08:00", close=100.3,
                momentum="stable"), previous=weak)
    assert restored["sourceCandleCloseTime"].endswith("23:00:00+08:00")
    assert restored["stateVersion"] > weak["stateVersion"]
    assert restored["confirmedCandleState"] == "BULLISH_RESTORED"
