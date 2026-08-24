from copy import deepcopy

import pytest

from app.engines.breakout_setup_manager import (
    assert_trigger_frozen,
    evaluate_breakout_setups,
)
from app.engines.decision_presentation import build_decision_presentation
from app.services.alert_aggregator import alert_category, semantic_key


def market(price: float, *, closed: float, trigger: float = 4615.0,
           candle: str = "2026-08-24T00:00:00Z") -> dict:
    return {
        "symbol": "XAUUSD", "timestamp_utc": candle,
        "decision": {"signal_score": 82},
        "normalized_analysis": {
            "trendBias": "bullish", "marketRegime": "strong_bullish",
            "shortTermMomentum": "accelerating", "currentPrice": price,
            "lastClosedCandlePrice": closed, "lastClosedCandleTimestamp": candle,
            "marketDataStatus": "GOOD", "atr15": 8.0,
            "confirmationLevels": [
                {"kind": "resistance", "timeframe": "15M", "price": trigger, "buffer": .8},
                {"kind": "support", "timeframe": "15M", "price": 4594.73, "buffer": .8},
            ],
        },
        "entry_engine": {"take_profit_1": 4650.0, "take_profit_2": 4660.0,
                         "take_profit_3": 4672.0},
    }


def test_4615_confirmation_ladder_replay_never_moves_goalpost():
    state = None
    replay = []
    sequence = [
        (4608, 4608, 4615), (4612, 4612, 4620),
        (4615.5, 4615.5, 4620), (4620, 4615.5, 4628),
        (4623, 4620, 4635), (4628, 4623, 4640), (4635, 4628, 4650),
    ]
    for index, (price, closed, newly_seen_resistance) in enumerate(sequence):
        data = market(price, closed=closed, trigger=(4615 if state is None else newly_seen_resistance),
                      candle=f"2026-08-24T00:{index:02d}:00Z")
        state, events = evaluate_breakout_setups(data, state)
        setup = state["activeSetup"]
        replay.append((price, setup["tradeState"], setup["primaryTrigger"],
                       [event["event_type"] for event in events]))
    assert [row[1] for row in replay] == [
        "WATCHING", "ARMED", "ENTER", "MANAGE", "MANAGE", "MANAGE", "MANAGE"]
    assert {row[2] for row in replay} == {4615.0}
    assert any("BREAKOUT_ENTRY_READY" in row[3] for row in replay)
    assert all("NEW_SETUP_CREATED" not in row[3] for row in replay[1:])


def test_atr_chase_for_a_grade_is_one_atr():
    state, _ = evaluate_breakout_setups(market(4608, closed=4608), None)
    setup = state["activeSetup"]
    assert setup["chaseFactor"] == 1.0
    assert setup["maxChaseDistance"] == 8.0
    assert setup["executionZoneLow"] == 4615.0
    assert setup["executionZoneHigh"] == 4623.0


def test_locked_trigger_security_gate_rejects_same_id_change():
    state, _ = evaluate_breakout_setups(market(4608, closed=4608), None)
    original = state["activeSetup"]
    moved = deepcopy(original)
    moved["primaryTrigger"] = 4620
    with pytest.raises(AssertionError, match="MOVING_GOALPOST"):
        assert_trigger_frozen(original, moved)


def test_confirmed_but_beyond_atr_chase_is_missed_wait_retest():
    state, _ = evaluate_breakout_setups(market(4608, closed=4608), None)
    state, events = evaluate_breakout_setups(market(4630, closed=4616), state)
    setup = state["activeSetup"]
    assert setup["primaryTriggerConfirmed"] is True
    assert setup["status"] == "WAIT_RETEST"
    assert setup["tradeState"] == "WAIT"
    assert any(event["event_type"] == "WAIT_RETEST" for event in events)


def test_entry_ready_is_one_telegram_semantic_event_and_ui_is_explicit():
    state, _ = evaluate_breakout_setups(market(4608, closed=4608), None)
    state, events = evaluate_breakout_setups(market(4615.5, closed=4615.5), state)
    ready = next(event for event in events if event["event_type"] == "BREAKOUT_ENTRY_READY")
    ready.update(symbol="XAUUSD", currentState="ENTRY_READY", currentPrice=4615.5,
                 canEnter=True, finalAction="ENTER_LONG")
    assert alert_category(ready) == "ENTRY_READY"
    assert semantic_key(ready) == semantic_key(dict(ready))
    presentation = build_decision_presentation({
        "currentState": "ENTER", "direction": "LONG", "currentPrice": 4615.5})
    assert "可以進場" in presentation["title"]
