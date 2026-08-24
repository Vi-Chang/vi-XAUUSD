from copy import deepcopy

from app.engines.decision_presentation import format_decision_message
from app.engines.directional_wording import format_level_cross
from app.engines.fake_breakout_recovery import evaluate_fake_breakout_recovery
from app.engines.market_direction import resolve_market_direction


def snapshot(*, close=100.8, candle="2026-08-24T16:00:00+00:00"):
    return {
        "symbol": "XAUUSD",
        "timestamp_utc": "2026-08-24T16:01:00+00:00",
        "normalized_analysis": {
            "currentPrice": close,
            "lastClosedCandlePrice": close,
            "lastClosedCandleTimestamp": candle,
            "atr15": 2.0,
            "trendBias": "bullish",
            "confirmationLevels": [
                {"kind": "support", "price": 98.0},
                {"kind": "resistance", "price": 101.0},
                {"kind": "resistance", "price": 103.0},
            ],
        },
    }


def failed_breakdown():
    return {
        "state": "FAILED_BREAKDOWN",
        "level": 100.0,
        "break_confidence": 16,
        "reclaim_confidence": 80,
        "follow_through": "INSUFFICIENT",
        "last_evaluated_candle": "2026-08-24T16:00:00+00:00",
    }


def test_confirmed_break_with_follow_through_does_not_create_recovery_plan():
    state, events = evaluate_fake_breakout_recovery(
        data=snapshot(),
        break_state={**failed_breakdown(), "state": "BREAK_CONFIRMED",
                     "break_confidence": 82, "follow_through": "SUFFICIENT"},
    )
    assert state["state"] == "IDLE"
    assert state["active"] is False
    assert events == []


def test_weak_failed_breakdown_invalidates_short_and_waits_for_long_confirmation():
    state, events = evaluate_fake_breakout_recovery(
        data=snapshot(), break_state=failed_breakdown())
    assert state["breakoutFailureState"] == "BEAR_BREAKOUT_FAILED"
    assert state["liquiditySweepState"] == "LIQUIDITY_SWEEP_SUSPECTED"
    assert state["invalidatedBreakoutDirection"] == "SHORT"
    assert state["oppositeDirection"] == "LONG"
    assert state["state"] == "WAIT_CONFIRMATION"
    assert state["nextAction"]["confirmationType"] == "CLOSED_CANDLE"
    assert events[0]["event_type"] == "FAKE_BREAKOUT_CONFIRMED"
    assert not any("ENTRY_READY" in str(value) for value in state.values())


def test_later_closed_candle_can_confirm_opposite_setup_but_never_auto_enter():
    first, _ = evaluate_fake_breakout_recovery(
        data=snapshot(), break_state=failed_breakdown())
    trigger = first["nextAction"]["triggerLevel"]
    later = snapshot(close=trigger + 0.2, candle="2026-08-24T16:15:00+00:00")
    state, events = evaluate_fake_breakout_recovery(
        data=later,
        break_state={**failed_breakdown(),
                     "last_evaluated_candle": "2026-08-24T16:15:00+00:00"},
        previous=deepcopy(first),
    )
    assert state["state"] == "LONG_SETUP_CONFIRMED"
    assert state["confirmedCandleTime"] == "2026-08-24T16:15:00+00:00"
    assert events[0]["event_type"] == "OPPOSITE_SETUP_CONFIRMED"
    assert events[0]["currentState"] != "ENTRY_READY"


def test_market_direction_fallback_does_not_depend_on_entry_signal():
    resolved = resolve_market_direction({
        "normalized_analysis": {},
        "timeframes": {"h4": {"trend": "bearish"}},
    }, {"entrySignal": "WAIT"})
    assert resolved == {
        "direction": "BEARISH",
        "source": "LATEST_STRUCTURAL_STATE",
        "available": True,
    }


def test_directional_wording_is_explicit_for_support_and_resistance():
    assert "收盤跌破" in format_level_cross(
        level_kind="support", movement="DOWN", level=100)
    assert "收盤突破" in format_level_cross(
        level_kind="resistance", movement="UP", level=105)


def test_recovery_telegram_explains_action_trigger_invalidation_and_targets():
    state, events = evaluate_fake_breakout_recovery(
        data=snapshot(), break_state=failed_breakdown())
    text = format_decision_message({
        **events[0], "fakeBreakoutRecovery": state,
        "nextAction": state["nextAction"],
    })
    assert "空頭跌破失敗" in text
    assert "先不要追價" in text
    assert "下一觸發：15M 收盤站上" in text
    assert "取消條件：15M 收盤跌破" in text
    assert "參考目標" in text
